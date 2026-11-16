"""Shared service behaviour: health, admin auth, fault middleware, error handling."""

import json
import tempfile
import time

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from aviro.common.config import Settings
from aviro.common.http import INJECT_DELAY_HEADER
from aviro.common.service import create_service

TOKEN = "unit-admin-token"
H = {"X-Aviro-Admin-Token": TOKEN}


def _app(service: str = "probe", token: str | None = TOKEN, tmp: str | None = None) -> TestClient:
    r = APIRouter()

    @r.get("/work")
    async def work() -> dict[str, str]:
        return {"ok": "yes"}

    @r.get("/broken")
    async def broken() -> dict[str, str]:
        return {}["missing"]  # a real KeyError, like a code bug

    settings = Settings(service=service, admin_token=token, data_dir=tmp or tempfile.gettempdir())
    return TestClient(create_service(settings, routers=[r]))


def test_health_endpoints() -> None:
    c = _app()
    assert c.get("/healthz").json()["status"] == "ok"
    assert c.get("/readyz").json() == {"ready": True, "checks": {}}


def test_admin_requires_token() -> None:
    assert _app(token=None).get("/admin/faults").status_code == 403  # admin disabled
    c = _app()
    assert c.get("/admin/faults").status_code == 401
    assert c.get("/admin/faults", headers={"X-Aviro-Admin-Token": "wrong"}).status_code == 401
    assert c.get("/admin/faults", headers=H).status_code == 200


def test_set_and_clear_fault_via_api() -> None:
    c = _app()
    r = c.post("/admin/faults", json={"type": "latency", "params": {"ms": 300}, "duration_s": 30}, headers=H)
    assert r.status_code == 201
    assert r.json()["params"]["ms"] == 300
    started = time.perf_counter()
    assert c.get("/work").status_code == 200
    assert time.perf_counter() - started >= 0.3
    # health and admin routes are never faulted
    started = time.perf_counter()
    c.get("/healthz")
    assert time.perf_counter() - started < 0.2
    assert c.delete("/admin/faults/latency", headers=H).json() == {"cleared": ["latency"]}


def test_bad_fault_is_422() -> None:
    c = _app()
    bad = c.post("/admin/faults", json={"type": "latency", "params": {"nope": 1}}, headers=H)
    assert bad.status_code == 422
    assert c.post("/admin/faults", json={"type": "teleport"}, headers=H).status_code == 422


def _logs(out: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in out.splitlines() if line.startswith("{")]


def test_error_rate_returns_500_and_logs(capsys: pytest.CaptureFixture[str]) -> None:
    c = _app()
    c.post(
        "/admin/faults",
        json={"type": "error_rate", "params": {"probability": 1, "message": "db exploded"}},
        headers=H,
    )
    r = c.get("/work")
    assert r.status_code == 500
    assert r.json() == {"error": "internal_error"}  # no internals leaked to clients
    errors = [r for r in _logs(capsys.readouterr().out) if r["msg"] == "unhandled exception"]
    assert errors
    assert errors[0]["exc_type"] == "RuntimeError"
    assert "db exploded" in str(errors[0]["stack"])


def test_real_bug_is_logged_with_stack(capsys: pytest.CaptureFixture[str]) -> None:
    r = _app().get("/broken")
    assert r.status_code == 500
    rec = next(r for r in _logs(capsys.readouterr().out) if r["msg"] == "unhandled exception")
    assert rec["exc_type"] == "KeyError"
    assert rec["http_path"] == "/broken"
    assert "test_service.py" in str(rec["stack"])  # points at the faulty line


def test_internal_services_honour_inject_delay_but_gateway_does_not() -> None:
    hdr = {INJECT_DELAY_HEADER: "300"}
    started = time.perf_counter()
    _app("payments").get("/work", headers=hdr)
    assert time.perf_counter() - started >= 0.3
    started = time.perf_counter()
    _app("gateway").get("/work", headers=hdr)
    assert time.perf_counter() - started < 0.2


def test_stats(tmp_path: object) -> None:
    s = _app(tmp=str(tmp_path)).get("/admin/stats", headers=H).json()
    assert s["service"] == "probe"
    assert s["rss_mb"] > 0
    assert s["data_dir_free_mb"] is not None
