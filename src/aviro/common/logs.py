"""JSON logs on stdout, with OpenTelemetry trace/span ids when tracing is active."""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_STD = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "service": self.service,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Added by opentelemetry-instrumentation-logging when log correlation is on.
        trace_id = getattr(record, "otelTraceID", "0")
        if trace_id and trace_id != "0":
            out["trace_id"] = trace_id
            out["span_id"] = getattr(record, "otelSpanID", None)
        for key, value in record.__dict__.items():
            if key not in _STD and not key.startswith("otel"):
                out[key] = value
        if record.exc_info:
            out["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            out["stack"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup_logging(service: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    # Keep the OTel log handler (if auto-instrumentation installed one); replace console handlers.
    root.handlers = [h for h in root.handlers if type(h).__module__.startswith("opentelemetry")]
    root.addHandler(handler)
    root.setLevel(level)
    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).setLevel(logging.WARNING)
