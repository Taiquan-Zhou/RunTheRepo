from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from repotrial import cli


def test_serve_dispatches_loopback_uvicorn(monkeypatch) -> None:
    calls: list[tuple[object, str, int]] = []
    sentinel = object()

    def fake_create_app(workspace: Path) -> object:
        assert workspace == Path.cwd()
        return sentinel

    def fake_run(application: object, *, host: str, port: int) -> None:
        calls.append((application, host, port))

    import uvicorn

    monkeypatch.setattr("repotrial.local_web.app.create_app", fake_create_app)
    monkeypatch.setattr(uvicorn, "run", fake_run)
    result = CliRunner().invoke(cli.create_app(), ["serve", "--port", "8877"])

    assert result.exit_code == 0
    assert calls == [(sentinel, "127.0.0.1", 8877)]
