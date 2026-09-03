import asyncio
import base64
import hashlib
import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from shutil import copytree

import pytest

from repotrial.sandbox.base import ExecResult
from repotrial.sandbox.fake import FakeSandboxProvider
from repotrial.trial import startup_inputs
from repotrial.trial.startup_inputs import (
    StartupInputUnsupported,
    materialize_startup_input,
    plan_startup_input,
)

_FIXTURE_ROOT = Path(__file__).parents[2] / "fixtures" / "startup_input_diagnostic"


def _copy_fixture(tmp_path: Path, case: str) -> Path:
    workspace = tmp_path / case
    copytree(_FIXTURE_ROOT / "valid", workspace)
    return workspace


def test_plans_valid_fixture_with_sorted_canonical_output(tmp_path: Path) -> None:
    workspace = _copy_fixture(tmp_path, "valid")

    plan = plan_startup_input(workspace, "compose.yaml")

    assert plan is not None
    assert plan.source_relative_path == ".env.sample"
    assert plan.target_relative_path == ".env"
    assert plan.policy_id == "startup-input-v1-option-b"
    assert plan.compose_relative_path == "compose.yaml"
    assert (
        plan.compose_config_hash
        == "3f557c02f339397a7150254f682d9db0af3c292ea22f1f64f4c43cd6143a492a"
    )
    assert plan.source_sha256 == (
        "0b63e607779ac40af45a3fc0555728bcdac6b7b93a66146f80180f5e9625f775"
    )
    assert plan.output_sha256 == (
        "af2923cb67efd7ceb82a1e5668cd4e76429a05bf59b82a1e6dad386b5bc1d5fa"
    )
    assert plan.output_bytes == (
        b"APP_DATA_DIR=./data\nAPP_OPTIONAL_SECRET=\nAPP_PORT=8080\n"
    )
    assert plan.all_source_key_names == (
        "APP_DATA_DIR",
        "APP_OPTIONAL_SECRET",
        "APP_PORT",
    )
    assert plan.accepted_key_names == plan.all_source_key_names
    assert plan.synthetic_key_names == ()
    assert plan.expected_service_names == ("web",)
    assert plan.omitted_control_key_names == ()


def test_omits_control_assignments_under_option_b(tmp_path: Path) -> None:
    workspace = _copy_fixture(tmp_path, "control")
    (workspace / ".env.sample").write_bytes(
        b"LD_HOST_PORT=9090\n"
        b"ld_preload=./bad.so\n"
        b"DOCKER_HOST=tcp://127.0.0.1:2375\n"
        b"APP_PASSWORD=requires-user-secret\n"
    )

    plan = plan_startup_input(workspace, "compose.yaml")

    assert plan is not None
    assert plan.output_bytes == b"APP_PASSWORD=repotrial-synthetic-value\n"
    assert plan.omitted_control_key_names == (
        "DOCKER_HOST",
        "LD_HOST_PORT",
        "ld_preload",
    )


def test_option_b_control_set_is_closed_and_case_insensitive(tmp_path: Path) -> None:
    workspace = _copy_fixture(tmp_path, "closed_control_set")
    control_keys = (
        "COMPOSE_FILE",
        "compose_project_name",
        "DOCKER_HOST",
        "docker_context",
        "DYLD_INSERT_LIBRARIES",
        "dyld_library_path",
        "LD_PRELOAD",
        "ld_host_port",
        "PATH",
        "path",
        "HOME",
        "home",
        "PYTHONHOME",
        "pythonhome",
        "PYTHONPATH",
        "pythonpath",
        "XDG_CONFIG_HOME",
        "xdg_config_home",
    )
    source_keys = (*control_keys, "APP_MODE")
    (workspace / ".env.sample").write_text(
        "".join(f"{key}=safe\n" for key in source_keys), encoding="utf-8"
    )

    plan = plan_startup_input(workspace, "compose.yaml")

    assert plan is not None
    assert plan.output_bytes == b"APP_MODE=safe\n"
    assert plan.accepted_key_names == ("APP_MODE",)
    assert plan.omitted_control_key_names == tuple(sorted(control_keys))
    assert plan.all_source_key_names == tuple(sorted(source_keys))


def test_option_b_allows_an_empty_generated_file(tmp_path: Path) -> None:
    workspace = _copy_fixture(tmp_path, "empty_control_output")
    (workspace / ".env.sample").write_bytes(b"DOCKER_HOST=safe\n")

    plan = plan_startup_input(workspace, "compose.yaml")

    assert plan is not None
    assert plan.output_bytes == b""
    assert plan.accepted_key_names == ()
    assert plan.omitted_control_key_names == ("DOCKER_HOST",)


def _diagnostic_workspace(tmp_path: Path, case: str) -> Path:
    workspace = _copy_fixture(tmp_path, case)
    case_root = _FIXTURE_ROOT / case
    source = workspace / ".env.sample"
    if case in {
        "duplicate_key",
        "shell_syntax",
        "forbidden_control_key",
        "secret_looking_value",
    }:
        source.write_bytes((case_root / ".env.sample").read_bytes())
    elif case == "invalid_utf8":
        descriptor = json.loads((case_root / "source.json").read_text())
        source.write_bytes(base64.b64decode(descriptor["base64"], validate=True))
    elif case == "oversized":
        descriptor = json.loads((case_root / "source.recipe.json").read_text())
        value = bytes.fromhex(descriptor["value_byte_hex"]) * descriptor["value_size"]
        source.write_bytes(
            b"".join(
                f"{descriptor['key_prefix']}{index:02d}=".encode("ascii")
                + value
                + b"\n"
                for index in range(descriptor["entry_count"])
            )
        )
    elif case == "symlink_source":
        source.unlink()
        source.symlink_to(case_root / ".env.sample.real")
    elif case == "target_escape":
        (workspace / "compose.yaml").write_text(
            "services:\n  web:\n    image: busybox:1.36.1\n    env_file:\n      - ../.env\n",
            encoding="utf-8",
        )
    elif case == "source_missing":
        source.unlink()
    elif case == "target_preexisting":
        (workspace / ".env").write_bytes(b"APP_MODE=existing\n")
    return workspace


@pytest.mark.parametrize(
    ("case", "reason"),
    (
        ("symlink_source", "source_linked"),
        ("oversized", "source_too_large"),
        ("duplicate_key", "duplicate_key"),
        ("invalid_utf8", "invalid_utf8"),
        ("shell_syntax", "unsupported_syntax"),
        ("target_escape", "target_escape"),
        ("source_missing", "source_missing"),
        ("target_preexisting", "target_preexisting"),
    ),
)
def test_diagnostic_corpus_rejections(tmp_path: Path, case: str, reason: str) -> None:
    workspace = _diagnostic_workspace(tmp_path, case)

    with pytest.raises(StartupInputUnsupported) as error:
        plan_startup_input(workspace, "compose.yaml")

    assert error.value.reason == reason
    assert str(error.value) == reason


def test_reconstructs_assignment_count_boundary(tmp_path: Path) -> None:
    workspace = _copy_fixture(tmp_path, "too_many_assignments")
    descriptor = json.loads(
        (_FIXTURE_ROOT / "oversized" / "source.recipe.json").read_text()
    )
    workspace.joinpath(".env.sample").write_bytes(
        b"".join(
            f"{descriptor['key_prefix']}{index:02d}=A\n".encode("ascii")
            for index in range(descriptor["entry_count"])
        )
    )

    with pytest.raises(StartupInputUnsupported) as error:
        plan_startup_input(workspace, "compose.yaml")

    assert error.value.reason == "too_many_assignments"


def test_source_identity_change_is_rejected_without_source_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _copy_fixture(tmp_path, "identity_change")
    source = workspace / ".env.sample"
    original_read = os.read

    def replace_after_read(descriptor: int, size: int) -> bytes:
        data = original_read(descriptor, size)
        source.unlink()
        source.write_bytes(b"APP_PORT=8081\n")
        return data

    monkeypatch.setattr(startup_inputs.os, "read", replace_after_read)

    with pytest.raises(StartupInputUnsupported) as error:
        plan_startup_input(workspace, "compose.yaml")

    assert error.value.reason == "source_changed"
    assert "APP_PORT=8081" not in str(error.value)


def test_same_inode_same_size_source_rewrite_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _copy_fixture(tmp_path, "same_inode_rewrite")
    source = workspace / ".env.sample"
    original_inode = source.stat().st_ino
    original_read = os.read

    def rewrite_after_read(descriptor: int, size: int) -> bytes:
        data = original_read(descriptor, size)
        source.write_bytes(data.replace(b"8080", b"8081"))
        assert source.stat().st_ino == original_inode
        assert source.stat().st_size == len(data)
        return data

    monkeypatch.setattr(startup_inputs.os, "read", rewrite_after_read)

    with pytest.raises(StartupInputUnsupported) as error:
        plan_startup_input(workspace, "compose.yaml")

    assert error.value.reason == "source_changed"


def test_source_missing_after_target_eligibility_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _copy_fixture(tmp_path, "source_missing_race")
    source = workspace / ".env.sample"
    original_open = os.open

    def remove_before_open(
        path: str | bytes | os.PathLike[str], flags: int, *args: int
    ) -> int:
        if Path(path) == source:
            source.unlink()
        return original_open(path, flags, *args)

    monkeypatch.setattr(startup_inputs.os, "open", remove_before_open)

    with pytest.raises(StartupInputUnsupported) as error:
        plan_startup_input(workspace, "compose.yaml")

    assert error.value.reason == "source_missing"


def test_ambiguous_compose_shape_is_not_selected(tmp_path: Path) -> None:
    workspace = _copy_fixture(tmp_path, "ambiguous_compose")
    (workspace / "compose.yaml").write_text(
        "services:\n"
        "  web:\n"
        "    image: busybox:1.36.1\n"
        "    env_file:\n"
        "      - .env\n"
        "      - extra.env\n",
        encoding="utf-8",
    )

    assert plan_startup_input(workspace, "compose.yaml") is None


@pytest.mark.parametrize("extra_required_paths", (("extra.env",), ("a.env", "b.env")))
def test_other_required_env_files_are_consistently_unsupported(
    tmp_path: Path, extra_required_paths: tuple[str, ...]
) -> None:
    workspace = _copy_fixture(tmp_path, "other_required_env")
    extra_lines = "".join(f"      - {path}\n" for path in extra_required_paths)
    (workspace / "compose.yaml").write_text(
        "services:\n"
        "  worker:\n"
        "    image: busybox:1.36.1\n"
        "    env_file:\n"
        f"{extra_lines}"
        "  web:\n"
        "    image: busybox:1.36.1\n"
        "    env_file:\n"
        "      - .env\n",
        encoding="utf-8",
    )

    assert plan_startup_input(workspace, "compose.yaml") is None


def test_accepted_compose_uses_clone_root_and_records_every_service(
    tmp_path: Path,
) -> None:
    workspace = _copy_fixture(tmp_path, "accepted_compose")
    accepted_dir = workspace / ".repotrial-accepted"
    accepted_dir.mkdir()
    accepted_compose = accepted_dir / "accepted-0001.compose.yaml"
    accepted_compose.write_text(
        "services:\n"
        "  db:\n"
        "    image: busybox:1.36.1\n"
        "  web:\n"
        "    image: busybox:1.36.1\n"
        "    env_file:\n"
        "      - .env\n",
        encoding="utf-8",
    )

    plan = plan_startup_input(
        workspace, accepted_compose.relative_to(workspace).as_posix()
    )

    assert plan is not None
    assert plan.compose_relative_path == (
        ".repotrial-accepted/accepted-0001.compose.yaml"
    )
    assert plan.source_relative_path == ".env.sample"
    assert plan.target_relative_path == ".env"
    assert plan.expected_service_names == ("db", "web")


def test_compose_path_with_parent_component_is_rejected(tmp_path: Path) -> None:
    workspace = _copy_fixture(tmp_path, "compose_parent_component")
    (workspace / "nested").mkdir()

    with pytest.raises(StartupInputUnsupported) as error:
        plan_startup_input(workspace, "nested/../compose.yaml")

    assert error.value.reason == "compose_path_invalid"


def test_linked_compose_path_is_rejected(tmp_path: Path) -> None:
    workspace = _copy_fixture(tmp_path, "linked_compose")
    linked = workspace / "linked-compose.yaml"
    linked.symlink_to("compose.yaml")

    with pytest.raises(StartupInputUnsupported) as error:
        plan_startup_input(workspace, linked.name)

    assert error.value.reason == "compose_path_invalid"


@pytest.mark.parametrize("workspace_kind", ("missing", "file"))
def test_invalid_workspace_is_rejected(tmp_path: Path, workspace_kind: str) -> None:
    workspace = tmp_path / "invalid-workspace"
    if workspace_kind == "file":
        workspace.write_bytes(b"not a directory")

    with pytest.raises(StartupInputUnsupported) as error:
        plan_startup_input(workspace, "compose.yaml")

    assert error.value.reason == "workspace_invalid"


@pytest.mark.parametrize(
    "compose_path", ("", "missing.yaml", "bad\\path.yaml", "bad\npath.yaml")
)
def test_invalid_compose_paths_are_rejected(tmp_path: Path, compose_path: str) -> None:
    workspace = _copy_fixture(tmp_path, "invalid_compose_path")

    with pytest.raises(StartupInputUnsupported) as error:
        plan_startup_input(workspace, compose_path)

    assert error.value.reason == "compose_path_invalid"


def test_absolute_env_target_is_rejected(tmp_path: Path) -> None:
    workspace = _copy_fixture(tmp_path, "absolute_env_target")
    (workspace / "compose.yaml").write_text(
        "services:\n  web:\n    image: busybox:1.36.1\n    env_file: /tmp/.env\n",
        encoding="utf-8",
    )

    with pytest.raises(StartupInputUnsupported) as error:
        plan_startup_input(workspace, "compose.yaml")

    assert error.value.reason == "target_escape"


def test_nonregular_source_is_rejected(tmp_path: Path) -> None:
    workspace = _copy_fixture(tmp_path, "nonregular_source")
    source = workspace / ".env.sample"
    source.unlink()
    source.mkdir()

    with pytest.raises(StartupInputUnsupported) as error:
        plan_startup_input(workspace, "compose.yaml")

    assert error.value.reason == "source_linked"


@pytest.mark.parametrize(
    "env_file_yaml",
    (
        "    env_file: .env\n",
        "    env_file:\n      - .env\n",
        "    env_file:\n      path: .env\n",
        "    env_file:\n      path: .env\n      required: true\n",
        "    env_file:\n      - path: .env\n        required: true\n",
    ),
)
def test_accepts_unambiguous_compose_env_file_forms(
    tmp_path: Path, env_file_yaml: str
) -> None:
    workspace = _copy_fixture(tmp_path, "compose_form")
    yaml = "services:\n  web:\n    image: busybox:1.36.1\n" + env_file_yaml
    (workspace / "compose.yaml").write_text(yaml, encoding="utf-8")

    plan = plan_startup_input(workspace, "compose.yaml")

    assert plan is not None


@pytest.mark.parametrize(
    "env_file_yaml",
    (
        "    env_file:\n      path: .env\n      required: false\n",
        "    env_file:\n      path: .env\n      required: invalid\n",
        "    env_file: 42\n",
    ),
)
def test_rejects_optional_or_invalid_env_file_forms(
    tmp_path: Path, env_file_yaml: str
) -> None:
    workspace = _copy_fixture(tmp_path, "invalid_compose_form")
    yaml = "services:\n  web:\n    image: busybox:1.36.1\n" + env_file_yaml
    (workspace / "compose.yaml").write_text(yaml, encoding="utf-8")

    assert plan_startup_input(workspace, "compose.yaml") is None


@pytest.mark.parametrize(
    "source", (b"# hidden\rcontrol\nAPP_MODE=safe\n", b"APP_MODE=sa\rfe\n")
)
def test_bare_carriage_return_is_rejected(tmp_path: Path, source: bytes) -> None:
    workspace = _copy_fixture(tmp_path, "bare_carriage_return")
    (workspace / ".env.sample").write_bytes(source)

    with pytest.raises(StartupInputUnsupported) as error:
        plan_startup_input(workspace, "compose.yaml")

    assert error.value.reason == "unsupported_syntax"


def test_crlf_line_endings_are_accepted(tmp_path: Path) -> None:
    workspace = _copy_fixture(tmp_path, "crlf")
    (workspace / ".env.sample").write_bytes(b"APP_MODE=safe\r\n")

    plan = plan_startup_input(workspace, "compose.yaml")

    assert plan is not None
    assert plan.output_bytes == b"APP_MODE=safe\n"


@pytest.mark.parametrize(
    "source",
    (
        b"APP_PATH=/tmp\n",
        b"APP_PATH=../tmp\n",
        b"APP_PATH=./a/../../b\n",
        b"APP_PATH=a/../b\n",
        b"APP_PATH=./bad?name\n",
        b"APP_MODE=safe\x00hidden\n",
        "APP_MODE=\N{SNOWMAN}\n".encode(),
    ),
)
def test_unsupported_value_forms_are_rejected(tmp_path: Path, source: bytes) -> None:
    workspace = _copy_fixture(tmp_path, "unsupported_value")
    (workspace / ".env.sample").write_bytes(source)

    with pytest.raises(StartupInputUnsupported) as error:
        plan_startup_input(workspace, "compose.yaml")

    assert error.value.reason == "unsupported_syntax"


@pytest.mark.parametrize(
    ("line_size", "accepted"), ((4095, True), (4096, True), (4097, False))
)
def test_logical_line_length_boundary(
    tmp_path: Path, line_size: int, accepted: bool
) -> None:
    workspace = _copy_fixture(tmp_path, f"line_{line_size}")
    prefix = b"APP_VALUE="
    (workspace / ".env.sample").write_bytes(prefix + b"A" * (line_size - len(prefix)))

    if accepted:
        assert plan_startup_input(workspace, "compose.yaml") is not None
    else:
        with pytest.raises(StartupInputUnsupported) as error:
            plan_startup_input(workspace, "compose.yaml")
        assert error.value.reason == "source_too_large"


def test_plan_repr_and_exception_do_not_expose_values(tmp_path: Path) -> None:
    workspace = _diagnostic_workspace(tmp_path, "secret_looking_value")
    plan = plan_startup_input(workspace, "compose.yaml")
    assert plan is not None

    rendered = repr(plan)
    assert "output_bytes" not in rendered
    assert "requires-user-secret" not in rendered
    assert "repotrial-synthetic-value" not in rendered
    assert plan.synthetic_key_names == ("SERVICE_TOKEN",)
    assert plan.output_bytes == b"SERVICE_TOKEN=repotrial-synthetic-value\n"


def test_forbidden_control_values_are_validated_before_omission(tmp_path: Path) -> None:
    workspace = _diagnostic_workspace(tmp_path, "forbidden_control_key")
    (workspace / ".env.sample").write_bytes(b"DOCKER_HOST=$(id)\n")

    with pytest.raises(StartupInputUnsupported) as error:
        plan_startup_input(workspace, "compose.yaml")

    assert error.value.reason == "unsupported_syntax"
    assert "$(id)" not in str(error.value)


class _MaterializeProvider(FakeSandboxProvider):
    def __init__(
        self,
        *,
        adapter_result: ExecResult | BaseException | None = None,
        config: object | None = None,
        config_result: ExecResult | BaseException | None = None,
    ) -> None:
        super().__init__()
        self.exec_calls: list[tuple[str, ...]] = []
        self.adapter_result = adapter_result or ExecResult(
            exit_code=0, stdout="root=/workspace\nmode=600\n", stderr=""
        )
        self.config = config or {"services": {"web": {"image": "busybox:1.36.1"}}}
        self.config_result = config_result

    async def exec(
        self, sandbox_id: str, argv: list[str], timeout_s: int = 60
    ) -> ExecResult:
        del sandbox_id, timeout_s
        self.exec_calls.append(tuple(argv))
        if len(self.exec_calls) == 1:
            if isinstance(self.adapter_result, BaseException):
                raise self.adapter_result
            return self.adapter_result
        if isinstance(self.config_result, BaseException):
            raise self.config_result
        if self.config_result is not None:
            return self.config_result
        return ExecResult(
            exit_code=0,
            stdout=json.dumps(self.config),
            stderr="",
        )


def test_materializes_guest_input_before_resolved_compose_config(
    tmp_path: Path,
) -> None:
    workspace = _copy_fixture(tmp_path, "materialize")
    plan = plan_startup_input(workspace, "compose.yaml")
    assert plan is not None
    provider = _MaterializeProvider()
    evidence_path = tmp_path / "startup-input-attempt-0001.jsonl"

    result = asyncio.run(
        materialize_startup_input(
            provider,
            "sandbox-1",
            plan,
            compose_path="compose.yaml",
            compose_env={"APP_MODE": "test"},
            evidence_path=evidence_path,
        )
    )

    assert provider.exec_calls[0][:10] == (
        "env",
        "-u",
        "APP_DATA_DIR",
        "-u",
        "APP_OPTIONAL_SECRET",
        "-u",
        "APP_PORT",
        "APP_MODE=test",
        "sh",
        "-eu",
    )
    assert provider.exec_calls[0][10] == "-c"
    assert hashlib.sha256(provider.exec_calls[0][11].encode()).hexdigest() == (
        "6c183581aa20385d694faf4f00ee19331ac57dfbf98cad553637df654be45fda"
    )
    assert provider.exec_calls[1][:8] == (
        "env",
        "-u",
        "APP_DATA_DIR",
        "-u",
        "APP_OPTIONAL_SECRET",
        "-u",
        "APP_PORT",
        "APP_MODE=test",
    )
    assert provider.exec_calls[1][8:15] == (
        "docker",
        "compose",
        "--project-directory",
        ".",
        "-f",
        "compose.yaml",
        "config",
    )
    assert provider.exec_calls[1][15:] == ("--format", "json")
    assert result.target_mode == 0o600
    assert result.artifact_relative_path == ".env"
    rows = [json.loads(line) for line in evidence_path.read_text().splitlines()]
    assert [row["outcome"] for row in rows] == ["start", "terminal"]
    assert rows[1]["resolved_compose_sha256"] == result.resolved_compose_sha256
    evidence_text = evidence_path.read_text()
    assert "8080" not in evidence_text
    assert "repotrial-synthetic-value" not in evidence_text
    assert base64.b64encode(plan.output_bytes).decode() not in evidence_text
    assert stat.S_IMODE(evidence_path.stat().st_mode) == 0o600
    assert rows[0]["expected_service_names"] == ["web"]


def test_materializer_uses_overlay_in_resolved_config_argv(tmp_path: Path) -> None:
    workspace = _copy_fixture(tmp_path, "materialize_overlay")
    plan = plan_startup_input(workspace, "compose.yaml")
    assert plan is not None
    provider = _MaterializeProvider()

    asyncio.run(
        materialize_startup_input(
            provider,
            "sandbox-1",
            plan,
            compose_path="compose.yaml",
            overlay_path="overlays/candidate.yaml",
            compose_env={},
            evidence_path=tmp_path / "attempt.jsonl",
        )
    )

    assert provider.exec_calls[1][-7:] == (
        "-f",
        "compose.yaml",
        "-f",
        "overlays/candidate.yaml",
        "config",
        "--format",
        "json",
    )


def test_evidence_collision_fails_before_guest_exec(tmp_path: Path) -> None:
    workspace = _copy_fixture(tmp_path, "evidence_collision")
    plan = plan_startup_input(workspace, "compose.yaml")
    assert plan is not None
    provider = _MaterializeProvider()
    evidence_path = tmp_path / "attempt.jsonl"
    evidence_path.write_text("owned", encoding="utf-8")

    with pytest.raises(StartupInputUnsupported) as error:
        asyncio.run(
            materialize_startup_input(
                provider,
                "sandbox-1",
                plan,
                compose_path="compose.yaml",
                compose_env={},
                evidence_path=evidence_path,
            )
        )

    assert error.value.reason == "evidence_collision"
    assert provider.exec_calls == []
    assert evidence_path.read_text(encoding="utf-8") == "owned"


def test_adapter_failure_is_bounded_and_skips_config(tmp_path: Path) -> None:
    workspace = _copy_fixture(tmp_path, "adapter_failure")
    plan = plan_startup_input(workspace, "compose.yaml")
    assert plan is not None
    provider = _MaterializeProvider(
        adapter_result=ExecResult(
            exit_code=25, stdout="", stderr="sensitive-repository-text"
        )
    )
    evidence_path = tmp_path / "attempt.jsonl"

    with pytest.raises(StartupInputUnsupported) as error:
        asyncio.run(
            materialize_startup_input(
                provider,
                "sandbox-1",
                plan,
                compose_path="compose.yaml",
                compose_env={},
                evidence_path=evidence_path,
            )
        )

    assert error.value.reason == "guest_validation_failed"
    assert len(provider.exec_calls) == 1
    evidence = evidence_path.read_text(encoding="utf-8")
    assert "guest_validation_failed" in evidence
    assert "sensitive-repository-text" not in evidence


@pytest.mark.parametrize(
    "adapter_result",
    (
        ExecResult(exit_code=0, stdout="root=/wrong\nmode=600\n", stderr=""),
        ExecResult(
            exit_code=0,
            stdout="root=/workspace\nmode=600\n",
            stderr="unexpected",
        ),
    ),
)
def test_invalid_adapter_success_shape_is_rejected(
    tmp_path: Path, adapter_result: ExecResult
) -> None:
    workspace = _copy_fixture(tmp_path, "adapter_shape")
    plan = plan_startup_input(workspace, "compose.yaml")
    assert plan is not None
    provider = _MaterializeProvider(adapter_result=adapter_result)

    with pytest.raises(StartupInputUnsupported) as error:
        asyncio.run(
            materialize_startup_input(
                provider,
                "sandbox-1",
                plan,
                compose_path="compose.yaml",
                compose_env={},
                evidence_path=tmp_path / "attempt.jsonl",
            )
        )

    assert error.value.reason == "guest_validation_failed"
    assert len(provider.exec_calls) == 1


def test_provider_exception_is_preserved_without_message_in_evidence(
    tmp_path: Path,
) -> None:
    workspace = _copy_fixture(tmp_path, "provider_exception")
    plan = plan_startup_input(workspace, "compose.yaml")
    assert plan is not None
    provider = _MaterializeProvider(
        adapter_result=TimeoutError("sensitive-provider-message")
    )
    evidence_path = tmp_path / "attempt.jsonl"

    with pytest.raises(TimeoutError, match="sensitive-provider-message"):
        asyncio.run(
            materialize_startup_input(
                provider,
                "sandbox-1",
                plan,
                compose_path="compose.yaml",
                compose_env={},
                evidence_path=evidence_path,
            )
        )

    evidence = evidence_path.read_text(encoding="utf-8")
    assert "provider_exception" in evidence
    assert "sensitive-provider-message" not in evidence


def test_config_failure_is_bounded(tmp_path: Path) -> None:
    workspace = _copy_fixture(tmp_path, "config_failure")
    plan = plan_startup_input(workspace, "compose.yaml")
    assert plan is not None
    provider = _MaterializeProvider(
        config_result=ExecResult(
            exit_code=1, stdout="", stderr="sensitive-config-message"
        )
    )
    evidence_path = tmp_path / "attempt.jsonl"

    with pytest.raises(StartupInputUnsupported) as error:
        asyncio.run(
            materialize_startup_input(
                provider,
                "sandbox-1",
                plan,
                compose_path="compose.yaml",
                compose_env={},
                evidence_path=evidence_path,
            )
        )

    assert error.value.reason == "resolved_config_failed"
    evidence = evidence_path.read_text(encoding="utf-8")
    assert "resolved_config_failed" in evidence
    assert "sensitive-config-message" not in evidence


@pytest.mark.parametrize(
    ("stdout", "reason"),
    (
        ("not-json", "resolved_config_invalid"),
        ("[]", "resolved_config_invalid"),
        (
            '{"services":{"other":{"image":"busybox"}}}',
            "resolved_service_mismatch",
        ),
        ('{"services":{"web":[]}}', "resolved_config_invalid"),
        (
            '{"services":{"web":{"image":"a","image":"b"}}}',
            "resolved_config_invalid",
        ),
        ('{"services":{"web":{"value":NaN}}}', "resolved_config_invalid"),
        ('{"services":{"web":{"value":1e999}}}', "resolved_config_invalid"),
    ),
)
def test_invalid_resolved_config_is_rejected(
    tmp_path: Path, stdout: str, reason: str
) -> None:
    workspace = _copy_fixture(tmp_path, "invalid_resolved_config")
    plan = plan_startup_input(workspace, "compose.yaml")
    assert plan is not None
    provider = _MaterializeProvider(
        config_result=ExecResult(exit_code=0, stdout=stdout, stderr="")
    )

    with pytest.raises(StartupInputUnsupported) as error:
        asyncio.run(
            materialize_startup_input(
                provider,
                "sandbox-1",
                plan,
                compose_path="compose.yaml",
                compose_env={},
                evidence_path=tmp_path / "attempt.jsonl",
            )
        )

    assert error.value.reason == reason


@pytest.mark.parametrize(
    "service",
    (
        {"image": "busybox", "privileged": True},
        {"image": "busybox", "network_mode": "host"},
        {"image": "busybox", "devices": ["/dev/null:/dev/null"]},
        {
            "image": "busybox",
            "volumes": [{"type": "bind", "source": "/host", "target": "/data"}],
        },
        {
            "image": "busybox",
            "volumes": [
                {
                    "type": "bind",
                    "source": "/workspace/data",
                    "target": "/var/run/docker.sock",
                }
            ],
        },
        {
            "image": "busybox",
            "volumes": [{"type": "volume", "source": "cache", "target": "docker.sock"}],
        },
    ),
)
def test_unsafe_resolved_compose_is_rejected(
    tmp_path: Path, service: dict[str, object]
) -> None:
    workspace = _copy_fixture(tmp_path, "unsafe_resolved")
    plan = plan_startup_input(workspace, "compose.yaml")
    assert plan is not None
    provider = _MaterializeProvider(config={"services": {"web": service}})

    with pytest.raises(StartupInputUnsupported) as error:
        asyncio.run(
            materialize_startup_input(
                provider,
                "sandbox-1",
                plan,
                compose_path="compose.yaml",
                compose_env={},
                evidence_path=tmp_path / "attempt.jsonl",
            )
        )

    assert error.value.reason == "unsafe_resolved_compose"


def test_confined_bind_and_named_volume_are_accepted(tmp_path: Path) -> None:
    workspace = _copy_fixture(tmp_path, "safe_resolved_volumes")
    plan = plan_startup_input(workspace, "compose.yaml")
    assert plan is not None
    provider = _MaterializeProvider(
        config={
            "services": {
                "web": {
                    "image": "busybox",
                    "volumes": [
                        {
                            "type": "bind",
                            "source": "/workspace/data",
                            "target": "/data",
                        },
                        {"type": "volume", "source": "cache", "target": "/cache"},
                    ],
                }
            }
        }
    )

    result = asyncio.run(
        materialize_startup_input(
            provider,
            "sandbox-1",
            plan,
            compose_path="compose.yaml",
            compose_env={},
            evidence_path=tmp_path / "attempt.jsonl",
        )
    )

    assert result.target_mode == 0o600


def test_invalid_identity_and_environment_fail_before_evidence(tmp_path: Path) -> None:
    workspace = _copy_fixture(tmp_path, "invalid_materializer_input")
    plan = plan_startup_input(workspace, "compose.yaml")
    assert plan is not None
    provider = _MaterializeProvider()

    with pytest.raises(StartupInputUnsupported, match="compose_identity_mismatch"):
        asyncio.run(
            materialize_startup_input(
                provider,
                "sandbox-1",
                plan,
                compose_path="other.yaml",
                compose_env={},
                evidence_path=tmp_path / "identity.jsonl",
            )
        )
    with pytest.raises(ValueError, match="controls the Compose toolchain"):
        asyncio.run(
            materialize_startup_input(
                provider,
                "sandbox-1",
                plan,
                compose_path="compose.yaml",
                compose_env={"DOCKER_HOST": "safe"},
                evidence_path=tmp_path / "environment.jsonl",
            )
        )
    invalid_plan = replace(plan, all_source_key_names=("BAD-NAME",))
    with pytest.raises(StartupInputUnsupported, match="environment_key_invalid"):
        asyncio.run(
            materialize_startup_input(
                provider,
                "sandbox-1",
                invalid_plan,
                compose_path="compose.yaml",
                compose_env={},
                evidence_path=tmp_path / "unset.jsonl",
            )
        )

    with pytest.raises(StartupInputUnsupported, match="overlay_path_invalid"):
        asyncio.run(
            materialize_startup_input(
                provider,
                "sandbox-1",
                plan,
                compose_path="compose.yaml",
                overlay_path="bad\npath.yaml",
                compose_env={},
                evidence_path=tmp_path / "overlay.jsonl",
            )
        )

    assert provider.exec_calls == []
