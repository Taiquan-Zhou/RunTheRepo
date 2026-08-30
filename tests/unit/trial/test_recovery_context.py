from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from repotrial.trial.recovery_context import (
    derive_recovery_context,
    project_recovery_evidence,
)


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_projection_orders_commands_bounds_fields_and_preserves_input() -> None:
    original = {
        "z-last": "z",
        "logs": "L" * 8_000,
        "up": "up",
        "ps": "ps",
        "a-first": "a",
    }

    projected = project_recovery_evidence(original)

    assert list(projected.logs) == ["up", "ps", "logs", "a-first", "z-last"]
    assert len(projected.logs["logs"]) == 4_096
    assert projected.logs["logs"].startswith("L" * 2_000)
    assert projected.logs["logs"].endswith("L" * 2_000)
    assert projected.logs["logs"].count("[TRUNCATED]") == 1
    assert original["logs"] == "L" * 8_000


def test_projection_enforces_entry_key_and_aggregate_limits_deterministically() -> None:
    logs = {f"unknown-{index:02d}": "x" * 4_096 for index in range(40)}
    logs["k" * 129] = "ignored"

    projected = project_recovery_evidence(logs)

    assert len(projected.logs) == 32
    assert list(projected.logs) == [f"unknown-{index:02d}" for index in range(32)]
    assert sum(map(len, projected.logs.values())) == 16_384
    assert "k" * 129 not in projected.logs


def test_projection_limits_aggregate_content_without_mutating_short_values() -> None:
    logs = {
        "up": "u" * 4_096,
        "ps": "p" * 4_096,
        "logs": "l" * 4_096,
        "extra": "e" * 4_096,
        "later": "must-not-appear",
    }

    projected = project_recovery_evidence(logs)

    assert list(projected.logs) == ["up", "ps", "logs", "extra"]
    assert sum(map(len, projected.logs.values())) == 16_384
    assert logs["later"] == "must-not-appear"


def test_derivation_collects_compose_forms_and_env_example_names_without_values(
    tmp_path: Path,
) -> None:
    compose = tmp_path / "compose.yml"
    _write(
        compose,
        "services:\n"
        "  web:\n"
        "    environment:\n"
        "      A: ${APP_TOKEN}\n"
        "      B: ${DATABASE_URL:-postgres://ignored}\n"
        "      C: ${CACHE_URL-redis://ignored}\n"
        "      D: ${REQUIRED_ONE:?missing}\n"
        "      E: ${REQUIRED_TWO?missing}\n",
    )
    env_example = tmp_path / ".env.example"
    _write(env_example, "DATABASE_URL=example-secret-value\nexport EXTRA_TOKEN=also-secret\n")

    context = derive_recovery_context(tmp_path, "compose.yml")

    assert context.allowed_env_keys == frozenset(
        {"APP_TOKEN", "DATABASE_URL", "CACHE_URL", "REQUIRED_ONE", "REQUIRED_TWO", "EXTRA_TOKEN"}
    )
    assert [(item.key, item.relative_path) for item in context.declarations] == [
        ("APP_TOKEN", "compose.yml"),
        ("CACHE_URL", "compose.yml"),
        ("DATABASE_URL", ".env.example"),
        ("DATABASE_URL", "compose.yml"),
        ("EXTRA_TOKEN", ".env.example"),
        ("REQUIRED_ONE", "compose.yml"),
        ("REQUIRED_TWO", "compose.yml"),
    ]
    assert {item.sha256 for item in context.declarations if item.relative_path == "compose.yml"} == {
        hashlib.sha256(compose.read_bytes()).hexdigest()
    }
    assert {item.sha256 for item in context.declarations if item.relative_path == ".env.example"} == {
        hashlib.sha256(env_example.read_bytes()).hexdigest()
    }
    assert "example-secret-value" not in context.model_dump_json()
    assert "also-secret" not in context.model_dump_json()


def test_derivation_ignores_env_host_readme_and_non_root_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path / "compose.yml", "services: {web: {image: example/web}}\n")
    _write(tmp_path / ".env", "HOST_ONLY=secret\n")
    _write(tmp_path / "README.md", "${README_INJECTION}\n")
    nested = tmp_path / "nested"
    nested.mkdir()
    _write(nested / ".env.example", "NESTED_ONLY=secret\n")
    monkeypatch.setenv("HOST_ENV_ONLY", "secret")

    context = derive_recovery_context(tmp_path, "compose.yml")

    assert context.allowed_env_keys == frozenset()
    assert context.declarations == ()


@pytest.mark.parametrize("content", ["\udcff", "x" * 65_537])
def test_derivation_rejects_invalid_or_oversized_utf8_sources(
    tmp_path: Path, content: str
) -> None:
    compose = tmp_path / "compose.yml"
    if content == "\udcff":
        compose.write_bytes(b"\xff")
    else:
        _write(compose, content)

    with pytest.raises(ValueError, match="compose"):
        derive_recovery_context(tmp_path, "compose.yml")


def test_derivation_rejects_linked_compose_and_env_example_without_skipping(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.yml"
    _write(target, "services: {}\n")
    (tmp_path / "compose.yml").symlink_to(target)

    with pytest.raises(ValueError, match="compose"):
        derive_recovery_context(tmp_path, "compose.yml")

    (tmp_path / "compose.yml").unlink()
    _write(tmp_path / "compose.yml", "services: {}\n")
    env_target = tmp_path / "env-target"
    _write(env_target, "DECLARED=secret\n")
    (tmp_path / ".env.example").symlink_to(env_target)

    with pytest.raises(ValueError, match="env.example"):
        derive_recovery_context(tmp_path, "compose.yml")


def test_derivation_limits_and_sorts_portable_keys(tmp_path: Path) -> None:
    keys = [f"KEY_{index:02d}" for index in range(40)]
    _write(
        tmp_path / "compose.yml",
        "\n".join(f"  - ${{{key}}}" for key in reversed(keys)) + "\n  - ${NOT-VALID}\n",
    )

    context = derive_recovery_context(tmp_path, "compose.yml")

    assert context.allowed_env_keys == frozenset(keys[:32])
    assert [item.key for item in context.declarations] == keys[:32]
