"""Metrics, figures, report and dashboard from a run directory."""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from .stats import cluster_bootstrap, holm, mcnemar_exact, wilson

DIFFS = ["easy", "medium", "hard", "extra"]


def load(run_dir: Path):
    traces = [json.loads(x) for x in (run_dir / "traces.jsonl").read_text().splitlines() if x.strip()]
    j = lambda n: json.loads((run_dir / n).read_text()) if (run_dir / n).exists() else None  # noqa: E731
    return traces, j("manifest.json") or {}, j("optimizer.json"), j("guardrails.json")


def rate(rows, key) -> dict:
    k, n = sum(bool(r.get(key)) for r in rows), len(rows)
    lo, hi = wilson(k, n)
    d = {"k": k, "n": n, "rate": k / n if n else None, "wilson": [lo, hi]}
    if n:
        _, a, b = cluster_bootstrap([float(bool(r.get(key))) for r in rows], [r["qid"] for r in rows])
        d["cluster"] = [a, b]
    return d


def pct(x):
    return "n/a" if x is None else f"{100 * x:.0f}%"


def summarize(traces, manifest) -> dict:
    models = list(dict.fromkeys(t["model"] for t in traces))
    systems = [s for s in manifest.get("systems", []) if any(t["system"] == s for t in traces)] or \
        list(dict.fromkeys(t["system"] for t in traces))
    by = defaultdict(list)
    for t in traces:
        by[(t["model"], t["system"], t["kind"])].append(t)
    rows = []
    for m in models:
        for s in systems:
            A = by[(m, s, "answerable")]
            if not A and not by[(m, s, "adversarial")]:
                continue
            first_failed = [r for r in A if r["first_outcome"] not in ("ok",)]
            lat = sorted(r["latency_s"] for r in A) or [0]
            opt = [r["optimizer"] for r in A if r.get("optimizer")]
            adv = by[(m, s, "adversarial")]
            rows.append({
                "model": m, "system": s, "n": len(A),
                "ex": rate(A, "correct"), "ex_lenient": rate(A, "correct_lenient"),
                "answered": rate([r | {"a": r["status"] == "answered"} for r in A], "a"),
                "first_failed": rate([r | {"f": r["first_outcome"] != "ok"} for r in A], "f"),
                "recovered": sum(r.get("recovered", False) for r in first_failed),
                "n_first_failed": len(first_failed),
                "silent_wrong": sum(1 for r in A if r["status"] == "answered" and not r["correct"]),
                "result_check": dict(Counter(r["result_check"] for r in A if r["result_check"])),
                "by_difficulty": {d: rate([r for r in A if r["difficulty"] == d], "correct") for d in DIFFS
                                  if any(r["difficulty"] == d for r in A)},
                "false_clarify": rate([r | {"x": r["status"] in ("clarify", "cannot_answer", "refused")} for r in A],
                                      "x"),
                "ambiguous_clarified": rate(by[(m, s, "ambiguous")], "correct"),
                "unanswerable_abstained": rate(by[(m, s, "unanswerable")], "correct"),
                "unanswerable_hallucinated": sum(r.get("hallucinated", False) for r in by[(m, s, "unanswerable")]),
                "adversarial_refused": rate(adv, "correct"),
                "harmful_sql_ran": sum(r.get("harmful_sql_ran", False) for r in adv),
                "adversarial_blocked_by": dict(Counter(b for r in adv for b in r.get("blocked_by", []))),
                "first_error_codes": dict(Counter(f"{r['first_outcome']}:{r['first_code']}" for r in first_failed)),
                "latency_p50": statistics.median(lat), "latency_p95": lat[int(0.95 * (len(lat) - 1))],
                "llm_calls": statistics.mean([r["llm_calls"] for r in A]) if A else 0,
                "tokens": statistics.mean([r["in_tokens"] + r["out_tokens"] for r in A]) if A else 0,
                "opt_rewritten": sum(1 for o in opt if o.get("chosen_rule")),
                "opt_geomean": statistics.geometric_mean([max(o.get("speedup") or 1, 1e-9) for o in opt]) if opt else None,
            })
    paired = []
    for m in models:
        fam = {}
        for a, b in zip(systems, systems[1:], strict=False):
            A = {(r["qid"], r["trial"]): r["correct"] for r in by[(m, a, "answerable")]}
            B = {(r["qid"], r["trial"]): r["correct"] for r in by[(m, b, "answerable")]}
            cells = sorted(set(A) & set(B))
            if not cells:
                continue
            res = mcnemar_exact([(A[c], B[c]) for c in cells])
            paired.append({"model": m, "from": a, "to": b, **res})
            fam[f"{a}>{b}"] = res["p"]
        adj = holm(fam)
        for p in paired:
            if p["model"] == m:
                p["p_holm"] = adj.get(f"{p['from']}>{p['to']}", p["p"])
    return {"models": models, "systems": systems, "rows": rows, "paired": paired, "n_traces": len(traces)}


# ------------------------------------------------------------------------------------------

def write_report(run_dir: Path, S, manifest, opt, guard, figs):
    L = [f"# Results: {manifest.get('name', '')}", ""]
    if manifest.get("scripted_only"):
        L += ["> **Agent results: harness validation only.** The agents in this run are scripted stand-ins, so "
              "the accuracy tables show that the pipeline and grading work, not how any model performs. The "
              "optimizer and guardrail studies below need no model and are real measurements.", ""]
    L += [f"- PostgreSQL {manifest.get('postgres', '?')}, driver `{manifest.get('driver', '?')}`, code "
          f"`{manifest.get('git', '?')}`, finished {manifest.get('finished', '?')}",
          f"- Agents: {', '.join(S['models'])}; systems: {', '.join(S['systems'])}; trials per cell: "
          f"{manifest.get('trials', 1)}",
          f"- Database unchanged after all agent runs (table fingerprints): "
          f"**{manifest.get('db_unchanged_by_agent_runs')}**", f"- Usage: {manifest.get('usage', {})}", ""]
    if S["rows"]:
        L += ["## Agent benchmark", "",
              "Execution accuracy (EX) compares result sets with the gold query. Intervals are 95% bootstrap over "
              "questions. *Lenient* allows extra columns.", ""]
        for m in S["models"]:
            R = [r for r in S["rows"] if r["model"] == m]
            L += [f"### {m}", "", "| system | EX | EX lenient | answered | first attempt failed | recovered | "
                  "silently wrong | asked on ambiguous | abstained on unanswerable | refused attacks | harmful SQL ran |"
                  " LLM calls | p50 latency |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
            for r in R:
                ci = r["ex"].get("cluster", r["ex"]["wilson"])
                L.append(f"| {r['system']} | {pct(r['ex']['rate'])} [{pct(ci[0])}, {pct(ci[1])}] | "
                         f"{pct(r['ex_lenient']['rate'])} | {pct(r['answered']['rate'])} | "
                         f"{r['n_first_failed']}/{r['n']} | {r['recovered']}/{r['n_first_failed']} | "
                         f"{r['silent_wrong']} | {pct(r['ambiguous_clarified']['rate'])} | "
                         f"{pct(r['unanswerable_abstained']['rate'])} | {pct(r['adversarial_refused']['rate'])} | "
                         f"**{r['harmful_sql_ran']}** | {r['llm_calls']:.1f} | {r['latency_p50']:.2f}s |")
            L += ["", "EX by difficulty:", "", "| system | " + " | ".join(DIFFS) + " |", "|---|" + "---|" * len(DIFFS)]
            for r in R:
                L.append(f"| {r['system']} | " + " | ".join(
                    f"{pct(r['by_difficulty'][d]['rate'])} ({r['by_difficulty'][d]['n']})" if d in r["by_difficulty"]
                    else "-" for d in DIFFS) + " |")
            P = [p for p in S["paired"] if p["model"] == m]
            if P:
                L += ["", "Paired comparison of consecutive systems (exact McNemar on matched questions, "
                      "Holm-adjusted):", "", "| from | to | only first correct | only second correct | p |",
                      "|---|---|---|---|---|"]
                L += [f"| {p['from']} | {p['to']} | {p['a_only']} | {p['b_only']} | {p['p_holm']:.3g} |" for p in P]
            L.append("")
    if guard:
        g = guard["summary"]
        L += ["## Guardrail study (real)", "",
              f"{g['n']} attack statements, each sent to every layer on its own and to the full stack. Database "
              f"unchanged afterwards: **{guard['db_unchanged']}**.", "",
              "| layer | blocked |", "|---|---|"]
        names = {"validator": "SQLGlot validator", "cost_check": "EXPLAIN cost check",
                 "role_only": "SELECT-only role (read-only mode off)",
                 "execution_path": "execution path (read-only txn + cursor + role + timeout)",
                 "full_stack": "full stack"}
        L += [f"| {names[k]} | {g[k]}/{g['n']} |" for k in names]
        L += ["", "Statements that got past a single layer:", ""]
        for k in ["validator", "role_only", "execution_path"]:
            miss = [f"`{r['id']}` {r['category']}" for r in guard["attacks"] if not r[k]["blocked"]]
            L.append(f"- **{names[k]}**: {', '.join(miss) or 'none'}")
        L.append("")
    if opt:
        s = opt["summary"]
        L += ["## Optimizer study (real)", "",
              f"{len(opt['queries'])} naive-but-correct queries; candidates verified on the main and edge-case "
              f"databases; {opt['rounds']} interleaved timing rounds; every policy's choice re-timed in a separate "
              f"{opt['confirm_rounds']}-round confirmation run (reported below).", "",
              "| policy | queries rewritten | geometric-mean speedup | rewrites slower (median) | confirmed "
              "regressions | timed executions |", "|---|---|---|---|---|---|"]
        for p in ["original", "sqlglot_only", "cost_top1", "gate_topk1", "gate_all"]:
            v = s[p]
            L.append(f"| {p} | {v['n_rewritten']} | {v['geomean_speedup']:.2f}x | {v['n_slower_median']} | "
                     f"{v['n_regressions']} | {v['timing_runs']} |")
        c = s["cost_vs_time"]
        mis = ", ".join(f"{x['id']} ({x['regret']:.1f}x slower)" for x in c["misled"]) or "none"
        L += ["", f"Planner cost vs measured time: picking the lowest-cost version would have been more than 10% "
              f"slower than the fastest verified version on {c['n_misled']}/{c['queries']} queries: {mis}. "
              f"{c['n_wrong_direction']}/{c['n_pairs']} candidates moved in the opposite direction from their cost "
              f"estimate; Spearman rank correlation of cost ratio and time ratio = {c['spearman']:.2f}. "
              f"Unsafe rewrites refused by rule: {s['unsafe_rewrites_refused']}.", "",
              "| query | expected | gate_all choice | confirmed speedup [95% CI] | cost_top1 choice | cost_top1 confirmed |",
              "|---|---|---|---|---|---|"]
        for q in opt["queries"]:
            g, ct = q["policies"]["gate_all"], q["policies"]["cost_top1"]
            L.append(f"| {q['id']} | {q['expect']} | {g['rule'] or 'original'} | {g['confirmed_speedup']:.2f}x "
                     f"[{g['confirmed_lo']:.2f}, {g['confirmed_hi']:.2f}] | {ct['rule'] or 'original'} | "
                     f"{ct['confirmed_speedup']:.2f}x |")
        L.append("")
    if figs:
        L += ["## Figures", ""] + [f"![{Path(f).stem}]({f})" for f in figs] + [""]
    L += ["## Read before quoting", "",
          "- 78 answerable questions on one synthetic schema. Results describe this benchmark, not text-to-SQL in "
          "general.",
          "- Timings are warm-cache, on one machine, server-side (planning + execution). Cold-cache behaviour can "
          "differ, especially for index-vs-scan choices.",
          "- Rewrite equivalence is checked empirically on two databases, not proven.",
          "- The optimization queries were written to contain known anti-patterns; real agent-written SQL may "
          "offer fewer opportunities (see the `optimized` system's rewrite count).", ""]
    (run_dir / "report.md").write_text("\n".join(L))


def make_figures(run_dir: Path, S, opt, guard) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fd = run_dir / "figures"
    fd.mkdir(exist_ok=True)
    ink, muted, grid = "#1d2330", "#5b6475", "#e3e6ec"
    cols = ["#2a78d6", "#eb6834", "#1baf7a"]
    plt.rcParams.update({"font.size": 9, "axes.edgecolor": grid, "axes.labelcolor": muted, "xtick.color": muted,
                         "ytick.color": muted, "axes.spines.top": False, "axes.spines.right": False})
    out = []
    if S["rows"]:
        fig, ax = plt.subplots(figsize=(7, 3.6), dpi=150)
        for k, m in enumerate(S["models"][:3]):
            R = [r for r in S["rows"] if r["model"] == m]
            xs = [S["systems"].index(r["system"]) + (k - (len(S["models"][:3]) - 1) / 2) * 0.18 for r in R]
            ys = [r["ex"]["rate"] for r in R]
            lo = [r["ex"]["rate"] - r["ex"].get("cluster", r["ex"]["wilson"])[0] for r in R]
            hi = [r["ex"].get("cluster", r["ex"]["wilson"])[1] - r["ex"]["rate"] for r in R]
            ax.errorbar(xs, ys, yerr=[lo, hi], fmt="o", color=cols[k], ms=6, capsize=0, elinewidth=1.4, label=m,
                        mec="white", mew=1)
        ax.set_xticks(range(len(S["systems"])), S["systems"])
        ax.set_ylim(0, 1.05)
        ax.set_ylabel("execution accuracy")
        ax.grid(axis="y", color=grid, lw=0.6)
        ax.legend(frameon=False, fontsize=8, loc="lower right")
        ax.set_title("Execution accuracy by system (95% CI)", loc="left", fontsize=10, color=ink)
        fig.tight_layout()
        fig.savefig(fd / "accuracy.png")
        plt.close(fig)
        out.append("figures/accuracy.png")
    if opt:
        Q = opt["queries"]
        fig, ax = plt.subplots(figsize=(7, 0.32 * len(Q) + 1.2), dpi=150)
        for i, q in enumerate(Q):
            for c in q["result"]["candidates"]:
                sp = c.get("speedup") or {}
                if not sp.get("median"):
                    continue
                color = cols[0] if c["accepted"] else "#9aa3b2"
                ax.plot([sp["lo"], sp["hi"]], [i, i], color=color, lw=1.4, alpha=0.6)
                ax.plot(sp["median"], i, "o", color=color, ms=5.5, mec="white", mew=0.8)
        ax.axvline(1, color=muted, lw=0.8, ls="--")
        ax.set_xscale("log")
        ax.set_yticks(range(len(Q)), [f"{q['id']} {q['expect']}" for q in Q])
        ax.invert_yaxis()
        ax.set_xlabel("speedup over original (log scale; <1 = slower)")
        ax.set_title("Every verified rewrite: blue = accepted by the gate", loc="left", fontsize=10, color=ink)
        ax.grid(axis="x", color=grid, lw=0.6)
        fig.tight_layout()
        fig.savefig(fd / "rewrites.png")
        plt.close(fig)
        out.append("figures/rewrites.png")
        P = opt["summary"]["cost_vs_time"]["pairs"]
        if P:
            fig, ax = plt.subplots(figsize=(5.2, 4.4), dpi=150)
            ax.axhline(1, color=grid, lw=1)
            ax.axvline(1, color=grid, lw=1)
            for p in P:
                wrong = (p["cost_ratio"] < .95 and p["time_ratio"] > 1.05) or (p["cost_ratio"] > 1.05 and
                                                                              p["time_ratio"] < .95)
                ax.plot(p["cost_ratio"], p["time_ratio"], "o", color="#e34948" if wrong else cols[0], ms=6,
                        mec="white", mew=0.8)
                if wrong:
                    ax.annotate(f"{p['id']} {p['rule']}", (p["cost_ratio"], p["time_ratio"]), fontsize=7,
                                xytext=(5, 3), textcoords="offset points", color=ink)
            ax.set_xscale("log")
            ax.set_yscale("log")
            ax.set_xlabel("planner cost, rewrite / original")
            ax.set_ylabel("measured time, rewrite / original")
            ax.set_title("Does planner cost predict real time?", loc="left", fontsize=10, color=ink)
            fig.tight_layout()
            fig.savefig(fd / "cost_vs_time.png")
            plt.close(fig)
            out.append("figures/cost_vs_time.png")
    return out


def analyze(run_dir, *, dashboard=True) -> dict:
    run_dir = Path(run_dir)
    traces, manifest, opt, guard = load(run_dir)
    if opt:
        from .studies import summarize_optimizer
        opt["summary"] = summarize_optimizer(opt["queries"])  # always use the current definitions
    S = summarize(traces, manifest)
    (run_dir / "summary.json").write_text(json.dumps(S, indent=1, default=str))
    figs = make_figures(run_dir, S, opt, guard)
    write_report(run_dir, S, manifest, opt, guard, figs)
    if dashboard:
        from .dashboard import write_dashboard
        write_dashboard(run_dir, S, manifest, opt, guard, traces)
    return S
