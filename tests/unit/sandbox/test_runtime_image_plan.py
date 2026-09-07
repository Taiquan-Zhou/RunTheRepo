import asyncio
import time

import pytest

from repotrial.sandbox import docker_sbx
from repotrial.sandbox.base import RuntimeImageBinding, RuntimeImagePlan
from repotrial.sandbox.docker_sbx import (
    DockerSbxError,
    DockerSbxPolicy,
    DockerSbxProvider,
)

_IMAGE_ID_A = "sha256:" + "a" * 64
_IMAGE_ID_B = "sha256:" + "b" * 64
_ALIAS_A = (
    "docker.io/library/repotrial-runtime-" + "1" * 32 + "-" + "2" * 32 + ":latest"
)
_ALIAS_B = (
    "docker.io/library/repotrial-runtime-" + "3" * 32 + "-" + "4" * 32 + ":latest"
)


def _binding(
    service: str = "web",
    source_reference: str = "docker.io/library/alpine:latest",
    image_id: str = _IMAGE_ID_A,
    alias: str = _ALIAS_A,
) -> RuntimeImageBinding:
    return RuntimeImageBinding(
        service=service,
        source_reference=source_reference,
        image_id=image_id,
        alias=alias,
    )


def _plan(
    *bindings: RuntimeImageBinding, image_ids: tuple[str, ...] = (_IMAGE_ID_A,)
) -> RuntimeImagePlan:
    return RuntimeImagePlan(
        inventory_sha256="c" * 64,
        bindings=bindings,
        image_ids=image_ids,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("service", "WEB"),
        ("source_reference", " docker.io/library/alpine:latest"),
        ("image_id", "sha256:" + "a" * 63),
        ("alias", _ALIAS_A + "x"),
    ],
)
def test_runtime_image_binding_rejects_malformed_identity(
    field: str, value: str
) -> None:
    values = {
        "service": "web",
        "source_reference": "docker.io/library/alpine:latest",
        "image_id": _IMAGE_ID_A,
        "alias": _ALIAS_A,
    }
    values[field] = value

    with pytest.raises(ValueError, match=f"runtime image binding {field} is invalid"):
        _binding(**values)


@pytest.mark.parametrize(
    ("bindings", "expected_error"),
    [
        (
            (
                _binding("api", "docker.io/library/api:latest"),
                _binding("api", "docker.io/library/web:latest", alias=_ALIAS_B),
            ),
            "runtime image plan services are duplicated",
        ),
        (
            (
                _binding("api", "docker.io/library/api:latest"),
                _binding("web", alias=_ALIAS_A),
            ),
            "runtime image plan aliases are duplicated",
        ),
        ((), "runtime image plan bindings are invalid"),
    ],
)
def test_runtime_image_plan_rejects_ambiguous_bindings(
    bindings: tuple[RuntimeImageBinding, ...], expected_error: str
) -> None:
    with pytest.raises(ValueError, match=expected_error):
        _plan(*bindings)


@pytest.mark.parametrize(
    ("inventory_sha256", "image_ids", "bindings", "expected_error"),
    [
        ("g" * 64, (_IMAGE_ID_A,), (_binding(),), "inventory hash is invalid"),
        (
            "c" * 64,
            ("sha256:" + "a" * 63,),
            (_binding(),),
            "runtime image plan image IDs are invalid",
        ),
    ],
)
def test_runtime_image_plan_rejects_invalid_identity_fields(
    inventory_sha256: str,
    image_ids: tuple[str, ...],
    bindings: tuple[RuntimeImageBinding, ...],
    expected_error: str,
) -> None:
    with pytest.raises(ValueError, match=expected_error):
        RuntimeImagePlan(
            inventory_sha256=inventory_sha256,
            bindings=bindings,
            image_ids=image_ids,
        )


@pytest.mark.parametrize(
    ("references", "image_ids"),
    [
        (("docker.io/library/busybox:latest",), (_IMAGE_ID_A,)),
        (("docker.io/library/alpine:latest",), (_IMAGE_ID_B,)),
    ],
)
def test_stage_runtime_image_plan_mismatch_fails_before_spawn(
    monkeypatch: pytest.MonkeyPatch,
    references: tuple[str, ...],
    image_ids: tuple[str, ...],
) -> None:
    provider = DockerSbxProvider(
        DockerSbxPolicy(
            cpus=1,
            memory_mb=512,
            pids_limit=64,
            disk_mb=2048,
            total_duration_s=300,
        )
    )
    sandbox_id = "sandbox-plan"
    provider._sandbox_states[sandbox_id] = docker_sbx._SandboxState.ACTIVE
    provider._sandbox_deadlines[sandbox_id] = time.monotonic() + 300.0
    spawn_called = False

    async def unexpected_spawn(*args: object, **kwargs: object) -> None:
        nonlocal spawn_called
        spawn_called = True
        raise AssertionError("stage mismatch must precede subprocess creation")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected_spawn)
    plan = _plan(_binding())

    with pytest.raises(DockerSbxError, match="image_binding_mismatch"):
        asyncio.run(
            provider.stage_runtime_image_bundle(
                sandbox_id,
                references,
                image_ids,
                runtime_image_plan=plan,
            )
        )
    assert spawn_called is False
