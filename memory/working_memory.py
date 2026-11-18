"""Structured working memory: the facts the system has established in a session.

Unlike the transcript (session_store.py), this is a typed, compact record the
orchestrator reads on every turn — e.g. the active order, the last intent and
open approvals — so follow-ups like "refund it" resolve without re-asking, and
the LLM context stays small. Persisted as JSON in SQLite.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field


class WorkingMemory(BaseModel):
    session_id: str
    customer_id: Optional[str] = None
    active_order_id: Optional[str] = None
    mentioned_orders: list[str] = Field(default_factory=list)
    last_intent: Optional[str] = None
    last_agent: Optional[str] = None
    open_approvals: list[str] = Field(default_factory=list)
    escalated: bool = False
    facts: dict[str, Any] = Field(default_factory=dict)   # e.g. {"ORD-1001.refund_eligible": true}
    turn_count: int = 0
    updated_at: Optional[str] = None

    def remember_order(self, order_id: Optional[str]) -> None:
        if not order_id:
            return
        self.active_order_id = order_id
        if order_id not in self.mentioned_orders:
            self.mentioned_orders.append(order_id)

    def as_context(self) -> str:
        return (f"customer_id={self.customer_id or 'unknown'}; "
                f"active_order_id={self.active_order_id or 'none'}; "
                f"last_intent={self.last_intent or 'none'}; "
                f"open_approvals={','.join(self.open_approvals) or 'none'}; "
                f"known_facts={self.facts or '{}'}")


class WorkingMemoryStore:
    def __init__(self, db_path: str | Path):
        self._lock = threading.Lock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        with self._conn:
            self._conn.execute("CREATE TABLE IF NOT EXISTS working_memory "
                               "(session_id TEXT PRIMARY KEY, data TEXT)")

    def load(self, session_id: str, customer_id: Optional[str] = None) -> WorkingMemory:
        with self._lock:
            row = self._conn.execute("SELECT data FROM working_memory WHERE session_id=?",
                                     (session_id,)).fetchone()
        if row:
            return WorkingMemory.model_validate_json(row[0])
        return WorkingMemory(session_id=session_id, customer_id=customer_id)

    def save(self, wm: WorkingMemory) -> None:
        wm.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock, self._conn:
            self._conn.execute("INSERT OR REPLACE INTO working_memory VALUES (?,?)",
                               (wm.session_id, wm.model_dump_json()))
