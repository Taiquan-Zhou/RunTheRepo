"""RED tests for per-service runtime image binding."""

import json

import pytest

from repotrial.trial.image_template import (
    ImageTemplateError,
    parse_runtime_compose_services,
    resolve_runtime_image_bindings,
)

_IMAGE_A = "sha256:" + "a" * 64
_IMAGE_B = "sha256:" + "b" * 64


def test_resolves_each_compose_service_to_one_exact_inspect_id_and_alias() -> None:
    config = json.dumps(
        {
            "services": {
                "web": {"image": "docker.io/library/web:latest"},
                "worker": {"image": "docker.io/library/worker@" + "sha256:" + "1" * 64},
            }
        }
    )

    services = parse_runtime_compose_services(config)
    plan = resolve_runtime_image_bindings(
        services,
        {
            "docker.io/library/web:latest": _IMAGE_A,
            "docker.io/library/worker@" + "sha256:" + "1" * 64: _IMAGE_B,
        },
        inventory_sha256="c" * 64,
    )

    assert [binding.service for binding in plan.bindings] == ["web", "worker"]
    assert [binding.image_id for binding in plan.bindings] == [_IMAGE_A, _IMAGE_B]
    assert len({binding.alias for binding in plan.bindings}) == 2
    assert plan.image_ids == (_IMAGE_A, _IMAGE_B)


def test_runtime_service_parser_rejects_noncanonical_service_name() -> None:
    config = '{"services":{"web":{"image":"docker.io/library/web:latest"},"WEB":{"image":"docker.io/library/web:latest"}}}'
    with pytest.raises(ImageTemplateError):
        parse_runtime_compose_services(config)


def test_runtime_service_parser_rejects_missing_image() -> None:
    config = '{"services":{"web":{"build":"."}}}'
    with pytest.raises(ImageTemplateError):
        parse_runtime_compose_services(config)
