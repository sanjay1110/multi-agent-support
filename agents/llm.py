"""LLM provider layer.

``AnthropicLLM`` — production path: Claude via the official Anthropic SDK,
native tool use, automatic retries, token usage -> cost.

``MockLLM`` — deterministic offline stand-in that speaks the *same* protocol
(returns text and/or tool_use blocks). It lets reviewers and CI run the full
pipeline — router, specialists, tool executor, approvals, memory, evals —
without an API key. It is NOT used when ANTHROPIC_API_KEY is set (LLM_MODE=auto).
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from ops.context import current_metrics
from ops.tracing import span

log = logging.getLogger("agents.llm")


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    content: list[dict[str, Any]] = field(default_factory=list)  # raw blocks for the transcript
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = "end_turn"
    model: str = ""


class LLMClient(Protocol):
    mode: str

    def complete(self, *, agent: str, system: str, messages: list[dict[str, Any]],
                 tools: Optional[list[dict[str, Any]]] = None,
                 tool_choice: Optional[dict[str, Any]] = None) -> LLMResponse: ...


# =========================================================================== #
# Anthropic (production)
# =========================================================================== #
class AnthropicLLM:
    mode = "anthropic"

    def __init__(self, api_key: str, model: str, router_model: str, max_tokens: int,
                 prices: dict[str, tuple[float, float]]):
        import anthropic  # imported lazily so mock mode needs no SDK install
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=60.0)
        self.model = model
        self.router_model = router_model
        self.max_tokens = max_tokens
        self.prices = prices

    def complete(self, *, agent, system, messages, tools=None, tool_choice=None) -> LLMResponse:
        model = self.router_model if agent == "router" else self.model
        kwargs: dict[str, Any] = dict(model=model, max_tokens=self.max_tokens, system=system,
                                      messages=messages)
        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        with span("llm.call", agent=agent, model=model, provider="anthropic") as sp:
            resp = self._client.messages.create(**kwargs)
            out = LLMResponse(model=model, stop_reason=resp.stop_reason or "",
                              input_tokens=resp.usage.input_tokens,
                              output_tokens=resp.usage.output_tokens)
            for block in resp.content:
                if block.type == "text":
                    out.text += block.text
                    out.content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    out.tool_calls.append(ToolCall(block.id, block.name, dict(block.input)))
                    out.content.append({"type": "tool_use", "id": block.id, "name": block.name,
                                        "input": dict(block.input)})
            sp.update(input_tokens=out.input_tokens, output_tokens=out.output_tokens)
        _account(out, self.prices["router" if agent == "router" else "default"])
        return out


# =========================================================================== #
# Mock (offline, deterministic)
# =========================================================================== #
_ORDER_RE = re.compile(r"\bORD-[A-Za-z0-9]+\b", re.I)
_AMOUNT_RE = re.compile(r"\$\s?(\d+(?:\.\d{1,2})?)")
_KEYWORDS = {
    "escalate": ["human", "real person", "manager", "supervisor", "representative", "lawyer", "sue "],
    "insurance": ["insur", "claim", "damaged", "broken", "cracked", "shattered", "stolen", "lost",
                  "never arrived", "coverage", "covered"],
    "refund": ["refund", "money back", "return", "cancel", "reimburse", "chargeback"],
    "tracking": ["where", "track", "status", "ship", "when will", "deliver", "arrive", "package", "eta"],
}
_FOLLOWUP = re.compile(r"\b(it|that|this|same)\b", re.I)


def _est_tokens(obj: Any) -> int:
    return max(1, len(json.dumps(obj, default=str)) // 4)


class MockLLM:
    mode = "mock"

    def __init__(self, prices: dict[str, tuple[float, float]]):
        self.prices = prices

    def complete(self, *, agent, system, messages, tools=None, tool_choice=None) -> LLMResponse:
        with span("llm.call", agent=agent, model="mock", provider="mock"):
            task, results = _parse_transcript(messages)
            policy = {"router": self._route, "tracking": self._tracking,
                      "refund": self._refund, "insurance": self._insurance}[agent]
            out = policy(task, results)
            out.model = "mock"
            out.input_tokens = _est_tokens(system) + _est_tokens(messages) + _est_tokens(tools or [])
            out.output_tokens = _est_tokens(out.content)
        _account(out, self.prices["router" if agent == "router" else "default"])
        return out

    # ------------------------------------------------------------- router
    def _route(self, task: str, _results: list) -> LLMResponse:
        msg, ctx = _split_context(task)
        low = msg.lower()
        dest = next((d for d, kws in _KEYWORDS.items() if any(k in low for k in kws)), None)
        order = _first_order(msg)
        if dest is None and ctx.get("last_intent") in ("tracking", "refund", "insurance") \
                and (order or _FOLLOWUP.search(msg)):
            dest = ctx["last_intent"]
        dest = dest or "general"
        if order is None and ctx.get("active_order_id") not in (None, "none"):
            order = ctx["active_order_id"]
        return _tool("route", {"destination": dest, "order_id": order,
                               "reason": f"keyword routing -> {dest}"})

    # ------------------------------------------------------------- tracking
    def _tracking(self, task: str, results: list) -> LLMResponse:
        msg, ctx = _split_context(task)
        order = _first_order(msg) or ctx.get("active_order_id")
        if not results:
            return _tool("track_shipment", {"order_id": order})
        name, _inp, res = results[-1]
        if not res["ok"]:
            return _text(_error_text(res, order))
        s = res["data"]
        status = s["status"]
        if status == "delivered":
            t = f"Good news — order {order} was delivered on {_d(s['last_scan'])} in {s['last_location']} ({s['carrier']} tracking {s['tracking_number']})."
        elif status == "in_transit":
            t = f"Order {order} is in transit with {s['carrier']} (tracking {s['tracking_number']}). It was last scanned in {s['last_location']} on {_d(s['last_scan'])} and is expected to arrive {_d(s['eta'])}."
        elif status == "delayed":
            t = f"I'm sorry — order {order} is delayed. {s['carrier']} last scanned it in {s['last_location']} on {_d(s['last_scan'])}; the new estimated delivery is {_d(s['eta'])}. If it hasn't arrived within 7 days of that date, we can help you file a lost-package claim."
        elif status == "lost":
            t = f"I'm sorry — {s['carrier']} has marked order {order} as lost. Our Insurance team can help you file a claim."
        elif status == "label_created":
            t = f"Order {order} is being prepared at our warehouse and hasn't shipped yet. Estimated delivery is {_d(s['eta'])}."
        else:
            t = f"Order {order} status: {status}."
        return _text(t)

    # ------------------------------------------------------------- refund
    def _refund(self, task: str, results: list) -> LLMResponse:
        msg, ctx = _split_context(task)
        order = _first_order(msg) or ctx.get("active_order_id")
        if not results:
            return _tool("check_refund_eligibility", {"order_id": order})
        name, inp, res = results[-1]
        if not res["ok"]:
            return _text(_error_text(res, order))
        d = res["data"]
        if name == "check_refund_eligibility":
            if not d["eligible"]:
                alt = ""
                if "insurance" in d["reason"].lower():
                    alt = " I can connect you with our Insurance team to file a claim."
                elif "transit" in d["reason"].lower():
                    alt = " Once it's delivered you'll have 30 days to request a refund."
                return _text(f"I'm sorry, order {order} isn't eligible for a refund: {d['reason']}{alt}")
            requested = _AMOUNT_RE.search(msg)
            amount = d["refundable_amount"]
            if requested and float(requested.group(1)) < amount:
                amount = float(requested.group(1))
            return _tool("issue_refund", {"order_id": order, "amount": round(amount, 2),
                                          "reason": _reason(msg)})
        if name == "issue_refund":
            elig = results[0][2]["data"]
            req = _AMOUNT_RE.search(msg)
            note = (f" (the maximum refundable is the order total of ${elig['refundable_amount']:.2f})"
                    if req and float(req.group(1)) > elig["refundable_amount"] else "")
            return _text(f"I've submitted a refund of ${d['amount']:.2f} for order {order}{note}. "
                         f"It's now awaiting review by our team (reference {d['approval_id']}); you'll be "
                         f"notified within 1 business day.")
        return _text("Your request has been processed.")

    # ------------------------------------------------------------- insurance
    def _insurance(self, task: str, results: list) -> LLMResponse:
        msg, ctx = _split_context(task)
        order = _first_order(msg) or ctx.get("active_order_id")
        low = msg.lower()
        if not results:
            return _tool("get_insurance_policy", {"order_id": order})
        name, inp, res = results[-1]
        if not res["ok"]:
            if res["error"]["code"] == "BUSINESS_RULE":
                return _text(f"I'm sorry, I can't file that claim: {res['error']['message']} "
                             f"I can connect you with a specialist if you'd like.")
            return _text(_error_text(res, order))
        d = res["data"]
        if name == "get_insurance_policy":
            if not d["insured"]:
                return _text(f"Order {order} doesn't have shipping insurance, so I can't file a claim. "
                             f"If it was delivered within the last 30 days, our Refunds team may still be able to help.")
            incident = ("stolen" if "stolen" in low else
                        "lost" if any(k in low for k in ("lost", "never arrived", "missing")) else
                        "damaged" if any(k in low for k in ("damaged", "broken", "cracked", "shattered")) else None)
            if incident is None:
                return _text(f"Order {order} is insured under policy {d['policy_id']}, covering "
                             f"{', '.join(d['covers'])} up to ${d['coverage_limit']:.2f} with a "
                             f"${d['deductible']:.2f} deductible.")
            return _tool("file_insurance_claim", {"order_id": order, "incident_type": incident,
                                                  "description": _reason(msg, 300)})
        if name == "file_insurance_claim":
            return _text(f"I've filed a '{d['incident_type']}' claim for order {order}. It's awaiting review "
                         f"(reference {d['approval_id']}); you'll hear back within 2 business days.")
        return _text("Your claim has been processed.")


# =========================================================================== #
# helpers
# =========================================================================== #
def _account(out: LLMResponse, price: tuple[float, float]) -> None:
    cost = out.input_tokens / 1e6 * price[0] + out.output_tokens / 1e6 * price[1]
    m = current_metrics()
    if m is not None:
        m.add_llm(out.input_tokens, out.output_tokens, cost)


def _tool(name: str, inp: dict[str, Any]) -> LLMResponse:
    tid = f"toolu_mock_{uuid.uuid4().hex[:10]}"
    return LLMResponse(tool_calls=[ToolCall(tid, name, inp)],
                       content=[{"type": "tool_use", "id": tid, "name": name, "input": inp}],
                       stop_reason="tool_use")


def _text(t: str) -> LLMResponse:
    return LLMResponse(text=t, content=[{"type": "text", "text": t}])


def _parse_transcript(messages: list[dict[str, Any]]) -> tuple[str, list[tuple[str, dict, dict]]]:
    """Return the latest user task text and the tool results that followed it."""
    task_idx = 0
    for i, m in enumerate(messages):
        if m["role"] == "user" and (isinstance(m["content"], str) or
                                    any(b.get("type") == "text" for b in m["content"])):
            task_idx = i
    m = messages[task_idx]
    task = m["content"] if isinstance(m["content"], str) else \
        " ".join(b["text"] for b in m["content"] if b.get("type") == "text")
    calls: dict[str, tuple[str, dict]] = {}
    results = []
    for m in messages[task_idx + 1:]:
        if isinstance(m["content"], str):
            continue
        for b in m["content"]:
            if b.get("type") == "tool_use":
                calls[b["id"]] = (b["name"], b["input"])
            elif b.get("type") == "tool_result":
                name, inp = calls.get(b["tool_use_id"], ("?", {}))
                content = b["content"]
                if isinstance(content, list):
                    content = content[0].get("text", "{}")
                results.append((name, inp, json.loads(content)))
    return task, results


def _split_context(task: str) -> tuple[str, dict[str, str]]:
    """Tasks are '<customer message>\n\n[context] k=v; k=v'."""
    if "[context]" not in task:
        return task, {}
    msg, ctx = task.split("[context]", 1)
    pairs = {}
    for part in ctx.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            pairs[k.strip()] = v.strip()
    msg = msg.replace("Customer message:", "").strip()
    return msg, pairs


def _first_order(text: str) -> Optional[str]:
    m = _ORDER_RE.search(text)
    return m.group(0).upper() if m else None


def _reason(msg: str, n: int = 200) -> str:
    r = re.sub(r"\s+", " ", msg).strip()
    return (r[:n] if len(r) >= 5 else "Customer request")


def _d(iso: Optional[str]) -> str:
    if not iso:
        return "soon"
    from datetime import date
    dt = date.fromisoformat(iso)
    return dt.strftime("%b ") + str(dt.day)


def _error_text(res: dict, order: Optional[str]) -> str:
    code = res["error"]["code"]
    if code == "NOT_OWNER":
        return (f"I couldn't find order {order} on your account. Could you double-check the order number?")
    if code == "NOT_FOUND":
        return f"I couldn't find an order with the number {order}. Could you double-check it?"
    if code == "INVALID_INPUT":
        return (f"'{order}' doesn't look like a valid order number. Order numbers look like ORD-1234 — "
                f"could you check your confirmation email?")
    if code == "BACKEND_UNAVAILABLE":
        return ("I'm sorry — our order system is temporarily unavailable. Please try again in a few "
                "minutes, or I can connect you with a specialist.")
    return "I'm sorry, something went wrong on our side. Please try again shortly."


def build_llm(settings) -> LLMClient:
    prices = {"default": (settings.price_input_per_mtok, settings.price_output_per_mtok),
              "router": (settings.router_price_input_per_mtok, settings.router_price_output_per_mtok)}
    if settings.resolved_llm_mode == "anthropic":
        if not settings.anthropic_api_key:
            raise RuntimeError("LLM_MODE=anthropic but ANTHROPIC_API_KEY is not set.")
        return AnthropicLLM(settings.anthropic_api_key, settings.model, settings.router_model,
                            settings.max_tokens, prices)
    return MockLLM(prices)
