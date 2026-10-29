"""Plan-aware query optimization: propose equivalent rewrites, prove they return the same rows on two
databases, time them fairly, and accept one only when it is reliably faster.

Pipeline for one query
  1. candidates   rule-based rewrites on the SQLGlot AST (each rule states when it is correct),
                  SQLGlot's own optimizer, and optionally rewrites proposed by an LLM
  2. safety       every candidate must pass the same validator as the agent's SQL
  3. equivalence  identical results on the main data AND on the small edge-case database
                  (NULLs, empty groups, ties, boundary dates); anything else is rejected
  4. planner cost EXPLAIN cost of every equivalent candidate (cheap, no execution)
  5. timing       interleaved rounds in shuffled order after a warm-up; server-side time from
                  EXPLAIN (ANALYZE, TIMING OFF) = planning + execution
  6. decision     accept the best candidate only if the bootstrap 95% CI of its paired speedup
                  over the original lies entirely above 1 + min_gain

Rules
  sargable_dates      extract(year ...) = N, date_trunc(unit, col) = d, col + k > d  ->  range on col
  having_to_where     HAVING conditions on grouped columns only  ->  WHERE
  in_to_exists        x IN (SELECT y ...)  ->  EXISTS (... AND y = x)                         always safe
  not_in_to_not_exists x NOT IN (SELECT y ...)  ->  NOT EXISTS (...)  only if x and y are NOT NULL
  correlated_to_join  correlated scalar aggregate subquery  ->  LEFT JOIN to a grouped derived table
  or_to_union_all     single-table WHERE a OR b  ->  UNION ALL of (a) and (b AND a IS NOT TRUE)
  cte_materialization flip MATERIALIZED / NOT MATERIALIZED
  distinct_join_to_exists SELECT DISTINCT a.* FROM a JOIN b  ->  ... WHERE EXISTS (b)
  sqlglot_optimize    SQLGlot's optimizer output
"""
from __future__ import annotations

import datetime as dt
import json
import math
import random
import statistics
import time
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.optimizer import optimize as sqlglot_optimize
from sqlglot.optimizer.qualify import qualify

from .db import DBError
from .grading import same_result
from .validate import validate

D = "postgres"


# ------------------------------------------------------------------------------------------
# AST helpers
# ------------------------------------------------------------------------------------------

def conjuncts(e):
    if e is None:
        return []
    if isinstance(e, exp.Paren):
        return conjuncts(e.this) if isinstance(e.this, exp.And) else [e]
    if isinstance(e, exp.And):
        return conjuncts(e.this) + conjuncts(e.expression)
    return [e]


def disjuncts(e):
    if isinstance(e, exp.Paren) and isinstance(e.this, exp.Or):
        return disjuncts(e.this)
    if isinstance(e, exp.Or):
        return disjuncts(e.this) + disjuncts(e.expression)
    return [e]


def and_all(parts):
    parts = [p for p in parts if p is not None]
    if not parts:
        return None
    out = parts[0]
    for p in parts[1:]:
        out = exp.And(this=out, expression=p)
    return out


def _from(sel):
    return sel.args.get("from_") or sel.args.get("from")


def _date_lit(e):
    if isinstance(e, exp.Cast) and e.to.is_type("date") and isinstance(e.this, exp.Literal) and e.this.is_string:
        e = e.this
    if isinstance(e, exp.Literal) and e.is_string:
        try:
            return dt.date.fromisoformat(e.name[:10])
        except ValueError:
            return None
    return None


def _dlit(d: dt.date):
    return exp.cast(exp.Literal.string(d.isoformat()), "date")


def _next(d: dt.date, unit: str) -> dt.date:
    if unit == "day":
        return d + dt.timedelta(days=1)
    if unit == "month":
        return dt.date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    if unit == "year":
        return dt.date(d.year + 1, 1, 1)
    raise ValueError(unit)


def _range(col, lo, hi):
    return exp.Paren(this=exp.And(this=exp.GTE(this=col.copy(), expression=_dlit(lo)),
                                  expression=exp.LT(this=col.copy(), expression=_dlit(hi))))


def _alias_map(sel) -> dict:
    """alias -> table name for base tables directly in this SELECT's FROM/JOINs."""
    out = {}
    nodes = [_from(sel)] + list(sel.args.get("joins") or [])
    for n in nodes:
        if n is None:
            continue
        t = n.this
        if isinstance(t, exp.Table):
            out[t.alias_or_name] = t.name
    return out


def _qualified(tree, schema):
    return qualify(tree.copy(), schema=schema, dialect=D, quote_identifiers=False, validate_qualify_columns=False)


def _nullable(col: exp.Column, sel, info) -> bool:
    """True unless the column is known to be NOT NULL in the schema."""
    table = _alias_map(sel).get(col.table)
    try:
        return info[table]["columns"][col.name]["nullable"]
    except (KeyError, TypeError):
        return True


# ------------------------------------------------------------------------------------------
# Rules. Each takes (tree, schema, info, notes) and returns a list of new trees.
# ------------------------------------------------------------------------------------------

def r_sargable_dates(tree, schema, info, notes):
    t = tree.copy()
    changed = False
    for where in t.find_all(exp.Where):
        parts, ym, out = conjuncts(where.this), {}, []
        for p in parts:
            new = None
            if isinstance(p, exp.EQ):
                a, b = (p.this, p.expression) if isinstance(p.this, (exp.Extract, exp.TimestampTrunc, exp.DateTrunc)) \
                    else (p.expression, p.this)
                if isinstance(a, exp.Extract) and isinstance(a.expression, exp.Column) and \
                        isinstance(b, exp.Literal) and b.is_int and a.this.name.lower() in ("year", "month"):
                    ym.setdefault(a.expression.sql(), {})[a.this.name.lower()] = (len(out), int(b.name), a.expression)
                elif isinstance(a, (exp.TimestampTrunc, exp.DateTrunc)) and isinstance(a.this, exp.Column):
                    unit, d = a.args["unit"].name.lower(), _date_lit(b)
                    if d and unit in ("day", "month", "year") and d == _trunc(d, unit):
                        new = _range(a.this, d, _next(d, unit))
            elif isinstance(p, (exp.GT, exp.GTE, exp.LT, exp.LTE)) and isinstance(p.this, (exp.Add, exp.Sub)):
                arith, d = p.this, _date_lit(p.expression)
                if d and isinstance(arith.this, exp.Column) and isinstance(arith.expression, exp.Literal) \
                        and arith.expression.is_int:
                    k = int(arith.expression.name) * (1 if isinstance(arith, exp.Add) else -1)
                    new = type(p)(this=arith.this.copy(), expression=_dlit(d - dt.timedelta(days=k)))
            out.append(new if new is not None else p)
            changed |= new is not None
        drop = set()
        for _, u in ym.items():
            if "year" in u:
                i, y, col = u["year"]
                if "month" in u:
                    j, mth, _ = u["month"]
                    if 1 <= mth <= 12:
                        lo = dt.date(y, mth, 1)
                        out[i], changed = _range(col, lo, _next(lo, "month")), True
                        drop.add(j)
                        continue
                out[i], changed = _range(col, dt.date(y, 1, 1), dt.date(y + 1, 1, 1)), True
        out = [p for k, p in enumerate(out) if k not in drop]
        where.set("this", and_all(out))
    return [t] if changed else []


def _trunc(d, unit):
    return {"day": d, "month": d.replace(day=1), "year": d.replace(month=1, day=1)}[unit]


def r_having_to_where(tree, schema, info, notes):
    t = tree.copy()
    changed = False
    for sel in t.find_all(exp.Select):
        having, group = sel.args.get("having"), sel.args.get("group")
        if not having or not group:
            continue
        gb = {e.sql() for e in group.expressions}
        move, keep = [], []
        for p in conjuncts(having.this):
            cols = list(p.find_all(exp.Column))
            if cols and not p.find(exp.AggFunc) and not p.find(exp.Window) and all(c.sql() in gb for c in cols):
                move.append(p.copy())
            else:
                keep.append(p)
        if move:
            sel.where(and_all(move), append=True, copy=False)
            sel.set("having", exp.Having(this=and_all(keep)) if keep else None)
            changed = True
    return [t] if changed else []


def _subquery_select(inn: exp.In):
    q = inn.args.get("query")
    sub = q.this if isinstance(q, exp.Subquery) else q
    if not isinstance(sub, exp.Select) or len(sub.expressions) != 1:
        return None
    if any(sub.args.get(k) for k in ("group", "having", "limit", "offset")) or sub.find(exp.AggFunc):
        return None
    proj = sub.expressions[0]
    proj = proj.this if isinstance(proj, exp.Alias) else proj
    return sub, proj


def _in_rules(tree, schema, info, notes, negated: bool):
    q = _qualified(tree, schema)
    changed = False
    for sel in list(q.find_all(exp.Select)):
        where = sel.args.get("where")
        if not where:
            continue
        out = []
        for p in conjuncts(where.this):
            inn = p.this if isinstance(p, exp.Not) else p
            if isinstance(inn, exp.In) and inn.args.get("query") is not None and isinstance(p, exp.Not) == negated:
                got = _subquery_select(inn)
                if got and isinstance(got[1], exp.Column) and isinstance(inn.this, exp.Column):
                    sub, proj = got
                    if negated and (_nullable(inn.this, sel, info) or _nullable(proj, sub, info)):
                        notes.append(f"not_in_to_not_exists skipped: {inn.this.sql()} or {proj.sql()} can be NULL, "
                                     "and NOT IN differs from NOT EXISTS when NULLs are present")
                        out.append(p)
                        continue
                    new = sub.copy()
                    new.set("expressions", [exp.Literal.number(1)])
                    new.where(exp.EQ(this=proj.copy(), expression=inn.this.copy()), append=True, copy=False)
                    rep = exp.Exists(this=new)
                    out.append(exp.Not(this=rep) if negated else rep)
                    changed = True
                    continue
            out.append(p)
        where.set("this", and_all(out))
    return [q] if changed else []


def r_in_to_exists(tree, schema, info, notes):
    return _in_rules(tree, schema, info, notes, negated=False)


def r_not_in_to_not_exists(tree, schema, info, notes):
    return _in_rules(tree, schema, info, notes, negated=True)


def r_correlated_to_join(tree, schema, info, notes):
    q = _qualified(tree, schema)
    changed, n = False, 0
    for sel in list(q.find_all(exp.Select)):
        outer_aliases = set(_alias_map(sel))
        if not outer_aliases:
            continue
        for sq in list(sel.find_all(exp.Subquery)):
            if sq.parent_select is not sel or isinstance(sq.parent, (exp.In, exp.Exists, exp.From, exp.Join)):
                continue
            inner = sq.this
            if not isinstance(inner, exp.Select) or len(inner.expressions) != 1 or inner.args.get("joins") \
                    or any(inner.args.get(k) for k in ("group", "having", "limit", "distinct")):
                continue
            proj = inner.expressions[0]
            proj = proj.this if isinstance(proj, exp.Alias) else proj
            if not isinstance(proj, (exp.Avg, exp.Sum, exp.Min, exp.Max, exp.Count)):
                continue
            if sel.args.get("group") and sq.find_ancestor(exp.Where) is None:
                continue
            inner_aliases = set(_alias_map(inner))
            corr, rest = [], []
            for p in conjuncts(inner.args["where"].this if inner.args.get("where") else None):
                cols = list(p.find_all(exp.Column))
                outer_cols = [c for c in cols if c.table in outer_aliases and c.table not in inner_aliases]
                if isinstance(p, exp.EQ) and len(outer_cols) == 1 and len(cols) == 2:
                    ic = p.this if p.expression is outer_cols[0] else p.expression
                    if isinstance(ic, exp.Column) and ic.table in inner_aliases:
                        corr.append((ic, outer_cols[0]))
                        continue
                if outer_cols:
                    corr = None
                    break
                rest.append(p)
            if not corr:
                continue
            alias = f"_s{n}"
            n += 1
            derived = exp.select(*[exp.alias_(ic.copy(), f"_k{i}") for i, (ic, _) in enumerate(corr)],
                                 exp.alias_(proj.copy(), "_v"))
            derived.set("from_" if "from_" in inner.args else "from", _from(inner).copy())
            if rest:
                derived.where(and_all([r.copy() for r in rest]), copy=False)
            derived.group_by(*[ic.copy() for ic, _ in corr], copy=False)
            on = and_all([exp.EQ(this=exp.column(f"_k{i}", alias), expression=oc.copy())
                          for i, (_, oc) in enumerate(corr)])
            sel.join(derived.subquery(alias), on=on, join_type="left", copy=False)
            val = exp.column("_v", alias)
            sq.replace(exp.Coalesce(this=val, expressions=[exp.Literal.number(0)]) if isinstance(proj, exp.Count)
                       else val)
            changed = True
    return [q] if changed else []


def r_or_to_union_all(tree, schema, info, notes):
    if not isinstance(tree, exp.Select) or tree.args.get("joins") or not tree.args.get("where"):
        return []
    if any(tree.args.get(k) for k in ("group", "having", "distinct", "limit", "order", "with")) or tree.find(exp.Window):
        return []
    tb = _from(tree).this if _from(tree) else None
    ds = disjuncts(tree.args["where"].this)
    if not isinstance(tb, exp.Table) or len(ds) < 2 or len(ds) > 4:
        return []
    branches = []
    for i, d in enumerate(ds):
        cond = d.copy()
        if i:
            prior = exp.Paren(this=and_or([x.copy() for x in ds[:i]]))
            cond = exp.And(this=exp.Paren(this=cond),
                           expression=exp.Not(this=exp.Paren(this=exp.Is(this=prior, expression=exp.true()))))
        branches.append(exp.select("*").from_(tb.copy()).where(cond))
    union = branches[0]
    for b in branches[1:]:
        union = exp.union(union, b, distinct=False)
    out = tree.copy()
    out.set("where", None)
    out.set("from_" if "from_" in tree.args else "from", exp.From(this=union.subquery(tb.alias_or_name)))
    return [out]


def and_or(parts):
    out = parts[0]
    for p in parts[1:]:
        out = exp.Or(this=out, expression=p)
    return out


def r_cte_materialization(tree, schema, info, notes):
    outs = []
    for i, cte in enumerate(list(tree.find_all(exp.CTE))):
        refs = sum(1 for t in tree.find_all(exp.Table) if t.name == cte.alias_or_name)
        mat = cte.args.get("materialized")
        target = (not mat) if mat is not None else (False if refs >= 2 else None)
        if target is None:
            continue
        t = tree.copy()
        list(t.find_all(exp.CTE))[i].set("materialized", target)
        outs.append(t)
    return outs


def r_distinct_join_to_exists(tree, schema, info, notes):
    q = _qualified(tree, schema)
    if not isinstance(q, exp.Select) or not q.args.get("distinct") or len(q.args.get("joins") or []) != 1:
        return []
    j = q.args["joins"][0]
    if j.side or j.kind not in ("", "INNER", None) or not isinstance(j.this, exp.Table):
        return []
    a, b = _from(q).this, j.this
    if not isinstance(a, exp.Table):
        return []
    aa, ba = a.alias_or_name, b.alias_or_name
    if any(c.table == ba for e in q.expressions for c in e.find_all(exp.Column)):
        return []
    if any(q.args.get(k) for k in ("group", "having", "order", "limit")):
        return []
    on = conjuncts(j.args.get("on"))
    keep_a, to_b = [], []
    for p in on + conjuncts(q.args["where"].this if q.args.get("where") else None):
        tables = {c.table for c in p.find_all(exp.Column)}
        if tables <= {aa}:
            keep_a.append(p.copy())
        elif ba in tables:
            to_b.append(p.copy())
        else:
            return []
    sub = exp.select("1").from_(b.copy()).where(and_all(to_b))
    out = exp.select(*[e.copy() for e in q.expressions]).distinct().from_(a.copy())
    out = out.where(and_all(keep_a + [exp.Exists(this=sub)]))
    return [out]


def r_sqlglot(tree, schema, info, notes):
    return [sqlglot_optimize(tree.copy(), schema=schema, dialect=D)]


RULES = {"sargable_dates": r_sargable_dates, "having_to_where": r_having_to_where, "in_to_exists": r_in_to_exists,
         "not_in_to_not_exists": r_not_in_to_not_exists, "correlated_to_join": r_correlated_to_join,
         "or_to_union_all": r_or_to_union_all, "cte_materialization": r_cte_materialization,
         "distinct_join_to_exists": r_distinct_join_to_exists, "sqlglot_optimize": r_sqlglot}

LLM_SYSTEM = ("You rewrite PostgreSQL SELECT queries to run faster while returning exactly the same rows "
              "(same columns in the same order, same duplicates, NULLs handled identically). Reply with JSON only: "
              '{"rewrites": ["<sql>", ...]} with at most 3 rewrites, or an empty list if you see no improvement.')


def generate(sql: str, schema: dict, info: dict, rules=None, llm=None, plan_text: str = "") -> tuple[list, list]:
    """Return ([(rule, sql)], notes)."""
    notes, out, seen = [], [], {_norm(sql)}
    tree = sqlglot.parse_one(sql, dialect=D)
    for name in rules or RULES:
        try:
            for t in RULES[name](tree, schema, info, notes):
                s = t.sql(dialect=D)
                if _norm(s) not in seen:
                    seen.add(_norm(s))
                    out.append((name, s))
        except Exception as e:  # a rule that cannot handle a query shape simply does not apply
            notes.append(f"{name} not applied: {type(e).__name__}: {str(e)[:120]}")
    if llm is not None:
        try:
            turn = llm.complete(LLM_SYSTEM, f"Query:\n{sql}\n\nCurrent plan:\n{plan_text[:3000]}", max_tokens=1500,
                                sample_key=f"opt|{_norm(sql)}")
            data = json.loads(turn.text.strip().strip("`").removeprefix("json").strip())
            for s in (data.get("rewrites") or [])[:3]:
                if isinstance(s, str) and _norm(s) not in seen:
                    seen.add(_norm(s))
                    out.append(("llm_rewrite", s.strip().rstrip(";")))
        except Exception as e:
            notes.append(f"llm_rewrite failed: {type(e).__name__}")
    return out, notes


def _norm(s):
    return " ".join(s.lower().split())


# ------------------------------------------------------------------------------------------
# Verification and timing
# ------------------------------------------------------------------------------------------

def _ordered(sql):
    t = sqlglot.parse_one(sql, dialect=D)
    return bool(t.args.get("order"))


def equivalent(db, sql_a, sql_b, *, max_rows=200_000, ordered=False) -> tuple[bool, str]:
    try:
        _, ra, ta = db.query(sql_a, max_rows)
        _, rb, tb = db.query(sql_b, max_rows)
    except DBError as e:
        return False, f"error: {e.kind}"
    if ta or tb:
        return False, "result too large to verify"
    return (True, "") if same_result(ra, rb, ordered=ordered) else (False, "different result")


def plan_cost(db, sql) -> float | None:
    try:
        return db.explain(sql)["Plan"]["Total Cost"]
    except DBError:
        return None


def time_once(db, sql) -> float | None:
    try:
        e = db.explain(sql, analyze=True, timing=False)
        return e["Planning Time"] + e["Execution Time"]
    except DBError:
        return None


def measure(db, sqls: list[str], rounds: int, seed: int = 0, warmup: int = 1) -> list[list]:
    """Interleaved timing: in each round every query runs once, in a fresh random order."""
    rng = random.Random(seed)
    for _ in range(warmup):
        for s in sqls:
            time_once(db, s)
    times = [[] for _ in sqls]
    for _ in range(rounds):
        order = list(range(len(sqls)))
        rng.shuffle(order)
        for i in order:
            times[i].append(time_once(db, sqls[i]))
    return times


def paired_speedup(base: list, cand: list, iters: int = 2000, seed: int = 0) -> dict:
    """Median paired speedup (base/cand per round) with a bootstrap 95% CI over rounds."""
    pairs = [(b, c) for b, c in zip(base, cand, strict=False) if b and c]
    if not pairs:
        return {"median": None, "lo": None, "hi": None, "n": 0}
    logs = [math.log(b / c) for b, c in pairs]
    rng = random.Random(seed)
    meds = sorted(statistics.median(rng.choices(logs, k=len(logs))) for _ in range(iters))
    return {"median": math.exp(statistics.median(logs)), "lo": math.exp(meds[int(0.025 * iters)]),
            "hi": math.exp(meds[min(iters - 1, int(0.975 * iters))]), "n": len(pairs)}


@dataclass
class Candidate:
    rule: str
    sql: str
    valid: bool = True
    equivalent_main: bool | None = None
    equivalent_edge: bool | None = None
    reject_reason: str = ""
    cost: float | None = None
    times_ms: list = field(default_factory=list)
    median_ms: float | None = None
    speedup: dict = field(default_factory=dict)
    accepted: bool = False

    @property
    def verified(self):
        return self.valid and self.equivalent_main and self.equivalent_edge is not False


@dataclass
class OptResult:
    original: str
    chosen: str
    original_cost: float | None = None
    original_ms: float | None = None
    original_times: list = field(default_factory=list)
    candidates: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    timed_runs: int = 0
    wall_s: float = 0.0

    @property
    def speedup(self) -> float:
        for c in self.candidates:
            if c.sql == self.chosen:
                return c.speedup.get("median") or 1.0
        return 1.0

    def summary(self) -> dict:
        return {"chosen_rule": next((c.rule for c in self.candidates if c.sql == self.chosen), None),
                "speedup": self.speedup, "n_candidates": len(self.candidates),
                "n_verified": sum(c.verified for c in self.candidates), "original_ms": self.original_ms,
                "timed_runs": self.timed_runs, "wall_s": round(self.wall_s, 2)}

    def to_dict(self) -> dict:
        return {"original": self.original, "chosen": self.chosen, "original_cost": self.original_cost,
                "original_ms": self.original_ms, "original_times": self.original_times, "notes": self.notes,
                "timed_runs": self.timed_runs, "wall_s": self.wall_s,
                "candidates": [c.__dict__ | {"verified": c.verified} for c in self.candidates]}


def optimize_query(ctx, sql: str, *, rounds: int = 9, min_gain: float = 0.05, rules=None, llm=None,
                   top_k: int | None = None, seed: int = 0, timing_db=None) -> OptResult:
    """Full pipeline for one query. ``top_k`` (if set) times only the k candidates with the lowest
    planner cost; otherwise every verified candidate is timed."""
    t0 = time.time()
    db = timing_db or ctx.db
    res = OptResult(sql, sql)
    plan_text = ""
    if llm is not None:
        try:
            plan_text = json.dumps(ctx.db.explain(sql)["Plan"])[:3000]
        except DBError:
            pass
    cands, notes = generate(sql, ctx.schema, ctx.info, rules=rules, llm=llm, plan_text=plan_text)
    res.notes = notes
    ordered = _ordered(sql)
    for rule, s in cands:
        c = Candidate(rule, s)
        res.candidates.append(c)
        v = validate(s, ctx.schema)
        if not v.ok:
            c.valid, c.reject_reason = False, f"validator: {v.code}"
            continue
        c.equivalent_main, why = equivalent(ctx.db, sql, s, ordered=ordered)
        if not c.equivalent_main:
            c.reject_reason = f"main data: {why}"
            continue
        if ctx.edge_db is not None:
            c.equivalent_edge, why = equivalent(ctx.edge_db, sql, s, ordered=ordered)
            if not c.equivalent_edge:
                c.reject_reason = f"edge data: {why}"
                continue
        c.cost = plan_cost(ctx.db, s)
    res.original_cost = plan_cost(ctx.db, sql)
    ok = [c for c in res.candidates if c.verified]
    if top_k is not None:
        ok = sorted(ok, key=lambda c: (c.cost is None, c.cost or 0))[:top_k]
    times = measure(db, [sql] + [c.sql for c in ok], rounds, seed=seed)
    res.timed_runs = (rounds + 1) * (1 + len(ok))
    res.original_times = times[0]
    good = [x for x in times[0] if x is not None]
    res.original_ms = statistics.median(good) if good else None
    best = None
    for c, ts in zip(ok, times[1:], strict=True):
        c.times_ms = ts
        g = [x for x in ts if x is not None]
        c.median_ms = statistics.median(g) if g else None
        c.speedup = paired_speedup(times[0], ts, seed=seed)
        if c.speedup.get("lo") and c.speedup["lo"] > 1 + min_gain:
            c.accepted = True
            if best is None or c.speedup["median"] > best.speedup["median"]:
                best = c
    if best is not None:
        res.chosen = best.sql
    res.wall_s = time.time() - t0
    return res
