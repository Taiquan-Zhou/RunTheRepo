from importlib import import_module
from types import ModuleType

import pytest


def _metrics() -> ModuleType:
    return import_module("repotrial.eval.metrics")


@pytest.mark.parametrize(
    ("name", "samples", "expected"),
    [
        ("boot_recovery_rate", [True, False, True, True], 0.75),
        ("journey_success_rate", [True, True, False, True, False], 0.6),
        ("hardening_acceptance_precision", [True, False, True], 2 / 3),
        ("unnecessary_privilege_removal_recall", [False, True, True, False], 0.5),
        ("cleanup_success_rate", [True, True, False], 2 / 3),
        ("replay_consistency", [True, False, True, True], 0.75),
    ],
)
def test_metric_definitions_use_every_observed_denominator_item(
    name: str, samples: list[bool], expected: float
) -> None:
    metric = getattr(_metrics(), name)

    assert metric(samples) == pytest.approx(expected)


@pytest.mark.parametrize(
    "name",
    [
        "boot_recovery_rate",
        "journey_success_rate",
        "hardening_acceptance_precision",
        "unnecessary_privilege_removal_recall",
        "cleanup_success_rate",
        "replay_consistency",
    ],
)
def test_zero_denominator_is_unavailable_not_zero(name: str) -> None:
    metric = getattr(_metrics(), name)

    assert metric([]) is None


@pytest.mark.parametrize(
    "name",
    [
        "boot_recovery_rate",
        "journey_success_rate",
        "hardening_acceptance_precision",
        "unnecessary_privilege_removal_recall",
        "cleanup_success_rate",
        "replay_consistency",
    ],
)
def test_missing_or_unobserved_samples_cannot_become_favorable(name: str) -> None:
    metric = getattr(_metrics(), name)

    assert metric(None) is None
    assert metric([True, None]) is None
