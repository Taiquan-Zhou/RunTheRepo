import asyncio
import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from repotrial.domain.models import Journey, JourneyAssertion, JourneyStep
from repotrial.trial import planner as planner_module
from repotrial.trial.planner import plan_journeys


class FakeModelAdapter:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls = 0
        self.system = ""
        self.user = ""
        self.schema: type[BaseModel] | None = None

    async def structured(
        self, *, system: str, user: str, schema: type[BaseModel]
    ) -> BaseModel:
        self.calls += 1
        self.system = system
        self.user = user
        self.schema = schema
        assert "untrusted" in system.lower()
        return schema.model_validate({"journeys": self.response})


class ConstructedModelAdapter:
    def __init__(self, journeys: object) -> None:
        self.journeys = journeys

    async def structured(
        self, *, system: str, user: str, schema: type[BaseModel]
    ) -> BaseModel:
        del system, user
        return schema.model_construct(journeys=self.journeys)


class ExactProposalModelAdapter:
    def __init__(self, proposal: BaseModel) -> None:
        self.proposal = proposal

    async def structured(
        self, *, system: str, user: str, schema: type[BaseModel]
    ) -> BaseModel:
        del system, user
        assert schema is type(self.proposal)
        return self.proposal


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


def _journey_with_json_body(body: object) -> dict[str, object]:
    journey = _http_journey()
    journey["steps"] = [_http_step()]
    step = journey["steps"][0]
    assert isinstance(step, dict)
    params = step["params"]
    assert isinstance(params, dict)
    params["json"] = body
    return journey


def _nested_json(depth: int) -> object:
    value: object = "leaf"
    for _ in range(depth):
        value = [value]
    return value


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


def test_readme_images_and_encoded_command_text_do_not_block_model_fallback(
    tmp_path: Path,
) -> None:
    model = FakeModelAdapter([_http_journey("/from-model")])

    journeys = _plan(
        tmp_path,
        "![Screenshot](/docs/screenshot.png) ![Badge](/badge.svg) "
        "[cmd](/bin/sh%20-c%20id) [curl](/curl%20https%3Aexample.test)",
        model,
    )

    assert [journey.steps[0].params["path"] for journey in journeys] == ["/from-model"]
    assert model.calls == 1


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


def test_declaration_swap_to_symlink_fails_closed_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declaration = tmp_path / "repotrial.journeys.json"
    declaration.write_text(json.dumps({"journeys": [_http_journey("/inside")]}))
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"journeys": [_http_journey("/outside")]}))
    original_lstat = Path.lstat
    swapped = False

    def swap_after_lstat(path: Path) -> object:
        nonlocal swapped
        metadata = original_lstat(path)
        if path == declaration and not swapped:
            swapped = True
            declaration.unlink()
            declaration.symlink_to(outside)
        return metadata

    monkeypatch.setattr(Path, "lstat", swap_after_lstat)

    with pytest.raises(ValueError, match="invalid declared journeys"):
        _plan(tmp_path)


def test_declaration_growth_after_stat_never_uses_unbounded_read_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declaration = tmp_path / "repotrial.journeys.json"
    declaration.write_text(json.dumps({"journeys": [_http_journey()]}))
    original_lstat = Path.lstat
    grew = False

    def grow_after_lstat(path: Path) -> object:
        nonlocal grew
        metadata = original_lstat(path)
        if path == declaration and not grew:
            grew = True
            declaration.write_bytes(b"x" * 65_537)
        return metadata

    def fail_if_full_reader_is_used(path: Path) -> bytes:
        pytest.fail(f"unbounded declaration reader used for {path}")

    monkeypatch.setattr(Path, "lstat", grow_after_lstat)
    monkeypatch.setattr(Path, "read_bytes", fail_if_full_reader_is_used)

    with pytest.raises(ValueError, match="invalid declared journeys"):
        _plan(tmp_path)


def test_deep_declaration_json_is_normalized_to_fixed_value_error(
    tmp_path: Path,
) -> None:
    body = "[" * 1_500 + "0" + "]" * 1_500
    encoded_journey = json.dumps(_journey_with_json_body(None))
    declaration = '{"journeys": [' + encoded_journey.replace("null", body, 1) + "]}"
    (tmp_path / "repotrial.journeys.json").write_text(declaration, encoding="utf-8")

    with pytest.raises(ValueError, match="invalid declared journeys"):
        _plan(tmp_path)


def test_deep_model_json_fails_closed_without_recursion_error(tmp_path: Path) -> None:
    model = FakeModelAdapter([_journey_with_json_body(_nested_json(1_500))])

    assert _plan(tmp_path, model=model) == []


def test_json_body_depth_boundary_is_exact(tmp_path: Path) -> None:
    at_limit = FakeModelAdapter([_journey_with_json_body(_nested_json(16))])
    above_limit = FakeModelAdapter([_journey_with_json_body(_nested_json(17))])

    assert _plan(tmp_path, model=at_limit)
    assert _plan(tmp_path, model=above_limit) == []


def test_json_body_node_and_container_boundaries_are_exact(tmp_path: Path) -> None:
    at_container_limit = FakeModelAdapter([_journey_with_json_body([0] * 64)])
    above_container_limit = FakeModelAdapter([_journey_with_json_body([0] * 65)])
    at_node_limit = FakeModelAdapter(
        [_journey_with_json_body([[0] * 64, [0] * 64, [0] * 64])]
    )
    above_node_limit = FakeModelAdapter(
        [_journey_with_json_body([[0] * 64, [0] * 64, [0] * 64, [0] * 64])]
    )

    assert _plan(tmp_path, model=at_container_limit)
    assert _plan(tmp_path, model=above_container_limit) == []
    assert _plan(tmp_path, model=at_node_limit)
    assert _plan(tmp_path, model=above_node_limit) == []


def test_json_body_aggregate_content_boundary_is_exact(tmp_path: Path) -> None:
    at_limit = FakeModelAdapter([_journey_with_json_body(["x" * 4_096] * 4)])
    above_limit = FakeModelAdapter([_journey_with_json_body(["x" * 4_096] * 4 + ["x"])])

    assert _plan(tmp_path, model=at_limit)
    assert _plan(tmp_path, model=above_limit) == []


def test_model_schema_has_strict_nested_journey_and_step_definitions(
    tmp_path: Path,
) -> None:
    model = FakeModelAdapter([])

    assert _plan(tmp_path, model=model) == []
    assert model.schema is not None
    schema = model.schema
    journey_items = schema.model_json_schema()["properties"]["journeys"]["items"]
    assert journey_items != {}
    with pytest.raises(ValidationError):
        schema.model_validate(
            {
                "journeys": [
                    {
                        "journey_id": 123,
                        "name": "bad",
                        "steps": {},
                        "unexpected": True,
                    }
                ]
            }
        )


def test_model_schema_exposes_transport_collection_bounds(tmp_path: Path) -> None:
    model = FakeModelAdapter([])

    assert _plan(tmp_path, model=model) == []
    assert model.schema is not None
    schema = model.schema.model_json_schema()
    definitions = schema["$defs"]
    journey = definitions[
        schema["properties"]["journeys"]["items"]["$ref"].rsplit("/", 1)[-1]
    ]
    step = definitions[
        journey["properties"]["steps"]["items"]["$ref"].rsplit("/", 1)[-1]
    ]

    assert schema["properties"]["journeys"]["maxItems"] == 5
    assert journey["properties"]["steps"]["maxItems"] == 8
    assert step["properties"]["assertions"]["maxItems"] == 64
    assert step["properties"]["params"]["maxProperties"] == 3


def test_over_limit_journeys_are_rejected_before_later_values_are_accessed(
    tmp_path: Path,
) -> None:
    class ExplodingJourney(planner_module._JourneyTransport):
        def __getattribute__(self, name: str) -> object:
            if name == "__dict__":
                raw = object.__getattribute__(self, "__dict__")
                if raw.get("explode", False):
                    raise AssertionError("over-limit journey was accessed")
            return super().__getattribute__(name)

    valid_journey = planner_module._JourneyTransport.model_construct(
        journey_id="health",
        name="Health",
        steps=[],
    )
    exploding_journey = ExplodingJourney.model_construct(
        journey_id="later",
        name="Later",
        steps=[],
    )
    object.__getattribute__(exploding_journey, "__dict__")["explode"] = True
    proposal = planner_module._JourneyProposal.model_construct(
        journeys=[valid_journey] * 5 + [exploding_journey]
    )

    assert _plan(tmp_path, model=ExactProposalModelAdapter(proposal)) == []


@pytest.mark.parametrize("over_limit", ["steps", "assertions", "params"])
def test_nested_transport_collection_limits_fail_closed(
    tmp_path: Path, over_limit: str
) -> None:
    assertion = planner_module._JourneyAssertionTransport.model_construct(
        kind="status_code",
        target="response.status",
        expected=200,
    )
    assertions: list[object] = [assertion]
    params: dict[str, object] = {"method": "GET", "path": "/"}
    if over_limit == "assertions":
        assertions = [assertion] * 65
    if over_limit == "params":
        params = {"method": "GET", "path": "/", "json": {}, "extra": True}
    step = planner_module._JourneyStepTransport.model_construct(
        step_id="request",
        tool="http",
        action="request",
        params=params,
        assertions=assertions,
    )
    steps: list[object] = [step]
    if over_limit == "steps":
        steps = [step] * 9
    journey = planner_module._JourneyTransport.model_construct(
        journey_id="health",
        name="Health",
        steps=steps,
    )
    proposal = planner_module._JourneyProposal.model_construct(journeys=[journey])

    assert _plan(tmp_path, model=ExactProposalModelAdapter(proposal)) == []


def test_model_construct_bypass_is_revalidated_before_materialization() -> None:
    model = ConstructedModelAdapter(
        [
            {
                "journey_id": "health",
                "name": "Health",
                "steps": [],
                "unexpected": True,
            }
        ]
    )

    with pytest.raises(ValidationError):
        asyncio.run(planner_module._journey_proposal_before_deadline(model, ""))


@pytest.mark.parametrize("missing", ["journey", "step", "assertion"])
def test_nested_model_construct_bypass_fails_closed(
    tmp_path: Path, missing: str
) -> None:
    assertion_values: dict[str, object] = {
        "kind": "status_code",
        "target": "response.status",
        "expected": 200,
    }
    if missing == "assertion":
        del assertion_values["expected"]
    assertion = planner_module._JourneyAssertionTransport.model_construct(
        **assertion_values
    )
    step_values: dict[str, object] = {
        "step_id": "request",
        "tool": "http",
        "action": "request",
        "params": {"method": "GET", "path": "/"},
        "assertions": [assertion],
    }
    if missing == "step":
        del step_values["step_id"]
    step = planner_module._JourneyStepTransport.model_construct(**step_values)
    journey_values: dict[str, object] = {
        "journey_id": "health",
        "name": "Health",
        "steps": [step],
    }
    if missing == "journey":
        del journey_values["journey_id"]
    journey = planner_module._JourneyTransport.model_construct(**journey_values)
    proposal = planner_module._JourneyProposal.model_construct(journeys=[journey])

    assert _plan(tmp_path, model=ExactProposalModelAdapter(proposal)) == []


@pytest.mark.parametrize("extra_on", ["proposal", "journey", "step", "assertion"])
def test_nested_model_construct_extra_fields_fail_closed(
    tmp_path: Path, extra_on: str
) -> None:
    assertion = planner_module._JourneyAssertionTransport.model_construct(
        kind="status_code",
        target="response.status",
        expected=200,
    )
    step = planner_module._JourneyStepTransport.model_construct(
        step_id="request",
        tool="http",
        action="request",
        params={"method": "GET", "path": "/"},
        assertions=[assertion],
    )
    journey = planner_module._JourneyTransport.model_construct(
        journey_id="health",
        name="Health",
        steps=[step],
    )
    proposal = planner_module._JourneyProposal.model_construct(journeys=[journey])
    {"proposal": proposal, "journey": journey, "step": step, "assertion": assertion}[
        extra_on
    ].__dict__["unexpected"] = {"tool": "shell"}

    assert _plan(tmp_path, model=ExactProposalModelAdapter(proposal)) == []


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
