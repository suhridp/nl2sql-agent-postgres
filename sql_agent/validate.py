"""Static SQL validation with SQLGlot, before anything reaches the database.

Checks, in order, each with its own code so the guardrail evaluation can say which rule fired:
  syntax_error, multi_statement, not_select, forbidden_construct (DML/DDL/SET/COPY/INTO/locking,
  including data-modifying CTEs), forbidden_function (sleep, file, large-object, admin, session and
  SQL-executing functions), system_catalog, unknown_table, unknown_column.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.qualify import qualify

logging.getLogger("sqlglot").setLevel(logging.ERROR)  # it warns when falling back to Command

_FORBIDDEN_NAMES = ["Insert", "Update", "Delete", "Drop", "Create", "Alter", "AlterTable", "Command", "Merge",
                    "TruncateTable", "Copy", "Set", "SetItem", "Into", "Lock", "Grant", "Revoke", "Transaction",
                    "Commit", "Rollback", "Use", "Pragma", "LoadData", "Analyze", "Cache", "Uncache"]
FORBIDDEN = tuple(getattr(exp, n) for n in _FORBIDDEN_NAMES if hasattr(exp, n))
SELECT_ROOTS = tuple(getattr(exp, n) for n in ("Select", "Union", "Intersect", "Except", "SetOperation")
                     if hasattr(exp, n))

# Functions that sleep, touch files or large objects, change settings, signal other sessions,
# take locks, write sequences, or execute SQL given as a string.
BAD_FUNC = re.compile(r"^(pg_|lo_|dblink|set_config|current_setting|query_to_xml|query_to_xmlschema|"
                      r"cursor_to_xml|table_to_xml|database_to_xml|schema_to_xml|nextval|setval|currval|"
                      r"txid_|xmlexists|ts_stat)", re.I)
SAFE_PG_FUNCS = {"pg_typeof"}
SYSTEM_SCHEMAS = {"pg_catalog", "information_schema", "pg_toast"}


@dataclass
class Verdict:
    ok: bool
    code: str = ""
    message: str = ""

    def __iter__(self):  # allows: ok, why = validate(...)
        return iter((self.ok, self.message))


def _func_name(node) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.this)
    return node.sql_name() if hasattr(node, "sql_name") else type(node).__name__


def validate(sql: str, schema: dict) -> Verdict:
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        return Verdict(False, "empty", "no SQL was given")
    try:
        trees = [t for t in sqlglot.parse(sql, dialect="postgres") if t is not None]
    except SqlglotError as e:
        return Verdict(False, "syntax_error", f"syntax error: {str(e).splitlines()[0][:200]}")
    if len(trees) != 1:
        return Verdict(False, "multi_statement", "exactly one statement is allowed")
    t = trees[0]
    for node in t.walk():
        if isinstance(node, FORBIDDEN):
            return Verdict(False, "forbidden_construct", f"{type(node).__name__} is not allowed; only read-only "
                                                         "SELECT queries can run")
    if not isinstance(t, SELECT_ROOTS):
        return Verdict(False, "not_select", "only SELECT queries are allowed")
    for f in t.find_all(exp.Func):
        name = _func_name(f).lower()
        if BAD_FUNC.match(name) and name not in SAFE_PG_FUNCS:
            return Verdict(False, "forbidden_function", f"function {name} is not allowed")
    ctes = {c.alias_or_name.lower() for c in t.find_all(exp.CTE)}
    for tb in t.find_all(exp.Table):
        name, db = tb.name.lower(), (tb.db or "").lower()
        if db in SYSTEM_SCHEMAS or (not db and name.startswith("pg_")):
            return Verdict(False, "system_catalog", f"system catalog {tb.sql()} is not accessible")
        if db and db != "public":
            return Verdict(False, "unknown_table", f"schema {db} is not accessible")
        if isinstance(tb.this, exp.Anonymous) or (tb.this and not isinstance(tb.this, exp.Identifier)):
            continue  # table-valued function like generate_series(...); checked as a function above
        if name and name not in ctes and name not in schema:
            return Verdict(False, "unknown_table", f"unknown table {tb.name}; tables are: {', '.join(sorted(schema))}")
    try:
        qualify(t.copy(), schema=schema, dialect="postgres", validate_qualify_columns=True,
                quote_identifiers=False)
    except SqlglotError as e:
        msg = str(e).splitlines()[0][:200]
        return Verdict(False, "unknown_column", f"schema error: {msg}")
    except Exception as e:  # SQLGlot can raise plain errors on exotic syntax; let PostgreSQL be the judge
        return Verdict(True, "", f"qualifier skipped: {type(e).__name__}")
    return Verdict(True)
