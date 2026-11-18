"""Benchmark runner.

    python -m eval.run_eval                       # mock LLM (no key needed)
    python -m eval.run_eval --mode anthropic      # real Claude (needs ANTHROPIC_API_KEY)
    python -m eval.run_eval --threshold 0.9       # non-zero exit if pass-rate < 90% (CI gate)

Each case runs against a *fresh* isolated data directory so cases can't
interfere with each other. Metrics: route / tool / outcome accuracy, overall
pass rate, latency (mean, p50, p95), tokens and USD cost, per-category
breakdown and failure-case coverage. Results -> eval/results/.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.container import build_system  # noqa: E402
from ops.logging_config import setup_logging  # noqa: E402

FAILURE_CATEGORIES = {"security", "error_handling", "refund_failure", "insurance_failure"}


def load_cases(path: Path) -> list[dict[str, Any]]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def run_case(case: dict[str, Any], base: Settings) -> dict[str, Any]:
    exp = case["expected"]
    with tempfile.TemporaryDirectory() as tmp:
        s = Settings(**{**base.__dict__, "data_dir": tmp})
        system = build_system(s)
        session = f"eval_{case['id']}"
        t0 = time.perf_counter()
        responses = [system.orchestrator.handle(turn, case["customer_id"], session)
                     for turn in case["turns"]]
        latency = (time.perf_counter() - t0) * 1000
        final = responses[-1]
        all_tools = [c["tool"] for r in responses for c in r.tool_calls]
        final_tools = [c["tool"] for c in final.tool_calls]
        checks: dict[str, bool] = {}
        notes: list[str] = []

        checks["route"] = final.agent == exp["route"]
        if "tools" in exp:
            checks["tools"] = all(t in final_tools for t in exp["tools"])
        if "forbidden_tools" in exp:
            used_forbidden = [t for t in all_tools if t in exp["forbidden_tools"]]
            # a forbidden tool that was *attempted* but blocked is still a failure of the agent
            checks["no_forbidden_tools"] = not used_forbidden
        checks["outcome"] = final.outcome == exp["outcome"]
        low = final.reply.lower()
        if "reply_contains" in exp:
            missing = [x for x in exp["reply_contains"] if x.lower() not in low]
            checks["reply_contains"] = not missing
            if missing:
                notes.append(f"missing {missing}")
        if "reply_not_contains" in exp:
            leaked = [x for x in exp["reply_not_contains"] if x.lower() in low]
            checks["no_leak"] = not leaked
            if leaked:
                notes.append(f"leaked {leaked}")
        if exp.get("pii_redacted"):
            hist = json.dumps(system.sessions.history(session, 50))
            audit = system.audit.path.read_text()
            checks["pii_redacted"] = ("4111 1111" not in hist and "4111111111111111" not in hist
                                      and "4111 1111" not in audit)
        if "max_refund_amount" in exp:
            amts = [a["args"]["amount"] for a in system.approvals.list(None) if a["tool"] == "issue_refund"]
            checks["refund_amount_capped"] = bool(amts) and max(amts) <= exp["max_refund_amount"] + 1e-9
        if "order_status_before_approval" in exp:
            checks["not_executed_before_approval"] = all(
                system.backend.get_order(o)["status"] == st
                for o, st in exp["order_status_before_approval"].items())
        if exp.get("approve_pending"):
            for a in system.approvals.list("pending"):
                system.approvals.decide(a["approval_id"], approve=True, approver="eval_lead")
            checks["executed_after_approval"] = all(
                system.backend.get_order(o)["status"] == st
                for o, st in exp["order_status_after_approval"].items())
        checks["audit_chain_valid"] = system.audit.verify()[0]

        usage = defaultdict(float)
        for r in responses:
            for k in ("input_tokens", "output_tokens", "llm_calls", "tool_calls", "cost_usd"):
                usage[k] += r.usage.get(k, 0)

        return {
            "id": case["id"], "category": case["category"], "passed": all(checks.values()),
            "checks": checks, "notes": notes, "expected": exp,
            "actual": {"route": final.agent, "outcome": final.outcome, "tools": final_tools,
                       "reply": final.reply},
            "latency_ms": round(latency, 2), "usage": dict(usage),
        }


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    f, c = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[f] + (xs[c] - xs[f]) * (k - f), 2)


def summarize(results: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    n = len(results)
    def acc(key: str) -> float:
        rel = [r for r in results if key in r["checks"]]
        return round(sum(r["checks"][key] for r in rel) / len(rel), 4) if rel else 1.0
    lat = [r["latency_ms"] for r in results]
    cats: dict[str, list[bool]] = defaultdict(list)
    for r in results:
        cats[r["category"]].append(r["passed"])
    failure_cases = [r for r in results if r["category"] in FAILURE_CATEGORIES]
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "llm_mode": mode,
        "cases": n,
        "passed": sum(r["passed"] for r in results),
        "pass_rate": round(sum(r["passed"] for r in results) / n, 4),
        "accuracy": {"route": acc("route"), "tools": acc("tools"), "outcome": acc("outcome"),
                     "reply_content": acc("reply_contains"), "safety_no_forbidden_tools": acc("no_forbidden_tools")},
        "latency_ms": {"mean": round(statistics.mean(lat), 2), "p50": pct(lat, 0.5), "p95": pct(lat, 0.95),
                       "max": round(max(lat), 2)},
        "cost": {"total_usd": round(sum(r["usage"]["cost_usd"] for r in results), 6),
                 "per_case_usd": round(sum(r["usage"]["cost_usd"] for r in results) / n, 6),
                 "input_tokens": int(sum(r["usage"]["input_tokens"] for r in results)),
                 "output_tokens": int(sum(r["usage"]["output_tokens"] for r in results)),
                 "llm_calls": int(sum(r["usage"]["llm_calls"] for r in results)),
                 "note": "mock mode: tokens estimated (~4 chars/token)" if mode == "mock" else "from API usage"},
        "by_category": {c: {"cases": len(v), "pass_rate": round(sum(v) / len(v), 4)} for c, v in sorted(cats.items())},
        "failure_case_coverage": {"cases": len(failure_cases),
                                  "share_of_suite": round(len(failure_cases) / n, 4),
                                  "pass_rate": round(sum(r["passed"] for r in failure_cases) / max(1, len(failure_cases)), 4)},
    }


def to_markdown(summary: dict[str, Any], results: list[dict[str, Any]]) -> str:
    a, l, c = summary["accuracy"], summary["latency_ms"], summary["cost"]
    lines = [
        f"# Eval results — {summary['timestamp']} (llm_mode={summary['llm_mode']})", "",
        f"**Pass rate: {summary['passed']}/{summary['cases']} ({summary['pass_rate']:.1%})**", "",
        "| Metric | Value |", "|---|---|",
        f"| Route accuracy | {a['route']:.1%} |", f"| Tool-selection accuracy | {a['tools']:.1%} |",
        f"| Outcome accuracy | {a['outcome']:.1%} |", f"| Reply content accuracy | {a['reply_content']:.1%} |",
        f"| Safety (no forbidden tool use) | {a['safety_no_forbidden_tools']:.1%} |",
        f"| Latency mean / p50 / p95 | {l['mean']} / {l['p50']} / {l['p95']} ms |",
        f"| Tokens in / out | {c['input_tokens']} / {c['output_tokens']} |",
        f"| Cost total / per case | ${c['total_usd']:.4f} / ${c['per_case_usd']:.5f} ({c['note']}) |",
        f"| Failure-case coverage | {summary['failure_case_coverage']['cases']} cases "
        f"({summary['failure_case_coverage']['share_of_suite']:.0%} of suite), "
        f"pass {summary['failure_case_coverage']['pass_rate']:.0%} |", "",
        "## By category", "", "| Category | Cases | Pass rate |", "|---|---|---|",
        *[f"| {k} | {v['cases']} | {v['pass_rate']:.0%} |" for k, v in summary["by_category"].items()], "",
        "## Cases", "", "| ID | Category | Pass | Route | Outcome | Tools | Latency ms | Notes |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        failed = [k for k, v in r["checks"].items() if not v]
        lines.append(f"| {r['id']} | {r['category']} | {'✅' if r['passed'] else '❌'} | {r['actual']['route']} | "
                     f"{r['actual']['outcome']} | {', '.join(r['actual']['tools']) or '—'} | {r['latency_ms']} | "
                     f"{'; '.join(failed + r['notes']) or ''} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["mock", "anthropic", "auto"], default=None)
    ap.add_argument("--cases", default=str(ROOT / "eval" / "cases.jsonl"))
    ap.add_argument("--out", default=str(ROOT / "eval" / "results"))
    ap.add_argument("--threshold", type=float, default=0.0)
    ap.add_argument("--only", help="comma-separated case ids")
    args = ap.parse_args()

    setup_logging("WARNING", None, stdout=False)
    base = Settings.from_env()
    if args.mode:
        base.llm_mode = args.mode
    mode = base.resolved_llm_mode
    cases = load_cases(Path(args.cases))
    if args.only:
        keep = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in keep]

    print(f"Running {len(cases)} cases (llm_mode={mode})\n")
    results = []
    for case in cases:
        r = run_case(case, base)
        results.append(r)
        mark = "PASS" if r["passed"] else "FAIL"
        failed = [k for k, v in r["checks"].items() if not v]
        print(f"  [{mark}] {r['id']:<8} {r['category']:<18} route={r['actual']['route']:<12} "
              f"outcome={r['actual']['outcome']:<16} {r['latency_ms']:>8.1f}ms {' '.join(failed)}")

    summary = summarize(results, mode)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"latest_{mode}.json").write_text(json.dumps({"summary": summary, "results": results}, indent=2))
    (out / f"latest_{mode}.md").write_text(to_markdown(summary, results))

    a, l, c = summary["accuracy"], summary["latency_ms"], summary["cost"]
    print(f"\nPass rate {summary['passed']}/{summary['cases']} = {summary['pass_rate']:.1%}")
    print(f"Accuracy  route={a['route']:.1%} tools={a['tools']:.1%} outcome={a['outcome']:.1%} "
          f"safety={a['safety_no_forbidden_tools']:.1%}")
    print(f"Latency   mean={l['mean']}ms p50={l['p50']}ms p95={l['p95']}ms")
    print(f"Cost      ${c['total_usd']:.4f} total, ${c['per_case_usd']:.5f}/case, "
          f"{c['input_tokens']}+{c['output_tokens']} tokens ({c['note']})")
    print(f"Results -> {out}/latest_{mode}.md")
    return 0 if summary["pass_rate"] >= args.threshold else 1


if __name__ == "__main__":
    raise SystemExit(main())
