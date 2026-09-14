from __future__ import annotations

from repotrial.local_web.network import (
    WindowsProxyConfig,
    bootstrap_wsl_proxy,
    parse_windows_proxy_server,
)


def test_explicit_proxy_environment_wins_without_reading_windows_settings() -> None:
    environment = {"aLl_PrOxY": "http://explicit.example:8080"}
    called = False

    def read_proxy() -> WindowsProxyConfig:
        nonlocal called
        called = True
        raise AssertionError("Windows settings must not be read")

    assert (
        bootstrap_wsl_proxy(
            environment,
            is_wsl=True,
            read_windows_proxy=read_proxy,
            read_gateway=lambda: "172.24.64.1",
            probe=lambda _: True,
        )
        is False
    )
    assert called is False
    assert environment == {"aLl_PrOxY": "http://explicit.example:8080"}


def test_non_wsl_does_not_bootstrap_proxy() -> None:
    environment: dict[str, str] = {}
    assert (
        bootstrap_wsl_proxy(
            environment,
            is_wsl=False,
            read_windows_proxy=lambda: WindowsProxyConfig(True, "127.0.0.1:7890"),
            read_gateway=lambda: "172.24.64.1",
            probe=lambda _: True,
        )
        is False
    )
    assert environment == {}


def test_localhost_probe_falls_back_to_dynamic_wsl_gateway() -> None:
    environment = {"NO_PROXY": "internal.example"}
    probes: list[str] = []

    def probe(proxy_url: str) -> bool:
        probes.append(proxy_url)
        return proxy_url == "http://172.24.64.1:7890"

    assert (
        bootstrap_wsl_proxy(
            environment,
            is_wsl=True,
            read_windows_proxy=lambda: WindowsProxyConfig(True, "127.0.0.1:7890"),
            read_gateway=lambda: "172.24.64.1",
            probe=probe,
        )
        is True
    )
    assert probes == ["http://127.0.0.1:7890", "http://172.24.64.1:7890"]
    assert environment["HTTP_PROXY"] == "http://172.24.64.1:7890"
    assert environment["HTTPS_PROXY"] == "http://172.24.64.1:7890"
    assert environment["http_proxy"] == "http://172.24.64.1:7890"
    assert environment["https_proxy"] == "http://172.24.64.1:7890"
    assert "ALL_PROXY" not in environment
    assert set(environment["NO_PROXY"].split(",")) >= {
        "internal.example",
        "localhost",
        "127.0.0.1",
        "::1",
    }


def test_failed_probe_does_not_mutate_environment() -> None:
    environment = {"NO_PROXY": "localhost"}
    before = dict(environment)
    assert (
        bootstrap_wsl_proxy(
            environment,
            is_wsl=True,
            read_windows_proxy=lambda: WindowsProxyConfig(True, "127.0.0.1:7890"),
            read_gateway=lambda: "172.24.64.1",
            probe=lambda _: False,
        )
        is False
    )
    assert environment == before


def test_windows_proxy_parser_rejects_credentials_pac_and_socks() -> None:
    assert parse_windows_proxy_server("127.0.0.1:7890") == "http://127.0.0.1:7890"
    assert (
        parse_windows_proxy_server("proxy.example:8080") == "http://proxy.example:8080"
    )
    assert (
        parse_windows_proxy_server("http=127.0.0.1:7890;https=127.0.0.1:7891") is None
    )
    assert parse_windows_proxy_server("https://user:secret@127.0.0.1:7890") is None
    assert parse_windows_proxy_server("socks=127.0.0.1:1080") is None
    assert parse_windows_proxy_server("http://proxy.example/pac.pac") is None


def test_windows_registry_reader_queries_only_proxy_values(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    from repotrial.local_web import network

    calls: list[tuple[str, ...]] = []

    def run(argv, **kwargs):
        calls.append(tuple(argv))
        value = argv[-1]
        output = (
            b"ProxyEnable    REG_DWORD    0x1\r\n"
            if value == "ProxyEnable"
            else b"ProxyServer    REG_SZ    127.0.0.1:7890\r\n"
        )
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr(network.subprocess, "run", run)
    result = network.read_windows_proxy_settings()

    assert result == network.WindowsProxyConfig(True, "127.0.0.1:7890")
    assert all(call[:3] == ("reg.exe", "query", network._PROXY_KEY) for call in calls)
    assert [call[-1] for call in calls] == ["ProxyEnable", "ProxyServer"]


import asyncio
import subprocess
from types import SimpleNamespace
from typing import ClassVar

import httpx
import pytest


def test_bootstrap_rejects_missing_disabled_and_unparseable_windows_settings() -> None:
    for config in (
        None,
        WindowsProxyConfig(False, "127.0.0.1:7890"),
        WindowsProxyConfig(True, ""),
    ):
        environment: dict[str, str] = {}
        assert (
            bootstrap_wsl_proxy(
                environment,
                is_wsl=True,
                read_windows_proxy=lambda config=config: config,
                probe=lambda _: pytest.fail("invalid settings must not be probed"),
            )
            is False
        )
        assert environment == {}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", None),
        ("http=127.0.0.1:7890;https=127.0.0.1:7891", None),
        ("https=proxy.example:8443", "http://proxy.example:8443"),
        ("http://[::1]:7890", "http://[::1]:7890"),
        ("http=127.0.0.1:7890;broken", None),
        ("http://proxy.example:0", None),
        ("http://proxy.example:65536", None),
        ("ftp://proxy.example:8080", None),
        ("http://bad_host:8080", None),
        ("http://[broken:8080", None),
    ],
)
def test_windows_proxy_parser_rejects_unsafe_or_malformed_values(
    value: str, expected: str | None
) -> None:
    assert parse_windows_proxy_server(value) == expected


def test_bootstrap_rejects_unusable_gateway_without_mutating_environment() -> None:
    environment: dict[str, str] = {}
    probes: list[str] = []
    assert (
        bootstrap_wsl_proxy(
            environment,
            is_wsl=True,
            read_windows_proxy=lambda: WindowsProxyConfig(True, "127.0.0.1:7890"),
            read_gateway=lambda: "not-an-ip",
            probe=probes.append,
        )
        is False
    )
    assert probes == ["http://127.0.0.1:7890"]
    assert environment == {}


def test_windows_registry_reader_returns_disabled_without_querying_server(
    monkeypatch,
) -> None:
    from repotrial.local_web import network

    calls: list[str] = []

    def run(argv, **kwargs):
        calls.append(argv[-1])
        return SimpleNamespace(
            returncode=0, stdout=b"ProxyEnable    REG_DWORD    0x0\r\n"
        )

    monkeypatch.setattr(network.subprocess, "run", run)
    assert network.read_windows_proxy_settings() == network.WindowsProxyConfig(
        False, ""
    )
    assert calls == ["ProxyEnable"]


@pytest.mark.parametrize(
    "result",
    [
        SimpleNamespace(returncode=1, stdout=b""),
        OSError("reg.exe unavailable"),
        SimpleNamespace(returncode=0, stdout=b"not a registry value"),
    ],
)
def test_windows_registry_reader_handles_command_or_shape_failures(
    monkeypatch, result
) -> None:
    from repotrial.local_web import network

    def run(argv, **kwargs):
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(network.subprocess, "run", run)
    assert network.read_windows_proxy_settings() is None


def test_windows_registry_reader_requires_server_when_enabled(monkeypatch) -> None:
    from repotrial.local_web import network

    outputs = {
        "ProxyEnable": b"ProxyEnable REG_DWORD 0x1\r\n",
        "ProxyServer": b"other REG_SZ 127.0.0.1:7890\r\n",
    }

    def run(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout=outputs[argv[-1]])

    monkeypatch.setattr(network.subprocess, "run", run)
    assert network.read_windows_proxy_settings() is None


def test_default_gateway_reads_proc_route(monkeypatch) -> None:
    from repotrial.local_web import network

    monkeypatch.setattr(
        network.Path,
        "read_text",
        lambda self, **kwargs: (
            "Iface Destination Gateway Flags\neth0 00000000 0102A8C0 0003\n"
        ),
    )
    assert network.read_wsl_default_gateway() == "192.168.2.1"


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        (b"default via 10.0.0.1 dev eth0\n", "10.0.0.1"),
        (b"default via not-an-ip dev eth0\n", None),
        (b"no default route\n", None),
    ],
)
def test_default_gateway_falls_back_to_ip_route(monkeypatch, stdout, expected) -> None:
    from repotrial.local_web import network

    monkeypatch.setattr(
        network.Path,
        "read_text",
        lambda self, **kwargs: (_ for _ in ()).throw(OSError()),
    )
    monkeypatch.setattr(
        network.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=stdout),
    )
    assert network.read_wsl_default_gateway() == expected


@pytest.mark.parametrize(
    "failure",
    [OSError("ip unavailable"), subprocess.SubprocessError("timeout")],
)
def test_default_gateway_handles_route_command_failure(monkeypatch, failure) -> None:
    from repotrial.local_web import network

    monkeypatch.setattr(
        network.Path,
        "read_text",
        lambda self, **kwargs: (_ for _ in ()).throw(OSError()),
    )

    def run(*args, **kwargs):
        raise failure

    monkeypatch.setattr(network.subprocess, "run", run)
    assert network.read_wsl_default_gateway() is None


@pytest.mark.parametrize(
    ("status", "expected"),
    [(200, True), (499, True), (407, False), (500, False)],
)
def test_probe_accepts_only_non_proxy_auth_http_responses(
    monkeypatch, status: int, expected: bool
) -> None:
    from repotrial.local_web import network

    class FakeClient:
        options: ClassVar[dict[str, object]] = {}

        def __init__(self, **kwargs):
            FakeClient.options = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def head(self, url: str):
            assert url == "https://github.com"
            return SimpleNamespace(status_code=status)

    monkeypatch.setattr(network.httpx, "AsyncClient", FakeClient)
    assert (
        asyncio.run(network._probe_https_connect("http://proxy.example:8080"))
        is expected
    )
    assert FakeClient.options["trust_env"] is False
    assert FakeClient.options["follow_redirects"] is False
    assert FakeClient.options["headers"] == {"Accept-Encoding": "identity"}


@pytest.mark.parametrize("failure", [httpx.ConnectError("failed"), TimeoutError()])
def test_probe_converts_network_failures_to_false(monkeypatch, failure) -> None:
    from repotrial.local_web import network

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def head(self, url: str):
            raise failure

    monkeypatch.setattr(network.httpx, "AsyncClient", FakeClient)
    assert network.probe_https_connect("http://proxy.example:8080") is False


def test_is_wsl_detection_handles_platform_and_proc_variants(monkeypatch) -> None:
    from repotrial.local_web import network

    monkeypatch.setattr(network.os, "name", "nt")
    assert network._is_wsl() is False

    monkeypatch.setattr(network.os, "name", "posix")
    monkeypatch.setattr(network.Path, "read_text", lambda self, **kwargs: "Linux")
    assert network._is_wsl() is False

    monkeypatch.setattr(network.Path, "read_text", lambda self, **kwargs: "Microsoft")
    assert network._is_wsl() is True


def test_is_wsl_ignores_unreadable_proc_files(monkeypatch) -> None:
    from repotrial.local_web import network

    monkeypatch.setattr(network.os, "name", "posix")
    monkeypatch.setattr(
        network.Path,
        "read_text",
        lambda self, **kwargs: (_ for _ in ()).throw(OSError()),
    )
    assert network._is_wsl() is False


def test_bootstrap_accepts_direct_proxy_without_gateway_lookup() -> None:
    environment: dict[str, str] = {}
    assert (
        bootstrap_wsl_proxy(
            environment,
            is_wsl=True,
            read_windows_proxy=lambda: WindowsProxyConfig(True, "proxy.example:8080"),
            read_gateway=lambda: pytest.fail("direct proxy must not need a gateway"),
            probe=lambda url: url == "http://proxy.example:8080",
        )
        is True
    )
    assert environment["HTTP_PROXY"] == "http://proxy.example:8080"


def test_default_gateway_skips_non_default_or_unusable_proc_routes(monkeypatch) -> None:
    from repotrial.local_web import network

    monkeypatch.setattr(
        network.Path,
        "read_text",
        lambda self, **kwargs: (
            "Iface Destination Gateway Flags\n"
            "eth0 00000000 0102A8C0 0000\n"
            "eth0 not-default 0102A8C0 0003\n"
        ),
    )
    monkeypatch.setattr(
        network.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=b"no default route\n"),
    )
    assert network.read_wsl_default_gateway() is None


def test_replace_proxy_host_rejects_missing_gateway_or_port() -> None:
    from repotrial.local_web import network

    assert network._replace_proxy_host("http://proxy.example:8080", None) is None
    assert network._replace_proxy_host("http://proxy.example", "10.0.0.1") is None


@pytest.mark.parametrize(
    "value",
    [
        "http=127.0.0.1:7890;https=127.0.0.2:7890",
        "http=127.0.0.1:7890;https=proxy.example:7890",
        "http=http://127.0.0.1:7890;https=https://127.0.0.1:7890",
    ],
)
def test_windows_proxy_parser_rejects_conflicting_protocol_mappings(value: str) -> None:
    assert parse_windows_proxy_server(value) is None


import time


def test_probe_enforces_hard_process_deadline(monkeypatch) -> None:
    from repotrial.local_web import network

    async def slow_probe(proxy_url: str) -> bool:
        await asyncio.sleep(1)

    monkeypatch.setattr(network, "_probe_https_connect", slow_probe)
    monkeypatch.setattr(network, "_PROXY_TIMEOUT_SECONDS", 0.05)
    started = time.monotonic()
    assert network.probe_https_connect("http://proxy.example:8080") is False
    assert time.monotonic() - started < 0.8


def test_probe_worker_reports_result_and_closes_connection(monkeypatch) -> None:
    from repotrial.local_web import network

    class FakeConnection:
        sent: ClassVar[list[bool]] = []
        closed = False

        def send(self, value: bool) -> None:
            self.sent.append(value)

        def close(self) -> None:
            self.closed = True

    async def successful_probe(proxy_url: str) -> bool:
        return True

    monkeypatch.setattr(network, "_probe_https_connect", successful_probe)
    connection = FakeConnection()
    network._probe_https_connect_worker("http://proxy.example:8080", connection)
    assert connection.sent == [True]
    assert connection.closed is True
