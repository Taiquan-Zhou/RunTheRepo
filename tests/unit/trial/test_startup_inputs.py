import base64
import json
import os
from pathlib import Path
from shutil import copytree

import pytest

from repotrial.trial import startup_inputs
from repotrial.trial.startup_inputs import (
    StartupInputUnsupported,
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
