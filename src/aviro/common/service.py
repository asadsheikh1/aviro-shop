"""Shared FastAPI app factory: health, admin/fault API, fault middleware, error logging."""

import asyncio
import hmac
import logging
import os
import shutil
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from aviro import __version__
from aviro.common.config import Settings
from aviro.common.faults import DEFAULT_DURATION_S, FaultEngine, FaultError, FaultType
from aviro.common.http import INJECT_DELAY_HEADER
from aviro.common.logs import setup_logging

ReadyCheck = Callable[[FastAPI], Awaitable[bool]]
Hook = Callable[[FastAPI], Awaitable[None]]
_UNFAULTED = ("/healthz", "/readyz", "/admin")
_STARTED = time.time()


class FaultRequest(BaseModel):
    type: FaultType
    params: dict[str, Any] = Field(default_factory=dict)
    duration_s: float = DEFAULT_DURATION_S


def _rss_mb() -> float:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    import resource  # macOS fallback: peak RSS in bytes

    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024, 1)


def _disk(path: Path) -> tuple[float, float | None]:
    """(MB used by files under path, MB free on its filesystem)."""
    if not path.exists():
        return 0.0, None
    used = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    return round(used / 1024 / 1024, 1), round(shutil.disk_usage(path).free / 1024 / 1024, 1)


def _delay_ms(request: Request) -> int:
    try:
        return max(0, int(request.headers.get(INJECT_DELAY_HEADER, "0") or 0))
    except ValueError:
        return 0


def require_admin(
    request: Request, token: Annotated[str | None, Header(alias="X-Aviro-Admin-Token")] = None
) -> None:
    expected = request.app.state.settings.admin_token
    if expected is None:
        raise HTTPException(403, "Admin API disabled (AVIRO_ADMIN_TOKEN not set).")
    if token is None or not hmac.compare_digest(token, expected.get_secret_value()):
        raise HTTPException(401, "Invalid admin token.")


admin = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@admin.get("/faults")
async def list_faults(request: Request) -> list[dict[str, Any]]:
    return [f.public() for f in request.app.state.faults.active()]


@admin.post("/faults", status_code=201)
async def set_fault(body: FaultRequest, request: Request) -> dict[str, Any]:
    try:
        fault = request.app.state.faults.set(body.type, body.params, body.duration_s)
    except FaultError as e:
        raise HTTPException(422, str(e)) from e
    return fault.public()  # type: ignore[no-any-return]


@admin.delete("/faults")
async def clear_all(request: Request) -> dict[str, list[str]]:
    return {"cleared": request.app.state.faults.clear()}


@admin.delete("/faults/{fault_type}")
async def clear_one(fault_type: FaultType, request: Request) -> dict[str, list[str]]:
    return {"cleared": request.app.state.faults.clear(fault_type)}


@admin.get("/stats")
async def stats(request: Request) -> dict[str, Any]:
    s: Settings = request.app.state.settings
    data = Path(s.data_dir)
    used_mb, free_mb = await asyncio.to_thread(_disk, data)
    return {
        "service": s.service,
        "version": __version__,
        "pid": os.getpid(),
        "uptime_s": round(time.time() - _STARTED, 1),
        "rss_mb": _rss_mb(),
        "cpu_s": round(time.process_time(), 2),
        "data_dir_used_mb": used_mb,
        "data_dir_free_mb": free_mb,
        "faults": [f.public() for f in request.app.state.faults.active()],
    }


def create_service(
    settings: Settings,
    *,
    routers: list[APIRouter],
    ready_checks: list[ReadyCheck] | None = None,
    on_startup: list[Hook] | None = None,
    on_shutdown: list[Hook] | None = None,
) -> FastAPI:
    setup_logging(settings.service, settings.log_level)
    log = logging.getLogger(f"aviro.{settings.service}")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        for hook in on_startup or []:
            await hook(app)
        log.info("service started", extra={"version": __version__})
        try:
            yield
        finally:
            for hook in on_shutdown or []:
                await hook(app)

    app = FastAPI(title=f"AVIRO {settings.service}", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.faults = FaultEngine(settings.service, settings.data_dir)
    internal_only_header_ignored = settings.service == "gateway"

    @app.middleware("http")
    async def faults_and_errors(request: Request, call_next: Any) -> Response:
        faults: FaultEngine = request.app.state.faults
        try:
            # Internal services honour a caller's dependency_timeout request to stall.
            # The public gateway never does (it also strips the header).
            delay_ms = 0 if internal_only_header_ignored else _delay_ms(request)
            if delay_ms > 0:
                await asyncio.sleep(min(delay_ms, 60_000) / 1000)
            if not request.url.path.startswith(_UNFAULTED):
                if delay := faults.request_delay_s():
                    await asyncio.sleep(delay)
                if message := faults.request_error():
                    raise RuntimeError(message)
            return await call_next(request)  # type: ignore[no-any-return]
        except Exception:
            # One place logs every unhandled error, with stack and trace id.
            log.exception(
                "unhandled exception",
                extra={"http_method": request.method, "http_path": request.url.path},
            )
            return JSONResponse({"error": "internal_error"}, status_code=500)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": settings.service, "version": __version__}

    @app.get("/readyz", tags=["health"])
    async def readyz(response: Response) -> dict[str, Any]:
        results = {c.__name__: await c(app) for c in ready_checks or []}
        ok = all(results.values())
        response.status_code = 200 if ok else 503
        return {"ready": ok, "checks": results}

    app.include_router(admin)
    for router in routers:
        app.include_router(router)
    return app
