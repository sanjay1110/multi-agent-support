"""Session memory: the conversation transcript per session (short-term memory).

Only *sanitized* text (after PII redaction) is stored. A session is bound to
the customer that created it; another customer re-using the session id is
rejected (session-fixation / hijack protection).
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class SessionOwnershipError(PermissionError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SessionStore:
    def __init__(self, db_path: str | Path, max_history: int = 12):
        self.max_history = max_history
        self._lock = threading.Lock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, customer_id TEXT,
                    created_at TEXT, updated_at TEXT);
                CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT, role TEXT, content TEXT, agent TEXT, request_id TEXT, ts TEXT);
                CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
                """)

    def ensure_session(self, session_id: str, customer_id: Optional[str]) -> None:
        with self._lock, self._conn:
            row = self._conn.execute("SELECT customer_id FROM sessions WHERE session_id=?",
                                     (session_id,)).fetchone()
            if row is None:
                self._conn.execute("INSERT INTO sessions VALUES (?,?,?,?)",
                                   (session_id, customer_id, _now(), _now()))
            elif row["customer_id"] != customer_id:
                raise SessionOwnershipError("Session belongs to a different customer.")
            else:
                self._conn.execute("UPDATE sessions SET updated_at=? WHERE session_id=?",
                                   (_now(), session_id))

    def append(self, session_id: str, role: str, content: str, agent: Optional[str] = None,
               request_id: Optional[str] = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO messages (session_id, role, content, agent, request_id, ts) "
                "VALUES (?,?,?,?,?,?)", (session_id, role, content, agent, request_id, _now()))

    def history(self, session_id: str, limit: Optional[int] = None) -> list[dict[str, str]]:
        """Most recent turns, oldest first, in Anthropic ``messages`` format."""
        limit = limit or self.max_history
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content, agent FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, limit)).fetchall()
        msgs = [{"role": r["role"], "content": r["content"], "agent": r["agent"]} for r in reversed(rows)]
        # Anthropic requires the first message to be from the user.
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        return msgs
