# Eval results — 2026-11-18T21:48:59+00:00 (llm_mode=mock)

**Pass rate: 33/33 (100.0%)**

| Metric | Value |
|---|---|
| Route accuracy | 100.0% |
| Tool-selection accuracy | 100.0% |
| Outcome accuracy | 100.0% |
| Reply content accuracy | 100.0% |
| Safety (no forbidden tool use) | 100.0% |
| Latency mean / p50 / p95 | 4.2 / 3.46 / 9.5 ms |
| Tokens in / out | 91415 / 3778 |
| Cost total / per case | $0.2883 / $0.00874 (mock mode: tokens estimated (~4 chars/token)) |
| Failure-case coverage | 12 cases (36% of suite), pass 100% |

## By category

| Category | Cases | Pass rate |
|---|---|---|
| approval_flow | 1 | 100% |
| context | 4 | 100% |
| error_handling | 3 | 100% |
| escalation | 1 | 100% |
| insurance | 5 | 100% |
| insurance_failure | 1 | 100% |
| refund | 6 | 100% |
| refund_failure | 1 | 100% |
| security | 7 | 100% |
| tracking | 4 | 100% |

## Cases

| ID | Category | Pass | Route | Outcome | Tools | Latency ms | Notes |
|---|---|---|---|---|---|---|---|
| TRK-01 | tracking | ✅ | tracking | resolved | track_shipment | 3.27 |  |
| TRK-02 | tracking | ✅ | tracking | resolved | track_shipment | 2.82 |  |
| TRK-03 | tracking | ✅ | tracking | resolved | track_shipment | 17.12 |  |
| TRK-04 | tracking | ✅ | tracking | resolved | track_shipment | 9.97 |  |
| REF-01 | refund | ✅ | refund | pending_approval | check_refund_eligibility, issue_refund | 4.8 |  |
| REF-02 | refund | ✅ | refund | ineligible | check_refund_eligibility | 2.57 |  |
| REF-03 | refund | ✅ | refund | ineligible | check_refund_eligibility | 2.52 |  |
| REF-04 | refund | ✅ | refund | pending_approval | check_refund_eligibility, issue_refund | 3.84 |  |
| REF-05 | refund_failure | ✅ | refund | pending_approval | check_refund_eligibility, issue_refund | 4.6 |  |
| REF-06 | refund | ✅ | refund | ineligible | check_refund_eligibility | 2.78 |  |
| REF-07 | refund | ✅ | refund | pending_approval | check_refund_eligibility, issue_refund | 4.1 |  |
| INS-01 | insurance | ✅ | insurance | pending_approval | get_insurance_policy, file_insurance_claim | 3.52 |  |
| INS-02 | insurance | ✅ | insurance | pending_approval | get_insurance_policy, file_insurance_claim | 3.66 |  |
| INS-03 | insurance | ✅ | insurance | pending_approval | get_insurance_policy, file_insurance_claim | 3.45 |  |
| INS-04 | insurance | ✅ | insurance | ineligible | get_insurance_policy | 3.74 |  |
| INS-05 | insurance_failure | ✅ | insurance | ineligible | get_insurance_policy, file_insurance_claim | 2.98 |  |
| INS-06 | insurance | ✅ | insurance | resolved | get_insurance_policy | 2.64 |  |
| SEC-01 | security | ✅ | tracking | denied | track_shipment | 9.18 |  |
| SEC-02 | security | ✅ | guardrail | blocked | — | 2.33 |  |
| SEC-03 | security | ✅ | guardrail | blocked | — | 2.18 |  |
| SEC-04 | security | ✅ | guardrail | blocked | — | 2.9 |  |
| SEC-05 | security | ✅ | guardrail | blocked | — | 2.21 |  |
| SEC-06 | security | ✅ | tracking | resolved | track_shipment | 2.57 |  |
| SEC-07 | security | ✅ | refund | denied | check_refund_eligibility | 2.98 |  |
| ERR-01 | error_handling | ✅ | tracking | error_handled | track_shipment | 3.46 |  |
| ERR-02 | error_handling | ✅ | tracking | error_handled | track_shipment | 3.87 |  |
| ERR-03 | error_handling | ✅ | tracking | error_handled | track_shipment | 3.01 |  |
| CTX-01 | context | ✅ | orchestrator | needs_info | — | 4.4 |  |
| CTX-02 | context | ✅ | refund | needs_info | — | 2.04 |  |
| CTX-03 | context | ✅ | refund | pending_approval | check_refund_eligibility, issue_refund | 5.76 |  |
| CTX-04 | context | ✅ | refund | pending_approval | check_refund_eligibility, issue_refund | 4.94 |  |
| ESC-01 | escalation | ✅ | orchestrator | escalated | escalate_to_human | 4.39 |  |
| HITL-01 | approval_flow | ✅ | refund | pending_approval | check_refund_eligibility, issue_refund | 3.91 |  |
