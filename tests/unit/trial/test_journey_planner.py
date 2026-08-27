import asyncio
import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from repotrial.domain.models import Journey, JourneyAssertion, JourneyStep
from repotrial.trial.planner import plan_journeys


class FakeModelAdapter:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls = 0
        self.system = ""
        self.user = ""

    async def structured(
        self, *, system: str, user: str, schema: type[BaseModel]
    ) -> BaseModel:
        self.calls += 1
        self.system = system
        self.user = user
        assert "untrusted" in system.lower()
        return schema.model_validate({"journeys": self.response})


class TimeoutModelAdapter:
    def __init__(self) -> None:
        self.cancelled = asyncio.Event()

    async def structured(
        self, *, system: str, user: str, schema: type[BaseModel]
    ) -> BaseModel:
        del system, user, schema
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("model wait unexpectedly completed")


class CancellationTrackingModelAdapter:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def structured(
        self, *, system: str, user: str, schema: type[BaseModel]
    ) -> BaseModel:
        del system, user, schema
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("model wait unexpectedly completed")


def _plan(
    repo_root: Path,
    readme_excerpt: str = "",
    model: FakeModelAdapter | None = None,
) -> list[Journey]:
    return asyncio.run(plan_journeys(repo_root, readme_excerpt, model))


def _http_step(path: str = "/health") -> dict[str, object]:
    return {
        "step_id": "request-health",
        "tool": "http",
        "action": "request",
        "params": {"method": "GET", "path": path},
        "assertions": [
            {"kind": "status_code", "target": "response.status", "expected": 200}
        ],
    }


def _http_journey(path: str = "/health") -> dict[str, object]:
    return {"journey_id": "health", "name": "Health", "steps": [_http_step(path)]}


def test_declared_journeys_are_authoritative_and_never_call_model(
    tmp_path: Path,
) -> None:
    declared = _http_journey()
    (tmp_path / "repotrial.journeys.json").write_text(
        json.dumps({"journeys": [declared]}), encoding="utf-8"
    )
    model = FakeModelAdapter([_http_journey("/model")])

    journeys = _plan(tmp_path, "[README](/readme)", model)

    assert journeys == [
        Journey(
            journey_id="health",
            name="Health",
            steps=[
                JourneyStep(
                    step_id="request-health",
                    tool="http",
                    action="request",
                    params={"method": "GET", "path": "/health"},
                    assertions=[
                        JourneyAssertion(
                            kind="status_code", target="response.status", expected=200
                        )
                    ],
                )
            ],
        )
    ]
    assert model.calls == 0


def test_invalid_present_declaration_fails_closed_without_model_fallback(
    tmp_path: Path,
) -> None:
    (tmp_path / "repotrial.journeys.json").write_text(
        '{"journeys": [], "unexpected": true}', encoding="utf-8"
    )
    model = FakeModelAdapter([_http_journey()])

    with pytest.raises(ValueError, match="invalid declared journeys"):
        _plan(tmp_path, model=model)

    assert model.calls == 0


def test_readme_links_are_deterministic_deduplicated_and_preempt_model(
    tmp_path: Path,
) -> None:
    model = FakeModelAdapter([_http_journey("/model")])

    journeys = _plan(
        tmp_path,
        "[Home](/) [Docs](/docs) [Again](/docs) "
        "[External](https://example.test/a) [Traversal](/a/../secret) "
        "[Fragment](/docs#one) [Command](curl%20https://example.test)",
        model,
    )

    assert [journey.steps[0].params["path"] for journey in journeys] == ["/", "/docs"]
    assert [journey.steps[0].params["method"] for journey in journeys] == ["GET", "GET"]
    assert all(
        journey.steps[0].assertions
        == [
            JourneyAssertion(kind="status_code", target="response.status", expected=200)
        ]
        for journey in journeys
    )
    assert model.calls == 0


def test_valid_model_output_is_materialized_only_when_other_sources_are_empty(
    tmp_path: Path,
) -> None:
    model = FakeModelAdapter([_http_journey("/from-model")])

    journeys = _plan(tmp_path, "no safe markdown links", model)

    assert journeys[0].steps[0].params["path"] == "/from-model"
    assert model.calls == 1
    assert "no safe markdown links" in model.user


@pytest.mark.parametrize(
    "step",
    [
        {
            "step_id": "js",
            "tool": "browser",
            "action": "evaluate",
            "params": {"javascript": "alert(1)"},
            "assertions": [],
        },
        {
            "step_id": "shell",
            "tool": "shell",
            "action": "run",
            "params": {"argv": ["id"]},
            "assertions": [],
        },
        {
            "step_id": "unknown",
            "tool": "http",
            "action": "fetch_everything",
            "params": {"method": "GET", "path": "/"},
            "assertions": [],
        },
    ],
)
def test_model_shell_javascript_and_unknown_actions_are_rejected(
    tmp_path: Path, step: dict[str, object]
) -> None:
    model = FakeModelAdapter(
        [{"journey_id": "unsafe", "name": "Unsafe", "steps": [step]}]
    )

    assert _plan(tmp_path, model=model) == []
    assert model.calls == 1


def test_planner_rejects_output_exceeding_journey_or_step_limits(
    tmp_path: Path,
) -> None:
    too_many_journeys = [_http_journey(f"/{index}") for index in range(6)]
    too_many_steps = _http_journey()
    too_many_steps["steps"] = [_http_step(f"/{index}") for index in range(9)]

    assert _plan(tmp_path, model=FakeModelAdapter(too_many_journeys)) == []
    assert _plan(tmp_path, model=FakeModelAdapter([too_many_steps])) == []


@pytest.mark.parametrize(
    "journey",
    [
        {
            "journey_id": "bad-http",
            "name": "Bad HTTP",
            "steps": [
                {
                    "step_id": "bad",
                    "tool": "http",
                    "action": "request",
                    "params": {"method": "PUT", "path": "https://elsewhere.test"},
                    "assertions": [],
                }
            ],
        },
        {
            "journey_id": "bad-browser",
            "name": "Bad browser",
            "steps": [
                {
                    "step_id": "bad",
                    "tool": "browser",
                    "action": "goto",
                    "params": {"path": "/ok", "extra": "no"},
                    "assertions": [],
                }
            ],
        },
        {
            "journey_id": "bad-assertion",
            "name": "Bad assertion",
            "steps": [
                {
                    "step_id": "bad",
                    "tool": "http",
                    "action": "request",
                    "params": {"method": "GET", "path": "/"},
                    "assertions": [
                        {"kind": "status_code", "target": "wrong", "expected": 200}
                    ],
                }
            ],
        },
        {
            "journey_id": "extra",
            "name": "Extra",
            "steps": [_http_step()],
            "extra": "not allowed",
        },
    ],
)
def test_model_output_with_invalid_params_assertions_or_schema_is_rejected(
    tmp_path: Path, journey: dict[str, object]
) -> None:
    assert _plan(tmp_path, model=FakeModelAdapter([journey])) == []


def test_browser_goto_query_is_rejected_to_match_runner_policy(tmp_path: Path) -> None:
    model = FakeModelAdapter(
        [
            {
                "journey_id": "browser-query",
                "name": "Browser query",
                "steps": [
                    {
                        "step_id": "goto",
                        "tool": "browser",
                        "action": "goto",
                        "params": {"path": "/login?next=/admin"},
                        "assertions": [],
                    }
                ],
            }
        ]
    )

    assert _plan(tmp_path, model=model) == []


def test_oversized_readme_is_rejected_before_model_invocation(tmp_path: Path) -> None:
    model = FakeModelAdapter([_http_journey()])

    assert _plan(tmp_path, "x" * 4_097, model) == []
    assert model.calls == 0


def test_oversized_declaration_is_rejected_before_model_fallback(
    tmp_path: Path,
) -> None:
    (tmp_path / "repotrial.journeys.json").write_bytes(b" " * 65_537)
    model = FakeModelAdapter([_http_journey()])

    with pytest.raises(ValueError, match="invalid declared journeys"):
        _plan(tmp_path, model=model)

    assert model.calls == 0


def test_model_timeout_fails_closed_and_cancels_inner_task(tmp_path: Path) -> None:
    model = TimeoutModelAdapter()

    async def exercise() -> None:
        assert await plan_journeys(tmp_path, "", model) == []
        await asyncio.wait_for(model.cancelled.wait(), timeout=0.1)

    asyncio.run(exercise())


def test_caller_cancellation_cancels_and_observes_inner_model_task(
    tmp_path: Path,
) -> None:
    model = CancellationTrackingModelAdapter()

    async def exercise() -> None:
        planner = asyncio.create_task(plan_journeys(tmp_path, "", model))
        await asyncio.wait_for(model.started.wait(), timeout=0.1)
        planner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await planner
        await asyncio.wait_for(model.cancelled.wait(), timeout=0.1)

    asyncio.run(exercise())


def test_none_model_returns_no_journeys_when_no_safe_source_exists(
    tmp_path: Path,
) -> None:
    assert _plan(tmp_path, "README text only") == []
