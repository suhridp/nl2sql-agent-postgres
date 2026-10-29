"""The NL-to-SQL agent.

question -> prompt (schema) -> model -> {sql | clarify | cannot_answer | refuse}
         -> validate (SQLGlot) -> cost check (EXPLAIN) -> execute (read-only cursor, timeout, row cap)
         -> result check (empty / NULL answers get one second look) -> repair loop on any failure
         -> optional plan-aware optimization (equivalent rewrite, verified on two databases)

Each stage can be switched off; the named SYSTEMS below are the configurations compared.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field

from .db import DBError
from .validate import validate

SYSTEM_GEN = """You are a careful data analyst who writes PostgreSQL queries for a read-only analytics database.
Reply with exactly one JSON object and nothing else, in one of these forms:
  {"sql": "<one PostgreSQL SELECT statement>"}
  {"clarify": "<one short question>"}   if the request is ambiguous in a way that changes the answer (an undefined metric, threshold, ranking criterion or time period)
  {"cannot_answer": "<reason>"}         if the database does not contain the data needed
  {"refuse": "<reason>"}                if the request asks to change data, settings or permissions, or to access files or system information
Rules: write a single SELECT (WITH ... SELECT is fine). Use only the tables and columns in the schema and follow the conventions in its comments. Return only the columns the question asks for. Do not add LIMIT unless the question asks for a number of rows."""

SYSTEMS = {
    # schema detail, static validation, EXPLAIN cost check, repair attempts, result check, optimizer
    "zero_shot":     dict(schema_level="names", validate=False, cost_check=False, max_repairs=0, result_check=False,
                          optimize=False),
    "schema_prompt": dict(schema_level="full", validate=False, cost_check=False, max_repairs=0, result_check=False,
                          optimize=False),
    "repair":        dict(schema_level="full", validate=True, cost_check=True, max_repairs=3, result_check=False,
                          optimize=False),
    "verified":      dict(schema_level="full", validate=True, cost_check=True, max_repairs=3, result_check=True,
                          optimize=False),
    "optimized":     dict(schema_level="full", validate=True, cost_check=True, max_repairs=3, result_check=True,
                          optimize=True),
}
MAX_COST = 5_000_000   # planner cost units; the most expensive gold query is ~40x below this


@dataclass
class Attempt:
    sql: str = ""
    outcome: str = ""      # ok | parse_error | validator | cost_check | database | result_check
    code: str = ""
    message: str = ""


@dataclass
class Answer:
    qid: str
    question: str
    status: str = "failed"   # answered | clarify | cannot_answer | refused | failed
    sql: str = ""
    cols: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    truncated: bool = False
    reply_text: str = ""     # clarifying question / reason given
    attempts: list = field(default_factory=list)
    llm_calls: int = 0
    in_tokens: int = 0
    out_tokens: int = 0
    result_check: str = ""   # "" | confirmed | changed | failed
    optimizer: dict = field(default_factory=dict)
    latency_s: float = 0.0

    @property
    def first_attempt_ok(self) -> bool:
        return bool(self.attempts) and self.attempts[0].outcome == "ok"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["rows"] = [[_jsonable(v) for v in r] for r in self.rows[:50]]
        d["first_attempt_ok"] = self.first_attempt_ok
        return d


def _jsonable(v):
    return v if isinstance(v, (int, float, str, bool)) or v is None else str(v)


def parse_reply(text: str):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json|sql)?\s*|\s*```$", "", text, flags=re.M).strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
                return obj if isinstance(obj, dict) else None
            except json.JSONDecodeError:
                return None
    return None


@dataclass
class Context:
    """Everything an agent needs about the database, computed once per run."""
    db: object                 # PG on the main schema
    edge_db: object = None     # PG on the edge-case schema, for optimizer equivalence checks
    schema: dict = None        # {table: {col: type}}
    info: dict = None          # full schema_info
    schema_texts: dict = None  # level -> text
    opt_rounds: int = 3
    max_rows: int = 5000       # rows fetched per answer; truncation is recorded and counts as wrong


class SQLAgent:
    def __init__(self, ctx: Context, llm, system: str | dict = "verified", *, sample_key: str = ""):
        self.ctx, self.llm = ctx, llm
        self.cfg = dict(SYSTEMS[system]) if isinstance(system, str) else dict(system)
        self.system_name = system if isinstance(system, str) else "custom"
        self.sample_key = sample_key

    def _call(self, ans: Answer, prompt: str, tag: str) -> dict | None:
        turn = self.llm.complete(SYSTEM_GEN, prompt, sample_key=f"{self.sample_key}|{tag}", max_tokens=1200)
        ans.llm_calls += 1
        ans.in_tokens += turn.in_tokens
        ans.out_tokens += turn.out_tokens
        return parse_reply(turn.text)

    def _check(self, sql: str) -> Attempt | None:
        """Return a failed Attempt if the SQL must not or cannot run, else None."""
        if self.cfg["validate"]:
            v = validate(sql, self.ctx.schema)
            if not v.ok:
                return Attempt(sql, "validator", v.code, v.message)
        if self.cfg["cost_check"]:
            try:
                cost = self.ctx.db.explain(sql)["Plan"]["Total Cost"]
            except DBError as e:
                return Attempt(sql, "database", e.kind, str(e))
            if cost > MAX_COST:
                return Attempt(sql, "cost_check", "too_expensive",
                               f"estimated cost {cost:,.0f} is too high; check the joins and add filters")
        return None

    def _run(self, sql: str):
        att = self._check(sql)
        if att is not None:
            return att, None
        try:
            return Attempt(sql, "ok"), self.ctx.db.query(sql, self.ctx.max_rows)
        except DBError as e:
            return Attempt(sql, "database", e.kind, str(e)), None

    def _result_check(self, ans: Answer, base: str, sql: str, result, k: int):
        """One second look when a query returns nothing or only NULLs: often a wrong literal
        ('Completed' vs 'completed') or a wrong filter, sometimes the true answer."""
        desc = "no rows" if not result[1] else "only NULL values"
        reply = self._call(ans, f"{base}\n\nYour query:\n{sql}\nreturned {desc}. If that is the right answer, "
                                "reply {\"confirm\": true}. Otherwise reply with a corrected {\"sql\": ...}.",
                           f"check{k}")
        new = str((reply or {}).get("sql") or "").strip().rstrip(";").strip()
        if not new or new == sql:
            ans.result_check = "confirmed"
            return sql, result
        att, res2 = self._run(new)
        att.code = att.code or "after_result_check"
        ans.attempts.append(att)
        if att.outcome == "ok":
            ans.result_check = "changed"
            return new, res2
        ans.result_check = "failed"
        return sql, result

    def _hint(self, att: Attempt) -> str:
        if att.code in ("undefined_column", "unknown_column", "ambiguous_column"):
            tables = [t for t in self.ctx.info if re.search(rf"\b{t}\b", att.sql, re.I)]
            cols = "; ".join(f"{t}: {', '.join(self.ctx.info[t]['columns'])}" for t in tables)
            return f"Columns of the tables you used: {cols}" if cols else ""
        return ""

    def ask(self, qid: str, question: str) -> Answer:
        t0 = time.time()
        ans = Answer(qid, question)
        schema_txt = self.ctx.schema_texts[self.cfg["schema_level"]]
        base = f"Database schema:\n{schema_txt}\n\nQuestion: {question}"
        prompt = base
        for k in range(self.cfg["max_repairs"] + 1):
            out = self._call(ans, prompt, f"gen{k}")
            terminal = out is not None and next((x for x in ("refuse", "cannot_answer", "clarify") if x in out), None)
            if terminal:
                ans.status = {"clarify": "clarify", "cannot_answer": "cannot_answer", "refuse": "refused"}[terminal]
                ans.reply_text = str(out[terminal])[:500]
                break
            if out is None:
                att = Attempt("", "parse_error", "not_json", "the reply was not a single JSON object")
            else:
                sql = str(out.get("sql", "")).strip().rstrip(";").strip()
                att, result = self._run(sql)
                if att.outcome == "ok":
                    ans.attempts.append(att)
                    if self.cfg["result_check"] and _suspicious(result[1]):
                        sql, result = self._result_check(ans, base, sql, result, k)
                    ans.status, ans.sql = "answered", sql
                    ans.cols, ans.rows, ans.truncated = result
                    break
            ans.attempts.append(att)
            prompt = (f"{base}\n\nYour previous reply produced this query:\n{att.sql or '(none)'}\n"
                      f"It failed ({att.outcome}): {att.message}\n{self._hint(att)}\n"
                      "Reply with a corrected JSON object.")
        if ans.status == "answered" and self.cfg["optimize"]:
            from .optimizer import optimize_query
            res = optimize_query(self.ctx, ans.sql, rounds=self.ctx.opt_rounds)
            ans.optimizer = res.summary()
            if res.chosen != ans.sql:
                ans.sql = res.chosen
        ans.latency_s = time.time() - t0
        return ans


def _suspicious(rows) -> bool:
    return not rows or all(v is None for r in rows for v in r)
