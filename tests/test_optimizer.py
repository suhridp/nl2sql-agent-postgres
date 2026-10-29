import yaml

from conftest import ROOT
from sql_agent.optimizer import Candidate, OptResult, equivalent, generate, optimize_query, paired_speedup
from sql_agent.studies import _policy_choice

OPT = {q["id"]: q for q in yaml.safe_load((ROOT / "bench" / "optimize.yaml").read_text())}


def rules_for(qid, snapshot):
    cands, notes = generate(OPT[qid]["sql"], snapshot["schema"], snapshot["info"])
    return {r for r, _ in cands}, cands, notes


def test_each_rule_fires_on_its_pattern(snapshot):
    expect = {"o01": "sargable_dates", "o02": "sargable_dates", "o04": "sargable_dates", "o05": "having_to_where",
              "o07": "not_in_to_not_exists", "o08": "in_to_exists", "o09": "correlated_to_join",
              "o10": "correlated_to_join", "o12": "or_to_union_all", "o13": "cte_materialization",
              "o14": "cte_materialization", "o16": "distinct_join_to_exists"}
    for qid, rule in expect.items():
        assert rule in rules_for(qid, snapshot)[0], qid


def test_year_and_month_combine_into_one_month_range(snapshot):
    _, cands, _ = rules_for("o03", snapshot)
    sql = dict(cands)["sargable_dates"]
    assert "'2024-02-01'" in sql and "'2024-03-01'" in sql and "EXTRACT" not in sql.upper()


def test_not_in_on_nullable_column_is_refused(snapshot):
    rules, _, notes = rules_for("o06", snapshot)
    assert "not_in_to_not_exists" not in rules and any("can be NULL" in n for n in notes)


def test_edge_database_catches_a_rewrite_the_main_data_cannot(ctx):
    """LEFT JOIN -> INNER JOIN is wrong when an order has no lines. The main data happens to have
    none, so only the edge-case database exposes it. This is why candidates are checked on both."""
    orig = "SELECT count(*) FROM orders o LEFT JOIN order_items oi ON oi.order_id = o.order_id"
    wrong = "SELECT count(*) FROM orders o JOIN order_items oi ON oi.order_id = o.order_id"
    assert equivalent(ctx.db, orig, wrong)[0]
    ok, why = equivalent(ctx.edge_db, orig, wrong)
    assert not ok and why == "different result"


def test_all_generated_rewrites_are_equivalent_on_both_databases(ctx):
    for qid, q in OPT.items():
        if qid == "o11":
            continue  # slow by design; covered by the study
        for rule, sql in generate(q["sql"], ctx.schema, ctx.info)[0]:
            assert equivalent(ctx.db, q["sql"], sql)[0], (qid, rule)
            assert equivalent(ctx.edge_db, q["sql"], sql)[0], (qid, rule)


def test_paired_speedup_math():
    s = paired_speedup([10, 10, 10, 10], [5, 5, 5, 5])
    assert abs(s["median"] - 2) < 1e-9 and abs(s["lo"] - 2) < 1e-9
    assert paired_speedup([None], [1])["n"] == 0


def test_gate_accepts_a_large_real_win(ctx):
    res = optimize_query(ctx, OPT["o03"]["sql"], rounds=5, rules=["sargable_dates"])
    assert res.chosen != res.original and res.speedup > 3


def test_policies_differ_where_they_should():
    res = OptResult("q", "q", original_cost=100, original_ms=10, original_times=[10] * 5)
    cheap_slow = Candidate("a", "a", cost=50, median_ms=30, speedup={"median": 0.33, "lo": 0.3, "hi": 0.4},
                           equivalent_main=True, equivalent_edge=True)
    dear_fast = Candidate("b", "b", cost=200, median_ms=4, speedup={"median": 2.5, "lo": 2.2, "hi": 2.8},
                          equivalent_main=True, equivalent_edge=True, accepted=True)
    res.candidates = [cheap_slow, dear_fast]
    assert _policy_choice(res, "cost_top1") is cheap_slow      # trusting the planner picks the slow one
    assert _policy_choice(res, "gate_topk1") is None           # it times the cheap one and rejects it
    assert _policy_choice(res, "gate_all") is dear_fast
    assert _policy_choice(res, "original") is None
