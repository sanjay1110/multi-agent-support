"""Dependency wiring (composition root). One place builds the whole system,
so the API, CLI, tests and eval harness all run the exact same stack."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agents.llm import LLMClient, build_llm
from agents.orchestrator import Orchestrator
from app.config import Settings
from memory.session_store import SessionStore
from memory.working_memory import WorkingMemoryStore
from security.approval import ApprovalQueue
from security.audit import AuditLog
from tools.backend import SimulatedBackend
from tools.executor import ToolExecutor
from tools.support_tools import build_registry


@dataclass
class System:
    settings: Settings
    backend: SimulatedBackend
    audit: AuditLog
    approvals: ApprovalQueue
    sessions: SessionStore
    working: WorkingMemoryStore
    orchestrator: Orchestrator
    llm: LLMClient


def build_system(settings: Optional[Settings] = None, llm: Optional[LLMClient] = None) -> System:
    s = settings or Settings.from_env()
    data = Path(s.data_dir)
    data.mkdir(parents=True, exist_ok=True)
    backend = SimulatedBackend(data / "backend.db", today=s.sim_today, latency_ms=s.sim_latency_ms)
    registry = build_registry(backend)
    audit = AuditLog(data / "audit.jsonl")
    approvals = ApprovalQueue(data / "app.db", registry, audit)
    sessions = SessionStore(data / "app.db")
    working = WorkingMemoryStore(data / "app.db")
    executor = ToolExecutor(registry, backend, approvals, audit)
    llm = llm or build_llm(s)
    orch = Orchestrator(llm, registry, executor, sessions, working, audit, s.max_agent_steps)
    return System(s, backend, audit, approvals, sessions, working, orch, llm)
