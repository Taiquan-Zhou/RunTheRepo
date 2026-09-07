"""RED tests for per-service runtime image binding."""

import pytest

from repotrial.trial.image_template import (
    ImageTemplateError,
    parse_compose_image_output,
    parse_compose_services_output,
    resolve_runtime_image_bindings,
)

_IMAGE_A = "sha256:" + "a" * 64
_IMAGE_B = "sha256:" + "b" * 64


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (
            "real-smoke-fixture-build_web",
            "docker.io/library/real-smoke-fixture-build_web:latest",
        ),
        ("busybox:1.36.1", "docker.io/library/busybox:1.36.1"),
    ],
)
def test_compose_image_output_canonicalizes_familiar_names(
    output: str, expected: str
) -> None:
    assert parse_compose_image_output(output, service="web") == expected


def test_compose_image_output_canonicalizes_tagged_digest() -> None:
    digest = "73" * 32
    output = f"busybox:1.36.1@sha256:{digest}"
    expected = f"docker.io/library/busybox:1.36.1@sha256:{digest}"

    assert parse_compose_image_output(output, service="web") == expected


def test_resolves_each_compose_service_to_one_exact_inspect_id_and_alias() -> None:
    services = (
        ("web", "docker.io/library/web:latest"),
        ("worker", "docker.io/library/worker@" + "sha256:" + "1" * 64),
    )
    plan = resolve_runtime_image_bindings(
        services,
        {
            "docker.io/library/web:latest": _IMAGE_A,
            "docker.io/library/worker@" + "sha256:" + "1" * 64: _IMAGE_B,
        },
        inventory_sha256="c" * 64,
        image_ids=(_IMAGE_A, _IMAGE_B),
    )

    assert [binding.service for binding in plan.bindings] == ["web", "worker"]
    assert [binding.image_id for binding in plan.bindings] == [_IMAGE_A, _IMAGE_B]
    assert len({binding.alias for binding in plan.bindings}) == 2
    assert plan.image_ids == (_IMAGE_A, _IMAGE_B)


def test_runtime_service_parser_rejects_noncanonical_service_name() -> None:
    with pytest.raises(ImageTemplateError):
        parse_compose_services_output("web\nWEB\n")


def test_runtime_service_parser_rejects_missing_or_ambiguous_image() -> None:
    with pytest.raises(ImageTemplateError):
        parse_compose_image_output("", service="web")
    with pytest.raises(ImageTemplateError):
        parse_compose_image_output("one\ntwo\n", service="web")


def test_runtime_service_parser_rejects_duplicate_or_invalid_service_output() -> None:
    with pytest.raises(ImageTemplateError):
        parse_compose_services_output("web\nweb\n")
    with pytest.raises(ImageTemplateError):
        parse_compose_services_output("web\nnot valid\n")


def test_runtime_image_alias_namespace_changes_for_each_resolution() -> None:
    services = (("web", "docker.io/library/web:latest"),)
    first = resolve_runtime_image_bindings(
        services,
        {"docker.io/library/web:latest": _IMAGE_A},
        inventory_sha256="c" * 64,
        image_ids=(_IMAGE_A, _IMAGE_B),
    )
    second = resolve_runtime_image_bindings(
        services,
        {"docker.io/library/web:latest": _IMAGE_A},
        inventory_sha256="c" * 64,
        image_ids=(_IMAGE_A, _IMAGE_B),
    )
    assert first.bindings[0].alias != second.bindings[0].alias
