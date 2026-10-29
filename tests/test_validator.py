"""Static validation: no database needed."""
import yaml

from sql_agent.validate import validate
from conftest import ROOT


def test_every_gold_query_passes(snapshot, bench):
    for q in bench["answerable"]:
        v = validate(q["sql"], snapshot["schema"])
        assert v.ok, (q["id"], v)


def test_attacks_are_rejected_except_resource_hogs(snapshot):
    attacks = yaml.safe_load((ROOT / "bench" / "attacks.yaml").read_text())
    passed = {a["id"] for a in attacks if validate(a["sql"], snapshot["schema"]).ok}
    # huge but syntactically valid SELECTs are the cost check's and the timeout's job, by design
    assert passed == {"r02", "r03"}


def test_specific_rules(snapshot):
    s = snapshot["schema"]
    cases = {
        "WITH d AS (DELETE FROM orders RETURNING *) SELECT count(*) FROM d": "forbidden_construct",
        "SELECT 1; SELECT 2": "multi_statement",
        "SELECT set_config('statement_timeout', '0', false)": "forbidden_function",
        "SELECT * FROM pg_catalog.pg_roles": "system_catalog",
        "SELECT * FROM information_schema.tables": "system_catalog",
        "SELECT nope FROM orders": "unknown_column",
        "SELECT * FROM order_item": "unknown_table",
        "SELECT * FROM orders FOR UPDATE": "forbidden_construct",
        "SELECT * INTO t FROM orders": "forbidden_construct",
        "": "empty",
    }
    for sql, code in cases.items():
        v = validate(sql, s)
        assert not v.ok and v.code == code, (sql, v)


def test_ctes_and_set_operations_allowed(snapshot):
    s = snapshot["schema"]
    assert validate("WITH t AS (SELECT customer_id FROM orders) SELECT count(*) FROM t", s).ok
    assert validate("SELECT country FROM customers UNION SELECT 'XX'", s).ok
    assert validate("SELECT pg_typeof(1)", s).ok
