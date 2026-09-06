import base64
import binascii
import hashlib
from collections.abc import Sequence

from repotrial.trial import compatibility as _compatibility

type FixtureMaterialization = tuple[str, str, str, bytes]
type FixtureMaterializationParse = FixtureMaterialization | int | None


def parse_fixture_materialization(
    argv: Sequence[str],
) -> FixtureMaterializationParse:
    """Parse only the code-owned guest materialization adapter contract."""
    if len(argv) != 8 or any(not isinstance(item, str) for item in argv):
        return None
    if tuple(argv[:3]) != ("sh", "-eu", "-c"):
        return None
    command_name = argv[4]
    if command_name == "repotrial-compatibility-overlay":
        adapter_script = _compatibility._COMPATIBILITY_ADAPTER_SCRIPT
        path_valid = argv[5] == _compatibility._COMPATIBILITY_RELATIVE_PATH
    elif command_name == "repotrial-experiment-overlay":
        adapter_script = _compatibility._EXPERIMENT_ADAPTER_SCRIPT
        path_valid = argv[5] == _compatibility._EXPERIMENT_RELATIVE_PATH
    elif command_name == "repotrial-accepted-compose":
        adapter_script = _compatibility._ACCEPTED_COMPOSE_ADAPTER_SCRIPT
        path_valid = (
            _compatibility._ACCEPTED_COMPOSE_PATTERN.fullmatch(argv[5]) is not None
        )
    else:
        return None
    if argv[3] != adapter_script:
        return None
    relative_path, expected_sha256, encoded_payload = argv[5:]
    if not path_valid:
        return 22
    if len(encoded_payload) > _compatibility._MAX_COMPATIBILITY_PAYLOAD_BYTES:
        return 25
    try:
        payload = base64.b64decode(encoded_payload, validate=True)
    except (UnicodeError, binascii.Error, ValueError):
        return 26
    if (
        _compatibility._SHA256_PATTERN.fullmatch(expected_sha256) is None
        or hashlib.sha256(payload).hexdigest() != expected_sha256
    ):
        return 31
    return command_name, relative_path, expected_sha256, payload
