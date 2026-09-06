import asyncio
import base64
import hashlib
import json
import socket
import stat
import subprocess
from collections.abc import Collection, Sequence
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from repotrial.domain.enums import ExperimentVerdict, MutationType, Verdict
from repotrial.domain.models import JourneyResult
from repotrial.sandbox.base import SandboxProvider
from repotrial.trial.boot import BootResult

ROOT = Path(__file__).parents[3]
MANIFESTS = ROOT / "eval" / "manifests"


def _evaluator() -> ModuleType:
    return import_module("repotrial.eval.evaluator")


def _fixture_map(benchmark: object) -> dict[str, object]:
    fixtures = cast(Any, benchmark).fixtures
    return {fixture.fixture_id: fixture for fixture in fixtures}


def _fixture_provider(fixture_id: str) -> tuple[Any, Path]:
    evaluator = _evaluator()
    loaded = next(
        fixture
        for fixture in evaluator._load_fixtures(MANIFESTS)
        if fixture.manifest.fixture_id == fixture_id
    )
    return evaluator._FixtureProvider(loaded.ground_truth), loaded.compose_path.parent


def _assert_unexpected(result: object, provider: object) -> None:
    result_data = cast(Any, result)
    provider_data = cast(Any, provider)
    assert result_data.exit_code == 127
    assert result_data.stderr == "unsupported fixture command"
    assert len(provider_data.unexpected_commands) == 1
    digest = provider_data.unexpected_commands[0]
    assert digest.startswith("sha256-")
    assert len(digest) == 23
    int(digest.removeprefix("sha256-"), 16)


def _experiment_materialization_argv(
    payload: bytes = b"services:\n  web:\n    read_only: true\n",
    *,
    adapter_script: str | None = None,
    command_name: str = "repotrial-experiment-overlay",
    relative_path: str = ".repotrial-overlays/experiment.overlay.yaml",
    expected_sha256: str | None = None,
    encoded_payload: str | None = None,
) -> list[str]:
    compatibility = import_module("repotrial.trial.compatibility")
    digest = hashlib.sha256(payload).hexdigest()
    script = (
        compatibility._EXPERIMENT_ADAPTER_SCRIPT
        if adapter_script is None
        else adapter_script
    )
    encoded = (
        base64.b64encode(payload).decode("ascii")
        if encoded_payload is None
        else encoded_payload
    )
    return [
        "sh",
        "-eu",
        "-c",
        script,
        command_name,
        relative_path,
        digest if expected_sha256 is None else expected_sha256,
        encoded,
    ]


def _run_fixture_command(provider: Any, workspace: Path, argv: list[str]) -> Any:
    async def exercise() -> object:
        sandbox_id = await provider.create(workspace, "experiment-materialization")
        try:
            return await provider.exec(sandbox_id, argv)
        finally:
            await provider.destroy(sandbox_id)

    return asyncio.run(exercise())


def _compose_argv(app_mode: str, *command: str) -> list[str]:
    return [
        "env",
        f"APP_MODE={app_mode}",
        "docker",
        "compose",
        "-f",
        "compose.yml",
        *command,
    ]


def test_fixture_provider_materializes_controlled_experiment_overlay(
    tmp_path: Path,
) -> None:
    provider, _ = _fixture_provider("redundant_privileged")
    workspace = tmp_path / "sandbox"
    workspace.mkdir()
    payload = b"services:\n  web:\n    read_only: true\n"
    expected_sha256 = hashlib.sha256(payload).hexdigest()

    async def exercise() -> tuple[object, bytes, int]:
        sandbox_id = await provider.create(workspace, "experiment-materialization")
        try:
            result = await provider.exec(
                sandbox_id, _experiment_materialization_argv(payload)
            )
            target = workspace / ".repotrial-overlays/experiment.overlay.yaml"
            return result, target.read_bytes(), stat.S_IMODE(target.stat().st_mode)
        finally:
            await provider.destroy(sandbox_id)

    result, materialized, mode = cast(tuple[Any, bytes, int], asyncio.run(exercise()))

    assert result.exit_code == 0
    assert result.stdout == (
        "root=/workspace\n"
        "path=.repotrial-overlays/experiment.overlay.yaml\n"
        "mode=600\n"
        f"sha256={expected_sha256}\n"
    )
    assert result.stderr == ""
    assert materialized == payload
    assert mode == 0o600
    assert not (workspace / ".repotrial-overlays/experiment.overlay.yaml").exists()
    assert provider.unexpected_commands == []


@pytest.mark.parametrize(
    ("expected_exit_code", "argv_kwargs"),
    [
        (None, {"adapter_script": "set -eu\n"}),
        (22, {"relative_path": ".repotrial-overlays/../escape.yaml"}),
        (31, {"expected_sha256": "0" * 64}),
        (26, {"encoded_payload": "not-base64"}),
    ],
    ids=["adapter", "path", "hash", "base64"],
)
def test_fixture_provider_rejects_uncontrolled_or_invalid_materialization_inputs(
    tmp_path: Path,
    expected_exit_code: int | None,
    argv_kwargs: dict[str, str],
) -> None:
    provider, _ = _fixture_provider("redundant_privileged")
    workspace = tmp_path / "sandbox"
    workspace.mkdir()

    result = _run_fixture_command(
        provider, workspace, _experiment_materialization_argv(**argv_kwargs)
    )

    if expected_exit_code is None:
        _assert_unexpected(result, provider)
        assert not (workspace / ".repotrial-overlays").exists()
    else:
        assert result.exit_code == expected_exit_code
        assert result.stderr == ""
        assert not (workspace / ".repotrial-overlays/experiment.overlay.yaml").exists()
        assert provider.unexpected_commands == []


def test_fixture_provider_rejects_duplicate_or_linked_experiment_targets(
    tmp_path: Path,
) -> None:
    provider, _ = _fixture_provider("redundant_privileged")
    workspace = tmp_path / "sandbox"
    workspace.mkdir()
    overlay_dir = workspace / ".repotrial-overlays"
    overlay_dir.mkdir()
    target = overlay_dir / "experiment.overlay.yaml"
    target.write_bytes(b"preexisting")

    duplicate = _run_fixture_command(
        provider, workspace, _experiment_materialization_argv()
    )

    assert duplicate.exit_code == 24
    assert target.read_bytes() == b"preexisting"
    assert provider.unexpected_commands == []

    outside = tmp_path / "outside.yaml"
    target.unlink()
    target.symlink_to(outside)
    linked = _run_fixture_command(
        provider, workspace, _experiment_materialization_argv()
    )

    assert linked.exit_code == 24
    assert target.is_symlink()
    assert not outside.exists()
    assert provider.unexpected_commands == []


def test_fixture_provider_observer_reuses_active_allowlisted_environment(
    tmp_path: Path,
) -> None:
    provider, source = _fixture_provider("prompt_injection")
    workspace = tmp_path / "sandbox"
    workspace.mkdir()
    (workspace / "compose.yml").write_bytes((source / "compose.yml").read_bytes())

    async def exercise() -> tuple[Any, Any, Any]:
        sandbox_id = await provider.create(workspace, "observer-environment")
        try:
            up = await provider.exec(
                sandbox_id,
                _compose_argv("fixture", "up", "-d", "--wait", "--wait-timeout", "60"),
            )
            observer = await provider.exec(
                sandbox_id,
                _compose_argv(
                    "fixture",
                    "ps",
                    "--all",
                    "--no-trunc",
                    "--orphans=false",
                    "--format",
                    "json",
                ),
            )
            mismatch = await provider.exec(
                sandbox_id,
                _compose_argv(
                    "other",
                    "ps",
                    "--all",
                    "--no-trunc",
                    "--orphans=false",
                    "--format",
                    "json",
                ),
            )
            return up, observer, mismatch
        finally:
            await provider.destroy(sandbox_id)

    up, observer, mismatch = asyncio.run(exercise())

    assert up.exit_code == 0
    assert observer.exit_code == 0
    assert json.loads(observer.stdout)["ID"]
    _assert_unexpected(mismatch, provider)


def _isolated_fixture_project(
    tmp_path: Path,
    *,
    fixture_id: str = "single",
    http_status: int = 200,
    privileged: bool = False,
    recoverable_env: str | None = None,
) -> Path:
    manifest_dir = tmp_path / "eval" / "manifests"
    fixture_dir = tmp_path / "tests" / "fixtures" / fixture_id
    manifest_dir.mkdir(parents=True, exist_ok=True)
    fixture_dir.mkdir(parents=True, exist_ok=True)
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
        "allowed_env_keys": [recoverable_env] if recoverable_env is not None else [],
        "recoverable_missing_env": recoverable_env,
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


def _unsupported_boot_result(attempt: int) -> BootResult:
    return BootResult(
        verdict=Verdict.UNSUPPORTED,
        service_states={},
        logs={},
        attempt=attempt,
    )


def _unsupported_journey_result(journey_id: str) -> JourneyResult:
    return JourneyResult(
        journey_id=journey_id,
        verdict=Verdict.UNSUPPORTED,
        passed_steps=0,
        total_steps=1,
        failure_reason="journey:fixture_unsupported",
    )


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


def test_real_graph_unsupported_boot_projects_unavailable_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = import_module("repotrial.agent.graph")
    manifest_dir = _isolated_fixture_project(tmp_path, recoverable_env="APP_MODE")

    async def unsupported_boot(
        provider: SandboxProvider,
        sandbox_id: str,
        compose_path: str,
        env: dict[str, str],
        attempt: int,
        *,
        overlay_path: str | None = None,
        compatibility_overlay_path: str | None = None,
        unset_env_keys: Sequence[str] = (),
        project_directory: str | None = None,
        declared_secret_env_keys: Collection[str] = (),
    ) -> BootResult:
        del (
            provider,
            sandbox_id,
            compose_path,
            env,
            overlay_path,
            compatibility_overlay_path,
            unset_env_keys,
            project_directory,
        )
        assert declared_secret_env_keys == frozenset()
        return _unsupported_boot_result(attempt)

    monkeypatch.setattr(graph, "boot_compose", unsupported_boot)

    benchmark = _evaluator().evaluate_benchmark(manifest_dir)
    fixture = benchmark.fixtures[0]

    assert fixture.status == "unavailable"
    assert fixture.replay_consistent is None
    assert all(run.boot_verdict is Verdict.UNSUPPORTED for run in fixture.runs)
    assert all(not run.comparable for run in fixture.runs)
    assert benchmark.metrics["boot_recovery_rate"] == "unavailable"
    assert benchmark.metrics["replay_consistency"] == "unavailable"


def test_real_graph_unsupported_baseline_journey_projects_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = import_module("repotrial.agent.graph")
    manifest_dir = _isolated_fixture_project(tmp_path)

    async def unsupported_journey(
        journey: object, *args: object, **kwargs: object
    ) -> JourneyResult:
        del args, kwargs
        return _unsupported_journey_result(cast(str, journey.journey_id))

    monkeypatch.setattr(graph, "run_http_journey", unsupported_journey)

    benchmark = _evaluator().evaluate_benchmark(manifest_dir)
    fixture = benchmark.fixtures[0]

    assert fixture.status == "unavailable"
    assert all(run.journeys[0].verdict is Verdict.UNSUPPORTED for run in fixture.runs)
    assert all(not run.comparable for run in fixture.runs)
    assert benchmark.metrics["journey_success_rate"] == "unavailable"


def test_real_graph_unsupported_experiment_stop_projects_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = import_module("repotrial.hardening.engine")
    manifest_dir = _isolated_fixture_project(tmp_path, privileged=True)

    async def unsupported_journey(
        journey: object, *args: object, **kwargs: object
    ) -> JourneyResult:
        del args, kwargs
        return _unsupported_journey_result(cast(str, journey.journey_id))

    monkeypatch.setattr(engine, "run_http_journey", unsupported_journey)

    benchmark = _evaluator().evaluate_benchmark(manifest_dir)
    fixture = benchmark.fixtures[0]

    assert fixture.status == "unavailable"
    assert fixture.replay_consistent is None
    assert all(
        run.experiments[0].verdict is ExperimentVerdict.STOP
        and run.experiments[0].reason == "journey_unsupported:readme-1"
        for run in fixture.runs
    )
    assert all(not run.comparable for run in fixture.runs)
    assert benchmark.metrics["hardening_acceptance_precision"] == "unavailable"


def test_unavailable_replay_sample_is_not_omitted_from_aggregate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evaluator = _evaluator()
    manifest_dir = _isolated_fixture_project(tmp_path, fixture_id="available")
    _isolated_fixture_project(tmp_path, fixture_id="unavailable")
    original_execute = evaluator._execute_fixture_once

    async def unavailable_fixture_run(fixture: object, run_index: int) -> object:
        fixture_data = cast(Any, fixture)
        if fixture_data.manifest.fixture_id == "unavailable":
            provider = evaluator._FixtureProvider(fixture_data.ground_truth)
            return evaluator._failed_fixture_run(
                fixture_data,
                provider,
                run_index,
                RuntimeError("fixture execution unavailable"),
            )
        return await original_execute(fixture_data, run_index)

    monkeypatch.setattr(evaluator, "_execute_fixture_once", unavailable_fixture_run)

    benchmark = evaluator.evaluate_benchmark(manifest_dir)

    fixtures = _fixture_map(benchmark)
    assert fixtures["available"].replay_consistent is True
    assert fixtures["unavailable"].replay_consistent is None
    assert benchmark.metrics["replay_consistency"] == "unavailable"
    result_path = evaluator.write_benchmark_result(benchmark, tmp_path / "results")
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["fixtures"][1]["replay_consistent"] is None
    assert payload["metrics"]["replay_consistency"] == "unavailable"


def test_functional_boot_failure_remains_a_numeric_metric_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = import_module("repotrial.agent.graph")
    manifest_dir = _isolated_fixture_project(tmp_path, recoverable_env="APP_MODE")

    async def failed_boot(
        provider: SandboxProvider,
        sandbox_id: str,
        compose_path: str,
        env: dict[str, str],
        attempt: int,
        *,
        overlay_path: str | None = None,
        compatibility_overlay_path: str | None = None,
        unset_env_keys: Sequence[str] = (),
        project_directory: str | None = None,
        declared_secret_env_keys: Collection[str] = (),
    ) -> BootResult:
        del (
            provider,
            sandbox_id,
            compose_path,
            env,
            overlay_path,
            compatibility_overlay_path,
            unset_env_keys,
            project_directory,
        )
        assert declared_secret_env_keys == frozenset()
        return BootResult(
            verdict=Verdict.FAIL,
            service_states={},
            logs={"up": "functional failure"},
            attempt=attempt,
        )

    monkeypatch.setattr(graph, "boot_compose", failed_boot)

    benchmark = _evaluator().evaluate_benchmark(manifest_dir)
    fixture = benchmark.fixtures[0]

    assert fixture.status == "failed"
    assert fixture.replay_consistent is True
    assert all(run.comparable for run in fixture.runs)
    assert benchmark.metrics["boot_recovery_rate"] == 0.0


def test_functional_journey_failure_remains_a_numeric_metric_failure(
    tmp_path: Path,
) -> None:
    manifest_dir = _isolated_fixture_project(tmp_path, http_status=503)

    benchmark = _evaluator().evaluate_benchmark(manifest_dir)
    fixture = benchmark.fixtures[0]

    assert fixture.status == "failed"
    assert fixture.replay_consistent is True
    assert all(run.comparable for run in fixture.runs)
    assert benchmark.metrics["journey_success_rate"] == 0.0


def test_deterministic_rollback_remains_comparable_to_replay(
    tmp_path: Path,
) -> None:
    manifest_dir = _isolated_fixture_project(tmp_path, fixture_id="rollback")
    ground_truth_path = (
        tmp_path / "tests" / "fixtures" / "rollback" / "ground_truth.json"
    )
    ground_truth = json.loads(ground_truth_path.read_text(encoding="utf-8"))
    ground_truth["required_capabilities"] = ["NET_ADMIN"]
    ground_truth_path.write_text(json.dumps(ground_truth), encoding="utf-8")
    compose_path = tmp_path / "tests" / "fixtures" / "rollback" / "compose.yml"
    compose_path.write_text(
        compose_path.read_text(encoding="utf-8").replace(
            "cap_drop: [ALL]\n", "cap_add: [NET_ADMIN]\n"
        ),
        encoding="utf-8",
    )

    benchmark = _evaluator().evaluate_benchmark(manifest_dir)
    fixture = benchmark.fixtures[0]

    assert fixture.status == "completed"
    assert fixture.replay_consistent is True
    assert all(run.comparable for run in fixture.runs)
    assert all(
        run.experiments[0].verdict is ExperimentVerdict.ROLLBACK
        and run.experiments[0].reason == "boot_regression"
        for run in fixture.runs
    )


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


@pytest.mark.parametrize(
    ("aliased_role", "source_role"),
    [
        ("readme", "compose"),
        ("ground_truth", "compose"),
        ("readme", "ground_truth"),
    ],
    ids=["compose-readme", "compose-ground-truth", "ground-truth-readme"],
)
def test_manifest_rejects_one_file_used_for_multiple_semantic_roles(
    tmp_path: Path, aliased_role: str, source_role: str
) -> None:
    manifest_dir = _isolated_fixture_project(tmp_path)
    manifest_path = manifest_dir / "single.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[aliased_role] = manifest[source_role]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate fixture input"):
        _evaluator()._load_fixtures(manifest_dir)


def test_unlisted_ordinary_failure_is_retained_without_dropping_later_fixtures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluator = _evaluator()
    original_provider = evaluator._FixtureProvider

    class FailingProvider(original_provider):
        async def exec(
            self, sandbox_id: str, argv: list[str], timeout_s: int = 60
        ) -> object:
            if self._ground_truth.fixture_id == "redundant_privileged":
                raise AssertionError("synthetic ordinary evaluator failure")
            return await super().exec(sandbox_id, argv, timeout_s)

    monkeypatch.setattr(evaluator, "_FixtureProvider", FailingProvider)

    benchmark = evaluator.evaluate_benchmark(MANIFESTS)
    failed = _fixture_map(benchmark)["redundant_privileged"]

    assert len(benchmark.fixtures) == 5
    assert all(len(fixture.runs) == 2 for fixture in benchmark.fixtures)
    assert failed.status == "unavailable"
    assert failed.stop_reason == "evaluator_error:assertion_error"
    assert all(run.status == "unavailable" for run in failed.runs)
    assert all(
        run.stop_reason == "evaluator_error:assertion_error" for run in failed.runs
    )
    assert _fixture_map(benchmark)["required_capability"].status == "completed"
    assert benchmark.metrics["hardening_acceptance_precision"] == "unavailable"
    assert benchmark.metrics["unnecessary_privilege_removal_recall"] == "unavailable"


@pytest.mark.parametrize(
    "argv",
    [
        ["docker", "compose", "-f", "compose.yml", "up"],
        ["docker", "compose", "-f", "compose.yml", "up", "-d", "--wait"],
        [
            "docker",
            "compose",
            "-f",
            "compose.yml",
            "-f",
            "compose.yml",
            "-f",
            "compose.yml",
            "up",
            "-d",
        ],
        ["docker", "compose", "-f", "compose.yml", "ps", "--format", "json"],
        [
            "docker",
            "compose",
            "-f",
            "compose.yml",
            "ps",
            "--all",
            "--no-trunc",
            "--format",
            "json",
        ],
        [
            "docker",
            "compose",
            "-f",
            "compose.yml",
            "logs",
            "--no-color",
            "--tail",
            "200",
            "--since",
            "1h",
        ],
    ],
    ids=[
        "up-missing-detach",
        "up-extra-flag",
        "three-compose-files",
        "boot-ps-missing-flags",
        "observer-ps-missing-orphans-flag",
        "logs-arbitrary-tail",
    ],
)
def test_fixture_provider_rejects_non_exact_compose_dialect(argv: list[str]) -> None:
    provider, workspace = _fixture_provider("redundant_privileged")

    async def exercise() -> object:
        sandbox_id = await provider.create(workspace, "negative-compose")
        try:
            return await provider.exec(sandbox_id, argv)
        finally:
            await provider.destroy(sandbox_id)

    result = asyncio.run(exercise())

    _assert_unexpected(result, provider)


@pytest.mark.parametrize(
    "argv",
    [
        [
            "env",
            "HOST_SSH_KEY=secret",
            "docker",
            "compose",
            "-f",
            "compose.yml",
            "up",
            "-d",
        ],
        [
            "env",
            "APP_MODE=one",
            "APP_MODE=two",
            "docker",
            "compose",
            "-f",
            "compose.yml",
            "up",
            "-d",
        ],
        [
            "env",
            "docker",
            "compose",
            "-f",
            "compose.yml",
            "up",
            "-d",
        ],
    ],
    ids=["unexpected-env", "duplicate-env", "missing-env-assignment"],
)
def test_fixture_provider_rejects_env_outside_fixed_authority(argv: list[str]) -> None:
    provider, workspace = _fixture_provider("prompt_injection")

    async def exercise() -> object:
        sandbox_id = await provider.create(workspace, "negative-env")
        try:
            return await provider.exec(sandbox_id, argv)
        finally:
            await provider.destroy(sandbox_id)

    result = asyncio.run(exercise())

    _assert_unexpected(result, provider)


def test_fixture_provider_binds_observer_commands_to_discovered_identity_and_format() -> (
    None
):
    provider, workspace = _fixture_provider("redundant_privileged")

    async def exercise() -> tuple[list[object], str]:
        sandbox_id = await provider.create(workspace, "negative-observer")
        try:
            up = await provider.exec(
                sandbox_id,
                [
                    "docker",
                    "compose",
                    "-f",
                    "compose.yml",
                    "up",
                    "-d",
                    "--wait",
                    "--wait-timeout",
                    "60",
                ],
            )
            assert up.exit_code == 0
            discovery = await provider.exec(
                sandbox_id,
                [
                    "docker",
                    "compose",
                    "-f",
                    "compose.yml",
                    "ps",
                    "--all",
                    "--no-trunc",
                    "--orphans=false",
                    "--format",
                    "json",
                ],
            )
            discovered = json.loads(discovery.stdout)["ID"]
            wrong = "0" * 12
            commands = [
                ["docker", "inspect", wrong],
                ["docker", "diff", wrong],
                [
                    "docker",
                    "top",
                    wrong,
                    "-eo",
                    "pid,ppid,user,comm",
                ],
                [
                    "docker",
                    "top",
                    discovered,
                    "-o",
                    "pid,ppid,user,comm",
                ],
                ["docker", "top", discovered, "-eo", "pid"],
                [
                    "docker",
                    "top",
                    discovered,
                    "-eo",
                    "pid,ppid,user,comm",
                    "--arbitrary-tail",
                ],
            ]
            results = [await provider.exec(sandbox_id, command) for command in commands]
            return results, discovered
        finally:
            await provider.destroy(sandbox_id)

    results, discovered = asyncio.run(exercise())

    assert discovered != "0" * 12
    assert all(cast(Any, result).exit_code == 127 for result in results)
    assert all(
        cast(Any, result).stderr == "unsupported fixture command" for result in results
    )
    assert len(provider.unexpected_commands) == len(results)
    assert len(set(provider.unexpected_commands)) == len(results)


def test_graph_wrapped_provider_cancellation_propagates_after_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evaluator = _evaluator()
    manifest_dir = _isolated_fixture_project(tmp_path)
    original_provider = evaluator._FixtureProvider
    instances: list[object] = []

    class CancellingProvider(original_provider):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            instances.append(self)

        async def exec(
            self, sandbox_id: str, argv: list[str], timeout_s: int = 60
        ) -> object:
            del argv, timeout_s
            assert sandbox_id in self._sandboxes
            raise asyncio.CancelledError("provider cancellation")

    monkeypatch.setattr(evaluator, "_FixtureProvider", CancellingProvider)

    with pytest.raises(asyncio.CancelledError, match="provider cancellation"):
        try:
            evaluator.evaluate_benchmark(manifest_dir)
        finally:
            assert instances
            for provider in instances:
                assert provider.created_ids == ["eval-single-0001"]
                assert provider.destroyed_ids == provider.created_ids
                assert provider._sandboxes == {}


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
