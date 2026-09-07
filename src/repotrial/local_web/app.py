"""FastAPI composition root for the trusted loopback console."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from repotrial.doctor import DoctorReport, render_json, run_doctor
from repotrial.intake.github import RepoIntakeError, parse_github_url
from repotrial.local_web.runner import (
    CliRequest,
    CliRunner,
    Report,
    ReportKind,
    RunnerBusyError,
)


class Runner(Protocol):
    @property
    def active(self) -> bool: ...

    async def submit(self, request: CliRequest) -> str: ...

    async def status(self, job_id: str) -> dict[str, object]: ...

    async def report(self, job_id: str, kind: ReportKind) -> Report | None: ...

    async def shutdown(self) -> None: ...


class Doctor(Protocol):
    def __call__(self) -> DoctorReport: ...


class JobInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: str = Field(min_length=1, max_length=2048)
    commit_sha: str = Field(min_length=40, max_length=40)
    container_port: int = Field(ge=1, le=65535)
    compose_path: str | None = Field(default=None, max_length=256)
    model_endpoint: str | None = Field(default=None, max_length=2048)
    model_name: str | None = Field(default=None, max_length=256)

    @field_validator("url")
    @classmethod
    def github_url(cls, value: str) -> str:
        try:
            canonical = parse_github_url(value).url
        except RepoIntakeError:
            raise ValueError(
                "URL must be a GitHub HTTPS owner/repository URL"
            ) from None
        return canonical

    @field_validator("commit_sha")
    @classmethod
    def full_lower_sha(cls, value: str) -> str:
        if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("commit_sha must be a full lowercase SHA")
        return value

    @field_validator("compose_path")
    @classmethod
    def safe_compose_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = Path(value)
        if (
            not value
            or "\x00" in value
            or path.is_absolute()
            or "\\" in value
            or any(part == ".." for part in path.parts)
        ):
            raise ValueError("compose_path must be a safe relative path")
        return value

    @model_validator(mode="after")
    def paired_model_settings(self) -> JobInput:
        if (self.model_endpoint is None) != (self.model_name is None):
            raise ValueError("model_endpoint and model_name must be supplied together")
        if self.model_endpoint is not None:
            parsed = urlparse(self.model_endpoint)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username
            ):
                raise ValueError(
                    "model_endpoint must be an HTTP(S) URL without credentials"
                )
        return self


def create_app(
    workspace: Path,
    *,
    runner: Runner | None = None,
    doctor: Doctor | None = None,
) -> FastAPI:
    actual_runner = runner or CliRunner(workspace)
    actual_doctor = doctor or run_doctor
    csrf_token = secrets.token_urlsafe(32)
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    admission_lock = asyncio.Lock()
    doctor_running = False

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await actual_runner.shutdown()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def local_guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        host = request.headers.get("host", "")
        if not _loopback_host(host):
            return JSONResponse({"detail": "loopback host required"}, status_code=400)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if not _same_origin(request):
                return JSONResponse({"detail": "origin rejected"}, status_code=403)
            token = request.headers.get("x-csrf-token", "")
            if not hmac.compare_digest(token, csrf_token):
                return JSONResponse({"detail": "csrf token rejected"}, status_code=403)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="index.html.j2",
            context={"csrf_token": csrf_token},
        )

    @app.post("/api/doctor")
    async def doctor_route() -> Response:
        nonlocal doctor_running
        async with admission_lock:
            if actual_runner.active or doctor_running:
                return JSONResponse(
                    {"detail": "doctor is available only while idle"}, status_code=409
                )
            doctor_running = True
        try:
            report = await _run_doctor(actual_doctor)
        finally:
            doctor_running = False
        return Response(content=render_json(report), media_type="application/json")

    @app.post("/api/jobs", status_code=202)
    async def submit(payload: JobInput) -> Response:
        request = CliRequest(**payload.model_dump())
        async with admission_lock:
            if doctor_running:
                return JSONResponse(
                    {"detail": "doctor is currently running"}, status_code=409
                )
            try:
                job_id = await actual_runner.submit(request)
            except RunnerBusyError:
                return JSONResponse(
                    {"detail": "a trial is already running"}, status_code=409
                )
        return JSONResponse({"job_id": job_id}, status_code=202)

    @app.get("/api/jobs/{job_id}")
    async def status(job_id: str) -> Response:
        return JSONResponse(await actual_runner.status(job_id))

    @app.get("/api/jobs/{job_id}/report/{kind}")
    async def report(job_id: str, kind: str) -> Response:
        if kind not in {"json", "html"}:
            return JSONResponse({"detail": "report kind not found"}, status_code=404)
        report_kind = cast(ReportKind, kind)
        result = await actual_runner.report(job_id, report_kind)
        if result is None:
            return JSONResponse({"detail": "report not available"}, status_code=404)
        headers = (
            {"Content-Security-Policy": "sandbox; default-src 'none'"}
            if report_kind == "html"
            else None
        )
        return Response(
            content=result.body, media_type=result.media_type, headers=headers
        )

    return app


async def _run_doctor(doctor: Doctor) -> DoctorReport:
    import asyncio

    return await asyncio.to_thread(doctor)


def _loopback_host(host: str) -> bool:
    if host.startswith("["):
        end = host.find("]")
        if end < 0 or (host[end + 1 :] and not host[end + 1 :].startswith(":")):
            return False
        port = host[end + 2 :] if host[end + 1 :].startswith(":") else ""
        hostname = host[1:end]
    else:
        if host.count(":") == 1:
            hostname, _, port = host.partition(":")
        else:
            hostname, port = host, ""
    if port and (not port.isdigit() or not 1 <= int(port) <= 65535):
        return False
    hostname = hostname.lower()
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True
    parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if not _loopback_host(parsed.netloc):
        return False
    request_hostname = request.url.hostname
    if request_hostname is None or parsed.scheme != request.url.scheme:
        return False
    if parsed.hostname.lower() != request_hostname.lower():
        return False
    default_port = 443 if parsed.scheme == "https" else 80
    try:
        origin_port = parsed.port or default_port
        request_port = request.url.port or default_port
    except ValueError:
        return False
    return origin_port == request_port
