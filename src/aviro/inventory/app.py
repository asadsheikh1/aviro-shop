"""inventory: product catalogue and stock reservations."""

from typing import Any, cast

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from aviro.common.config import get_settings
from aviro.common.db import Database
from aviro.common.service import create_service, require_admin

router = APIRouter(tags=["inventory"])
scenario_admin = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


class Item(BaseModel):
    sku: str = Field(min_length=1, max_length=40)
    qty: int = Field(ge=1, le=100)


class ReservationRequest(BaseModel):
    items: list[Item] = Field(min_length=1, max_length=20)


def _db(request: Request) -> Database:
    return request.app.state.db  # type: ignore[no-any-return]


def unit_price_cents(product: dict[str, Any]) -> int:
    """Price of a single unit. Products are priced per pack."""
    return int(product["price_cents"] // product["pack_size"])


def _public(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "sku": p["sku"],
        "name": p["name"],
        "price_cents": p["price_cents"],
        "pack_size": p["pack_size"],
        "unit_price_cents": unit_price_cents(p),
        "in_stock": p["stock"] > 0,
    }


@router.get("/products")
async def list_products(request: Request, limit: int = 50) -> list[dict[str, Any]]:
    rows = await _db(request).fetch_all("SELECT * FROM products ORDER BY sku LIMIT %s", (min(limit, 200),))
    return [_public(r) for r in rows]


@router.get("/products/{sku}")
async def get_product(sku: str, request: Request) -> dict[str, Any]:
    row = await _db(request).fetch_one("SELECT * FROM products WHERE sku = %s", (sku,))
    if row is None:
        raise HTTPException(404, f"Unknown product {sku}")
    return _public(row)


@router.post("/reservations")
async def reserve(body: ReservationRequest, request: Request) -> dict[str, Any]:
    """Atomically take stock for every item, or nothing."""
    db = _db(request)
    lines: list[tuple[str, int, int]] = []
    async with db.pool.connection() as conn, conn.transaction():
        await db.maybe_hang(conn)
        for item in sorted(body.items, key=lambda i: i.sku):  # fixed order: no deadlocks
            cur = await conn.execute(
                "UPDATE products SET stock = stock - %s, updated_at = now() "
                "WHERE sku = %s AND stock >= %s RETURNING sku, price_cents, pack_size",
                (item.qty, item.sku, item.qty),
            )
            row = cast(dict[str, Any] | None, await cur.fetchone())
            if row is None:
                exists = await (
                    await conn.execute("SELECT 1 FROM products WHERE sku = %s", (item.sku,))
                ).fetchone()
                raise HTTPException(409 if exists else 404, f"Cannot reserve {item.sku}")
            lines.append((item.sku, item.qty, unit_price_cents(row)))
    return {
        "items": [{"sku": sku, "qty": qty, "unit_price_cents": unit} for sku, qty, unit in lines],
        "total_cents": sum(qty * unit for _, qty, unit in lines),
    }


@router.post("/reservations/release")
async def release(body: ReservationRequest, request: Request) -> dict[str, str]:
    """Return stock after a failed payment."""
    db = _db(request)
    async with db.pool.connection() as conn, conn.transaction():
        await db.maybe_hang(conn)
        for item in body.items:
            await conn.execute("UPDATE products SET stock = stock + %s WHERE sku = %s", (item.qty, item.sku))
    return {"status": "released"}


# Scenario S10: a data change (no deploy) that exposes a latent bug. A bulk
# "sample" product is added with pack_size 0, which unit_price_cents can't handle.
S10_SKU = "SMP-000"


@scenario_admin.post("/scenario-data/s10")
async def add_s10_product(request: Request) -> dict[str, str]:
    await _db(request).fetch_all(
        "INSERT INTO products (sku, name, price_cents, pack_size, stock) "
        "VALUES (%s, 'Free sample bundle', 0, 0, 1000000) ON CONFLICT (sku) DO NOTHING "
        "RETURNING sku",
        (S10_SKU,),
    )
    return {"added": S10_SKU}


@scenario_admin.delete("/scenario-data/s10")
async def remove_s10_product(request: Request) -> dict[str, str]:
    await _db(request).fetch_all("DELETE FROM products WHERE sku = %s RETURNING sku", (S10_SKU,))
    return {"removed": S10_SKU}


async def _startup(app: FastAPI) -> None:
    app.state.db = Database(app.state.settings, app.state.faults)
    await app.state.db.open()


async def _shutdown(app: FastAPI) -> None:
    await app.state.db.close()


async def database(app: FastAPI) -> bool:
    return await app.state.db.ping()  # type: ignore[no-any-return]


def build() -> FastAPI:
    settings = get_settings().model_copy(update={"service": "inventory"})
    return create_service(
        settings,
        routers=[router, scenario_admin],
        ready_checks=[database],
        on_startup=[_startup],
        on_shutdown=[_shutdown],
    )


app = build()
