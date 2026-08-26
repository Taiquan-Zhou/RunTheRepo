from collections.abc import Mapping
from pathlib import Path

from .base import ExecResult, NetworkLogResult, SandboxProvider


class FakeSandboxProvider(SandboxProvider):
    def __init__(
        self,
        scripts: Mapping[tuple[str, ...], ExecResult] | None = None,
        ports: Mapping[int, int] | None = None,
        network_result: NetworkLogResult | None = None,
    ) -> None:
        self._scripts = dict(scripts or {})
        self._ports = dict(ports or {})
        self._network_result = network_result or NetworkLogResult(
            events=[],
            supported=False,
            unsupported_reason="network logging is not supported by FakeSandboxProvider",
        )
        self._active_sandboxes: set[str] = set()
        self._next_sandbox_number = 1
        self.calls: list[tuple[object, ...]] = []

    async def create(self, workspace: Path, name: str) -> str:
        self.calls.append(("create", workspace, name))
        sandbox_id = f"sandbox-{self._next_sandbox_number}"
        self._next_sandbox_number += 1
        self._active_sandboxes.add(sandbox_id)
        return sandbox_id

    async def exec(
        self, sandbox_id: str, argv: list[str], timeout_s: int = 60
    ) -> ExecResult:
        if not isinstance(argv, list) or not all(
            isinstance(argument, str) for argument in argv
        ):
            raise TypeError("argv must be a list of strings")
        argv_snapshot = tuple(argv)
        self.calls.append(("exec", sandbox_id, argv_snapshot, timeout_s))
        self._require_active(sandbox_id)
        try:
            return self._scripts[argv_snapshot]
        except KeyError:
            raise KeyError(f"no scripted result for argv: {argv_snapshot!r}") from None

    async def publish_port(self, sandbox_id: str, container_port: int) -> int:
        self.calls.append(("publish_port", sandbox_id, container_port))
        self._require_active(sandbox_id)
        try:
            return self._ports[container_port]
        except KeyError:
            raise KeyError(
                f"no published port configured for container port {container_port}"
            ) from None

    async def copy(self, sandbox_id: str, remote_path: str, local_path: Path) -> None:
        self.calls.append(("copy", sandbox_id, remote_path, local_path))
        self._require_active(sandbox_id)

    async def network_log(self, sandbox_id: str) -> NetworkLogResult:
        self.calls.append(("network_log", sandbox_id))
        self._require_active(sandbox_id)
        return self._network_result

    async def destroy(self, sandbox_id: str) -> None:
        self.calls.append(("destroy", sandbox_id))
        self._require_active(sandbox_id)
        self._active_sandboxes.remove(sandbox_id)

    def _require_active(self, sandbox_id: str) -> None:
        if sandbox_id not in self._active_sandboxes:
            raise RuntimeError(f"sandbox is not active: {sandbox_id}")
