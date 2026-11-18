"""Interactive terminal client.

    python -m app.cli --customer C-001          # chat as a customer
    python -m app.cli approvals                  # list pending approvals
    python -m app.cli approve APR-XXXX [--reject] # decide as a support lead
    python -m app.cli audit                      # verify audit-log hash chain
"""
from __future__ import annotations

import argparse
import json
import uuid

from app.config import Settings
from app.container import build_system
from ops.logging_config import setup_logging


def main() -> None:
    ap = argparse.ArgumentParser(description="ShopCo multi-agent support CLI")
    sub = ap.add_subparsers(dest="cmd")
    ap.add_argument("--customer", default="C-001")
    ap.add_argument("--verbose", action="store_true", help="print tool calls and metrics")
    sub.add_parser("approvals")
    a = sub.add_parser("approve")
    a.add_argument("approval_id")
    a.add_argument("--reject", action="store_true")
    a.add_argument("--approver", default="support_lead")
    a.add_argument("--note", default="")
    sub.add_parser("audit")
    args = ap.parse_args()

    s = Settings.from_env()
    setup_logging("WARNING", s.log_file, stdout=False)
    system = build_system(s)

    if args.cmd == "approvals":
        rows = system.approvals.list("pending")
        if not rows:
            print("No pending approvals.")
        for r in rows:
            print(f"{r['approval_id']}  {r['customer_id']}  {r['summary']}  (agent={r['agent']}, at {r['created_at']})")
        return
    if args.cmd == "approve":
        rec = system.approvals.decide(args.approval_id, approve=not args.reject,
                                      approver=args.approver, note=args.note)
        print(json.dumps(rec, indent=2))
        return
    if args.cmd == "audit":
        ok, n = system.audit.verify()
        print(f"audit chain valid={ok} entries={n}")
        return

    session = f"cli_{uuid.uuid4().hex[:8]}"
    print(f"ShopCo Support — customer {args.customer}, session {session}, llm={system.llm.mode}")
    print("Type a message (Ctrl+C to quit). Try: 'Where is ORD-1002?'\n")
    while True:
        try:
            msg = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not msg:
            continue
        r = system.orchestrator.handle(msg, args.customer, session)
        print(f"[{r.agent} | {r.outcome}] {r.reply}")
        if args.verbose:
            print(f"   tools={json.dumps(r.tool_calls)}")
            print(f"   latency={r.latency_ms}ms usage={r.usage}")
        print()


if __name__ == "__main__":
    main()
