"""Human-in-the-loop approval queue for irreversible actions.

Agents never execute ``irreversible`` tools directly. The executor stores the
validated arguments here with status ``pending``; a human approver (via API
``POST /v1/approvals/{id}/decision`` or the CLI) approves or rejects it. Only
then is the tool executed — with the exact arguments that were reviewed.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from security.audit import AuditLog
from tools.registry import ToolRegistry


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ApprovalQueue:
    def __init__(self, db_path: str | Path, registry: ToolRegistry, audit: AuditLog):
        self.registry = registry
        self.audit = audit
        self._lock = threading.Lock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY, created_at TEXT, request_id TEXT, session_id TEXT,
                    customer_id TEXT, agent TEXT, tool TEXT, args TEXT, summary TEXT, status TEXT,
                    decided_by TEXT, decided_at TEXT, note TEXT, result TEXT)""")

    def create(self, *, tool: str, args: dict[str, Any], agent: str, session_id: str,
               customer_id: Optional[str], request_id: str, summary: str) -> str:
        approval_id = f"APR-{uuid.uuid4().hex[:8].upper()}"
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (approval_id, _now(), request_id, session_id, customer_id, agent, tool,
                 json.dumps(args), summary, "pending", None, None, None, None))
        self.audit.record("approval_requested", actor=agent, approval_id=approval_id,
                          tool=tool, args=args, customer_id=customer_id)
        return approval_id

    def get(self, approval_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM approvals WHERE approval_id=?",
                                     (approval_id,)).fetchone()
        return self._row(row) if row else None

    def list(self, status: Optional[str] = "pending", session_id: Optional[str] = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM approvals WHERE 1=1", []
        if status:
            sql += " AND status=?"
            args.append(status)
        if session_id:
            sql += " AND session_id=?"
            args.append(session_id)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY created_at", args).fetchall()
        return [self._row(r) for r in rows]

    def decide(self, approval_id: str, *, approve: bool, approver: str,
               note: str = "") -> dict[str, Any]:
        rec = self.get(approval_id)
        if rec is None:
            raise KeyError(approval_id)
        if rec["status"] != "pending":
            raise ValueError(f"Approval {approval_id} is already {rec['status']}.")
        if not approve:
            self._update(approval_id, "rejected", approver, note, None)
            self.audit.record("approval_rejected", actor=approver, approval_id=approval_id,
                              tool=rec["tool"], note=note)
            return self.get(approval_id)  # type: ignore[return-value]

        spec = self.registry.get(rec["tool"])
        assert spec is not None
        result = self.registry.run(spec, spec.input_model.model_validate(rec["args"]))
        status = "executed" if result.ok else "failed"
        self._update(approval_id, status, approver, note, result.model_dump(mode="json"))
        self.audit.record("approval_executed" if result.ok else "approval_execution_failed",
                          actor=approver, approval_id=approval_id, tool=rec["tool"],
                          args=rec["args"], result=result.model_dump(mode="json"))
        return self.get(approval_id)  # type: ignore[return-value]

    # ------------------------------------------------------------- internals
    def _update(self, approval_id: str, status: str, approver: str, note: str,
                result: Optional[dict[str, Any]]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE approvals SET status=?, decided_by=?, decided_at=?, note=?, result=? "
                "WHERE approval_id=?",
                (status, approver, _now(), note, json.dumps(result) if result else None, approval_id))

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["args"] = json.loads(d["args"])
        d["result"] = json.loads(d["result"]) if d["result"] else None
        return d
