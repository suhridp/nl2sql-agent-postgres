"""Command line.

  python -m sql_agent run configs/smoke.yaml            # benchmark + studies, then report and dashboard
  python -m sql_agent run configs/main.yaml --dry-run   # trial count only
  python -m sql_agent analyze results/smoke             # rebuild report/figures/dashboard
  python -m sql_agent ask "Which channel had the most revenue?" --model claude-sonnet-5-5
  python -m sql_agent optimize "SELECT ... "            # optimize one query (no model needed)
  python -m sql_agent guardrails                        # run the attack suite against every layer
"""
from __future__ import annotations

import argparse
import json
import sys

import yaml

from .analysis import analyze
from .runner import ROOT, build_context, run


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m sql_agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("config")
    r.add_argument("--out")
    r.add_argument("--fresh", action="store_true")
    r.add_argument("--dry-run", action="store_true")
    a = sub.add_parser("analyze")
    a.add_argument("run_dir")
    q = sub.add_parser("ask", help="ask one question with a real model")
    q.add_argument("question")
    q.add_argument("--provider", default="anthropic", choices=["anthropic", "openai_compat"])
    q.add_argument("--model", default="claude-sonnet-5-5")
    q.add_argument("--base-url", default=None)
    q.add_argument("--system", default="optimized")
    o = sub.add_parser("optimize", help="optimize one SELECT and show every candidate")
    o.add_argument("sql")
    o.add_argument("--rounds", type=int, default=9)
    sub.add_parser("guardrails", help="run bench/attacks.yaml against each layer")
    args = ap.parse_args(argv)

    if args.cmd == "run":
        out = run(args.config, args.out, resume=not args.fresh, dry_run=args.dry_run)
        if not args.dry_run:
            analyze(out)
            print(f"\nwrote {out}/report.md and {out}/dashboard.html")
        return 0
    if args.cmd == "analyze":
        analyze(args.run_dir)
        print(f"wrote {args.run_dir}/report.md and dashboard.html")
        return 0
    if args.cmd == "ask":
        from .agent import SQLAgent
        from .providers import Budget, make_provider
        spec = {"provider": args.provider, "model": args.model, "temperature": 0}
        if args.base_url:
            spec["base_url"] = args.base_url
        llm = make_provider(spec, budget=Budget())
        ctx = build_context()
        ans = SQLAgent(ctx, llm, args.system).ask("cli", args.question)
        print(f"status: {ans.status}  ({ans.llm_calls} model calls, {ans.latency_s:.1f}s)")
        for i, att in enumerate(ans.attempts, 1):
            print(f"\nattempt {i}: {att.outcome} {att.code}\n{att.sql}\n{att.message}")
        if ans.reply_text:
            print("\nagent:", ans.reply_text)
        if ans.status == "answered":
            print("\n" + " | ".join(ans.cols))
            for row in ans.rows[:20]:
                print(" | ".join(map(str, row)))
            if ans.optimizer:
                print("\noptimizer:", json.dumps(ans.optimizer))
        return 0
    if args.cmd == "optimize":
        from .optimizer import optimize_query
        ctx = build_context(timeout_ms=120000)
        res = optimize_query(ctx, args.sql, rounds=args.rounds)
        print(f"original: {res.original_ms:.2f} ms (cost {res.original_cost:,.0f})")
        for c in res.candidates:
            sp = c.speedup
            status = "ACCEPTED" if c.accepted else ("verified" if c.verified else f"rejected: {c.reject_reason}")
            line = f"\n[{c.rule}] {status}"
            if sp.get("median"):
                line += f"  speedup {sp['median']:.2f}x [{sp['lo']:.2f}, {sp['hi']:.2f}]  cost {c.cost:,.0f}"
            print(line + "\n" + c.sql)
        for n in res.notes:
            print("note:", n)
        print("\nchosen:", "original" if res.chosen == res.original else res.chosen)
        return 0
    if args.cmd == "guardrails":
        from .studies import guardrail_study
        g = guardrail_study(None, yaml.safe_load((ROOT / "bench/attacks.yaml").read_text()))
        print(json.dumps(g["summary"]), "database unchanged:", g["db_unchanged"])
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
