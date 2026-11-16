"""gateway: the only public entry point.

Routes /api/* to the services and /admin/<service>/* to their admin APIs.
"""

import logging
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response

from aviro.common.config import get_settings
from aviro.common.http import INJECT_DELAY_HEADER, DependencyError, ServiceClient
from aviro.common.service import create_service

log = logging.getLogger("aviro.gateway")
router = APIRouter(tags=["gateway"])

SERVICES = ("inventory", "payments", "orders")
# Hop-by-hop and internal headers never forwarded from the public side.
_DROP = {"host", "content-length", "connection", "transfer-encoding", INJECT_DELAY_HEADER.lower()}


def _client(request: Request, service: str) -> ServiceClient:
    return request.app.state.clients[service]  # type: ignore[no-any-return]


async def _forward(request: Request, service: str, path: str) -> Response:
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _DROP}
    try:
        upstream = await _client(request, service).request(
            request.method,
            path,
            params=request.query_params,
            content=await request.body(),
            headers=headers,
        )
    except DependencyError as e:
        log.error("upstream failed", extra={"upstream": service, "detail": e.detail})
        raise HTTPException(504 if e.timeout else 502, f"{service} {e.detail}") from e
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )


@router.get("/api/products")
async def products(request: Request) -> Response:
    return await _forward(request, "inventory", "/products")


@router.get("/api/products/{sku}")
async def product(sku: str, request: Request) -> Response:
    return await _forward(request, "inventory", f"/products/{sku}")


@router.post("/api/orders")
async def create_order(request: Request) -> Response:
    return await _forward(request, "orders", "/orders")


@router.get("/api/orders/{order_id}")
async def get_order(order_id: str, request: Request) -> Response:
    return await _forward(request, "orders", f"/orders/{order_id}")


@router.api_route("/admin/{service}/{path:path}", methods=["GET", "POST", "DELETE"])
async def admin_proxy(service: str, path: str, request: Request) -> Response:
    """Forward to a service's admin API. The service itself checks the admin token."""
    if service not in SERVICES:
        raise HTTPException(404, f"Unknown service {service}")
    return await _forward(request, service, f"/admin/{path}")


@router.get("/api/health")
async def health(request: Request) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in SERVICES:
        try:
            r = await _client(request, name).request("GET", "/readyz")
            out[name] = r.json().get("ready", False)
        except DependencyError:
            out[name] = False
    return {"ready": all(out.values()), "services": out}


async def _startup(app: FastAPI) -> None:
    s = app.state.settings
    urls = {"inventory": s.inventory_url, "payments": s.payments_url, "orders": s.orders_url}
    # Orders can legitimately take up to ~2 dependency timeouts; give it room.
    app.state.clients = {
        name: ServiceClient(name, url, s.http_timeout_s * (3 if name == "orders" else 1), app.state.faults)
        for name, url in urls.items()
    }


async def _shutdown(app: FastAPI) -> None:
    for c in app.state.clients.values():
        await c.close()


def build() -> FastAPI:
    settings = get_settings().model_copy(update={"service": "gateway"})
    return create_service(settings, routers=[router], on_startup=[_startup], on_shutdown=[_shutdown])


app = build()
