import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from ruamel.yaml import YAML

from repotrial.domain.models import Journey

FIXTURE_DIR = Path(__file__).parents[2] / "fixtures" / "app"
PROJECT_ROOT = Path(__file__).parents[3]
READINESS_TIMEOUT_S = 5.0
READINESS_POLL_INTERVAL_S = 0.02
READINESS_PROBE_TIMEOUT_S = READINESS_POLL_INTERVAL_S


@dataclass(frozen=True)
class FixtureServer:
    port: int
    process: subprocess.Popen[str]
    readiness_elapsed_s: float | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _random_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _http_request(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, str] | None = None,
    timeout_s: float = 1.0,
) -> tuple[int, bytes]:
    data = None if payload is None else json.dumps(payload).encode()
    request = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout_s) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


def _wait_for_ready(server: FixtureServer) -> float:
    started = time.monotonic()
    deadline = started + READINESS_TIMEOUT_S
    while time.monotonic() < deadline:
        if server.process.poll() is not None:
            output = (
                server.process.stdout.read()
                if server.process.stdout is not None
                else ""
            )
            pytest.fail(f"fixture exited before readiness: {output}")
        try:
            status, body = _http_request(
                f"{server.base_url}/health", timeout_s=READINESS_PROBE_TIMEOUT_S
            )
        except OSError:
            time.sleep(READINESS_POLL_INTERVAL_S)
            continue
        if status == 200 and body == b'{"status":"ok"}':
            return time.monotonic() - started
        time.sleep(READINESS_POLL_INTERVAL_S)
    pytest.fail("fixture did not become ready before the bounded deadline")


def _reap(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def _wait_for_failure(process: subprocess.Popen[str]) -> tuple[int, str]:
    return_code = process.wait(timeout=3)
    output = process.stdout.read() if process.stdout is not None else ""
    return return_code, output


@contextmanager
def _running_fixture(
    tmp_path: Path,
    *,
    startup_delay_s: str | None = None,
    proc_status: str | None = None,
) -> Iterator[FixtureServer]:
    environment = os.environ.copy()
    environment.update(
        {
            "APP_REQUIRED_TOKEN": "contract-token",
            "REPOTRIAL_FIXTURE_DATA_PATH": str(tmp_path / "items.json"),
            "REPOTRIAL_FIXTURE_TMP_PATH": str(tmp_path / "repotrial.tmp"),
            "REPOTRIAL_FIXTURE_PROC_STATUS": str(tmp_path / "status"),
        }
    )
    if startup_delay_s is not None:
        environment["STARTUP_DELAY_S"] = startup_delay_s
    else:
        environment.pop("STARTUP_DELAY_S", None)
    if proc_status is not None:
        (tmp_path / "status").write_text(proc_status, encoding="utf-8")
    port = _random_local_port()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app:app",
            "--app-dir",
            str(FIXTURE_DIR),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    server = FixtureServer(port=port, process=process)
    try:
        readiness_elapsed_s = _wait_for_ready(server)
        yield FixtureServer(
            port=port,
            process=process,
            readiness_elapsed_s=readiness_elapsed_s,
        )
    finally:
        _reap(process)


def _start_process(
    tmp_path: Path, *, token: str | None, delay: str | None = None
) -> subprocess.Popen[str]:
    environment = os.environ.copy()
    if token is None:
        environment.pop("APP_REQUIRED_TOKEN", None)
    else:
        environment["APP_REQUIRED_TOKEN"] = token
    environment.update(
        {
            "REPOTRIAL_FIXTURE_DATA_PATH": str(tmp_path / "items.json"),
            "REPOTRIAL_FIXTURE_TMP_PATH": str(tmp_path / "repotrial.tmp"),
            "REPOTRIAL_FIXTURE_PROC_STATUS": str(tmp_path / "status"),
        }
    )
    if delay is not None:
        environment["STARTUP_DELAY_S"] = delay
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app:app",
            "--app-dir",
            str(FIXTURE_DIR),
            "--host",
            "127.0.0.1",
            "--port",
            str(_random_local_port()),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def test_fixture_requires_token_and_rejects_invalid_startup_delays(
    tmp_path: Path,
) -> None:
    process = _start_process(tmp_path, token=None)
    try:
        return_code, output = _wait_for_failure(process)
        assert return_code != 0
        assert "APP_REQUIRED_TOKEN is required" in output
    finally:
        _reap(process)

    for invalid_delay in ("-0.01", "not-a-number", "nan", "inf"):
        process = _start_process(tmp_path, token="contract-token", delay=invalid_delay)
        try:
            return_code, output = _wait_for_failure(process)
            assert return_code != 0
            assert "STARTUP_DELAY_S must be a finite non-negative number" in output
        finally:
            _reap(process)


def test_fixture_health_crud_files_and_accessible_ui(tmp_path: Path) -> None:
    with _running_fixture(tmp_path) as server:
        status, body = _http_request(f"{server.base_url}/health")
        assert (status, body) == (200, b'{"status":"ok"}')

        status, body = _http_request(
            f"{server.base_url}/items", method="POST", payload={"name": ""}
        )
        assert status == 422
        assert _http_request(f"{server.base_url}/items")[1] == b"[]"

        status, body = _http_request(
            f"{server.base_url}/items", method="POST", payload={"name": "first item"}
        )
        assert (status, json.loads(body)) == (200, {"id": 1, "name": "first item"})
        status, body = _http_request(
            f"{server.base_url}/items", method="POST", payload={"name": "second item"}
        )
        assert (status, json.loads(body)) == (200, {"id": 2, "name": "second item"})
        assert json.loads((tmp_path / "items.json").read_text(encoding="utf-8")) == [
            {"id": 1, "name": "first item"},
            {"id": 2, "name": "second item"},
        ]
        assert (tmp_path / "repotrial.tmp").read_text(encoding="utf-8") == "created"

        status, body = _http_request(f"{server.base_url}/items")
        assert (status, json.loads(body)) == (
            200,
            [{"id": 1, "name": "first item"}, {"id": 2, "name": "second item"}],
        )
        status, body = _http_request(f"{server.base_url}/")
        page = body.decode()
        assert status == 200
        assert '<label for="item-name">Item name</label>' in page
        assert 'aria-label="Create item"' in page
        assert "first item" in page and "second item" in page
        assert 'aria-label="Delete first item"' in page
        assert 'aria-label="Delete second item"' in page

        status, body = _http_request(f"{server.base_url}/items/1", method="DELETE")
        assert (status, body) == (204, b"")
        assert json.loads(_http_request(f"{server.base_url}/items")[1]) == [
            {"id": 2, "name": "second item"}
        ]
        _assert_cap_net_raw(server, tmp_path / "status")


def test_fixture_applies_non_negative_startup_delay(tmp_path: Path) -> None:
    with _running_fixture(tmp_path / "zero-delay") as zero_delay_server:
        assert _http_request(f"{zero_delay_server.base_url}/health")[0] == 200
        zero_delay_readiness_s = zero_delay_server.readiness_elapsed_s
    with _running_fixture(
        tmp_path / "delayed", startup_delay_s="0.5"
    ) as delayed_server:
        assert _http_request(f"{delayed_server.base_url}/health")[0] == 200
        delayed_readiness_s = delayed_server.readiness_elapsed_s

    assert zero_delay_readiness_s is not None
    assert delayed_readiness_s is not None
    assert delayed_readiness_s >= zero_delay_readiness_s + 0.25


def test_readiness_probe_preserves_startup_delay_with_controlled_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ControlledClock:
        def __init__(self) -> None:
            self.elapsed_s = 0.0

        def monotonic(self) -> float:
            return self.elapsed_s

        def sleep(self, seconds: float) -> None:
            self.elapsed_s += seconds

    class RunningProcess:
        def poll(self) -> None:
            return None

    clock = ControlledClock()
    ready_at_s_by_port = {41001: 0.0, 41002: 0.5}

    def controlled_probe(url: str, *, timeout_s: float = 1.0) -> tuple[int, bytes]:
        clock.elapsed_s += timeout_s
        port = int(url.rsplit(":", maxsplit=1)[1].split("/", maxsplit=1)[0])
        if clock.elapsed_s >= ready_at_s_by_port[port]:
            return 200, b'{"status":"ok"}'
        raise OSError("fixture is not ready")

    def readiness_elapsed_s(port: int) -> float:
        clock.elapsed_s = 0.0
        return _wait_for_ready(FixtureServer(port=port, process=RunningProcess()))

    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    monkeypatch.setattr(sys.modules[__name__], "_http_request", controlled_probe)

    zero_delay_readiness_s = readiness_elapsed_s(41001)
    delayed_readiness_s = readiness_elapsed_s(41002)

    assert delayed_readiness_s >= zero_delay_readiness_s + 0.25


def _assert_cap_net_raw(server: FixtureServer, status_path: Path) -> None:
    cases = [
        (None, False),
        ("Name:\tfixture\nCapEff:\t0000000000002000\n", True),
        ("Name:\tfixture\nCapEff:\t0000000000000000\n", False),
        ("Name:\tfixture\nCapEff:\t-1\n", False),
        ("Name:\tfixture\nCapEff:\tnot-hex\n", False),
    ]
    for status_data, expected in cases:
        if status_data is None:
            status_path.unlink(missing_ok=True)
        else:
            status_path.write_text(status_data, encoding="utf-8")
        status, body = _http_request(f"{server.base_url}/debug/cap-net-raw")
        assert (status, json.loads(body)) == (200, {"cap_net_raw": expected})


def test_fixture_journey_asset_is_valid_current_domain_data() -> None:
    payload = json.loads(
        (FIXTURE_DIR / "repotrial.journeys.json").read_text(encoding="utf-8")
    )

    journeys = [Journey.model_validate(item) for item in payload]

    assert [journey.journey_id for journey in journeys] == ["health", "items"]


def test_compose_passes_invoker_controls_without_hardcoded_values() -> None:
    compose = YAML(typ="safe", pure=True).load(
        StringIO((FIXTURE_DIR / "compose.yml").read_text(encoding="utf-8"))
    )

    environment = compose["services"]["app"]["environment"]

    assert environment["APP_REQUIRED_TOKEN"] == "${APP_REQUIRED_TOKEN-}"
    assert environment["STARTUP_DELAY_S"] == "${STARTUP_DELAY_S:-0}"
