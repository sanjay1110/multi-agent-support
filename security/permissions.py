"""Permission boundaries: least-privilege tool allow-lists per agent.

An agent can only *see* (in its tool schema) and *call* the tools listed here.
Even if a model hallucinates or is manipulated into calling another tool, the
executor rejects it with PERMISSION_DENIED and writes an audit record.
"""
from __future__ import annotations

AGENT_TOOL_ALLOWLIST: dict[str, frozenset[str]] = {
    "orchestrator": frozenset({"escalate_to_human"}),
    "tracking": frozenset({"get_order", "track_shipment", "escalate_to_human"}),
    "refund": frozenset({"get_order", "check_refund_eligibility", "issue_refund", "escalate_to_human"}),
    "insurance": frozenset({"get_order", "get_insurance_policy", "file_insurance_claim",
                            "escalate_to_human"}),
}

# Roles allowed to decide on approvals (enforced at the API layer by ADMIN_TOKEN).
APPROVER_ROLES = frozenset({"support_lead", "finance"})


def allowed_tools(agent: str) -> list[str]:
    return sorted(AGENT_TOOL_ALLOWLIST.get(agent, frozenset()))


def is_allowed(agent: str, tool: str) -> bool:
    return tool in AGENT_TOOL_ALLOWLIST.get(agent, frozenset())
