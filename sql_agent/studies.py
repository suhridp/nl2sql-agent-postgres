"""Two studies that need only PostgreSQL (no model): the optimizer study and the guardrail study.

Optimizer study
  For each query in bench/optimize.yaml: generate and verify candidates, time them, then compare
  selection policies. Choosing a winner and measuring it on the same timings overstates the gain
  (winner's curse), so each policy's choice is re-timed against the original in a separate
  confirmation run with a different seed, and that run is what gets reported.

  Policies
    original        never rewrite
    sqlglot_only    phase-1 behaviour: take SQLGlot's rewrite if its median is >= 10% faster
    cost_top1       trust the planner: take the verified candidate with the lowest EXPLAIN cost,
                    if lower than the original's (no timing at all)
    gate_topk1      time only the cheapest-by-cost candidate, accept it through the statistical gate
    gate_all        time every verified candidate, accept the best through the gate (default)

Guardrail study
  Each statement in bench/attacks.yaml goes through every layer on its own, then the full stack:
    validator         static SQLGlot checks
    cost_check        EXPLAIN as the analyst; blocked if it errors or the estimated cost is too high
    role_only         the analyst role with read-only mode switched OFF for the session, statement
                      sent as-is: what database privileges alone stop
    execution_path    the agent's execution path without the validator: read-only transaction,
                      SELECT-only cursor, analyst role, short statement timeout
    full_stack        validator, then cost check, then the execution path
  A fingerprint of every table is taken before and after; any change fails the study.
"""
from __future__ import annotations

import statistics
import time

import yaml

from .agent import MAX_COST
from .db import PG, DBError
from .optimizer import RULES, measure, optimize_query, paired_speedup
from .validate import validate

POLICIES = ["original", "sqlglot_only", "cost_top1", "gate_topk1", "gate_all"]


def _policy_choice(res, policy: str, min_gain: float = 0.05):
    verified = [c for c in res.candidates if c.verified]
    if policy == "original":
        return None
    if policy == "sqlglot_only":
        c = next((c for c in verified if c.rule == "sqlglot_optimize"), None)
        return c if c and c.median_ms and res.original_ms and c.median_ms <= 0.9 * res.original_ms else None
    if policy == "cost_top1":
        c = min((c for c in verified if c.cost is not None), key=lambda c: c.cost, default=None)
        return c if c and res.original_cost and c.cost < res.original_cost else None
    if policy == "gate_topk1":
        c = min((c for c in verified if c.cost is not None), key=lambda c: c.cost, default=None)
        return c if c and c.speedup.get("lo") and c.speedup["lo"] > 1 + min_gain else None
    if policy == "gate_all":
        acc = [c for c in verified if c.accepted]
        return max(acc, key=lambda c: c.speedup["median"], default=None)
    raise ValueError(policy)


def _timing_runs(res, policy):
    """How many timed executions a policy needs (warm-up included)."""
    verified = [c for c in res.candidates if c.verified]
    per = len(res.original_times) + 1
    return {"original": 0, "cost_top1": 0, "sqlglot_only": 2 * per, "gate_topk1": 2 * per if verified else 0,
            "gate_all": per * (1 + len(verified))}[policy]


def optimizer_study(ctx, queries: list[dict], *, rounds: int = 9, confirm_rounds: int = 9, seed: int = 0,
                    llm=None, log=print) -> dict:
    out = []
    for q in queries:
        t0 = time.time()
        res = optimize_query(ctx, q["sql"], rounds=rounds, seed=seed, llm=llm)
        row = {"id": q["id"], "expect": q.get("expect", ""), "sql": q["sql"], "result": res.to_dict(), "policies": {}}
        choices = {p: _policy_choice(res, p) for p in POLICIES}
        distinct = {c.sql: c for c in choices.values() if c is not None}
        confirm = {}
        if distinct:
            sqls = [q["sql"]] + list(distinct)
            times = measure(ctx.db, sqls, confirm_rounds, seed=seed + 1000)
            for s, ts in zip(sqls[1:], times[1:], strict=True):
                confirm[s] = paired_speedup(times[0], ts, seed=seed + 1)
        for p, c in choices.items():
            sp = confirm.get(c.sql) if c else None
            row["policies"][p] = {"rule": c.rule if c else None,
                                  "confirmed_speedup": sp["median"] if sp else 1.0,
                                  "confirmed_lo": sp["lo"] if sp else 1.0, "confirmed_hi": sp["hi"] if sp else 1.0,
                                  "timing_runs": _timing_runs(res, p)}
        out.append(row)
        g = row["policies"]["gate_all"]
        log(f"  {q['id']}: {len(res.candidates)} candidates, {sum(c.verified for c in res.candidates)} verified, "
            f"gate_all -> {g['rule'] or 'original'} x{g['confirmed_speedup']:.2f}  ({time.time() - t0:.1f}s)")
    return {"queries": out, "rounds": rounds, "confirm_rounds": confirm_rounds, "rules": list(RULES),
            "summary": summarize_optimizer(out)}


def summarize_optimizer(rows: list[dict]) -> dict:
    s = {}
    for p in POLICIES:
        sp = [r["policies"][p]["confirmed_speedup"] for r in rows]
        chosen = [r for r in rows if r["policies"][p]["rule"]]
        s[p] = {"n_rewritten": len(chosen),
                "geomean_speedup": statistics.geometric_mean(sp) if sp else 1.0,
                "median_speedup": statistics.median(sp) if sp else 1.0,
                "n_regressions": sum(1 for r in chosen if r["policies"][p]["confirmed_hi"] < 1.0),
                "n_slower_median": sum(1 for r in chosen if r["policies"][p]["confirmed_speedup"] < 1.0),
                "timing_runs": sum(r["policies"][p]["timing_runs"] for r in rows)}
    # how well does planner cost rank candidates by real time?
    pairs, regrets, n_q = [], [], 0
    for r in rows:
        res = r["result"]
        cs = [c for c in res["candidates"] if c["verified"] and c["cost"] and c["median_ms"]]
        pts = [(res["original_cost"], 0, res["original_ms"])] + [(c["cost"], 1, c["median_ms"]) for c in cs]
        if len(pts) >= 2 and all(p[0] and p[2] for p in pts):
            n_q += 1
            for c in cs:
                pairs.append({"id": r["id"], "rule": c["rule"], "cost_ratio": c["cost"] / res["original_cost"],
                              "time_ratio": c["median_ms"] / res["original_ms"]})
            cheapest = min(pts, key=lambda p: (p[0], p[1]))  # on a cost tie the planner keeps the original
            regrets.append({"id": r["id"], "regret": cheapest[2] / min(p[2] for p in pts)})
    misled = [x for x in regrets if x["regret"] > 1.10]
    wrong_dir = [p for p in pairs if (p["cost_ratio"] < 0.95 and p["time_ratio"] > 1.05)
                 or (p["cost_ratio"] > 1.05 and p["time_ratio"] < 0.95)]
    s["cost_vs_time"] = {"pairs": pairs, "queries": n_q, "n_misled": len(misled),
                         "misled": [{"id": x["id"], "regret": x["regret"]} for x in misled],
                         "geomean_regret": statistics.geometric_mean([x["regret"] for x in regrets]) if regrets else 1.0,
                         "n_pairs": len(pairs), "n_wrong_direction": len(wrong_dir),
                         "spearman": _spearman([p["cost_ratio"] for p in pairs], [p["time_ratio"] for p in pairs])}
    s["unsafe_rewrites_refused"] = sum(1 for r in rows for n in r["result"]["notes"] if "skipped" in n)
    s["rejected_not_equivalent"] = sum(1 for r in rows for c in r["result"]["candidates"]
                                       if c["valid"] and not c["verified"])
    return s


def _spearman(x, y):
    if len(x) < 3:
        return None

    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        for k, i in enumerate(order):
            r[i] = k
        return r
    rx, ry = ranks(x), ranks(y)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else None


# ------------------------------------------------------------------------------------------

def _outcome(fn):
    try:
        r = fn()
        return {"blocked": False, "outcome": "executed", "detail": f"{len(r) if r is not None else 0} rows"}
    except DBError as e:
        if e.sqlstate == "57014":
            return {"blocked": True, "outcome": "timeout", "detail": str(e)[:120]}
        return {"blocked": True, "outcome": e.kind, "detail": str(e)[:120]}


def guardrail_study(dsn: str | None, attacks: list[dict], *, timeout_ms: int = 3000, log=print) -> dict:
    ref = PG(dsn, timeout_ms=60000)
    schema = ref.schema()
    before = ref.fingerprint()
    exec_path = PG(dsn, timeout_ms=timeout_ms)
    rows = []
    for a in attacks:
        sql = a["sql"]
        v = validate(sql, schema)
        res = {"id": a["id"], "category": a["category"], "sql": sql,
               "validator": {"blocked": not v.ok, "outcome": v.code or "passed", "detail": v.message[:120]}}

        def cost_check():
            c = exec_path.explain(sql)["Plan"]["Total Cost"]
            if c > MAX_COST:
                raise DBError(f"estimated cost {c:,.0f} over limit", "cost")
            return []
        res["cost_check"] = _outcome(cost_check)
        if res["cost_check"]["outcome"] == "sqlstate_cost":
            res["cost_check"]["outcome"] = "too_expensive"
        role = PG(dsn, timeout_ms=timeout_ms, read_only=False)
        res["role_only"] = _outcome(lambda: (role.raw(sql)[1] or []))
        role.close()
        res["execution_path"] = _outcome(lambda: exec_path.query(sql)[1])
        if not v.ok:
            res["full_stack"] = {"blocked": True, "outcome": f"validator:{v.code}", "detail": ""}
        elif res["cost_check"]["blocked"]:
            res["full_stack"] = {"blocked": True, "outcome": f"cost_check:{res['cost_check']['outcome']}",
                                 "detail": ""}
        else:
            res["full_stack"] = res["execution_path"]
        rows.append(res)
        log(f"  {a['id']:4} {a['category']:16} validator={res['validator']['outcome']:20} "
            f"role_only={res['role_only']['outcome']:22} exec_path={res['execution_path']['outcome']:22} "
            f"full={res['full_stack']['outcome']}")
    after = ref.fingerprint()
    layers = ["validator", "cost_check", "role_only", "execution_path", "full_stack"]
    return {"attacks": rows, "timeout_ms": timeout_ms, "db_unchanged": before == after,
            "fingerprint_before": before, "fingerprint_after": after,
            "summary": {k: sum(r[k]["blocked"] for r in rows) for k in layers} | {"n": len(rows)},
            "by_category": _by_category(rows, layers)}


def _by_category(rows, layers):
    cats = {}
    for r in rows:
        c = cats.setdefault(r["category"], {k: 0 for k in layers} | {"n": 0})
        c["n"] += 1
        for k in layers:
            c[k] += r[k]["blocked"]
    return cats


def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)
