"""The three specialist agents. Each differs only in prompt and tool allow-list —
the behaviour lives in the prompt, the safety lives in the executor."""
from __future__ import annotations

from agents.base import SpecialistAgent
from agents.prompts import INSURANCE_PROMPT, REFUND_PROMPT, TRACKING_PROMPT


class TrackingAgent(SpecialistAgent):
    """Read-only: order + shipment lookups."""
    name = "tracking"
    system_prompt = TRACKING_PROMPT


class RefundAgent(SpecialistAgent):
    """Eligibility checks + refund requests (refunds go to human approval)."""
    name = "refund"
    system_prompt = REFUND_PROMPT


class InsuranceAgent(SpecialistAgent):
    """Coverage lookups + insurance claims (payouts go to human approval)."""
    name = "insurance"
    system_prompt = INSURANCE_PROMPT


SPECIALISTS = {cls.name: cls for cls in (TrackingAgent, RefundAgent, InsuranceAgent)}
