import base64
import hashlib
import json
from pathlib import Path, PurePosixPath

import pytest
from ruamel.yaml import YAML

_FIXTURE_ROOT = Path(__file__).parents[2] / "fixtures" / "startup_input_diagnostic"
_EXPECTED_CASES = (
    "valid",
    "symlink_source",
    "oversized",
    "duplicate_key",
    "invalid_utf8",
    "shell_syntax",
    "forbidden_control_key",
    "secret_looking_value",
    "target_escape",
    "preexisting_target",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_startup_input_fixture_corpus_is_complete_and_content_addressed() -> None:
    manifest_path = _FIXTURE_ROOT / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["schema_version"] == 1
    assert manifest["corpus_id"] == "repotrial-startup-input-diagnostic-v1"
    cases = manifest["cases"]
    assert tuple(case["id"] for case in cases) == _EXPECTED_CASES

    declared_paths: set[Path] = set()
    for case in cases:
        assert set(case) == {"id", "representation", "artifacts"}
        assert case["artifacts"]
        for artifact in case["artifacts"]:
            assert set(artifact) == {"path", "sha256"}
            relative = PurePosixPath(artifact["path"])
            assert not relative.is_absolute()
            assert ".." not in relative.parts
            path = _FIXTURE_ROOT.joinpath(*relative.parts)
            assert path.is_file()
            assert not path.is_symlink()
            assert _sha256(path) == artifact["sha256"]
            declared_paths.add(path)

    actual_paths = {
        path
        for path in _FIXTURE_ROOT.rglob("*")
        if path.is_file() and path != manifest_path
    }
    assert actual_paths == declared_paths


def test_valid_fixture_has_service_env_file_and_absent_target() -> None:
    case_root = _FIXTURE_ROOT / "valid"
    compose = YAML(typ="safe").load(
        (case_root / "compose.yaml").read_text(encoding="utf-8")
    )
    sample = (case_root / ".env.sample").read_bytes()

    assert compose["services"]["web"]["env_file"] == [".env"]
    assert not (case_root / ".env").exists()
    assert len(sample) <= 4096
    assert sample.decode("utf-8") == (
        "APP_PORT=8080\nAPP_DATA_DIR=./data\nAPP_OPTIONAL_SECRET=\n"
    )


def test_compact_descriptors_reconstruct_nonportable_boundary_bytes() -> None:
    symlink = json.loads((_FIXTURE_ROOT / "symlink_source" / "source.json").read_text())
    oversized = json.loads(
        (_FIXTURE_ROOT / "oversized" / "source.recipe.json").read_text()
    )
    invalid_utf8 = json.loads(
        (_FIXTURE_ROOT / "invalid_utf8" / "source.json").read_text()
    )

    assert symlink == {"kind": "symlink", "target": ".env.sample.real"}
    value = bytes.fromhex(oversized["value_byte_hex"]) * oversized["value_size"]
    oversized_bytes = b"".join(
        f"{oversized['key_prefix']}{index:02d}=".encode("ascii") + value + b"\n"
        for index in range(oversized["entry_count"])
    )
    assignments = [line for line in oversized_bytes.splitlines() if line]
    assert len(oversized_bytes) == 65_650
    assert len(assignments) == 65
    assert all(line.count(b"=") == 1 for line in assignments)
    assert hashlib.sha256(oversized_bytes).hexdigest() == oversized["sha256"]
    invalid_bytes = base64.b64decode(invalid_utf8["base64"], validate=True)
    assert hashlib.sha256(invalid_bytes).hexdigest() == invalid_utf8["sha256"]
    with pytest.raises(UnicodeDecodeError):
        invalid_bytes.decode("utf-8")
