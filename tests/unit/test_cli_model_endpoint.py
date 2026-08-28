from pathlib import Path

import pytest
from typer.testing import CliRunner

from repotrial import cli
from repotrial.agent.state import GraphContext, GraphState
from repotrial.domain.models import RunState
from repotrial.sandbox.fake import FakeSandboxProvider


@pytest.mark.parametrize(
    ("options", "missing"),
    [
        (["--model-endpoint", "http://127.0.0.1:8000/v1"], "--model-name"),
        (["--model-name", "local-model"], "--model-endpoint"),
    ],
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_model_endpoint_and_name_must_be_supplied_as_a_pair_before_artifacts(
    tmp_path: Path, options: list[str], missing: str, dry_run: bool
) -> None:
    artifacts_root = tmp_path / "artifacts"
    app = cli.create_app(artifacts_root=artifacts_root)

    command = ["inspect", *options]
    if dry_run:
        command.append("--dry-run")
    command.append("https://github.com/a/b")
    result = CliRunner().invoke(app, command)

    assert result.exit_code != 0
    assert missing in result.output
    assert not artifacts_root.exists()


def test_model_options_construct_and_inject_the_adapter_at_the_cli_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    class StubAdapter:
        def __init__(
            self, endpoint: str, model_name: str, *, api_key: str | None = None
        ) -> None:
            captured["adapter"] = self
            captured["endpoint"] = endpoint
            captured["model_name"] = model_name
            captured["api_key"] = api_key

    async def fake_ainvoke(
        graph: object, state: RunState, *, context: GraphContext
    ) -> GraphState:
        captured["graph"] = graph
        captured["context_model"] = context.model
        return GraphState(run=state.model_copy(update={"stop_reason": "stopped"}))

    monkeypatch.setenv("REPOTRIAL_MODEL_API_KEY", "test-secret-key")
    monkeypatch.setattr(cli, "OpenAICompatibleModelAdapter", StubAdapter)
    monkeypatch.setattr(cli, "build_run_graph", lambda: object())
    monkeypatch.setattr(cli, "ainvoke_run", fake_ainvoke)
    app = cli.create_app(
        artifacts_root=tmp_path / "artifacts",
        provider_factory=lambda _: FakeSandboxProvider(),
    )

    result = CliRunner().invoke(
        app,
        [
            "inspect",
            "--provider",
            "fake",
            "--model-endpoint",
            "http://127.0.0.1:8000/v1",
            "--model-name",
            "local-model",
            "https://github.com/a/b",
        ],
    )

    assert result.exit_code == 3
    assert captured["endpoint"] == "http://127.0.0.1:8000/v1"
    assert captured["model_name"] == "local-model"
    assert captured["api_key"] == "test-secret-key"
    assert captured["context_model"] is captured["adapter"]
    assert "test-secret-key" not in result.output
