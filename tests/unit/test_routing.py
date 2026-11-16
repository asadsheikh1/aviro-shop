"""Gateway routing and orders' dependency error mapping (downstream mocked with respx)."""

from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from aviro.common.faults import FaultType
from aviro.common.http import INJECT_DELAY_HEADER, ServiceClient
from aviro.inventory.app import unit_price_cents

INV = "http://inventory:8000"
PAY = "http://payments:8000"
ORD = "http://orders:8000"
ORDER = {
    "customer_email": "a@example.com",
    "items": [{"sku": "KB-101", "qty": 1}],
    "card_token": "tok_visa_1234",
}


def test_every_service_module_builds() -> None:
    """Catches import-time problems (e.g. a missing optional dependency)."""
    import aviro.gateway.app
    import aviro.inventory.app
    import aviro.orders.app
    import aviro.payments.app
    import aviro.worker.tasks  # noqa: F401


# ------------------------------------------------------------------- gateway


@pytest.fixture
def gateway() -> TestClient:
    from aviro.gateway.app import build

    with TestClient(build()) as c:
        yield c


@respx.mock
def test_gateway_forwards_and_strips_internal_header(gateway: TestClient) -> None:
    route = respx.get(f"{INV}/products").mock(return_value=httpx.Response(200, json=[{"sku": "X"}]))
    r = gateway.get("/api/products", headers={INJECT_DELAY_HEADER: "60000"})
    assert r.status_code == 200
    assert r.json() == [{"sku": "X"}]
    assert INJECT_DELAY_HEADER.lower() not in {k.lower() for k in route.calls[0].request.headers}


@respx.mock
def test_gateway_maps_upstream_timeout_to_504(gateway: TestClient) -> None:
    respx.post(f"{ORD}/orders").mock(side_effect=httpx.ReadTimeout("slow"))
    assert gateway.post("/api/orders", json=ORDER).status_code == 504


@respx.mock
def test_gateway_admin_proxy(gateway: TestClient) -> None:
    respx.get(f"{PAY}/admin/stats").mock(return_value=httpx.Response(200, json={"service": "payments"}))
    assert gateway.get("/admin/payments/stats").json() == {"service": "payments"}
    assert gateway.get("/admin/nope/stats").status_code == 404


# -------------------------------------------------------------------- orders


class FakeCelery:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send_task(self, name: str, **kwargs: Any) -> None:
        self.sent.append({"name": name, **kwargs})


@pytest.fixture
def orders() -> TestClient:
    from aviro.orders.app import build

    app = build()  # no lifespan: wire state by hand so no DB/broker is needed
    f = app.state.faults
    app.state.inventory = ServiceClient("inventory", INV, 0.5, f)
    app.state.payments = ServiceClient("payments", PAY, 0.5, f)
    app.state.celery = FakeCelery()
    return TestClient(app)


@respx.mock
def test_payments_timeout_is_504(orders: TestClient) -> None:
    respx.post(f"{INV}/reservations").mock(
        return_value=httpx.Response(200, json={"items": [], "total_cents": 100})
    )
    respx.post(f"{PAY}/charges").mock(side_effect=httpx.ReadTimeout("slow"))
    r = orders.post("/orders", json=ORDER)
    assert r.status_code == 504
    assert "payments" in r.json()["detail"]


@respx.mock
def test_payments_500_is_502(orders: TestClient) -> None:
    respx.post(f"{INV}/reservations").mock(
        return_value=httpx.Response(200, json={"items": [], "total_cents": 100})
    )
    respx.post(f"{PAY}/charges").mock(return_value=httpx.Response(500, json={"error": "internal_error"}))
    assert orders.post("/orders", json=ORDER).status_code == 502


@respx.mock
def test_declined_card_releases_stock(orders: TestClient) -> None:
    respx.post(f"{INV}/reservations").mock(
        return_value=httpx.Response(200, json={"items": [], "total_cents": 100})
    )
    respx.post(f"{PAY}/charges").mock(return_value=httpx.Response(402, json={"detail": {}}))
    release = respx.post(f"{INV}/reservations/release").mock(return_value=httpx.Response(200, json={}))
    assert orders.post("/orders", json=ORDER).status_code == 402
    assert release.called


@respx.mock
def test_out_of_stock_passes_through(orders: TestClient) -> None:
    respx.post(f"{INV}/reservations").mock(
        return_value=httpx.Response(409, json={"detail": "Cannot reserve"})
    )
    assert orders.post("/orders", json=ORDER).status_code == 409


@respx.mock
def test_dependency_timeout_fault_asks_callee_to_stall(orders: TestClient) -> None:
    orders.app.state.faults.set(FaultType.DEPENDENCY_TIMEOUT, {"target": "inventory", "hang_s": 9}, 60)  # type: ignore[attr-defined]
    route = respx.post(f"{INV}/reservations").mock(return_value=httpx.Response(409, json={"detail": "x"}))
    orders.post("/orders", json=ORDER)
    assert route.calls[0].request.headers[INJECT_DELAY_HEADER] == "9000"


def test_order_validation() -> None:
    from aviro.orders.app import build

    c = TestClient(build())
    assert c.post("/orders", json={**ORDER, "customer_email": "nope"}).status_code == 422
    assert c.post("/orders", json={**ORDER, "items": []}).status_code == 422


# ----------------------------------------------------------------- inventory


def test_unit_price() -> None:
    assert unit_price_cents({"price_cents": 1500, "pack_size": 3}) == 500


def test_s10_latent_bug_exists_by_design() -> None:
    """S10: a pack_size of 0 (only reachable via a data change) breaks pricing."""
    with pytest.raises(ZeroDivisionError):
        unit_price_cents({"price_cents": 0, "pack_size": 0})
