"""Orchestrator (supervisor) — the entry point for every customer message.

    message ─► input guardrail ─► load memory ─► LLM router ─► slot check
            ─► specialist agent (tool loop via secure executor)
            ─► output guardrail ─► persist memory ─► response + metrics
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from agents.base import AgentResult
from agents.llm import LLMClient
from agents.prompts import ROUTER_PROMPT
from agents.specialists import SPECIALISTS
from memory.session_store import SessionStore
from memory.working_memory import WorkingMemory, WorkingMemoryStore
from ops.context import (RequestMetrics, customer_id_var, metrics_var, new_request_id,
                         request_id_var, session_id_var)
from ops.tracing import span
from security import guardrails
from security.audit import AuditLog
from tools.executor import ToolExecutor
from tools.registry import ToolRegistry
from tools.schemas import ToolErrorCode, ToolResult

log = logging.getLogger("agents.orchestrator")

DESTINATIONS = ["tracking", "refund", "insurance", "escalate", "general"]
ROUTE_TOOL = {
    "name": "route",
    "description": "Route the customer's message to exactly one destination.",
    "input_schema": {
        "type": "object",
        "properties": {
            "destination": {"type": "string", "enum": DESTINATIONS},
            "order_id": {"type": ["string", "null"], "description": "ORD-1234 if known, else null"},
            "reason": {"type": "string", "description": "One short sentence explaining the choice."},
        },
        "required": ["destination", "order_id", "reason"],
    },
}
_ORDER_RE = re.compile(r"\bORD-[A-Za-z0-9]+\b", re.I)


@dataclass
class ChatResponse:
    request_id: str
    session_id: str
    agent: str
    reply: str
    outcome: str
    order_id: Optional[str] = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    approvals: list[str] = field(default_factory=list)
    guardrail: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class Orchestrator:
    def __init__(self, llm: LLMClient, registry: ToolRegistry, executor: ToolExecutor,
                 sessions: SessionStore, working: WorkingMemoryStore, audit: AuditLog,
                 max_steps: int = 6):
        self.llm = llm
        self.executor = executor
        self.sessions = sessions
        self.working = working
        self.audit = audit
        self.agents = {name: cls(llm, registry, executor, max_steps) for name, cls in SPECIALISTS.items()}

    # ===================================================================== API
    def handle(self, message: str, customer_id: Optional[str],
               session_id: Optional[str] = None) -> ChatResponse:
        session_id = session_id or f"sess_{uuid.uuid4().hex[:10]}"
        rid = new_request_id()
        tokens = [request_id_var.set(rid), session_id_var.set(session_id),
                  customer_id_var.set(customer_id)]
        metrics = RequestMetrics()
        mtoken = metrics_var.set(metrics)
        t0 = time.perf_counter()
        try:
            with span("request", customer_id=customer_id or "anon") as sp:
                resp = self._handle(message, customer_id, session_id, rid)
                sp.update(agent=resp.agent, outcome=resp.outcome)
            resp.latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            resp.usage = {"llm_calls": metrics.llm_calls, "input_tokens": metrics.input_tokens,
                          "output_tokens": metrics.output_tokens, "tool_calls": metrics.tool_calls,
                          "cost_usd": round(metrics.cost_usd, 6), "llm_mode": self.llm.mode}
            log.info("request_complete", extra={
                "agent": resp.agent, "outcome": resp.outcome, "latency_ms": resp.latency_ms,
                **{f"usage_{k}": v for k, v in resp.usage.items()}})
            return resp
        finally:
            metrics_var.reset(mtoken)
            for t, var in zip(tokens, (request_id_var, session_id_var, customer_id_var)):
                var.reset(t)

    # ================================================================ pipeline
    def _handle(self, message: str, customer_id: Optional[str], session_id: str,
                rid: str) -> ChatResponse:
        # 1. input guardrail
        g = guardrails.check_input(message)
        gmeta = {"violations": g.violations, "redactions": g.redactions}
        self.sessions.ensure_session(session_id, customer_id)
        wm = self.working.load(session_id, customer_id)

        if not g.allowed:
            self.audit.record("guardrail_block", actor="orchestrator", violations=g.violations)
            log.warning("guardrail_block", extra={"violations": g.violations})
            return self._finish(wm, session_id, rid, g.sanitized_text or "[blocked]",
                                guardrails.BLOCKED_REPLY, "guardrail", "blocked", None, [], gmeta)

        text = g.sanitized_text
        history = self.sessions.history(session_id)

        # 2. route (LLM supervisor)
        dest, order_id, reason = self._route(text, history, wm)

        # 3. dispatch
        if dest == "escalate":
            tr = self.executor.execute("orchestrator", "escalate_to_human",
                                       {"reason": _clip(text), "priority": "high"})
            reply = (f"I've connected you with a human specialist (ticket {tr.data['ticket_id']}). "
                     f"Someone will reach out within {tr.data['expected_response_hours']} hours."
                     if tr.ok else "I'm sorry, I couldn't reach a specialist right now. Please try again shortly.")
            wm.escalated = tr.ok
            calls = [{"tool": "escalate_to_human", "input": {"reason": _clip(text)}, "ok": tr.ok,
                      "error": tr.error.code.value if tr.error else None, "approval_id": None}]
            return self._finish(wm, session_id, rid, text, reply, "orchestrator",
                                _outcome("escalate", [tr]), order_id, calls, gmeta, [tr])

        if dest == "general":
            if order_id and self._owns(order_id, customer_id):
                wm.remember_order(order_id)
            else:
                order_id = None
            reply = (f"Happy to help with order {order_id}! Would you like to track it, request a refund, "
                     f"or file a shipping-insurance claim?" if order_id else
                     "Hi! I can help you track an order, request a refund, or file a shipping-insurance "
                     "claim. What do you need help with, and what's your order number (e.g. ORD-1234)?")
            return self._finish(wm, session_id, rid, text, reply, "orchestrator", "needs_info",
                                order_id, [], gmeta)

        if not order_id:   # slot filling: specialists need an order id
            wm.last_intent = dest
            reply = {"tracking": "I can check on that for you. What's your order number? It looks like ORD-1234.",
                     "refund": "I can help with a refund. Which order is it for? (e.g. ORD-1234)",
                     "insurance": "I can help with a shipping-insurance claim. What's the order number? (e.g. ORD-1234)"}[dest]
            return self._finish(wm, session_id, rid, text, reply, dest, "needs_info", None, [], gmeta)

        task = (f"Customer message: {text}\n\n[context] customer_id={customer_id}; "
                f"order_id={order_id}; active_order_id={order_id}; last_intent={wm.last_intent or 'none'}")
        agent_hist = [{"role": m["role"], "content": m["content"]} for m in history]
        result: AgentResult = self.agents[dest].run(agent_hist, task)
        wm.last_intent = dest
        bad = {ToolErrorCode.NOT_OWNER, ToolErrorCode.NOT_FOUND, ToolErrorCode.INVALID_INPUT}
        if not any(r.error and r.error.code in bad for r in result.tool_results):
            wm.remember_order(order_id)   # only remember orders verified to belong to this customer
        self._update_facts(wm, result.tool_results)
        return self._finish(wm, session_id, rid, text, result.reply, dest,
                            _outcome(dest, result.tool_results), order_id, result.tool_calls, gmeta,
                            result.tool_results)

    # ================================================================ routing
    def _route(self, text: str, history: list[dict], wm: WorkingMemory) -> tuple[str, Optional[str], str]:
        recent = "\n".join(f"{m['role']}: {m['content'][:300]}" for m in history[-6:]) or "(none)"
        prompt = (f"Customer message: {text}\n\n[context] {wm.as_context()}\n\n"
                  f"Recent conversation:\n{recent}")
        resp = self.llm.complete(agent="router", system=ROUTER_PROMPT,
                                 messages=[{"role": "user", "content": prompt}],
                                 tools=[ROUTE_TOOL], tool_choice={"type": "tool", "name": "route"})
        call = next((c for c in resp.tool_calls if c.name == "route"), None)
        dest, order_id, reason = "general", None, "router returned no decision"
        if call:
            dest = call.input.get("destination") if call.input.get("destination") in DESTINATIONS else "general"
            order_id = call.input.get("order_id") or None
            reason = call.input.get("reason", "")
        # Deterministic safety net: an explicit id in the message always wins over the model.
        m = _ORDER_RE.search(text)
        if m:
            order_id = m.group(0).upper()
        elif order_id and order_id.upper() not in (wm.active_order_id or "").upper():
            order_id = wm.active_order_id   # never let the model invent an order id
        log.info("routed", extra={"destination": dest, "order_id": order_id, "reason": reason})
        return dest, order_id, reason

    # ================================================================ helpers
    def _owns(self, order_id: str, customer_id: Optional[str]) -> bool:
        try:
            return self.executor.backend.get_order_owner(order_id) == customer_id
        except Exception:  # noqa: BLE001
            return False

    def _update_facts(self, wm: WorkingMemory, results: list[ToolResult]) -> None:
        for r in results:
            if r.ok and r.approval_id:
                wm.open_approvals.append(r.approval_id)
            if r.ok and r.tool == "check_refund_eligibility" and r.data:
                wm.facts[f"{r.data['order_id']}.refund_eligible"] = r.data["eligible"]
            if r.ok and r.tool == "get_insurance_policy" and r.data:
                wm.facts[f"{r.data['order_id']}.insured"] = r.data["insured"]
            if r.ok and r.tool == "track_shipment" and r.data:
                wm.facts[f"{r.data['order_id']}.shipment_status"] = r.data["status"]

    def _finish(self, wm: WorkingMemory, session_id: str, rid: str, user_text: str, reply: str,
                agent: str, outcome: str, order_id: Optional[str], calls: list, gmeta: dict,
                results: Optional[list[ToolResult]] = None) -> ChatResponse:
        reply = guardrails.check_output(reply)
        self.sessions.append(session_id, "user", user_text, request_id=rid)
        self.sessions.append(session_id, "assistant", reply, agent=agent, request_id=rid)
        wm.last_agent = agent
        wm.turn_count += 1
        self.working.save(wm)
        approvals = [r.approval_id for r in (results or []) if r.approval_id]
        return ChatResponse(request_id=rid, session_id=session_id, agent=agent, reply=reply,
                            outcome=outcome, order_id=order_id, tool_calls=calls,
                            approvals=approvals, guardrail=gmeta)


def _outcome(dest: str, results: list[ToolResult]) -> str:
    """Business-level outcome derived from tool results (model-independent)."""
    codes = {r.error.code for r in results if r.error}
    if codes & {ToolErrorCode.NOT_OWNER, ToolErrorCode.PERMISSION_DENIED}:
        return "denied"
    if any(r.approval_id for r in results):
        return "pending_approval"
    if any(r.tool == "escalate_to_human" and r.ok for r in results):
        return "escalated"
    if codes & {ToolErrorCode.BACKEND_UNAVAILABLE, ToolErrorCode.NOT_FOUND,
                ToolErrorCode.INVALID_INPUT, ToolErrorCode.INTERNAL}:
        return "error_handled"
    if ToolErrorCode.BUSINESS_RULE in codes:
        return "ineligible"
    for r in results:
        if r.ok and r.tool == "check_refund_eligibility" and r.data and not r.data["eligible"]:
            return "ineligible"
        if r.ok and r.tool == "get_insurance_policy" and r.data and not r.data["insured"]:
            return "ineligible"
    return "resolved" if results else "answered"


def _clip(s: str, n: int = 280) -> str:
    s = s.strip()
    return s[:n] if len(s) >= 3 else "Customer requested a human"
