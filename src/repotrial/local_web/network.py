"""WSL-local proxy bootstrap for the trusted console process."""

from __future__ import annotations

import asyncio
import ipaddress
import multiprocessing
import os
import re
import socket
import struct
import subprocess
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

_PROXY_ENV_NAMES = frozenset({"http_proxy", "https_proxy", "all_proxy"})
_PROXY_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
_PROXY_TIMEOUT_SECONDS = 2.0
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
)
_NO_PROXY_TOKENS = ("localhost", "127.0.0.1", "::1")


@dataclass(frozen=True)
class WindowsProxyConfig:
    enabled: bool
    server: str


def bootstrap_wsl_proxy(
    environ: MutableMapping[str, str],
    *,
    is_wsl: bool | None = None,
    read_windows_proxy: Callable[[], WindowsProxyConfig | None] | None = None,
    read_gateway: Callable[[], str | None] | None = None,
    probe: Callable[[str], bool] | None = None,
) -> bool:
    """Apply a verified Windows HTTP proxy to this process, only when needed."""

    if _has_explicit_proxy(environ):
        return False
    if not (_is_wsl() if is_wsl is None else is_wsl):
        return False

    config = (read_windows_proxy or read_windows_proxy_settings)()
    if config is None or not config.enabled:
        return False
    proxy_url = parse_windows_proxy_server(config.server)
    if proxy_url is None:
        return False

    candidates = [proxy_url]
    parsed = urlsplit(proxy_url)
    if parsed.hostname is not None and parsed.hostname.casefold() in _LOOPBACK_HOSTS:
        gateway = (read_gateway or read_wsl_default_gateway)()
        gateway_url = _replace_proxy_host(proxy_url, gateway)
        if gateway_url is not None and gateway_url not in candidates:
            candidates.append(gateway_url)

    check = probe or probe_https_connect
    for candidate in candidates:
        if check(candidate):
            _apply_proxy_environment(environ, candidate)
            return True
    return False


def parse_windows_proxy_server(value: str) -> str | None:
    """Normalize only explicit unauthenticated HTTP(S) proxy values."""

    raw = value.strip()
    if not raw:
        return None
    if "=" in raw:
        values: dict[str, str] = {}
        for part in raw.split(";"):
            name, separator, candidate = part.partition("=")
            if not separator:
                return None
            values[name.strip().casefold()] = candidate.strip()
        http_value = values.get("http")
        https_value = values.get("https")
        if http_value and https_value:
            http_proxy = parse_windows_proxy_server(http_value)
            https_proxy = parse_windows_proxy_server(https_value)
            if http_proxy is None or https_proxy is None or http_proxy != https_proxy:
                return None
        raw = http_value or https_value or ""
    if not raw or raw.casefold().startswith(("pac:", "socks", "direct")):
        return None
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not hostname
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    try:
        host = _format_host(hostname)
    except ValueError:
        return None
    return urlunsplit((parsed.scheme.casefold(), f"{host}:{port}", "", "", ""))


def read_windows_proxy_settings() -> WindowsProxyConfig | None:
    enabled_output = _run_reg_query("ProxyEnable")
    enabled_match = re.search(
        rb"ProxyEnable\s+REG_DWORD\s+0x([0-9a-f]+)", enabled_output, re.IGNORECASE
    )
    if enabled_match is None:
        return None
    enabled = int(enabled_match.group(1), 16) == 1
    if not enabled:
        return WindowsProxyConfig(False, "")
    server_output = _run_reg_query("ProxyServer")
    server_match = re.search(
        rb"ProxyServer\s+REG_SZ\s+(.+?)\s*$",
        server_output,
        re.IGNORECASE | re.MULTILINE,
    )
    if server_match is None:
        return None
    return WindowsProxyConfig(
        True, server_match.group(1).decode("utf-8", "replace").strip()
    )


def read_wsl_default_gateway() -> str | None:
    route_path = Path("/proc/net/route")
    try:
        for line in route_path.read_text(encoding="ascii").splitlines()[1:]:
            fields = line.split()
            if len(fields) < 4 or fields[1] != "00000000":
                continue
            if int(fields[3], 16) & 2 == 0:
                continue
            gateway = int(fields[2], 16)
            return socket.inet_ntoa(struct.pack("<I", gateway))
    except (OSError, ValueError):
        pass
    try:
        result = subprocess.run(
            ("ip", "route", "show", "default"),
            capture_output=True,
            timeout=_PROXY_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(rb"\bdefault\s+via\s+([^\s]+)", result.stdout)
    if match is None:
        return None
    gateway_text = match.group(1).decode("ascii", "ignore")
    try:
        ipaddress.ip_address(gateway_text)
    except ValueError:
        return None
    return gateway_text


def probe_https_connect(proxy_url: str) -> bool:
    """Probe through a short-lived child so DNS cannot outlive the deadline."""
    context = multiprocessing.get_context()
    read_conn, write_conn = context.Pipe(duplex=False)
    process = context.Process(
        target=_probe_https_connect_worker,
        args=(proxy_url, write_conn),
    )
    process.daemon = True
    try:
        process.start()
    except (OSError, RuntimeError):
        read_conn.close()
        write_conn.close()
        return False
    write_conn.close()
    process.join(_PROXY_TIMEOUT_SECONDS)
    if process.is_alive():
        process.terminate()
        process.join(_PROXY_TIMEOUT_SECONDS)
    if process.is_alive():
        process.kill()
        process.join(_PROXY_TIMEOUT_SECONDS)
    try:
        if process.is_alive():
            return False
        try:
            return bool(read_conn.recv()) if read_conn.poll() else False
        except EOFError:
            return False
    finally:
        read_conn.close()
        if not process.is_alive():
            process.close()


def _probe_https_connect_worker(proxy_url: str, connection: Connection) -> None:
    try:
        result = asyncio.run(_probe_https_connect(proxy_url))
    except (OSError, RuntimeError, httpx.HTTPError, TimeoutError):
        result = False
    try:
        connection.send(result)
    finally:
        connection.close()


async def _probe_https_connect(proxy_url: str) -> bool:
    try:
        async with asyncio.timeout(_PROXY_TIMEOUT_SECONDS):
            timeout = httpx.Timeout(
                connect=_PROXY_TIMEOUT_SECONDS,
                read=_PROXY_TIMEOUT_SECONDS,
                write=_PROXY_TIMEOUT_SECONDS,
                pool=_PROXY_TIMEOUT_SECONDS,
            )
            async with httpx.AsyncClient(
                proxy=proxy_url,
                trust_env=False,
                follow_redirects=False,
                timeout=timeout,
                headers={"Accept-Encoding": "identity"},
            ) as client:
                response = await client.head("https://github.com")
                return 200 <= response.status_code < 500 and response.status_code != 407
    except (httpx.HTTPError, TimeoutError):
        return False


def _run_reg_query(value_name: str) -> bytes:
    try:
        result = subprocess.run(
            ("reg.exe", "query", _PROXY_KEY, "/v", value_name),
            capture_output=True,
            timeout=_PROXY_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return b""
    return result.stdout if result.returncode == 0 else b""


def _replace_proxy_host(proxy_url: str, gateway: str | None) -> str | None:
    if gateway is None:
        return None
    try:
        ipaddress.ip_address(gateway)
        parsed = urlsplit(proxy_url)
        port = parsed.port
        if port is None:
            return None
        host = _format_host(gateway)
    except (ValueError, TypeError):
        return None
    return urlunsplit((parsed.scheme, f"{host}:{port}", "", "", ""))


def _format_host(hostname: str) -> str:
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        if (
            len(hostname) > 253
            or not re.fullmatch(r"[A-Za-z0-9.-]+", hostname)
            or hostname.startswith(".")
            or hostname.endswith(".")
        ):
            raise
        return hostname.casefold()
    return f"[{ip}]" if ip.version == 6 else str(ip)


def _has_explicit_proxy(environ: Mapping[str, str]) -> bool:
    return any(name.casefold() in _PROXY_ENV_NAMES for name in environ)


def _is_wsl() -> bool:
    if os.name != "posix":
        return False
    for path in (Path("/proc/version"), Path("/proc/sys/kernel/osrelease")):
        try:
            if (
                "microsoft"
                in path.read_text(encoding="utf-8", errors="ignore").casefold()
            ):
                return True
        except OSError:
            continue
    return False


def _apply_proxy_environment(environ: MutableMapping[str, str], proxy_url: str) -> None:
    for key in _PROXY_ENV_KEYS:
        environ[key] = proxy_url
    existing: list[str] = []
    for key in ("NO_PROXY", "no_proxy"):
        existing.extend(token.strip() for token in environ.get(key, "").split(","))
    tokens: list[str] = []
    for token in (*existing, *_NO_PROXY_TOKENS):
        if token and token.casefold() not in {item.casefold() for item in tokens}:
            tokens.append(token)
    no_proxy = ",".join(tokens)
    environ["NO_PROXY"] = no_proxy
    environ["no_proxy"] = no_proxy
