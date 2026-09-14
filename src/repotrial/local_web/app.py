"""FastAPI composition root for the trusted loopback console."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from inspect import isawaitable
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from repotrial.doctor import DoctorReport, run_doctor
from repotrial.intake.github import RepoIntakeError, parse_github_url
from repotrial.local_web.model_discovery import (
    ModelDiscoveryError,
    discover_models,
    validate_api_key,
)
from repotrial.local_web.repository import (
    GithubRepositoryDiscovery,
    RepositoryInput,
    RepositoryMetadata,
)
from repotrial.local_web.runner import (
    CliRequest,
    CliRunner,
    JobNotActiveError,
    JobNotFoundError,
    Report,
    ReportKind,
    RunnerBusyError,
)
from repotrial.local_web.services import (
    RequiredServices,
    render_verified_json,
    verified_ready,
    verified_report_dict,
)
from repotrial.local_web.settings import (
    ModelSettingsStore,
    ModelSettingsUpdate,
    SettingsValidationError,
    same_endpoint,
)


class Runner(Protocol):
    @property
    def active(self) -> bool: ...

    async def submit(self, request: CliRequest) -> str: ...

    async def status(self, job_id: str) -> dict[str, object]: ...

    async def report(self, job_id: str, kind: ReportKind) -> Report | None: ...
    async def stop(self, job_id: str) -> dict[str, object]: ...

    async def shutdown(self) -> None: ...


class Doctor(Protocol):
    def __call__(self) -> DoctorReport: ...


class Services(Protocol):
    def ensure_ready(self) -> DoctorReport: ...


class RepositoryService(Protocol):
    def discover(self, payload: RepositoryInput) -> RepositoryMetadata: ...


class JobInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: str = Field(min_length=1, max_length=2048)
    commit_sha: str = Field(min_length=40, max_length=40)
    container_port: int = Field(ge=1, le=65535)
    compose_path: str | None = Field(default=None, max_length=256)

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


class ModelSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: str | None = Field(default=None, max_length=32)
    endpoint: str | None = Field(default=None, max_length=2048)
    model_name: str | None = Field(default=None, max_length=256)
    api_key: SecretStr = Field(default=SecretStr(""))


class ModelDiscoveryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    endpoint: str = Field(min_length=1, max_length=2048)
    api_key: SecretStr = Field(default=SecretStr(""))


def create_app(
    workspace: Path,
    *,
    runner: Runner | None = None,
    doctor: Doctor | None = None,
    repository: RepositoryService | None = None,
    services: Services | None = None,
    settings_store: ModelSettingsStore | None = None,
    model_discovery: Callable[
        [str, str | None], Awaitable[tuple[str, ...]] | tuple[str, ...]
    ]
    | None = None,
) -> FastAPI:
    actual_runner = runner or CliRunner(workspace)
    actual_doctor = doctor or run_doctor
    actual_repository = repository or GithubRepositoryDiscovery()
    actual_services = services or RequiredServices(doctor=actual_doctor)
    actual_settings = settings_store or ModelSettingsStore()
    actual_model_discovery = model_discovery or discover_models
    csrf_token = secrets.token_urlsafe(32)
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    admission_lock = asyncio.Lock()
    doctor_running = False
    startup_report = DoctorReport(checks=())
    startup_verified = False

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal startup_report, startup_verified
        startup_report = await asyncio.to_thread(actual_services.ensure_ready)
        startup_verified = True
        yield
        await actual_runner.shutdown()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        if any("api_key" in error.get("loc", ()) for error in exc.errors()):
            return JSONResponse(
                {"detail": "api_key is invalid"},
                status_code=422,
            )
        detail = [
            {
                "loc": error.get("loc", ()),
                "msg": error.get("msg", "invalid value"),
                "type": error.get("type", "value_error"),
            }
            for error in exc.errors()
        ]
        return JSONResponse({"detail": detail}, status_code=422)

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
            context={
                "csrf_token": csrf_token,
                "startup_report": (
                    verified_report_dict(startup_report) if startup_verified else None
                ),
            },
        )

    @app.get("/api/settings/model")
    async def model_settings_get() -> Response:
        return JSONResponse(actual_settings.load_public().as_dict())

    @app.put("/api/settings/model")
    async def model_settings_put(payload: ModelSettingsInput) -> Response:
        try:
            api_key = payload.api_key.get_secret_value()
            if len(api_key) > 4096 or "\x00" in api_key:
                raise SettingsValidationError("api_key is invalid")
            result = actual_settings.save(
                ModelSettingsUpdate(
                    provider=payload.provider,
                    endpoint=payload.endpoint,
                    model_name=payload.model_name,
                    api_key=api_key,
                )
            )
        except SettingsValidationError as error:
            return JSONResponse({"detail": str(error)}, status_code=422)
        return JSONResponse(result.as_dict())

    @app.post("/api/settings/model/discover")
    async def model_settings_discover(payload: ModelDiscoveryInput) -> Response:
        supplied_key = payload.api_key.get_secret_value()
        try:
            validate_api_key(supplied_key or None)
        except ModelDiscoveryError:
            return JSONResponse({"detail": "api_key is invalid"}, status_code=422)
        api_key = supplied_key or None
        if api_key is None:
            stored = actual_settings.load_resolved()
            if stored is not None and same_endpoint(stored.endpoint, payload.endpoint):
                api_key = stored.api_key
        try:
            result = await asyncio.to_thread(
                actual_model_discovery, payload.endpoint, api_key
            )
            models = await result if isawaitable(result) else result
        except ModelDiscoveryError as error:
            status_code = (
                422 if error.code in {"invalid_endpoint", "invalid_api_key"} else 502
            )
            return JSONResponse({"detail": str(error)}, status_code=status_code)
        return JSONResponse({"models": list(models)})

    @app.delete("/api/settings/model/key")
    async def model_settings_clear_key() -> Response:
        try:
            result = actual_settings.clear_key()
        except SettingsValidationError as error:
            return JSONResponse({"detail": str(error)}, status_code=422)
        return JSONResponse(result.as_dict())

    @app.delete("/api/settings/model")
    async def model_settings_clear() -> Response:
        try:
            result = actual_settings.clear()
        except SettingsValidationError as error:
            return JSONResponse({"detail": str(error)}, status_code=422)
        return JSONResponse(result.as_dict())

    @app.post("/api/repository")
    async def repository_route(payload: RepositoryInput) -> Response:
        metadata = await asyncio.to_thread(actual_repository.discover, payload)
        return JSONResponse(metadata.to_dict())

    @app.post("/api/doctor")
    async def doctor_route() -> Response:
        nonlocal doctor_running, startup_report, startup_verified
        async with admission_lock:
            if actual_runner.active or doctor_running:
                return JSONResponse(
                    {"detail": "doctor is available only while idle"}, status_code=409
                )
            doctor_running = True
        try:
            report = await asyncio.to_thread(actual_services.ensure_ready)
            if services is not None:
                doctor_report = await _run_doctor(actual_doctor)
                existing = {check.name for check in report.checks}
                report = DoctorReport(
                    checks=report.checks
                    + tuple(
                        check
                        for check in doctor_report.checks
                        if check.name not in existing
                    )
                )
            startup_report = report
            startup_verified = True
        finally:
            doctor_running = False
        return Response(
            content=render_verified_json(report), media_type="application/json"
        )

    @app.post("/api/jobs", status_code=202)
    async def submit(payload: JobInput) -> Response:
        async with admission_lock:
            if doctor_running:
                return JSONResponse(
                    {"detail": "doctor is currently running"}, status_code=409
                )
            if startup_verified and not verified_ready(startup_report):
                return JSONResponse(
                    {
                        "detail": "required local services are not ready; check the environment and retry"
                    },
                    status_code=409,
                )
            resolved = actual_settings.load_resolved()
            request = CliRequest(
                url=payload.url,
                commit_sha=payload.commit_sha,
                container_port=payload.container_port,
                compose_path=payload.compose_path,
                model_endpoint=resolved.endpoint if resolved is not None else None,
                model_name=resolved.model_name if resolved is not None else None,
                model_api_key=resolved.api_key if resolved is not None else None,
            )
            try:
                job_id = await actual_runner.submit(request)
            except RunnerBusyError:
                return JSONResponse(
                    {"detail": "a trial is already running"}, status_code=409
                )
        return JSONResponse({"job_id": job_id}, status_code=202)

    @app.post("/api/jobs/{job_id}/stop", status_code=202)
    async def stop(job_id: str) -> Response:
        try:
            result = await actual_runner.stop(job_id)
        except JobNotFoundError:
            return JSONResponse({"detail": "job not found"}, status_code=404)
        except JobNotActiveError:
            return JSONResponse({"detail": "job is not active"}, status_code=409)
        return JSONResponse(result, status_code=202)

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
