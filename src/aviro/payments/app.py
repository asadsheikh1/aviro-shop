"""payments: card charges (simulated card network)."""

import uuid
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from aviro.common.config import get_settings
from aviro.common.db import Database
from aviro.common.service import create_service

router = APIRouter(tags=["payments"])

DECLINE_PREFIX = "tok_decline"


class ChargeRequest(BaseModel):
    order_id: uuid.UUID
    amount_cents: int = Field(ge=0, le=10_000_000)
    card_token: str = Field(min_length=8, max_length=64)


def _db(request: Request) -> Database:
    return request.app.state.db  # type: ignore[no-any-return]


@router.post("/charges", status_code=201)
async def charge(body: ChargeRequest, request: Request) -> dict[str, Any]:
    declined = body.card_token.startswith(DECLINE_PREFIX)
    row = await _db(request).fetch_one(
        "INSERT INTO payments (order_id, amount_cents, status, card_last4) "
        "VALUES (%s, %s, %s, %s) RETURNING id, status",
        (body.order_id, body.amount_cents, "declined" if declined else "captured", body.card_token[-4:]),
    )
    assert row is not None  # noqa: S101 - INSERT … RETURNING always returns a row
    if declined:
        raise HTTPException(402, {"charge_id": str(row["id"]), "status": "declined"})
    return {"charge_id": str(row["id"]), "status": row["status"]}


@router.get("/charges/{charge_id}")
async def get_charge(charge_id: uuid.UUID, request: Request) -> dict[str, Any]:
    row = await _db(request).fetch_one("SELECT * FROM payments WHERE id = %s", (charge_id,))
    if row is None:
        raise HTTPException(404, "Unknown charge")
    return {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in row.items()}


async def _startup(app: FastAPI) -> None:
    app.state.db = Database(app.state.settings, app.state.faults)
    await app.state.db.open()


async def _shutdown(app: FastAPI) -> None:
    await app.state.db.close()


async def database(app: FastAPI) -> bool:
    return await app.state.db.ping()  # type: ignore[no-any-return]


def build() -> FastAPI:
    settings = get_settings().model_copy(update={"service": "payments"})
    return create_service(
        settings, routers=[router], ready_checks=[database], on_startup=[_startup], on_shutdown=[_shutdown]
    )


app = build()
