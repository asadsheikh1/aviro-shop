"""Every fault reproduces its intended symptom on the live stack (`make up`, then `make smoke`).

These run against http://127.0.0.1:8080 and use `podman` for container-level
checks (restarts, stopping the broker). Each test clears its faults afterwards.
"""

import os
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.smoke

URL = os.environ.get("AVIRO_URL", "http://127.0.0.1:8080")
TOKEN = os.environ.get("AVIRO_ADMIN_TOKEN", "aviro-admin-dev-only")
ADMIN = {"X-Aviro-Admin-Token": TOKEN}
PROJECT = "aviro"


def order_body(sku: str = "KB-101") -> dict[str, Any]:
    return {
        "customer_email": "smoke@example.com",
        "items": [{"sku": sku, "qty": 1}],
        "card_token": f"tok_visa_{uuid.uuid4().hex[:8]}",
    }


def wait_until(check: Callable[[], bool], timeout: float = 60, every: float = 1.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if check():
                return True
        except httpx.HTTPError:
            pass
        time.sleep(every)
    return False


def podman(*args: str) -> str:
    return subprocess.run(
        ["podman", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def started_at(service: str) -> str:
    return podman("inspect", "--format", "{{.State.StartedAt}}", f"{PROJECT}_{service}_1")


@pytest.fixture(scope="module")
def http() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=URL, timeout=30) as c:
        assert wait_until(lambda: c.get("/api/health").json()["ready"], 90), "stack not ready"
        yield c


@pytest.fixture
def fault(http: httpx.Client) -> Iterator[Callable[..., dict[str, Any]]]:
    touched: set[str] = set()

    def set_fault(service: str, ftype: str, duration_s: float = 120, **params: Any) -> dict[str, Any]:
        touched.add(service)
        r = http.post(
            f"/admin/{service}/faults",
            json={"type": ftype, "params": params, "duration_s": duration_s},
            headers=ADMIN,
        )
        assert r.status_code == 201, r.text
        return r.json()  # type: ignore[no-any-return]

    yield set_fault
    for service in touched:
        wait_until(lambda s=service: http.delete(f"/admin/{s}/faults", headers=ADMIN).is_success, 60)
    assert wait_until(lambda: http.get("/api/health").json()["ready"], 90)


def stats(http: httpx.Client, service: str) -> dict[str, Any]:
    return http.get(f"/admin/{service}/stats", headers=ADMIN).json()  # type: ignore[no-any-return]


# ------------------------------------------------------------- baseline


def test_admin_requires_token(http: httpx.Client) -> None:
    assert http.get("/admin/payments/faults").status_code == 401
    assert http.get("/admin/payments/faults", headers={"X-Aviro-Admin-Token": "x"}).status_code == 401


def test_order_is_placed_and_fulfilled(http: httpx.Client) -> None:
    r = http.post("/api/orders", json=order_body())
    assert r.status_code == 201, r.text
    order_id = r.json()["order_id"]
    assert wait_until(lambda: http.get(f"/api/orders/{order_id}").json()["status"] == "fulfilled", 20)


def test_declined_card_is_402(http: httpx.Client) -> None:
    body = {**order_body(), "card_token": "tok_decline_0002"}
    assert http.post("/api/orders", json=body).status_code == 402


# ------------------------------------------------------- request faults


def test_latency(http: httpx.Client, fault: Callable[..., Any]) -> None:
    fault("inventory", "latency", ms=800)
    started = time.perf_counter()
    assert http.get("/api/products").status_code == 200
    assert time.perf_counter() - started >= 0.8


def test_error_rate(http: httpx.Client, fault: Callable[..., Any]) -> None:
    fault("payments", "error_rate", probability=1.0)
    r = http.post("/api/orders", json=order_body())
    assert r.status_code == 502  # orders reports its dependency failing
    assert "payments" in r.json()["detail"]


def test_dependency_timeout_on_a_service(http: httpx.Client, fault: Callable[..., Any]) -> None:
    """S05-style: orders' calls to payments hang, so orders' real 3 s timeout fires."""
    fault("orders", "dependency_timeout", target="payments", hang_s=10)
    started = time.perf_counter()
    r = http.post("/api/orders", json=order_body())
    elapsed = time.perf_counter() - started
    assert r.status_code == 504
    assert "payments timed out" in r.json()["detail"]
    assert 2.5 <= elapsed < 9


def test_dependency_timeout_on_the_database(http: httpx.Client, fault: Callable[..., Any]) -> None:
    """Queries hang; Postgres cancels them at statement_timeout (2 s)."""
    fault("inventory", "dependency_timeout", target="db", hang_s=10)
    started = time.perf_counter()
    r = http.get("/api/products")
    assert r.status_code == 500
    assert time.perf_counter() - started < 6


# ------------------------------------------------------ resource faults


def test_memory_leak_grows_rss(http: httpx.Client, fault: Callable[..., Any]) -> None:
    before = stats(http, "payments")["rss_mb"]
    fault("payments", "memory_leak", mb_per_s=30, max_mb=150)
    assert wait_until(lambda: stats(http, "payments")["rss_mb"] >= before + 80, 20)


def test_memory_leak_ends_in_oom_restart(http: httpx.Client, fault: Callable[..., Any]) -> None:
    """S06: inventory has a 300 MB memory limit, so an unbounded leak gets it OOM-killed."""
    first_start = started_at("inventory")
    fault("inventory", "memory_leak", mb_per_s=100, max_mb=2000)
    assert wait_until(lambda: started_at("inventory") != first_start, 60), "inventory was not restarted"
    oom = podman("inspect", "--format", "{{.State.OOMKilled}}", f"{PROJECT}_inventory_1")
    exit_code = podman("inspect", "--format", "{{.State.ExitCode}}", f"{PROJECT}_inventory_1")
    print(f"inventory restarted; OOMKilled={oom} last exit={exit_code}")


def test_cpu_hog(http: httpx.Client, fault: Callable[..., Any]) -> None:
    before = stats(http, "payments")["cpu_s"]
    fault("payments", "cpu_hog", threads=1)
    time.sleep(3)
    assert stats(http, "payments")["cpu_s"] - before >= 1.5


def test_disk_fill_makes_writes_fail(http: httpx.Client, fault: Callable[..., Any]) -> None:
    fault("orders", "disk_fill", mb_per_s=100, max_mb=1000)
    assert wait_until(lambda: (stats(http, "orders")["data_dir_free_mb"] or 0) < 1, 30)
    r = http.post("/api/orders", json=order_body())
    assert r.status_code == 500  # receipt write fails: No space left on device
    http.delete("/admin/orders/faults/disk_fill", headers=ADMIN)
    assert http.post("/api/orders", json=order_body()).status_code == 201


def test_process_crash_restarts(http: httpx.Client, fault: Callable[..., Any]) -> None:
    first_start = started_at("payments")
    fault("payments", "process_crash", delay_s=0.5)
    assert wait_until(lambda: started_at("payments") != first_start, 30), "payments did not restart"
    assert wait_until(lambda: http.post("/api/orders", json=order_body()).status_code == 201, 60)


# ------------------------------------------------- infrastructure / data


def test_broker_down_breaks_checkout(http: httpx.Client) -> None:
    """S07: Valkey unavailable → orders can't queue fulfilment."""
    podman("stop", "-t", "2", f"{PROJECT}_valkey_1")
    try:
        assert http.post("/api/orders", json=order_body()).status_code == 500
    finally:
        podman("start", f"{PROJECT}_valkey_1")
    assert wait_until(lambda: http.post("/api/orders", json=order_body()).status_code == 201, 60)


def test_s10_data_change_triggers_latent_bug(http: httpx.Client) -> None:
    """S10: no deploy; a bad catalogue row breaks product listing."""
    assert http.post("/admin/inventory/scenario-data/s10", headers=ADMIN).status_code == 200
    try:
        assert http.get("/api/products").status_code == 500
    finally:
        http.delete("/admin/inventory/scenario-data/s10", headers=ADMIN)
    assert http.get("/api/products").status_code == 200
