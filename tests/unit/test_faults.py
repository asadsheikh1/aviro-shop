"""FaultEngine behaviour (no containers)."""

import threading
import time
from pathlib import Path

import pytest

from aviro.common import faults as faults_mod
from aviro.common.faults import FaultEngine, FaultError, FaultType


@pytest.fixture
def engine(tmp_path: Path) -> FaultEngine:
    e = FaultEngine("test", str(tmp_path))
    yield e
    e.clear()


def test_set_list_clear(engine: FaultEngine) -> None:
    engine.set(FaultType.LATENCY, {"ms": 5}, 60)
    engine.set(FaultType.ERROR_RATE, {"probability": 1}, 60)
    assert {f.type for f in engine.active()} == {FaultType.LATENCY, FaultType.ERROR_RATE}
    assert engine.clear(FaultType.LATENCY) == ["latency"]
    assert [f.type for f in engine.active()] == [FaultType.ERROR_RATE]
    assert engine.clear() == ["error_rate"]
    assert engine.active() == []


def test_faults_expire(engine: FaultEngine) -> None:
    engine.set(FaultType.LATENCY, {"ms": 5}, 0.05)
    assert engine.request_delay_s() == pytest.approx(0.005)
    time.sleep(0.1)
    assert engine.request_delay_s() == 0
    assert engine.active() == []


@pytest.mark.parametrize(
    ("ftype", "params", "duration"),
    [
        (FaultType.LATENCY, {"bogus": 1}, 60),
        (FaultType.ERROR_RATE, {"probability": 2}, 60),
        (FaultType.CPU_HOG, {"threads": 64}, 60),
        (FaultType.LATENCY, {"ms": -1}, 60),
        (FaultType.LATENCY, {}, 0),
        (FaultType.LATENCY, {}, 10**9),
    ],
)
def test_validation(
    engine: FaultEngine, ftype: FaultType, params: dict[str, object], duration: float
) -> None:
    with pytest.raises(FaultError):
        engine.set(ftype, params, duration)


def test_error_rate_probability(engine: FaultEngine) -> None:
    engine.set(FaultType.ERROR_RATE, {"probability": 0.0}, 60)
    assert all(engine.request_error() is None for _ in range(200))
    engine.set(FaultType.ERROR_RATE, {"probability": 1.0, "message": "boom"}, 60)
    assert engine.request_error() == "boom"


def test_dependency_hang_only_for_target(engine: FaultEngine) -> None:
    engine.set(FaultType.DEPENDENCY_TIMEOUT, {"target": "payments", "hang_s": 7}, 60)
    assert engine.dependency_hang_s("payments") == 7
    assert engine.dependency_hang_s("db") == 0


def test_memory_leak_grows_then_frees(engine: FaultEngine) -> None:
    engine.set(FaultType.MEMORY_LEAK, {"mb_per_s": 5, "max_mb": 50}, 60)
    time.sleep(2.3)
    assert len(engine._leak) >= 10  # ~5 MB per second
    engine.clear(FaultType.MEMORY_LEAK)
    assert engine._leak == []


def test_disk_fill_writes_then_cleans_up(engine: FaultEngine, tmp_path: Path) -> None:
    engine.set(FaultType.DISK_FILL, {"mb_per_s": 20, "max_mb": 5}, 60)
    time.sleep(0.6)
    fill = tmp_path / ".fill"
    assert fill.exists()
    assert fill.stat().st_size >= 3 * 1024 * 1024
    engine.clear(FaultType.DISK_FILL)
    assert not fill.exists()


def test_cpu_hog_threads_start_and_stop(engine: FaultEngine) -> None:
    engine.set(FaultType.CPU_HOG, {"threads": 2}, 60)
    time.sleep(0.1)
    assert sum(t.name.startswith("fault-cpu_hog") for t in threading.enumerate()) == 2
    engine.clear(FaultType.CPU_HOG)
    time.sleep(0.3)
    assert not any(t.name.startswith("fault-cpu_hog") for t in threading.enumerate())


def test_crash_calls_exit_after_delay(engine: FaultEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    exited = threading.Event()
    codes: list[int] = []
    monkeypatch.setattr(faults_mod.os, "_exit", lambda code: (codes.append(code), exited.set()))
    engine.set(FaultType.PROCESS_CRASH, {"delay_s": 0.05, "exit_code": 3}, 60)
    assert exited.wait(2)
    assert codes == [3]


def test_crash_can_be_cancelled(engine: FaultEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    codes: list[int] = []
    monkeypatch.setattr(faults_mod.os, "_exit", codes.append)
    engine.set(FaultType.PROCESS_CRASH, {"delay_s": 0.3}, 60)
    engine.clear(FaultType.PROCESS_CRASH)
    time.sleep(0.5)
    assert codes == []
