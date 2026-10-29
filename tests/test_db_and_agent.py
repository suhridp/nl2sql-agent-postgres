"""Database safety and the agent pipeline, against real PostgreSQL."""
import json
import time

import pytest

from sql_agent.agent import SQLAgent
from sql_agent.db import PG, DBError
from sql_agent.grading import results_match
from sql_agent.providers import Turn
from sql_agent.scripted import ScriptedLLM


def test_role_cannot_write_even_with_read_only_off(db):
    rw = PG(read_only=False, timeout_ms=3000)
    for s in ["DELETE FROM orders", "UPDATE products SET list_price = 0", "DROP TABLE reviews",
              "CREATE TABLE x (i int)", "CREATE TEMP TABLE x (i int)"]:
        with pytest.raises(DBError) as e:
            rw.raw(s)
        assert e.value.sqlstate in ("42501", "42P01"), s


def test_cursor_path_only_runs_selects(db):
    for s in ["DELETE FROM orders", "SHOW server_version", "SET statement_timeout = 0"]:
        with pytest.raises(DBError):
            db.query(s)


def test_tampered_timeout_does_not_carry_over():
    d = PG(timeout_ms=1000)
    d.query("SELECT set_config('statement_timeout', '0', false)")
    t = time.time()
    with pytest.raises(DBError) as e:
        d.query("SELECT pg_sleep(3)")
    assert e.value.sqlstate == "57014" and time.time() - t < 2.5


def test_row_cap_and_types(db):
    cols, rows, truncated = db.query("SELECT order_id, order_date, 1.5::numeric FROM orders", 10)
    assert len(rows) == 10 and truncated and cols[0] == "order_id"
    assert type(rows[0][1]).__name__ == "date" and type(rows[0][2]).__name__ == "Decimal"


def test_schema_text_levels(ctx):
    names, full = ctx.schema_texts["names"], ctx.schema_texts["full"]
    assert "orders(order_id, customer_id" in names and "--" not in names
    assert "Line revenue = quantity * unit_price * (1 - discount)" in full and "values: cancelled" in full


@pytest.fixture(scope="module")
def gold(db, bench):
    return {q["id"]: db.query(q["sql"], 100000)[1] for q in bench["answerable"]}


def _ex(ctx, bench, gold, llm, system):
    agent = SQLAgent(ctx, llm, system)
    out = {}
    for q in bench["answerable"]:
        a = agent.ask(q["id"], q["question"])
        out[q["id"]] = (a, a.status == "answered" and results_match(gold[q["id"]], a.rows,
                                                                    ordered=q.get("order_matters", False)))
    return out


def test_oracle_scores_100_percent(ctx, bench, gold):
    res = _ex(ctx, bench, gold, ScriptedLLM(bench, "oracle"), "verified")
    assert all(ok for _, ok in res.values()), [k for k, (_, ok) in res.items() if not ok]


def test_repair_recovers_every_loud_error_and_result_check_catches_wrong_case(ctx, bench, gold):
    llm = ScriptedLLM(bench, "noisy")
    no_repair = _ex(ctx, bench, gold, llm, "schema_prompt")
    repair = _ex(ctx, bench, gold, llm, "repair")
    verified = _ex(ctx, bench, gold, llm, "verified")
    items = {q["id"]: q for q in bench["answerable"]}
    for qid, (a, ok) in repair.items():
        kind = (llm.corruption(items[qid]) or ("none",))[0]
        if kind in ("typo_column", "wrong_table", "ambiguous_column", "syntax"):
            assert not no_repair[qid][1] and ok and len(a.attempts) == 2, qid
    case = [qid for qid in repair if (llm.corruption(items[qid]) or ("",))[0] == "wrong_literal_case"
            and items[qid]["sql"].lstrip().lower().startswith("select sum")]
    for qid in case:  # 'Completed' makes sum() return NULL; the result check gets a second look
        assert verified[qid][1] and verified[qid][0].result_check == "changed", qid
    assert sum(ok for _, ok in verified.values()) > sum(ok for _, ok in repair.values()) > \
        sum(ok for _, ok in no_repair.values())


def test_adversarial_requests_never_change_the_database(ctx, bench, db):
    before = db.fingerprint()
    llm = ScriptedLLM(bench, "noisy")  # complies with every attack
    for system in ("zero_shot", "verified"):
        agent = SQLAgent(ctx, llm, system)
        for q in bench["adversarial"]:
            if q["id"] == "d07" and system == "zero_shot":
                continue  # pg_sleep(60) without the validator just waits for the timeout; covered elsewhere
            a = agent.ask(q["id"], q["question"])
            assert a.status != "answered" or "count" in a.sql.lower(), (system, q["id"], a.sql)
            assert not any(x.outcome == "ok" and x.sql == q["attack_sql"] for x in a.attempts)
    assert db.fingerprint() == before


class Recorder:
    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def complete(self, system, prompt, **kw):
        self.prompts.append(prompt)
        return Turn(text=self.reply)


def test_zero_shot_prompt_has_names_only_and_terminal_replies(ctx):
    r = Recorder(json.dumps({"clarify": "Which metric?"}))
    a = SQLAgent(ctx, r, "zero_shot").ask("q", "Show me the best products.")
    assert a.status == "clarify" and "--" not in r.prompts[0]
    assert SQLAgent(ctx, Recorder('{"refuse": "no"}'), "verified").ask("q", "x").status == "refused"
    assert SQLAgent(ctx, Recorder('{"cannot_answer": "no"}'), "verified").ask("q", "x").status == "cannot_answer"
    bad = SQLAgent(ctx, Recorder("I think the answer is 42"), "repair").ask("q", "x")
    assert bad.status == "failed" and bad.attempts[0].outcome == "parse_error" and bad.llm_calls == 4


def test_cost_check_stops_cartesian_product(ctx):
    r = Recorder(json.dumps({"sql": "SELECT count(*) FROM order_items a CROSS JOIN order_items b"}))
    a = SQLAgent(ctx, r, {**{"schema_level": "full", "validate": True, "cost_check": True, "max_repairs": 0,
                             "result_check": False, "optimize": False}}).ask("q", "x")
    assert a.status == "failed" and a.attempts[0].outcome == "cost_check"
