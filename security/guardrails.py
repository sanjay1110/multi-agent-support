"""Input / output guardrails.

Input guardrail runs BEFORE any LLM call:
  1. structural validation (empty, too long, control characters)
  2. prompt-injection / policy-bypass detection  -> request blocked
  3. PII redaction (payment cards, SSNs) -> sanitized text is what the LLM,
     memory and logs see; the raw value is never persisted.

Output guardrail runs on every reply before it reaches the customer:
  * redacts any PII that slipped through
  * strips internal identifiers that must never be shown (system prompt text).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_INPUT_CHARS = 2000

INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("override_instructions", re.compile(r"\b(ignore|disregard|forget)\b.{0,40}\b(previous|prior|above|all|your)\b.{0,20}\b(instructions?|rules|prompts?)\b", re.I)),
    ("role_hijack", re.compile(r"\byou are now\b|\bact as (an? )?(admin|administrator|developer|system)\b|\bdeveloper mode\b|\bjailbreak\b|\bDAN\b", re.I)),
    ("prompt_exfiltration", re.compile(r"\b(reveal|show|print|repeat|output|tell me)\b.{0,30}\b(system prompt|hidden prompt|instructions you were given|your prompt)\b", re.I)),
    ("approval_bypass", re.compile(r"\b(without|skip|bypass|no need for|override)\b.{0,20}\b(approval|review|verification|authorization)\b", re.I)),
    ("privilege_claim", re.compile(r"\b(i am|i'm|as) (an? |the )?(admin|administrator|supervisor|manager|employee|staff)\b.{0,60}\b(refund|approve|issue|grant)\b", re.I)),
    ("bulk_action", re.compile(r"\b(refund|cancel|delete)\b.{0,15}\b(all|every|everyone's)\b.{0,15}\b(orders?|customers?|accounts?)\b", re.I)),
    ("tool_injection", re.compile(r"</?(system|tool_use|tool_result|function_call)>|\{\s*\"(tool|name)\"\s*:", re.I)),
]

_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass
class GuardrailResult:
    allowed: bool
    sanitized_text: str
    violations: list[str] = field(default_factory=list)
    redactions: list[str] = field(default_factory=list)


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def redact_pii(text: str) -> tuple[str, list[str]]:
    redactions: list[str] = []

    def _card(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            redactions.append("payment_card")
            return "[REDACTED_CARD]"
        return m.group(0)

    text = _CARD.sub(_card, text)
    if _SSN.search(text):
        redactions.append("ssn")
        text = _SSN.sub("[REDACTED_SSN]", text)
    return text, redactions


def check_input(text: str) -> GuardrailResult:
    if text is None or not text.strip():
        return GuardrailResult(False, "", ["empty_input"])
    if len(text) > MAX_INPUT_CHARS:
        return GuardrailResult(False, text[:MAX_INPUT_CHARS], ["input_too_long"])
    cleaned = _CONTROL.sub("", text).strip()
    sanitized, redactions = redact_pii(cleaned)
    violations = [name for name, pat in INJECTION_PATTERNS if pat.search(sanitized)]
    return GuardrailResult(allowed=not violations, sanitized_text=sanitized,
                           violations=violations, redactions=redactions)


def check_output(text: str) -> str:
    text, _ = redact_pii(text or "")
    return text


BLOCKED_REPLY = ("I'm sorry, but I can't help with that request. I can help you track an order, "
                 "request a refund, or file a shipping-insurance claim. All refunds and claims are "
                 "reviewed by our team.")
