from collections.abc import Sequence

type ObservedSamples = Sequence[bool | None] | None


def boot_recovery_rate(samples: ObservedSamples) -> float | None:
    """Recovered recoverable startup failures / recoverable startup failures."""
    return _observed_rate(samples)


def journey_success_rate(samples: ObservedSamples) -> float | None:
    """Completed PASS journeys / planned journeys."""
    return _observed_rate(samples)


def hardening_acceptance_precision(samples: ObservedSamples) -> float | None:
    """Ground-truth-preserving KEEP mutations / all KEEP mutations."""
    return _observed_rate(samples)


def unnecessary_privilege_removal_recall(samples: ObservedSamples) -> float | None:
    """Ground-truth redundant privileges removed by KEEP / all redundant ones."""
    return _observed_rate(samples)


def cleanup_success_rate(samples: ObservedSamples) -> float | None:
    """Correctly destroyed runners / all started runners."""
    return _observed_rate(samples)


def replay_consistency(samples: ObservedSamples) -> float | None:
    """Comparable fixtures with identical verdict projections / all comparable."""
    return _observed_rate(samples)


def _observed_rate(samples: ObservedSamples) -> float | None:
    if samples is None or not samples or any(sample is None for sample in samples):
        return None
    return sum(sample is True for sample in samples) / len(samples)
