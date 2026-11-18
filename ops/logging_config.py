"""Structured JSON logging. Every line carries request_id + session_id."""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ops.context import request_id_var, session_id_var

_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
            "session_id": session_id_var.get(),
        }
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(level: str = "INFO", log_file: Optional[str] = None, stdout: bool = True) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())
    fmt = JsonFormatter()
    if stdout:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(fmt)
        root.addHandler(h)
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    if not root.handlers:
        root.addHandler(logging.NullHandler())
    for noisy in ("httpx", "httpcore", "anthropic", "uvicorn.access"):
        logging.getLogger(noisy).setLevel("WARNING")
