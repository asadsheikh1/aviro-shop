"""Telemetry leaves every service (needs `make up-otel`, i.e. the debug collector).

Skipped when the debug collector isn't running.
"""

import os
import subprocess
import time
import uuid

import httpx
import pytest

from .test_faults_live import URL, wait_until

pytestmark = pytest.mark.smoke
COLLECTOR = "aviro_otel-debug_1"
SERVICES = ["gateway", "inventory", "payments", "orders", "worker"]


def _collector_logs() -> str:
    out = subprocess.run(
        ["podman", "logs", COLLECTOR],
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout + out.stderr


def _collector_running() -> bool:
    out = subprocess.run(
        ["podman", "inspect", "--format", "{{.State.Running}}", COLLECTOR],
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout.strip() == "true"


def test_traces_metrics_logs_reach_the_collector() -> None:
    if not _collector_running():
        if os.environ.get("AVIRO_REQUIRE_OTEL"):  # CI must not silently skip this
            pytest.fail("debug collector not running")
        pytest.skip("debug collector not running (use make up-otel)")
    with httpx.Client(base_url=URL, timeout=30) as c:
        for _ in range(3):
            r = c.post(
                "/api/orders",
                json={
                    "customer_email": "otel@example.com",
                    "items": [{"sku": "MS-202", "qty": 1}],
                    "card_token": f"tok_visa_{uuid.uuid4().hex[:8]}",
                },
            )
            assert r.status_code == 201
        time.sleep(1)

    def every_service_traced() -> bool:
        logs = _collector_logs()
        return all(f"service.name: Str({s})" in logs for s in SERVICES)

    # Metrics export every 15 s; give batches time to arrive.
    assert wait_until(every_service_traced, 60, every=3), "not every service's telemetry arrived"
    logs = _collector_logs()
    assert "Traces" in logs
    assert "Metrics" in logs
    assert "Logs" in logs
    assert "service.namespace: Str(aviro)" in logs
