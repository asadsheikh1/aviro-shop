"""Runtime fault injection.

Each service process holds its own ``FaultEngine``. Faults are switched on
through the authenticated admin API (``/admin/faults``) and switch themselves
off after ``duration_s`` (default 5 minutes), so a forgotten fault can't break
the demo forever.

| type                 | effect                                                        |
|----------------------|---------------------------------------------------------------|
| latency              | sleep ``ms`` before handling a request (with ``probability``) |
| error_rate           | fail requests with HTTP 500 (with ``probability``)            |
| dependency_timeout   | calls to ``target`` hang: ``db`` → statement timeout,         |
|                      | a service name → the HTTP client's timeout fires              |
| memory_leak          | allocate ``mb_per_s`` until ``max_mb`` (→ OOM kill if limited) |
| cpu_hog              | ``threads`` busy-looping threads                              |
| disk_fill            | write ``mb_per_s`` into the data dir until ``max_mb`` / full  |
| process_crash        | ``os._exit(exit_code)`` after ``delay_s``                     |
"""

import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

log = logging.getLogger("aviro.faults")

DEFAULT_DURATION_S = 300
MAX_DURATION_S = 6 * 3600


class FaultType(StrEnum):
    LATENCY = "latency"
    ERROR_RATE = "error_rate"
    DEPENDENCY_TIMEOUT = "dependency_timeout"
    MEMORY_LEAK = "memory_leak"
    CPU_HOG = "cpu_hog"
    DISK_FILL = "disk_fill"
    PROCESS_CRASH = "process_crash"


DEFAULTS: dict[FaultType, dict[str, Any]] = {
    FaultType.LATENCY: {"ms": 800, "probability": 1.0},
    FaultType.ERROR_RATE: {"probability": 0.5, "message": "Unexpected internal error"},
    FaultType.DEPENDENCY_TIMEOUT: {"target": "db", "hang_s": 10.0},
    FaultType.MEMORY_LEAK: {"mb_per_s": 10, "max_mb": 1024},
    FaultType.CPU_HOG: {"threads": 1},
    FaultType.DISK_FILL: {"mb_per_s": 50, "max_mb": 4096},
    FaultType.PROCESS_CRASH: {"delay_s": 1.0, "exit_code": 1},
}


class FaultError(ValueError):
    pass


@dataclass
class ActiveFault:
    type: FaultType
    params: dict[str, Any]
    expires_at: float
    stop: threading.Event = field(default_factory=threading.Event)

    def public(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "params": self.params,
            "expires_in_s": max(0, round(self.expires_at - time.time())),
        }


def _validate(ftype: FaultType, params: dict[str, Any]) -> dict[str, Any]:
    unknown = set(params) - set(DEFAULTS[ftype])
    if unknown:
        raise FaultError(f"unknown parameter(s) for {ftype}: {sorted(unknown)}")
    merged = {**DEFAULTS[ftype], **params}
    for key in ("probability",):
        if key in merged and not 0 <= float(merged[key]) <= 1:
            raise FaultError(f"{key} must be between 0 and 1")
    for key in ("ms", "mb_per_s", "max_mb", "threads", "hang_s", "delay_s"):
        if key in merged and float(merged[key]) < 0:
            raise FaultError(f"{key} must be >= 0")
    if ftype is FaultType.CPU_HOG and not 1 <= int(merged["threads"]) <= 8:
        raise FaultError("threads must be 1..8")
    return merged


class FaultEngine:
    def __init__(self, service: str, data_dir: str) -> None:
        self.service = service
        self.data_dir = Path(data_dir)
        self._faults: dict[FaultType, ActiveFault] = {}
        self._lock = threading.Lock()
        self._leak: list[bytearray] = []

    # ------------------------------------------------------------ control

    def set(self, ftype: FaultType, params: dict[str, Any], duration_s: float) -> ActiveFault:
        if not 0 < duration_s <= MAX_DURATION_S:
            raise FaultError(f"duration_s must be in (0, {MAX_DURATION_S}]")
        merged = _validate(ftype, params)
        self.clear(ftype)
        fault = ActiveFault(ftype, merged, time.time() + duration_s)
        with self._lock:
            self._faults[ftype] = fault
        self._start_background(fault)
        log.warning("fault enabled", extra={"fault": ftype.value, "params": merged})
        return fault

    def clear(self, ftype: FaultType | None = None) -> list[str]:
        with self._lock:
            targets = [ftype] if ftype else list(self._faults)
            removed = [self._faults.pop(t) for t in targets if t in self._faults]
        for f in removed:
            f.stop.set()
            self._cleanup(f.type)
            log.warning("fault cleared", extra={"fault": f.type.value})
        return [f.type.value for f in removed]

    def get(self, ftype: FaultType) -> ActiveFault | None:
        with self._lock:
            fault = self._faults.get(ftype)
        if fault and fault.expires_at <= time.time():
            self.clear(ftype)
            return None
        return fault

    def active(self) -> list[ActiveFault]:
        return [f for t in list(self._faults) if (f := self.get(t)) is not None]

    # -------------------------------------------------- request-time hooks

    def request_delay_s(self) -> float:
        f = self.get(FaultType.LATENCY)
        if f and random.random() < float(f.params["probability"]):  # noqa: S311 - not crypto
            return float(f.params["ms"]) / 1000
        return 0.0

    def request_error(self) -> str | None:
        f = self.get(FaultType.ERROR_RATE)
        if f and random.random() < float(f.params["probability"]):  # noqa: S311
            return str(f.params["message"])
        return None

    def dependency_hang_s(self, target: str) -> float:
        f = self.get(FaultType.DEPENDENCY_TIMEOUT)
        if f and f.params["target"] == target:
            return float(f.params["hang_s"])
        return 0.0

    # ------------------------------------------------- background effects

    def _start_background(self, fault: ActiveFault) -> None:
        workers = {
            FaultType.MEMORY_LEAK: self._leak_memory,
            FaultType.CPU_HOG: self._burn_cpu,
            FaultType.DISK_FILL: self._fill_disk,
            FaultType.PROCESS_CRASH: self._crash,
        }
        if fault.type not in workers:
            return
        count = int(fault.params.get("threads", 1)) if fault.type is FaultType.CPU_HOG else 1
        for i in range(count):
            threading.Thread(
                target=workers[fault.type], args=(fault,), name=f"fault-{fault.type}-{i}", daemon=True
            ).start()

    def _leak_memory(self, fault: ActiveFault) -> None:
        step = int(fault.params["mb_per_s"])
        while not fault.stop.wait(1.0):
            if len(self._leak) + step > int(fault.params["max_mb"]):
                continue
            for _ in range(step):
                chunk = bytearray(1024 * 1024)
                chunk[::4096] = b"x" * len(chunk[::4096])  # touch pages so RSS really grows
                self._leak.append(chunk)

    def _burn_cpu(self, fault: ActiveFault) -> None:
        x = 0
        while not fault.stop.is_set():
            for _ in range(200_000):
                x = (x * 31 + 7) % 1_000_003

    def _fill_disk(self, fault: ActiveFault) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / ".fill"
        block = os.urandom(1024 * 1024)
        written = 0
        while not fault.stop.wait(1.0 / max(1, int(fault.params["mb_per_s"]))):
            if written >= int(fault.params["max_mb"]):
                continue
            try:
                # Open per write: no long-lived handle, so clearing the fault
                # (unlink) frees the space immediately. An open file's space is
                # only released when its last descriptor closes.
                with path.open("ab") as fh:
                    fh.write(block)
                written += 1
            except OSError as e:  # disk full: keep it full until cleared
                log.error("disk fill stopped: %s", e)
                fault.stop.wait()
                return

    def _crash(self, fault: ActiveFault) -> None:
        if fault.stop.wait(float(fault.params["delay_s"])):
            return
        log.critical("fatal error, process exiting", extra={"exit_code": fault.params["exit_code"]})
        logging.shutdown()
        os._exit(int(fault.params["exit_code"]))

    def _cleanup(self, ftype: FaultType) -> None:
        if ftype is FaultType.MEMORY_LEAK:
            self._leak.clear()
        elif ftype is FaultType.DISK_FILL:
            (self.data_dir / ".fill").unlink(missing_ok=True)
