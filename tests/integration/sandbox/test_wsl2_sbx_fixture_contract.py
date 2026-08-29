from pathlib import Path

from ruamel.yaml import YAML

_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "wsl2_compose"


def test_wsl2_compose_fixture_has_pinned_minimal_web_service() -> None:
    """Detect a mutable or privileged calibration fixture before runtime use."""
    compose_path = _FIXTURE_ROOT / "compose.yaml"
    fixture_text = compose_path.read_text(encoding="utf-8")
    compose = YAML(typ="safe").load(fixture_text)
    service = compose["services"]["web"]

    assert service["image"] == (
        "busybox:1.36.1@sha256:"
        "73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"
    )
    assert service["command"] == ["httpd", "-f", "-p", "8080", "-h", "/www"]
    assert service["volumes"] == ["./www:/www:ro"]
    assert service["healthcheck"]["test"] == [
        "CMD",
        "wget",
        "-q",
        "-O",
        "-",
        "http://127.0.0.1:8080/health.txt",
    ]
    assert "ports" not in service
    assert "privileged" not in service
    assert "network_mode" not in service
    assert "/var/run/docker.sock" not in fixture_text
    assert (_FIXTURE_ROOT / "www" / "index.html").read_text(encoding="utf-8") == (
        "repotrial-wsl2-sbx-ok\n"
    )
    assert (_FIXTURE_ROOT / "www" / "health.txt").read_text(encoding="utf-8") == "ok\n"
