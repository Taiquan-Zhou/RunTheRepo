from __future__ import annotations

import asyncio
import json
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TypeVar

import pytest
from pydantic import BaseModel

from eval import model_runtime_calibration as calibration
from repotrial.domain.models import Journey, JourneyAssertion, JourneyStep
from repotrial.models.base import ModelAdapter, RecoveryAction

RAW_SENTINEL = "RAW_MODEL_OUTPUT_MUST_NOT_APPEAR"
METRIC_FIELDS = {
    "schema_version",
    "event",
    "schema",
    "run",
    "runtime_version",
    "model",
    "digest",
    "status",
    "statuses",
    "requests",
    "fallback",
    "validation",
    "public_stop_reason",
    "journey_count",
    "elapsed_seconds",
    "recovery_validation",
    "journey_validation",
    "resident",
    "outer_deadline_seconds",
}
ModelT = TypeVar("ModelT", bound=BaseModel)


class FakeTracingAdapter:
    def __init__(self, purpose: str) -> None:
        self.purpose = purpose
        self.trace = calibration.RequestTrace(modes=["json_schema"], statuses=[200])

    async def structured(
        self, *, system: str, user: str, schema: type[ModelT]
    ) -> ModelT:
        del system, user, schema
        raise AssertionError("public planner must own structured model calls")


class FakeAdapterFactory:
    def __init__(self) -> None:
        self.adapters: list[FakeTracingAdapter] = []

    def __call__(self, endpoint: str, model: str) -> FakeTracingAdapter:
        assert endpoint == calibration.DEFAULT_ENDPOINT
        assert model == calibration.DEFAULT_MODEL
        adapter = FakeTracingAdapter(f"regular-{len(self.adapters) + 1}")
        self.adapters.append(adapter)
        return adapter


class FakeNativeApi:
    def __init__(
        self,
        *,
        runtime_version: str = calibration.DEFAULT_RUNTIME_VERSION,
        model_digest: str = calibration.DEFAULT_MODEL_DIGEST,
        resident_after_unload: bool = False,
    ) -> None:
        self.runtime_version = runtime_version
        self.model_digest = model_digest
        self.resident_after_unload = resident_after_unload
        self.events: list[str] = []
        self.posts: list[tuple[str, dict[str, object]]] = []

    async def get_json(self, path: str) -> calibration.NativeResponse:
        self.events.append(f"GET {path}")
        if path == "/api/version":
            data: object = {"version": self.runtime_version}
        elif path == "/api/tags":
            data = {
                "models": [
                    {
                        "name": calibration.DEFAULT_MODEL,
                        "digest": self.model_digest,
                    }
                ]
            }
        elif path == "/api/ps":
            data = {
                "models": (
                    [{"name": calibration.DEFAULT_MODEL}]
                    if self.resident_after_unload
                    else []
                )
            }
        else:
            raise AssertionError(f"unexpected GET path: {path}")
        return calibration.NativeResponse(status_code=200, data=data)

    async def post_json(
        self, path: str, payload: dict[str, object]
    ) -> calibration.NativeResponse:
        self.events.append(f"POST {path}")
        self.posts.append((path, payload))
        return calibration.NativeResponse(status_code=200, data={"done": True})


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        self.value += 0.125
        return self.value


class FakeInvalidSessionContext(AbstractContextManager[object]):
    def __init__(self, session: calibration.InvalidProbeSession) -> None:
        self.session = session

    def __enter__(self) -> calibration.InvalidProbeSession:
        return self.session

    def __exit__(self, *args: object) -> None:
        del args


class PlannerSpies:
    def __init__(
        self,
        native_api: FakeNativeApi,
        invalid_recovery: FakeTracingAdapter,
        invalid_journey: FakeTracingAdapter,
        *,
        accept_invalid: bool = False,
    ) -> None:
        self.native_api = native_api
        self.invalid_recovery = invalid_recovery
        self.invalid_journey = invalid_journey
        self.accept_invalid = accept_invalid
        self.recovery_models: list[ModelAdapter | None] = []
        self.journey_models: list[ModelAdapter | None] = []
        self.regular_journey_calls = 0

    async def propose_recovery(
        self,
        logs: dict[str, str],
        readme_excerpt: str,
        allowed_env_keys: set[str],
        repeated_error_count: int,
        model: ModelAdapter | None = None,
    ) -> RecoveryAction:
        assert logs == {"logs": calibration.RECOVERY_EVIDENCE}
        assert readme_excerpt == ""
        assert allowed_env_keys == set()
        assert repeated_error_count == 0
        self.recovery_models.append(model)
        if model is self.invalid_recovery and self.accept_invalid:
            return RecoveryAction(action="retry", params={}, reason=RAW_SENTINEL)
        return RecoveryAction(action="stop", params={}, reason="unsafe proposal")

    async def plan_journeys(
        self,
        repo_root: Path,
        readme_excerpt: str,
        model: ModelAdapter | None = None,
    ) -> list[Journey]:
        assert repo_root.is_dir()
        assert readme_excerpt == calibration.JOURNEY_EVIDENCE
        self.journey_models.append(model)
        if model is self.invalid_journey:
            return [_journey()] if self.accept_invalid else []
        self.regular_journey_calls += 1
        if self.regular_journey_calls == 4:
            assert self.native_api.events[-1] == "GET /api/ps"
        return [_journey()]


def _journey() -> Journey:
    return Journey(
        journey_id="health",
        name=RAW_SENTINEL,
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


def _dependencies(
    *,
    native_api: FakeNativeApi | None = None,
    accept_invalid: bool = False,
) -> tuple[
    calibration.CalibrationDependencies,
    FakeNativeApi,
    PlannerSpies,
]:
    api = native_api or FakeNativeApi()
    adapter_factory = FakeAdapterFactory()
    invalid_recovery = FakeTracingAdapter("invalid-recovery")
    invalid_journey = FakeTracingAdapter("invalid-journey")
    invalid_session = calibration.InvalidProbeSession(
        recovery_adapter=invalid_recovery,
        journey_adapter=invalid_journey,
        request_count=lambda: 2,
    )
    spies = PlannerSpies(
        api,
        invalid_recovery,
        invalid_journey,
        accept_invalid=accept_invalid,
    )
    dependencies = calibration.CalibrationDependencies(
        native_api=api,
        adapter_factory=adapter_factory,
        invalid_session_factory=lambda endpoint, model: FakeInvalidSessionContext(
            invalid_session
        ),
        propose_recovery=spies.propose_recovery,
        plan_journeys=spies.plan_journeys,
        monotonic=FakeClock(),
    )
    return dependencies, api, spies


def _run(
    dependencies: calibration.CalibrationDependencies,
) -> list[calibration.MetricRecord]:
    metrics: list[calibration.MetricRecord] = []
    asyncio.run(
        calibration.run_calibration(
            calibration.CalibrationConfig(), dependencies, metrics.append
        )
    )
    return metrics


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://user:secret@127.0.0.1:11434/v1",
        "http://127.0.0.1:11434/v1?token=secret",
        "http://127.0.0.1:11434/v1#fragment",
    ],
)
def test_calibration_config_rejects_endpoint_secrets_and_ambiguous_urls(
    endpoint: str,
) -> None:
    with pytest.raises(ValueError, match="endpoint"):
        calibration.CalibrationConfig(endpoint=endpoint)


def test_calibration_config_rejects_unsafe_model_identifier() -> None:
    with pytest.raises(ValueError, match="model"):
        calibration.CalibrationConfig(model="qwen3:4b?token=secret")


def test_cli_config_failure_does_not_echo_credentials(
    capsys: pytest.CaptureFixture[str],
) -> None:
    credential = "RAW_CREDENTIAL_SENTINEL"

    exit_code = calibration.main(
        ["--endpoint", f"http://user:{credential}@127.0.0.1:11434/v1"]
    )

    assert exit_code == 2
    output = capsys.readouterr().out
    assert credential not in output
    assert json.loads(output) == {
        "schema_version": 1,
        "event": "failure",
        "validation": "config_invalid",
    }


def test_recovery_calibration_evidence_reaches_public_model_path() -> None:
    class SchemaAdapter:
        def __init__(self) -> None:
            self.calls = 0

        async def structured(
            self, *, system: str, user: str, schema: type[ModelT]
        ) -> ModelT:
            del system, user
            self.calls += 1
            return schema.model_validate(
                {"action": "retry", "params": {}, "reason": "bounded retry"}
            )

    adapter = SchemaAdapter()

    result = asyncio.run(
        calibration.propose_recovery(
            {"logs": calibration.RECOVERY_EVIDENCE}, "", set(), 0, adapter
        )
    )

    assert adapter.calls == 1
    assert result.action == "retry"


def test_complete_calibration_uses_public_planners_and_emits_only_sanitized_metrics() -> (
    None
):
    dependencies, native_api, spies = _dependencies()

    metrics = _run(dependencies)

    assert [metric["event"] for metric in metrics] == [
        "metadata",
        "prewarm",
        "warm_recovery",
        "warm_recovery",
        "warm_recovery",
        "warm_journey",
        "warm_journey",
        "warm_journey",
        "intentionally_invalid",
        "unload",
        "residency",
        "cold_journey",
        "summary",
    ]
    assert len(spies.recovery_models) == 4
    assert len(spies.journey_models) == 5
    assert all(set(metric) <= METRIC_FIELDS for metric in metrics)
    assert RAW_SENTINEL not in "\n".join(
        json.dumps(metric, sort_keys=True) for metric in metrics
    )
    recovery_metrics = [
        metric for metric in metrics if metric["event"] == "warm_recovery"
    ]
    assert all(
        metric["validation"] == "schema_valid_policy_rejected"
        and metric["public_stop_reason"] == "unsafe proposal"
        for metric in recovery_metrics
    )
    invalid = next(
        metric for metric in metrics if metric["event"] == "intentionally_invalid"
    )
    assert invalid == {
        "schema_version": 1,
        "event": "intentionally_invalid",
        "schema": "RecoveryAction+Journey",
        "statuses": [200, 200],
        "requests": 2,
        "fallback": False,
        "recovery_validation": "fail_closed",
        "journey_validation": "fail_closed",
    }
    assert native_api.posts == [
        (
            "/api/generate",
            {
                "model": calibration.DEFAULT_MODEL,
                "prompt": "",
                "stream": False,
                "keep_alive": -1,
            },
        ),
        (
            "/api/generate",
            {
                "model": calibration.DEFAULT_MODEL,
                "stream": False,
                "keep_alive": 0,
            },
        ),
    ]


@pytest.mark.parametrize(
    ("runtime_version", "digest"),
    [
        ("0.33.1", calibration.DEFAULT_MODEL_DIGEST),
        (calibration.DEFAULT_RUNTIME_VERSION, "f" * 64),
    ],
)
def test_metadata_mismatch_fails_before_prewarm_or_model_calls(
    runtime_version: str, digest: str
) -> None:
    native_api = FakeNativeApi(runtime_version=runtime_version, model_digest=digest)
    dependencies, _, spies = _dependencies(native_api=native_api)

    with pytest.raises(calibration.CalibrationError, match="metadata_mismatch"):
        _run(dependencies)

    assert native_api.posts == []
    assert spies.recovery_models == []
    assert spies.journey_models == []


def test_resident_model_after_unload_blocks_cold_planning() -> None:
    native_api = FakeNativeApi(resident_after_unload=True)
    dependencies, _, spies = _dependencies(native_api=native_api)

    with pytest.raises(calibration.CalibrationError, match="model_still_resident"):
        _run(dependencies)

    assert spies.regular_journey_calls == 3


def test_invalid_responses_must_both_fail_closed() -> None:
    dependencies, _, _ = _dependencies(accept_invalid=True)

    with pytest.raises(calibration.CalibrationError, match="invalid_response_accepted"):
        _run(dependencies)
