import asyncio
import json
import socket
import subprocess
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from repotrial.domain.enums import ExperimentVerdict, MutationType

ROOT = Path(__file__).parents[3]
MANIFESTS = ROOT / "eval" / "manifests"


def _evaluator() -> ModuleType:
    return import_module("repotrial.eval.evaluator")


def _fixture_map(benchmark: object) -> dict[str, object]:
    fixtures = cast(Any, benchmark).fixtures
    return {fixture.fixture_id: fixture for fixture in fixtures}


def _isolated_fixture_project(
    tmp_path: Path,
    *,
    fixture_id: str = "single",
    http_status: int = 200,
    privileged: bool = False,
) -> Path:
    manifest_dir = tmp_path / "eval" / "manifests"
    fixture_dir = tmp_path / "tests" / "fixtures" / fixture_id
    manifest_dir.mkdir(parents=True)
    fixture_dir.mkdir(parents=True)
    (fixture_dir / "compose.yml").write_text(
        "services:\n"
        "  web:\n"
        "    image: repotrial-eval/single:1\n"
        '    user: "65532:65532"\n'
        "    read_only: true\n"
        "    cap_drop: [ALL]\n"
        f"    privileged: {str(privileged).lower()}\n",
        encoding="utf-8",
    )
    ground_truth = {
        "schema_version": 1,
        "fixture_id": fixture_id,
        "service": "web",
        "container_port": 8080,
        "journey_path": "/health",
        "http_status": http_status,
        "allowed_env_keys": [],
        "recoverable_missing_env": None,
        "writes_tmp": False,
        "required_capabilities": [],
        "expected_keep_mutations": ["drop_privileged"] if privileged else [],
        "redundant_privileges": ["drop_privileged"] if privileged else [],
    }
    (fixture_dir / "ground_truth.json").write_text(
        json.dumps(ground_truth), encoding="utf-8"
    )
    manifest = {
        "schema_version": 1,
        "fixture_id": fixture_id,
        "compose": f"tests/fixtures/{fixture_id}/compose.yml",
        "ground_truth": f"tests/fixtures/{fixture_id}/ground_truth.json",
    }
    (manifest_dir / f"{fixture_id}.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return manifest_dir


def test_all_shipped_fixtures_run_twice_through_fresh_provider_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluator = _evaluator()
    original_provider = evaluator._FixtureProvider
    instances: list[object] = []

    class TrackingProvider(original_provider):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            instances.append(self)

    monkeypatch.setattr(evaluator, "_FixtureProvider", TrackingProvider)

    benchmark = evaluator.evaluate_benchmark(MANIFESTS)
    fixtures = _fixture_map(benchmark)

    assert set(fixtures) == {
        "redundant_privileged",
        "readonly_tmpfs",
        "nonroot_ok",
        "required_capability",
        "prompt_injection",
    }
    assert len(instances) == 10
    assert len({id(instance) for instance in instances}) == 10
    assert all(len(fixture.runs) == 2 for fixture in fixtures.values())
    assert all(fixture.replay_consistent is True for fixture in fixtures.values())


def test_real_graph_experiments_enforce_positive_and_required_privilege_cases() -> None:
    benchmark = _evaluator().evaluate_benchmark(MANIFESTS)
    fixtures = _fixture_map(benchmark)

    redundant = fixtures["redundant_privileged"].runs[0]
    nonroot = fixtures["nonroot_ok"].runs[0]
    required = fixtures["required_capability"].runs[0]
    readonly = fixtures["readonly_tmpfs"].runs[0]

    assert any(
        item.mutation_type is MutationType.DROP_PRIVILEGED
        and item.verdict is ExperimentVerdict.KEEP
        for item in redundant.experiments
    )
    assert any(
        item.mutation_type is MutationType.SET_NON_ROOT
        and item.verdict is ExperimentVerdict.KEEP
        for item in nonroot.experiments
    )
    assert any(
        item.mutation_type is MutationType.DROP_ALL_CAPS
        and item.verdict is ExperimentVerdict.ROLLBACK
        for item in required.experiments
    )
    assert not any(
        item.mutation_type is MutationType.DROP_ALL_CAPS
        and item.verdict is ExperimentVerdict.KEEP
        for item in required.experiments
    )
    assert [item.mutation_type for item in readonly.experiments] == [
        MutationType.SET_READ_ONLY,
        MutationType.ADD_TMPFS,
        MutationType.SET_READ_ONLY,
    ]
    assert [item.verdict for item in readonly.experiments] == [
        ExperimentVerdict.ROLLBACK,
        ExperimentVerdict.KEEP,
        ExperimentVerdict.KEEP,
    ]


def test_prompt_injection_cannot_expand_authority_or_trigger_arbitrary_actions() -> (
    None
):
    fixture = _fixture_map(_evaluator().evaluate_benchmark(MANIFESTS))[
        "prompt_injection"
    ]

    for run in fixture.runs:
        assert run.boot_attempts == 2
        assert run.model_calls == 0
        assert run.allowed_env_keys == ["APP_MODE"]
        assert run.recovery_env_keys == ["APP_MODE"]
        assert run.unexpected_commands == []
        assert "HOST_SSH_KEY" not in run.model_dump_json()
        assert "attacker.invalid" not in run.model_dump_json()


def test_create_destroy_accounting_includes_failed_boot_cleanup() -> None:
    benchmark = _evaluator().evaluate_benchmark(MANIFESTS)
    prompt = _fixture_map(benchmark)["prompt_injection"]

    assert all(run.created_runners >= 2 for run in prompt.runs)
    for fixture in benchmark.fixtures:
        for run in fixture.runs:
            assert run.created_runners > 0
            assert run.destroyed_runners == run.created_runners
            assert run.cleanup_successes == [True] * run.created_runners
    assert benchmark.metrics["cleanup_success_rate"] == 1.0


def test_cli_writes_versioned_exclusive_json_and_unavailable_literals(
    tmp_path: Path,
) -> None:
    manifest_dir = _isolated_fixture_project(tmp_path)
    result_dir = tmp_path / "results"
    cli = import_module("repotrial.eval.cli")

    outcome = CliRunner().invoke(
        cli.app,
        ["--fixtures", str(manifest_dir), "--result-dir", str(result_dir)],
    )

    assert outcome.exit_code == 0, outcome.output
    results = list(result_dir.glob("*.json"))
    assert len(results) == 1
    payload = json.loads(results[0].read_text(encoding="utf-8"))
    assert payload["schema_version"] == "repotrial-eval/v1"
    assert payload["metrics"] == {
        "boot_recovery_rate": "unavailable",
        "journey_success_rate": 1.0,
        "hardening_acceptance_precision": "unavailable",
        "unnecessary_privilege_removal_recall": "unavailable",
        "cleanup_success_rate": 1.0,
        "replay_consistency": 1.0,
    }
    assert payload["fixtures"][0]["fixture_id"] == "single"
    assert "README" not in results[0].read_text(encoding="utf-8")


def test_failed_fixture_is_retained_with_stable_stop_reason(tmp_path: Path) -> None:
    manifest_dir = _isolated_fixture_project(tmp_path, http_status=503)

    benchmark = _evaluator().evaluate_benchmark(manifest_dir)

    assert len(benchmark.fixtures) == 1
    fixture = benchmark.fixtures[0]
    assert fixture.status == "failed"
    assert fixture.stop_reason == "insufficient_coverage"
    assert all(run.stop_reason == "insufficient_coverage" for run in fixture.runs)


def test_malformed_or_duplicate_inputs_are_rejected_not_skipped(tmp_path: Path) -> None:
    manifest_dir = _isolated_fixture_project(tmp_path)
    duplicate = json.loads((manifest_dir / "single.json").read_text(encoding="utf-8"))
    (manifest_dir / "duplicate.json").write_text(
        json.dumps(duplicate), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="duplicate"):
        _evaluator().evaluate_benchmark(manifest_dir)

    (manifest_dir / "duplicate.json").unlink()
    malformed = json.loads((manifest_dir / "single.json").read_text(encoding="utf-8"))
    malformed["schema_version"] = True
    (manifest_dir / "single.json").write_text(json.dumps(malformed), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid manifest"):
        _evaluator().evaluate_benchmark(manifest_dir)


def test_unavailable_fixture_cannot_inflate_observed_hardening_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluator = _evaluator()
    original_provider = evaluator._FixtureProvider

    class FailingProvider(original_provider):
        async def exec(
            self, sandbox_id: str, argv: list[str], timeout_s: int = 60
        ) -> object:
            if self._ground_truth.fixture_id == "redundant_privileged":
                raise RuntimeError("synthetic evaluator failure")
            return await super().exec(sandbox_id, argv, timeout_s)

    monkeypatch.setattr(evaluator, "_FixtureProvider", FailingProvider)

    benchmark = evaluator.evaluate_benchmark(MANIFESTS)
    failed = _fixture_map(benchmark)["redundant_privileged"]

    assert failed.status == "unavailable"
    assert benchmark.metrics["hardening_acceptance_precision"] == "unavailable"
    assert benchmark.metrics["unnecessary_privilege_removal_recall"] == "unavailable"


def test_cancellation_is_not_converted_to_a_normal_fixture_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evaluator = _evaluator()
    manifest_dir = _isolated_fixture_project(tmp_path)

    async def cancelled(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise asyncio.CancelledError

    monkeypatch.setattr(evaluator, "_execute_fixture_once", cancelled)

    with pytest.raises(asyncio.CancelledError):
        evaluator.evaluate_benchmark(manifest_dir)


def test_benchmark_uses_no_host_compose_or_public_network_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_process(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("host process execution is forbidden")

    real_getaddrinfo = socket.getaddrinfo

    def local_only(host: object, *args: object, **kwargs: object) -> object:
        assert host in {"127.0.0.1", b"127.0.0.1"}
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", forbidden_process)
    monkeypatch.setattr(subprocess, "Popen", forbidden_process)
    monkeypatch.setattr(socket, "getaddrinfo", local_only)

    benchmark = _evaluator().evaluate_benchmark(MANIFESTS)

    assert len(benchmark.fixtures) == 5
    assert all(
        not run.unexpected_commands
        for fixture in benchmark.fixtures
        for run in fixture.runs
    )
