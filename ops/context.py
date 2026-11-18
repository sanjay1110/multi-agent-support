"""Per-request context propagated via contextvars (works across threads/async)."""
from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Optional

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
session_id_var: ContextVar[str] = ContextVar("session_id", default="-")
customer_id_var: ContextVar[Optional[str]] = ContextVar("customer_id", default=None)


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:12]}"


@dataclass
class RequestMetrics:
    """Accumulates cost/latency for one request across all LLM and tool calls."""
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    spans: list[dict] = field(default_factory=list)

    def add_llm(self, input_tokens: int, output_tokens: int, cost: float) -> None:
        self.llm_calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cost_usd += cost


metrics_var: ContextVar[Optional[RequestMetrics]] = ContextVar("metrics", default=None)


def current_metrics() -> Optional[RequestMetrics]:
    return metrics_var.get()
