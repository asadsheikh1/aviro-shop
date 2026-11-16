"""orders: checkout (reserve stock → charge card → save → queue fulfilment)."""

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any

from celery import Celery
from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from aviro.common.config import get_settings
from aviro.common.db import Database
from aviro.common.http import DependencyError, ServiceClient
from aviro.common.service import create_service

log = logging.getLogger("aviro.orders")
router = APIRouter(tags=["orders"])

FULFIL_TASK = "aviro.fulfil_order"


class Item(BaseModel):
    sku: str = Field(min_length=1, max_length=40)
    qty: int = Field(ge=1, le=100)


class OrderRequest(BaseModel):
    customer_email: EmailStr
    items: list[Item] = Field(min_length=1, max_length=20)
    card_token: str = Field(min_length=8, max_length=64)


def _dependency_failed(e: DependencyError) -> HTTPException:
    log.error(
        "dependency call failed",
        extra={"dependency": e.service, "dependency_status": e.status, "detail": e.detail},
    )
    return HTTPException(504 if e.timeout else 502, f"{e.service} {e.detail}")


async def _call(client: ServiceClient, method: str, path: str, body: dict[str, Any]) -> Any:
    resp = await client.request(method, path, json=body)
    if resp.status_code >= 500:
        raise DependencyError(client.name, resp.status_code, f"returned {resp.status_code}")
    return resp


def _write_receipt(data_dir: str, order: dict[str, Any]) -> None:
    """Receipts are written to local disk (a disk_fill fault makes this fail)."""
    folder = Path(data_dir) / "receipts"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{order['order_id']}.json").write_text(json.dumps(order))


@router.post("/orders", status_code=201)
async def create_order(body: OrderRequest, request: Request) -> dict[str, Any]:
    state = request.app.state
    order_id = uuid.uuid4()
    items = [i.model_dump() for i in body.items]
    try:
        reservation = await _call(state.inventory, "POST", "/reservations", {"items": items})
        if reservation.status_code != 200:
            raise HTTPException(reservation.status_code, reservation.json().get("detail"))
        reserved = reservation.json()

        charge = await _call(
            state.payments,
            "POST",
            "/charges",
            {
                "order_id": str(order_id),
                "amount_cents": reserved["total_cents"],
                "card_token": body.card_token,
            },
        )
        if charge.status_code != 201:
            await _call(state.inventory, "POST", "/reservations/release", {"items": items})
            raise HTTPException(402, "Payment declined")
    except DependencyError as e:
        raise _dependency_failed(e) from e

    db: Database = state.db
    async with db.pool.connection() as conn, conn.transaction():
        await db.maybe_hang(conn)
        await conn.execute(
            "INSERT INTO orders (id, customer_email, status, total_cents, payment_id) "
            "VALUES (%s, %s, 'paid', %s, %s)",
            (order_id, body.customer_email, reserved["total_cents"], charge.json()["charge_id"]),
        )
        for line in reserved["items"]:
            await conn.execute(
                "INSERT INTO order_items (order_id, sku, qty, unit_price_cents) VALUES (%s, %s, %s, %s)",
                (order_id, line["sku"], line["qty"], line["unit_price_cents"]),
            )

    order = {
        "order_id": str(order_id),
        "status": "paid",
        "total_cents": reserved["total_cents"],
        "items": reserved["items"],
    }
    _write_receipt(state.settings.data_dir, order)
    await asyncio.to_thread(
        state.celery.send_task,
        FULFIL_TASK,
        kwargs={"order": {**order, "customer_email": body.customer_email}},
        retry=True,
        retry_policy={"max_retries": 2, "interval_start": 0, "interval_step": 0.2},
    )
    log.info("order placed", extra={"order_id": str(order_id), "total_cents": order["total_cents"]})
    return order


@router.get("/orders/{order_id}")
async def get_order(order_id: uuid.UUID, request: Request) -> dict[str, Any]:
    db: Database = request.app.state.db
    order = await db.fetch_one("SELECT * FROM orders WHERE id = %s", (order_id,))
    if order is None:
        raise HTTPException(404, "Unknown order")
    items = await db.fetch_all(
        "SELECT sku, qty, unit_price_cents FROM order_items WHERE order_id = %s ORDER BY sku",
        (order_id,),
    )
    return {
        "order_id": str(order["id"]),
        "status": order["status"],
        "total_cents": order["total_cents"],
        "items": items,
        "created_at": order["created_at"].isoformat(),
        "fulfilled_at": order["fulfilled_at"].isoformat() if order["fulfilled_at"] else None,
    }


async def _startup(app: FastAPI) -> None:
    s = app.state.settings
    app.state.db = Database(s, app.state.faults)
    await app.state.db.open()
    app.state.inventory = ServiceClient("inventory", s.inventory_url, s.http_timeout_s, app.state.faults)
    app.state.payments = ServiceClient("payments", s.payments_url, s.http_timeout_s, app.state.faults)
    app.state.celery = Celery("aviro", broker=s.broker_url)
    app.state.celery.conf.broker_connection_timeout = 2


async def _shutdown(app: FastAPI) -> None:
    await app.state.inventory.close()
    await app.state.payments.close()
    await app.state.db.close()


async def database(app: FastAPI) -> bool:
    return await app.state.db.ping()  # type: ignore[no-any-return]


def build() -> FastAPI:
    settings = get_settings().model_copy(update={"service": "orders"})
    return create_service(
        settings, routers=[router], ready_checks=[database], on_startup=[_startup], on_shutdown=[_shutdown]
    )


app = build()
