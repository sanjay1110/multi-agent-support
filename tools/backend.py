"""Simulated backend services (Order, Shipping, Payments, Insurance, Ticketing).

Backed by a local SQLite file seeded from ``tools/data/seed.json`` so that a
reviewer can run the whole system with zero external dependencies. In
production each method here would be replaced by an HTTP/gRPC client for the
real service — the tool layer and agents would not change.

Fault injection:
* ``ORD-9999`` simulates an upstream outage (raises BACKEND_UNAVAILABLE).
* ``SIM_LATENCY_MS`` adds artificial latency to every call.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Optional

from tools.schemas import ToolErrorCode

SEED_PATH = Path(__file__).parent / "data" / "seed.json"
OUTAGE_ORDERS = {"ORD-9999"}


class BackendError(Exception):
    def __init__(self, code: ToolErrorCode, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class SimulatedBackend:
    def __init__(self, db_path: str | Path, today: str = "2026-11-17",
                 latency_ms: int = 0, seed_path: Path = SEED_PATH):
        self.db_path = str(db_path)
        self.today = date.fromisoformat(today)
        self.latency_ms = latency_ms
        self._lock = threading.Lock()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        self._seed_if_empty(seed_path)

    # ------------------------------------------------------------------ setup
    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS customers (customer_id TEXT PRIMARY KEY, name TEXT, email TEXT, tier TEXT);
                CREATE TABLE IF NOT EXISTS orders (order_id TEXT PRIMARY KEY, customer_id TEXT, item TEXT,
                    price REAL, status TEXT, order_date TEXT, delivered_date TEXT);
                CREATE TABLE IF NOT EXISTS shipments (order_id TEXT PRIMARY KEY, carrier TEXT, tracking_number TEXT,
                    status TEXT, eta TEXT, last_scan TEXT, last_location TEXT);
                CREATE TABLE IF NOT EXISTS policies (order_id TEXT PRIMARY KEY, policy_id TEXT, covers TEXT,
                    coverage_limit REAL, deductible REAL);
                CREATE TABLE IF NOT EXISTS refunds (refund_id TEXT PRIMARY KEY, order_id TEXT, amount REAL,
                    reason TEXT, status TEXT, created_at TEXT);
                CREATE TABLE IF NOT EXISTS claims (claim_id TEXT PRIMARY KEY, order_id TEXT, incident_type TEXT,
                    description TEXT, payout_amount REAL, status TEXT, created_at TEXT);
                CREATE TABLE IF NOT EXISTS tickets (ticket_id TEXT PRIMARY KEY, customer_id TEXT, reason TEXT,
                    priority TEXT, status TEXT, created_at TEXT);
                """
            )

    def _seed_if_empty(self, seed_path: Path) -> None:
        cur = self._conn.execute("SELECT COUNT(*) FROM orders")
        if cur.fetchone()[0]:
            return
        seed = json.loads(Path(seed_path).read_text())
        with self._lock, self._conn:
            for c in seed["customers"]:
                self._conn.execute("INSERT INTO customers VALUES (:customer_id,:name,:email,:tier)", c)
            for o in seed["orders"]:
                self._conn.execute(
                    "INSERT INTO orders VALUES (:order_id,:customer_id,:item,:price,:status,:order_date,:delivered_date)", o)
            for s in seed["shipments"]:
                self._conn.execute(
                    "INSERT INTO shipments VALUES (:order_id,:carrier,:tracking_number,:status,:eta,:last_scan,:last_location)", s)
            for p in seed["policies"]:
                self._conn.execute("INSERT INTO policies VALUES (?,?,?,?,?)",
                                   (p["order_id"], p["policy_id"], json.dumps(p["covers"]),
                                    p["coverage_limit"], p["deductible"]))

    # ---------------------------------------------------------------- helpers
    def _simulate(self, order_id: Optional[str] = None) -> None:
        if self.latency_ms:
            time.sleep(self.latency_ms / 1000)
        if order_id in OUTAGE_ORDERS:
            raise BackendError(ToolErrorCode.BACKEND_UNAVAILABLE,
                               "Order service timed out (simulated outage).", retryable=True)

    def _one(self, sql: str, *args: Any) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _exec(self, sql: str, *args: Any) -> None:
        with self._lock, self._conn:
            self._conn.execute(sql, args)

    # ----------------------------------------------------------------- orders
    def get_order_owner(self, order_id: str) -> str:
        self._simulate(order_id)
        row = self._one("SELECT customer_id FROM orders WHERE order_id=?", order_id)
        if not row:
            raise BackendError(ToolErrorCode.NOT_FOUND, f"Order {order_id} not found.")
        return row["customer_id"]

    def get_order(self, order_id: str) -> dict[str, Any]:
        self._simulate(order_id)
        row = self._one("SELECT * FROM orders WHERE order_id=?", order_id)
        if not row:
            raise BackendError(ToolErrorCode.NOT_FOUND, f"Order {order_id} not found.")
        return row

    def get_shipment(self, order_id: str) -> dict[str, Any]:
        self._simulate(order_id)
        row = self._one("SELECT * FROM shipments WHERE order_id=?", order_id)
        if not row:
            raise BackendError(ToolErrorCode.NOT_FOUND, f"No shipment found for {order_id}.")
        return row

    def get_policy(self, order_id: str) -> Optional[dict[str, Any]]:
        self._simulate(order_id)
        row = self._one("SELECT * FROM policies WHERE order_id=?", order_id)
        if row:
            row["covers"] = json.loads(row["covers"])
        return row

    # ------------------------------------------------------------- mutations
    def create_refund(self, order_id: str, amount: float, reason: str) -> dict[str, Any]:
        self._simulate(order_id)
        refund_id = f"RF-{uuid.uuid4().hex[:8].upper()}"
        self._exec("INSERT INTO refunds VALUES (?,?,?,?,?,?)",
                   refund_id, order_id, amount, reason, "completed", self.today.isoformat())
        self._exec("UPDATE orders SET status='refunded' WHERE order_id=?", order_id)
        return {"refund_id": refund_id, "order_id": order_id, "amount": amount, "status": "completed"}

    def create_claim(self, order_id: str, incident_type: str, description: str,
                     payout: float) -> dict[str, Any]:
        self._simulate(order_id)
        claim_id = f"CL-{uuid.uuid4().hex[:8].upper()}"
        self._exec("INSERT INTO claims VALUES (?,?,?,?,?,?,?)", claim_id, order_id, incident_type,
                   description, payout, "approved_paid", self.today.isoformat())
        return {"claim_id": claim_id, "order_id": order_id, "incident_type": incident_type,
                "payout_amount": payout, "status": "approved_paid"}

    def has_claim(self, order_id: str) -> bool:
        return self._one("SELECT 1 FROM claims WHERE order_id=?", order_id) is not None

    def create_ticket(self, customer_id: Optional[str], reason: str, priority: str) -> dict[str, Any]:
        self._simulate()
        ticket_id = f"TCK-{uuid.uuid4().hex[:6].upper()}"
        self._exec("INSERT INTO tickets VALUES (?,?,?,?,?,?)", ticket_id, customer_id, reason,
                   priority, "open", self.today.isoformat())
        return {"ticket_id": ticket_id, "status": "open",
                "expected_response_hours": 4 if priority == "high" else 24}
