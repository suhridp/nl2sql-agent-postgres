"""Port Spider-format benchmarks (SQLite databases + questions with gold SQL) to PostgreSQL.

    python -m sql_agent.spider --spider-dir ~/data/spider --split dev --dbs concert_singer,pets_1 \
        --out bench/spider_dev.yaml

For each database: create schema ``spider_<db_id>`` (owner connection), copy every table with mapped
types, grant SELECT to the analyst role. For each question: transpile the gold SQL from SQLite to
PostgreSQL with SQLGlot, run it on both engines, and keep it only if the results agree. Questions
that fail to port are listed with the reason, so the benchmark you evaluate on is known to be valid.

Spider is distributed under CC BY-SA 4.0; check the licence of any dataset before redistributing it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from pathlib import Path

import sqlglot
import yaml

from .db import PG, DBError
from .grading import same_result

ADMIN_DSN = os.getenv("SQLAGENT_ADMIN_DSN", "host=127.0.0.1 dbname=retail user=retail_owner password=owner")
TYPEMAP = [(r"int", "bigint"), (r"char|clob|text|varchar", "text"), (r"real|floa|doub", "double precision"),
           (r"dec|num", "numeric"), (r"bool", "boolean"), (r"date|time", "text")]


def pg_type(sqlite_type: str) -> str:
    t = (sqlite_type or "").lower()
    for pat, pg in TYPEMAP:
        if re.search(pat, t):
            return pg
    return "text"


def _lit(v):
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return repr(v) if v == v else "NULL"
    if isinstance(v, bytes):
        v = v.decode(errors="replace")
    return "'" + str(v).replace("'", "''") + "'"


def port_database(sqlite_path: Path, schema: str, admin: PG, analyst_role: str = "analyst") -> dict:
    con = sqlite3.connect(sqlite_path)
    con.text_factory = lambda b: b.decode(errors="replace")
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    admin.raw(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    admin.raw(f'CREATE SCHEMA "{schema}"')
    counts = {}
    for t in tables:
        cols = con.execute(f'PRAGMA table_info("{t}")').fetchall()
        defs = ", ".join(f'"{c[1].lower()}" {pg_type(c[2])}' for c in cols)
        admin.raw(f'CREATE TABLE "{schema}"."{t.lower()}" ({defs})')
        types = [pg_type(c[2]) for c in cols]
        rows = con.execute(f'SELECT * FROM "{t}"').fetchall()
        for i in range(0, len(rows), 500):
            vals = ", ".join("(" + ", ".join(_cast(v, ty) for v, ty in zip(r, types, strict=True)) + ")"
                             for r in rows[i:i + 500])
            admin.raw(f'INSERT INTO "{schema}"."{t.lower()}" VALUES {vals}')
        counts[t.lower()] = len(rows)
    admin.raw(f'GRANT USAGE ON SCHEMA "{schema}" TO {analyst_role}')
    admin.raw(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{schema}" TO {analyst_role}')
    con.close()
    return counts


def _cast(v, ty):
    """SQLite is loosely typed; values that do not fit the declared type become NULL rather than failing."""
    if v is None:
        return "NULL"
    if ty in ("bigint", "double precision", "numeric"):
        try:
            float(v)
            return _lit(v) if not isinstance(v, str) else _lit(v) + f"::{ty}"
        except (TypeError, ValueError):
            return "NULL"
    if ty == "boolean":
        return "TRUE" if str(v).lower() in ("1", "t", "true", "y", "yes") else "FALSE"
    return _lit(v)


def port_questions(items: list[dict], sqlite_dir: Path, dsn: str | None = None) -> tuple[list, list]:
    kept, dropped = [], []
    dbs = {}
    for i, it in enumerate(items):
        db_id, gold = it["db_id"], it["query"].strip().rstrip(";")
        schema = f"spider_{db_id}"
        try:
            pg_sql = sqlglot.transpile(gold, read="sqlite", write="postgres")[0]
        except sqlglot.errors.SqlglotError as e:
            dropped.append({"db_id": db_id, "question": it["question"], "reason": f"transpile: {e}"[:200]})
            continue
        lite = sqlite3.connect(sqlite_dir / db_id / f"{db_id}.sqlite")
        lite.text_factory = lambda b: b.decode(errors="replace")
        try:
            want = lite.execute(gold).fetchall()
        except sqlite3.Error as e:
            dropped.append({"db_id": db_id, "question": it["question"], "reason": f"sqlite: {e}"})
            continue
        finally:
            lite.close()
        db = dbs.setdefault(schema, PG(dsn, search_path=schema))
        try:
            got = db.query(pg_sql, 100_000)[1]
        except DBError as e:
            dropped.append({"db_id": db_id, "question": it["question"], "reason": f"postgres: {e}"[:200]})
            continue
        ordered = bool(re.search(r"\border\s+by\b", gold, re.I))
        if not same_result(want, got, ordered=ordered):
            dropped.append({"db_id": db_id, "question": it["question"], "reason": "results differ after porting"})
            continue
        kept.append({"id": f"sp{i:04d}", "difficulty": it.get("difficulty", "spider"), "tags": ["spider", db_id],
                     "schema": schema, "question": it["question"], "sql": pg_sql, "order_matters": ordered})
    return kept, dropped


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--spider-dir", required=True)
    ap.add_argument("--split", default="dev")
    ap.add_argument("--dbs", default="", help="comma-separated db_ids (default: all in the split)")
    ap.add_argument("--out", default="bench/spider.yaml")
    a = ap.parse_args(argv)
    root = Path(a.spider_dir)
    items = json.loads((root / f"{a.split}.json").read_text())
    want = set(filter(None, a.dbs.split(","))) or {it["db_id"] for it in items}
    items = [it for it in items if it["db_id"] in want]
    admin = PG(ADMIN_DSN, read_only=False, timeout_ms=600_000)
    for db_id in sorted(want):
        n = port_database(root / "database" / db_id / f"{db_id}.sqlite", f"spider_{db_id}", admin)
        print(f"ported {db_id}: {n}")
    kept, dropped = port_questions(items, root / "database")
    Path(a.out).write_text(yaml.safe_dump({"answerable": kept, "ambiguous": [], "unanswerable": [], "adversarial": [],
                                           "dropped": dropped}, sort_keys=False, allow_unicode=True))
    print(f"{len(kept)} questions kept, {len(dropped)} dropped -> {a.out}")


if __name__ == "__main__":
    main()
