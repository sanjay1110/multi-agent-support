"""Append-only, hash-chained audit log (JSON Lines).

Each entry stores the SHA-256 of the previous entry, so any edit or deletion
of a past line breaks the chain and is detected by ``verify()``.
Records: tool calls, permission denials, guardrail blocks, approval
requests and approval decisions.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ops.context import request_id_var, session_id_var

GENESIS = "0" * 64


def _hash(entry: dict[str, Any]) -> str:
    body = json.dumps({k: v for k, v in entry.items() if k != "hash"}, sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last = GENESIS
        if self.path.exists():
            lines = self.path.read_text().strip().splitlines()
            if lines:
                self._last = json.loads(lines[-1])["hash"]

    def record(self, event: str, actor: str, **details: Any) -> dict[str, Any]:
        with self._lock:
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "event": event,
                "actor": actor,
                "request_id": request_id_var.get(),
                "session_id": session_id_var.get(),
                "details": details,
                "prev_hash": self._last,
            }
            entry["hash"] = _hash(entry)
            with self.path.open("a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
            self._last = entry["hash"]
            return entry

    def read(self, limit: int = 100) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text().strip().splitlines()
        return [json.loads(l) for l in lines[-limit:]]

    def verify(self) -> tuple[bool, int]:
        """Return (chain_ok, number_of_entries)."""
        if not self.path.exists():
            return True, 0
        prev = GENESIS
        n = 0
        for n, line in enumerate(self.path.read_text().strip().splitlines(), 1):
            e = json.loads(line)
            if e["prev_hash"] != prev or _hash(e) != e["hash"]:
                return False, n
            prev = e["hash"]
        return True, n
