"""Concrete customer-support tools bound to the simulated backend."""
from __future__ import annotations

from datetime import date

from ops.context import customer_id_var
from tools.backend import BackendError, SimulatedBackend
from tools.registry import ToolRegistry, ToolSpec
from tools.schemas import (ClaimOut, EscalateInput, EscalationOut, FileClaimInput,
                           IssueRefundInput, OrderIdInput, OrderOut, PolicyOut,
                           RefundEligibilityOut, RefundOut, ShipmentOut, ToolErrorCode)

REFUND_WINDOW_DAYS = 30


def build_registry(backend: SimulatedBackend) -> ToolRegistry:
    reg = ToolRegistry()

    # ------------------------------------------------------------- read tools
    def get_order(inp: OrderIdInput) -> OrderOut:
        o = backend.get_order(inp.order_id)
        return OrderOut(**{k: o[k] for k in OrderOut.model_fields})

    def track_shipment(inp: OrderIdInput) -> ShipmentOut:
        return ShipmentOut(**backend.get_shipment(inp.order_id))

    def check_refund_eligibility(inp: OrderIdInput) -> RefundEligibilityOut:
        o = backend.get_order(inp.order_id)
        eligible, reason = _refund_rule(o, backend.today)
        return RefundEligibilityOut(order_id=o["order_id"], eligible=eligible,
                                    refundable_amount=o["price"] if eligible else 0.0, reason=reason)

    def get_insurance_policy(inp: OrderIdInput) -> PolicyOut:
        backend.get_order(inp.order_id)  # existence check
        p = backend.get_policy(inp.order_id)
        if not p:
            return PolicyOut(order_id=inp.order_id, insured=False)
        return PolicyOut(order_id=inp.order_id, insured=True, policy_id=p["policy_id"],
                         covers=p["covers"], coverage_limit=p["coverage_limit"],
                         deductible=p["deductible"])

    # ---------------------------------------------- irreversible write tools
    def refund_precheck(inp: IssueRefundInput) -> None:
        o = backend.get_order(inp.order_id)
        eligible, reason = _refund_rule(o, backend.today)
        if not eligible:
            raise BackendError(ToolErrorCode.BUSINESS_RULE, f"Refund not allowed: {reason}")
        if inp.amount > o["price"] + 1e-9:
            raise BackendError(ToolErrorCode.BUSINESS_RULE,
                               f"Requested amount ${inp.amount:.2f} exceeds the order total "
                               f"${o['price']:.2f}.")

    def issue_refund(inp: IssueRefundInput) -> RefundOut:
        refund_precheck(inp)  # defense in depth: re-check at execution time
        return RefundOut(**backend.create_refund(inp.order_id, round(inp.amount, 2), inp.reason))

    def _payout(inp: FileClaimInput) -> float:
        o = backend.get_order(inp.order_id)
        p = backend.get_policy(inp.order_id)
        if not p:
            raise BackendError(ToolErrorCode.BUSINESS_RULE, "Order has no shipping insurance.")
        if inp.incident_type not in p["covers"]:
            raise BackendError(ToolErrorCode.BUSINESS_RULE,
                               f"Policy {p['policy_id']} does not cover '{inp.incident_type}' "
                               f"(covers: {', '.join(p['covers'])}).")
        if backend.has_claim(inp.order_id):
            raise BackendError(ToolErrorCode.BUSINESS_RULE, "A claim already exists for this order.")
        return round(max(0.0, min(o["price"], p["coverage_limit"]) - p["deductible"]), 2)

    def claim_precheck(inp: FileClaimInput) -> None:
        _payout(inp)

    def file_insurance_claim(inp: FileClaimInput) -> ClaimOut:
        payout = _payout(inp)
        return ClaimOut(**backend.create_claim(inp.order_id, inp.incident_type, inp.description, payout))

    def escalate_to_human(inp: EscalateInput) -> EscalationOut:
        return EscalationOut(**backend.create_ticket(customer_id_var.get(), inp.reason, inp.priority))

    reg.register(ToolSpec("get_order", "Look up an order's item, price, status and dates.",
                          OrderIdInput, OrderOut, get_order, scope="orders:read"))
    reg.register(ToolSpec("track_shipment",
                          "Get carrier, tracking number, shipment status, ETA and last scan location.",
                          OrderIdInput, ShipmentOut, track_shipment, scope="shipments:read"))
    reg.register(ToolSpec("check_refund_eligibility",
                          "Check whether an order can be refunded under policy and the maximum refundable amount. "
                          "Always call this before issue_refund.",
                          OrderIdInput, RefundEligibilityOut, check_refund_eligibility, scope="refunds:read"))
    reg.register(ToolSpec("issue_refund",
                          "Request a refund to the original payment method. IRREVERSIBLE: the request is queued "
                          "for human approval and is NOT executed immediately.",
                          IssueRefundInput, RefundOut, issue_refund, scope="refunds:write",
                          irreversible=True, precheck=refund_precheck))
    reg.register(ToolSpec("get_insurance_policy",
                          "Get the shipping-insurance policy for an order: whether insured, covered incident "
                          "types, coverage limit and deductible.",
                          OrderIdInput, PolicyOut, get_insurance_policy, scope="insurance:read"))
    reg.register(ToolSpec("file_insurance_claim",
                          "File a shipping-insurance claim that triggers a payout. IRREVERSIBLE: queued for "
                          "human approval and NOT executed immediately.",
                          FileClaimInput, ClaimOut, file_insurance_claim, scope="insurance:write",
                          irreversible=True, precheck=claim_precheck))
    reg.register(ToolSpec("escalate_to_human",
                          "Open a ticket for a human support specialist. Use when the customer asks for a human, "
                          "is very upset, or the request is outside your capabilities.",
                          EscalateInput, EscalationOut, escalate_to_human, scope="support:escalate",
                          order_scoped=False))
    return reg


def _refund_rule(order: dict, today: date) -> tuple[bool, str]:
    status = order["status"]
    if status == "refunded":
        return False, "This order has already been refunded."
    if status == "processing":
        return True, "Order has not shipped yet; it can be cancelled for a full refund."
    if status == "in_transit":
        return False, "Order is still in transit. Refunds are available after delivery."
    if status == "lost":
        return False, "Order was lost in transit; this is handled as a shipping-insurance claim."
    if status == "delivered":
        days = (today - date.fromisoformat(order["delivered_date"])).days
        if days > REFUND_WINDOW_DAYS:
            return False, (f"Delivered {days} days ago, outside the {REFUND_WINDOW_DAYS}-day "
                           f"refund window.")
        return True, f"Delivered {days} days ago, within the {REFUND_WINDOW_DAYS}-day refund window."
    return False, f"Order status '{status}' is not refundable."
