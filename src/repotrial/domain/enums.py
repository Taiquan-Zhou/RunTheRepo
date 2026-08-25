from enum import StrEnum


class Verdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNSUPPORTED = "unsupported"


class ExperimentVerdict(StrEnum):
    KEEP = "keep"
    ROLLBACK = "rollback"
    STOP = "stop"


class MutationType(StrEnum):
    SET_NON_ROOT = "set_non_root"
    DROP_ALL_CAPS = "drop_all_caps"
    SET_READ_ONLY = "set_read_only"
    ADD_TMPFS = "add_tmpfs"
    DROP_PRIVILEGED = "drop_privileged"
    REMOVE_DOCKER_SOCKET = "remove_docker_socket"
    BRIDGE_NETWORK = "bridge_network"
