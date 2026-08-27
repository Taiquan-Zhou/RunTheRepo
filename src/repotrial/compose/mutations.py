"""Pure, auditable single-service Compose mutations."""

from copy import deepcopy

from repotrial.compose.risk import (
    _RESET,
    _UNSUPPORTED,
    _effective_value,
    _is_docker_socket,
    _recognized_host_bind,
)
from repotrial.domain.enums import MutationType
from repotrial.domain.models import Mutation


class MutationError(ValueError):
    """Raised when a requested mutation is invalid, inapplicable, or a no-op."""


def apply_mutation(base: dict[str, object], mutation: Mutation) -> dict[str, object]:
    """Return a deep-copied candidate with exactly one target-service mutation."""
    _validate_mutation_request(base, mutation)
    candidate = deepcopy(base)
    services = candidate["services"]
    assert isinstance(services, dict)
    target = deepcopy(services[mutation.service])
    if not isinstance(target, dict):
        raise MutationError("invalid service")
    services[mutation.service] = target

    match mutation.type:
        case MutationType.SET_NON_ROOT:
            _set_scalar(target, "user", "65532:65532", None)
        case MutationType.DROP_ALL_CAPS:
            _set_scalar(target, "cap_drop", ["ALL"], None)
        case MutationType.SET_READ_ONLY:
            _set_scalar(target, "read_only", True, False)
        case MutationType.ADD_TMPFS:
            _add_tmpfs(target)
        case MutationType.DROP_PRIVILEGED:
            _set_scalar(target, "privileged", False, False)
        case MutationType.REMOVE_DOCKER_SOCKET:
            _remove_docker_socket(target)
        case MutationType.BRIDGE_NETWORK:
            _remove_host_network(target)
        case _:
            raise MutationError("unsupported mutation")
    return candidate


def _validate_mutation_request(base: object, mutation: object) -> None:
    if not isinstance(base, dict) or not isinstance(mutation, Mutation):
        raise MutationError("invalid mutation request")
    if not isinstance(mutation.mutation_id, str) or not mutation.mutation_id:
        raise MutationError("missing mutation id")
    if not isinstance(mutation.type, MutationType):
        raise MutationError("unsupported mutation")
    if not isinstance(mutation.service, str) or not mutation.service:
        raise MutationError("invalid service")
    if mutation.params:
        raise MutationError("unsupported mutation params")
    services = base.get("services")
    if not isinstance(services, dict):
        raise MutationError("invalid services")
    if mutation.service not in services:
        raise MutationError("unknown service")
    if not isinstance(services[mutation.service], dict):
        raise MutationError("invalid service")


def _set_scalar(
    service: dict[object, object], key: str, value: object, default: object
) -> None:
    if _effective_field(service, key, default) == value:
        raise MutationError("mutation already satisfied")
    service[key] = value


def _effective_field(
    service: dict[object, object], key: str, default: object
) -> object:
    effective = _effective_value(service.get(key, default))
    if effective is _UNSUPPORTED:
        raise MutationError("unsupported compose tag")
    if effective is _RESET:
        return default
    return effective


def _add_tmpfs(service: dict[object, object]) -> None:
    current = _effective_field(service, "tmpfs", None)
    if current is None:
        service["tmpfs"] = ["/tmp"]
        return
    if isinstance(current, str):
        current = [current]
    if not isinstance(current, list) or not all(
        isinstance(item, str) for item in current
    ):
        raise MutationError("invalid tmpfs")
    if current.count("/tmp") == 1:
        raise MutationError("mutation already satisfied")
    retained = [item for item in current if item != "/tmp"]
    service["tmpfs"] = [*retained, "/tmp"]


def _remove_docker_socket(service: dict[object, object]) -> None:
    volumes = _effective_field(service, "volumes", None)
    if not isinstance(volumes, list):
        raise MutationError("invalid volumes")
    removable = [
        index
        for index, volume in enumerate(volumes)
        if (bind := _recognized_host_bind(volume)) is not None
        and _is_docker_socket(bind[0])
    ]
    if not removable:
        raise MutationError("mutation already satisfied")
    for index in reversed(removable):
        del volumes[index]


def _remove_host_network(service: dict[object, object]) -> None:
    if _effective_field(service, "network_mode", None) != "host":
        raise MutationError("mutation already satisfied")
    del service["network_mode"]
