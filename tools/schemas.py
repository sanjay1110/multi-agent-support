"""Typed input/output contracts for every tool.

Inputs are strict (``extra="forbid"``) so an LLM cannot smuggle extra arguments
into a backend call. Outputs are explicit models so agents only ever see the
fields we choose to expose (no internal notes, no other customers' data).
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

ORDER_ID_PATTERN = r"^ORD-\d{4}$"


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class ToolErrorCode(str, Enum):
    INVALID_INPUT = "INVALID_INPUT"
    NOT_FOUND = "NOT_FOUND"
    PERMISSION_DENIED = "PERMISSION_DENIED"   # agent not allowed to use tool
    NOT_OWNER = "NOT_OWNER"                   # customer does not own resource
    BUSINESS_RULE = "BUSINESS_RULE"           # e.g. refund window expired
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    INTERNAL = "INTERNAL"


class ToolError(BaseModel):
    code: ToolErrorCode
    message: str
    retryable: bool = False


class ToolResult(BaseModel):
    """Uniform envelope returned to agents for every tool invocation."""
    tool: str
    ok: bool
    data: Optional[dict[str, Any]] = None
    error: Optional[ToolError] = None
    approval_id: Optional[str] = None
    latency_ms: float = 0.0

    def for_llm(self) -> dict[str, Any]:
        """Compact representation sent back to the model."""
        if self.ok:
            return {"ok": True, "data": self.data}
        assert self.error is not None
        return {"ok": False, "error": {"code": self.error.code.value,
                                       "message": self.error.message,
                                       "retryable": self.error.retryable}}


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OrderIdInput(_StrictInput):
    order_id: str = Field(pattern=ORDER_ID_PATTERN,
                          description="Order identifier in the form ORD-1234.")


class IssueRefundInput(OrderIdInput):
    amount: float = Field(gt=0, le=10_000, description="Refund amount in USD.")
    reason: str = Field(min_length=3, max_length=300,
                        description="Short customer-facing reason for the refund.")


class FileClaimInput(OrderIdInput):
    incident_type: Literal["damaged", "lost", "stolen"]
    description: str = Field(min_length=5, max_length=500,
                             description="What happened, in the customer's words (PII removed).")


class EscalateInput(_StrictInput):
    reason: str = Field(min_length=3, max_length=300)
    priority: Literal["low", "normal", "high"] = "normal"


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #
class OrderOut(BaseModel):
    order_id: str
    item: str
    price: float
    status: str
    order_date: str
    delivered_date: Optional[str] = None


class ShipmentOut(BaseModel):
    order_id: str
    carrier: str
    tracking_number: Optional[str]
    status: str
    eta: Optional[str]
    last_scan: Optional[str]
    last_location: Optional[str]


class RefundEligibilityOut(BaseModel):
    order_id: str
    eligible: bool
    refundable_amount: float
    reason: str


class RefundOut(BaseModel):
    refund_id: str
    order_id: str
    amount: float
    status: str


class PolicyOut(BaseModel):
    order_id: str
    insured: bool
    policy_id: Optional[str] = None
    covers: list[str] = []
    coverage_limit: float = 0.0
    deductible: float = 0.0


class ClaimOut(BaseModel):
    claim_id: str
    order_id: str
    incident_type: str
    payout_amount: float
    status: str


class EscalationOut(BaseModel):
    ticket_id: str
    status: str
    expected_response_hours: int
