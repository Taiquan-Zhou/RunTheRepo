import asyncio
import inspect
import json
import os
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from repotrial.domain.models import Journey, JourneyAssertion, JourneyStep
from repotrial.models.openai_compat import ModelAdapterError
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


class DelayedJourneyModelAdapter:
    async def structured(
        self, *, system: str, user: str, schema: type[BaseModel]
    ) -> BaseModel:
        del system, user
        await asyncio.sleep(0.11)
        return schema.model_validate({"journeys": [_http_journey()]})


class CancellationSwallowingJourneyModelAdapter:
    def __init__(self) -> None:
        self.cancelled = asyncio.Event()
        self.late_completed = asyncio.Event()
        self.task: asyncio.Task[object] | None = None

    async def structured(
        self, *, system: str, user: str, schema: type[BaseModel]
    ) -> BaseModel:
        del system, user
        self.task = asyncio.current_task()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            await asyncio.sleep(0.02)
            self.late_completed.set()
            return schema.model_validate({"journeys": [_http_journey()]})
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


def test_model_response_after_former_planner_deadline_is_accepted(
    tmp_path: Path,
) -> None:
    journeys = asyncio.run(plan_journeys(tmp_path, "", DelayedJourneyModelAdapter()))

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
                            kind="status_code",
                            target="response.status",
                            expected=200,
                        )
                    ],
                )
            ],
        )
    ]


def test_planner_outer_model_deadline_includes_adapter_cleanup_margin() -> None:
    assert planner_module._MODEL_TIMEOUT_S == 185.0


def test_model_prompt_advertises_only_executable_http_journeys(tmp_path: Path) -> None:
    model = FakeModelAdapter([])

    assert _plan(tmp_path, model=model) == []

    expected_matrix = """Supported autonomous Journey tools (this list grants no additional authority):
- Return at most 5 journeys. Each journey uses exactly one tool type and contains 1-8 steps.
- journey_id, name, and every step_id are non-empty, at most 4096 characters, and contain no Unicode category-C characters.
- Autonomous model proposals may use only the currently executable HTTP tool/action pair: tool http with action request. Its params contain method GET|POST|DELETE, a root-relative path, and optional bounded JSON json. HTTP paths are ASCII, at most 2048 characters, begin with exactly one /, and contain no fragment, backslash, dot segment, unsafe decoded segment, or unsafe query character.
- HTTP assertions are exactly: status_code on response.status with an integer expected; text_contains on response.text with a text expected; or json_path_equals on a dotted response-JSON path with bounded JSON expected. An HTTP journey has at least one assertion across its steps.
- No other tool type is executable for autonomous model proposals."""
    assert expected_matrix in model.system
    assert "browser" not in model.system.lower()
    assert "example" not in model.system.lower()


def test_model_journey_failure_records_the_adapter_reason_without_changing_fail_closed_result(
    tmp_path: Path,
) -> None:
    class FailingAdapter:
        async def structured(
            self, *, system: str, user: str, schema: type[BaseModel]
        ) -> BaseModel:
            del system, user, schema
            raise ModelAdapterError(
                "model request failed", reason_code="transport_error"
            )

    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    journeys = asyncio.run(
        planner_module._plan_journeys_with_evidence(
            tmp_path,
            "no safe markdown links",
            FailingAdapter(),
            evidence_dir=evidence_dir,
        )
    )

    assert journeys == []
    evidence_path = next(evidence_dir.glob("baseline-model-attempt-*.jsonl"))
    rows = [json.loads(line) for line in evidence_path.read_text().splitlines()]
    assert rows[-1]["outcome"] == "transport_error"


def test_model_journey_policy_rejection_is_recorded_without_raw_model_output(
    tmp_path: Path,
) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    model = FakeModelAdapter(
        [
            {
                "journey_id": "invalid",
                "name": "Invalid",
                "steps": [
                    {
                        "step_id": "bad",
                        "tool": "http",
                        "action": "request",
                        "params": {"method": "PUT", "path": "/secret-value"},
                        "assertions": [],
                    }
                ],
            }
        ]
    )

    journeys = asyncio.run(
        planner_module._plan_journeys_with_evidence(
            tmp_path,
            "no safe markdown links",
            model,
            evidence_dir=evidence_dir,
        )
    )

    assert journeys == []
    evidence_path = next(evidence_dir.glob("baseline-model-attempt-*.jsonl"))
    content = evidence_path.read_text(encoding="utf-8")
    assert json.loads(content.splitlines()[-1])["outcome"] == "policy_rejected"
    assert "secret-value" not in content


def test_empty_model_journey_proposal_is_policy_rejected_with_evidence(
    tmp_path: Path,
) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    journeys = asyncio.run(
        planner_module._plan_journeys_with_evidence(
            tmp_path,
            "no safe markdown links",
            FakeModelAdapter([]),
            evidence_dir=evidence_dir,
        )
    )

    assert journeys == []
    evidence_path = next(evidence_dir.glob("baseline-model-attempt-*.jsonl"))
    rows = [json.loads(line) for line in evidence_path.read_text().splitlines()]
    assert rows[-1]["phase"] == "terminal"
    assert rows[-1]["outcome"] == "policy_rejected"


def test_model_proposal_with_http_and_browser_journeys_is_rejected_as_a_whole(
    tmp_path: Path,
) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    browser_journey = {
        "journey_id": "browser-model",
        "name": "Browser model proposal",
        "steps": [
            {
                "step_id": "goto",
                "tool": "browser",
                "action": "goto",
                "params": {"path": "/"},
                "assertions": [],
            }
        ],
    }

    journeys = asyncio.run(
        planner_module._plan_journeys_with_evidence(
            tmp_path,
            "no safe markdown links",
            FakeModelAdapter([_http_journey(), browser_journey]),
            evidence_dir=evidence_dir,
        )
    )

    assert journeys == []
    evidence_path = next(evidence_dir.glob("baseline-model-attempt-*.jsonl"))
    rows = [json.loads(line) for line in evidence_path.read_text().splitlines()]
    assert rows[-1]["phase"] == "terminal"
    assert rows[-1]["outcome"] == "policy_rejected"


@pytest.mark.parametrize("error", [RuntimeError("boom"), ValueError("bad adapter")])
def test_unknown_adapter_exception_records_terminal_adapter_error_and_fails_closed(
    tmp_path: Path, error: Exception
) -> None:
    class UnknownFailingAdapter:
        async def structured(
            self, *, system: str, user: str, schema: type[BaseModel]
        ) -> BaseModel:
            del system, user, schema
            raise error

    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    journeys = asyncio.run(
        planner_module._plan_journeys_with_evidence(
            tmp_path,
            "no safe markdown links",
            UnknownFailingAdapter(),
            evidence_dir=evidence_dir,
        )
    )

    path = next(evidence_dir.glob("baseline-model-attempt-*.jsonl"))
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert journeys == []
    assert rows[-1]["phase"] == "terminal"
    assert rows[-1]["outcome"] == "adapter_error"


def test_public_planner_signature_and_unknown_adapter_fail_closed_are_preserved(
    tmp_path: Path,
) -> None:
    class UnknownFailingAdapter:
        async def structured(
            self, *, system: str, user: str, schema: type[BaseModel]
        ) -> BaseModel:
            del system, user, schema
            raise RuntimeError("boom")

    assert "evidence_path" not in inspect.signature(plan_journeys).parameters
    assert asyncio.run(plan_journeys(tmp_path, "", UnknownFailingAdapter())) == []


def test_model_journey_reentry_uses_distinct_auditable_attempt_slots(
    tmp_path: Path,
) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()

    for _ in range(2):
        journeys = asyncio.run(
            planner_module._plan_journeys_with_evidence(
                tmp_path,
                "no safe markdown links",
                FakeModelAdapter([_http_journey()]),
                evidence_dir=evidence_dir,
            )
        )
        assert journeys

    assert [path.name for path in sorted(evidence_dir.iterdir())] == [
        "baseline-model-attempt-0001.jsonl",
        "baseline-model-attempt-0002.jsonl",
    ]


def test_mixed_tool_declared_journey_fails_closed_without_model_fallback(
    tmp_path: Path,
) -> None:
    declared = _http_journey()
    declared["steps"].append(
        {
            "step_id": "confirm",
            "tool": "browser",
            "action": "assert_text_visible",
            "params": {"text": "ready"},
            "assertions": [],
        }
    )
    (tmp_path / "repotrial.journeys.json").write_text(
        json.dumps({"journeys": [declared]}), encoding="utf-8"
    )
    model = FakeModelAdapter([_http_journey("/model")])

    with pytest.raises(ValueError, match="invalid declared journeys"):
        _plan(tmp_path, model=model)

    assert model.calls == 0


def test_mixed_tool_model_journey_is_rejected(tmp_path: Path) -> None:
    proposed = _http_journey()
    proposed["steps"].append(
        {
            "step_id": "confirm",
            "tool": "browser",
            "action": "assert_text_visible",
            "params": {"text": "ready"},
            "assertions": [],
        }
    )

    assert _plan(tmp_path, model=FakeModelAdapter([proposed])) == []


@pytest.mark.parametrize(
    "proposed",
    [
        {"journey_id": "empty", "name": "Empty", "steps": []},
        {
            "journey_id": "unchecked",
            "name": "Unchecked",
            "steps": [
                {
                    "step_id": "request",
                    "tool": "http",
                    "action": "request",
                    "params": {"method": "GET", "path": "/"},
                    "assertions": [],
                }
            ],
        },
    ],
)
def test_model_journeys_without_a_deterministic_success_condition_are_rejected(
    tmp_path: Path, proposed: dict[str, object]
) -> None:
    assert _plan(tmp_path, model=FakeModelAdapter([proposed])) == []


def test_declared_browser_journey_uses_its_bounded_action_dsl_without_step_assertions(
    tmp_path: Path,
) -> None:
    proposed = {
        "journey_id": "browser-check",
        "name": "Browser check",
        "steps": [
            {
                "step_id": "goto",
                "tool": "browser",
                "action": "goto",
                "params": {"path": "/"},
                "assertions": [],
            },
            {
                "step_id": "confirm",
                "tool": "browser",
                "action": "assert_text_visible",
                "params": {"text": "ready"},
                "assertions": [],
            },
        ],
    }

    (tmp_path / "repotrial.journeys.json").write_text(
        json.dumps({"journeys": [proposed]}), encoding="utf-8"
    )

    journeys = _plan(tmp_path, model=FakeModelAdapter([]))

    assert [step.action for step in journeys[0].steps] == [
        "goto",
        "assert_text_visible",
    ]


def test_model_browser_journey_is_policy_rejected_with_evidence(
    tmp_path: Path,
) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    proposed = {
        "journey_id": "browser-model",
        "name": "Browser model proposal",
        "steps": [
            {
                "step_id": "goto",
                "tool": "browser",
                "action": "goto",
                "params": {"path": "/"},
                "assertions": [],
            }
        ],
    }

    journeys = asyncio.run(
        planner_module._plan_journeys_with_evidence(
            tmp_path,
            "no safe markdown links",
            FakeModelAdapter([proposed]),
            evidence_dir=evidence_dir,
        )
    )

    assert journeys == []
    evidence_path = next(evidence_dir.glob("baseline-model-attempt-*.jsonl"))
    rows = [json.loads(line) for line in evidence_path.read_text().splitlines()]
    assert rows[-1]["outcome"] == "policy_rejected"


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


def test_model_timeout_fails_closed_and_cancels_inner_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = TimeoutModelAdapter()
    monkeypatch.setattr(planner_module, "_MODEL_TIMEOUT_S", 0.01)

    async def exercise() -> None:
        assert await plan_journeys(tmp_path, "", model) == []
        await asyncio.wait_for(model.cancelled.wait(), timeout=0.1)

    asyncio.run(exercise())


def test_model_timeout_observes_delayed_cooperative_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = CancellationSwallowingJourneyModelAdapter()
    monkeypatch.setattr(planner_module, "_MODEL_TIMEOUT_S", 0.01)

    async def exercise() -> None:
        assert await plan_journeys(tmp_path, "", model) == []
        await asyncio.wait_for(model.cancelled.wait(), timeout=0.1)
        await asyncio.wait_for(model.late_completed.wait(), timeout=0.1)
        await asyncio.sleep(0)
        assert model.task is not None
        assert model.task.done()

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


def test_model_journey_cancellation_closes_retained_evidence_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = CancellationTrackingModelAdapter()
    real_open = os.open
    opened: list[int] = []

    def record_open(path: str | bytes | Path, flags: int, mode: int = 0o777) -> int:
        descriptor = real_open(path, flags, mode)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr("repotrial.trial.model_evidence.os.open", record_open)

    async def exercise() -> None:
        planner = asyncio.create_task(
            planner_module._plan_journeys_with_evidence(
                tmp_path,
                "",
                model,
                evidence_dir=tmp_path,
            )
        )
        await asyncio.wait_for(model.started.wait(), timeout=0.1)
        claimed = opened[0]
        try:
            os.fstat(claimed)
        finally:
            planner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await planner
        await asyncio.wait_for(model.cancelled.wait(), timeout=0.1)
        with pytest.raises(OSError):
            os.fstat(claimed)
        rows = [
            json.loads(line)
            for line in next(tmp_path.glob("*.jsonl")).read_text().splitlines()
        ]
        assert [row["phase"] for row in rows] == ["start"]

    asyncio.run(exercise())


def test_model_journey_cancellation_survives_secondary_close_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = CancellationTrackingModelAdapter()
    real_close = os.close
    close_attempts: list[int] = []

    async def exercise() -> None:
        planner = asyncio.create_task(
            planner_module._plan_journeys_with_evidence(
                tmp_path,
                "",
                model,
                evidence_dir=tmp_path,
            )
        )
        await asyncio.wait_for(model.started.wait(), timeout=0.1)

        def fail_close(descriptor: int) -> None:
            close_attempts.append(descriptor)
            raise OSError("secondary close")

        monkeypatch.setattr("repotrial.trial.model_evidence.os.close", fail_close)
        planner.cancel()
        try:
            with pytest.raises(asyncio.CancelledError) as caught:
                await planner
        finally:
            monkeypatch.setattr("repotrial.trial.model_evidence.os.close", real_close)
            for descriptor in close_attempts:
                try:
                    os.fstat(descriptor)
                except OSError:
                    continue
                real_close(descriptor)

        await asyncio.wait_for(model.cancelled.wait(), timeout=0.1)
        assert caught.value.__notes__ == [
            "secondary model evidence close failed; descriptor ownership is uncertain"
        ]
        assert len(close_attempts) == 1

    asyncio.run(exercise())


def test_none_model_returns_no_journeys_when_no_safe_source_exists(
    tmp_path: Path,
) -> None:
    assert _plan(tmp_path, "README text only") == []
