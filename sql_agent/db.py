"""PostgreSQL access for the agent: SELECT-only role, read-only transactions, statement timeout,
server-side cursor with a row cap, and schema introspection (types, nullability, keys, comments).

Driver: psycopg2 if installed, else psycopg 3, else a small ctypes binding to the system libpq.
All three raise ``DBError`` with the SQLSTATE so failures can be classified.
"""
from __future__ import annotations

import json
import os
import re
import threading

DEFAULT_DSN = "host=127.0.0.1 port=5432 dbname=retail user=analyst password=analyst"

# SQLSTATE classes used in analysis and repair prompts
SQLSTATE_KIND = {"42P01": "undefined_table", "42703": "undefined_column", "42702": "ambiguous_column",
                 "42803": "grouping_error", "42883": "undefined_function", "42804": "datatype_mismatch",
                 "42601": "syntax_error", "22012": "division_by_zero", "25006": "read_only_violation",
                 "42501": "insufficient_privilege", "57014": "query_canceled_timeout", "22P02": "invalid_input",
                 "42725": "ambiguous_function", "21000": "cardinality_violation", "34000": "invalid_cursor",
                 "0A000": "feature_not_supported", "54001": "statement_too_complex"}


class DBError(Exception):
    def __init__(self, msg: str, sqlstate: str = ""):
        super().__init__(msg)
        self.sqlstate = sqlstate

    @property
    def kind(self) -> str:
        return SQLSTATE_KIND.get(self.sqlstate, f"sqlstate_{self.sqlstate or 'unknown'}")


# ------------------------------------------------------------------------------------------
# Backends
# ------------------------------------------------------------------------------------------

class _Backend:
    name = "?"

    def execute(self, sql: str):
        raise NotImplementedError

    def close(self):
        pass


class _Psycopg2(_Backend):
    name = "psycopg2"

    def __init__(self, dsn):
        import psycopg2
        self.mod = psycopg2
        try:
            self.conn = psycopg2.connect(dsn)
        except psycopg2.Error as e:
            raise DBError(str(e).strip().splitlines()[0], getattr(e, "pgcode", "") or "08001") from None
        self.conn.autocommit = True

    @property
    def closed(self):
        return bool(self.conn.closed)

    def execute(self, sql):
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql)
                if cur.description is None:
                    return None, None
                return [d[0] for d in cur.description], [tuple(r) for r in cur.fetchall()]
        except self.mod.Error as e:
            raise DBError(str(e).strip().splitlines()[0].removeprefix("ERROR:").strip(), e.pgcode or "") from None

    def close(self):
        self.conn.close()


class _Psycopg3(_Psycopg2):
    name = "psycopg"

    def __init__(self, dsn):
        import psycopg
        self.mod = psycopg
        try:
            self.conn = psycopg.connect(dsn, autocommit=True)
        except psycopg.Error as e:
            raise DBError(str(e).strip().splitlines()[0], getattr(e, "sqlstate", "") or "08001") from None

    def execute(self, sql):
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql.encode() if False else sql, prepare=False)
                if cur.description is None:
                    return None, None
                return [d.name for d in cur.description], [tuple(r) for r in cur.fetchall()]
        except self.mod.Error as e:
            raise DBError(str(e).strip().splitlines()[0], getattr(e, "sqlstate", "") or "") from None


class _Libpq(_Backend):
    name = "libpq-ctypes"

    def __init__(self, dsn):
        from ._libpq import Connection, LibpqError
        self.err = LibpqError
        try:
            self.conn = Connection(dsn)
        except LibpqError as e:
            raise DBError(str(e), e.sqlstate) from None

    @property
    def closed(self):
        return self.conn.closed

    def execute(self, sql):
        try:
            return self.conn.execute(sql)
        except self.err as e:
            raise DBError(str(e), e.sqlstate) from None

    def close(self):
        self.conn.close()


def _connect(dsn: str, prefer: str | None = None) -> _Backend:
    order = [prefer] if prefer else ["psycopg2", "psycopg", "libpq"]
    errors = []
    for name in order:
        try:
            return {"psycopg2": _Psycopg2, "psycopg": _Psycopg3, "libpq": _Libpq}[name](dsn)
        except ImportError as e:
            errors.append(f"{name}: {e}")
        except OSError as e:  # libpq shared library missing
            errors.append(f"{name}: {e}")
    raise DBError("no PostgreSQL driver available (" + "; ".join(errors) + ")", "08001")


# ------------------------------------------------------------------------------------------
# PG
# ------------------------------------------------------------------------------------------

class PG:
    """One connection with safe session settings. Thread-safe by locking (one statement at a time)."""

    def __init__(self, dsn: str | None = None, *, timeout_ms: int = 15000, search_path: str = "public",
                 read_only: bool = True, driver: str | None = None):
        self.dsn = dsn or os.getenv("SQLAGENT_DSN", DEFAULT_DSN)
        self.timeout_ms, self.search_path, self.read_only, self.driver = timeout_ms, search_path, read_only, driver
        self._b: _Backend | None = None
        self.lock = threading.RLock()

    # -- connection ---------------------------------------------------------------------
    def _conn(self) -> _Backend:
        if self._b is None or self._b.closed:
            self._b = _connect(self.dsn, self.driver)
            for s in (f"SET statement_timeout = {int(self.timeout_ms)}",
                      f"SET default_transaction_read_only = {'on' if self.read_only else 'off'}",
                      f"SET search_path = {self.search_path}",
                      "SET cursor_tuple_fraction = 1.0",  # cursors must not change plans vs. plain execution
                      "SET application_name = 'sql_agent'",
                      "SET client_min_messages = error"):
                self._b.execute(s)
        return self._b

    @property
    def backend(self) -> str:
        return self._conn().name

    def close(self):
        if self._b is not None:
            self._b.close()
            self._b = None

    def raw(self, sql: str):
        """Execute exactly one string with only the session settings as protection.
        Used to measure what the role/transaction layers stop on their own."""
        with self.lock:
            b = self._conn()
            try:
                return b.execute(sql)
            except DBError:
                self._reset(b)
                raise

    def _harden(self, b):
        """Re-assert limits inside every transaction. A query can change session settings (for example
        SELECT set_config('statement_timeout', '0', false)); SET LOCAL here means such a change can
        never carry over to the next query."""
        b.execute(f"SET LOCAL statement_timeout = {int(self.timeout_ms)}")
        b.execute(f"SET LOCAL search_path = {self.search_path}")

    def _reset(self, b):
        try:
            b.execute("ROLLBACK")
        except DBError:
            pass

    # -- the safe path the agent uses -----------------------------------------------------
    def query(self, sql: str, max_rows: int = 500):
        """SELECT through a read-only transaction and a server-side cursor. Only a query that can be
        declared as a cursor (SELECT/VALUES/TABLE) runs at all. Returns (cols, rows, truncated)."""
        with self.lock:
            b = self._conn()
            try:
                b.execute("BEGIN READ ONLY")
                self._harden(b)
                b.execute(f"DECLARE _q NO SCROLL CURSOR FOR {sql}")
                cols, rows = b.execute(f"FETCH FORWARD {int(max_rows) + 1} FROM _q")
                b.execute("COMMIT")
            except DBError:
                self._reset(b)
                raise
            return cols, rows[:max_rows], len(rows) > max_rows

    def explain(self, sql: str, *, analyze: bool = False, timing: bool = True, buffers: bool = False) -> dict:
        opts = ["FORMAT JSON"]
        if analyze:
            opts += ["ANALYZE", f"TIMING {'ON' if timing else 'OFF'}"] + (["BUFFERS"] if buffers else [])
        with self.lock:
            b = self._conn()
            try:
                b.execute("BEGIN READ ONLY")
                self._harden(b)
                _, rows = b.execute(f"EXPLAIN ({', '.join(opts)}) {sql}")
                b.execute("COMMIT")
            except DBError:
                self._reset(b)
                raise
        out = rows[0][0]
        return (json.loads(out) if isinstance(out, str) else out)[0]

    def value(self, sql: str):
        return self.query(sql, 1)[1][0][0]

    # -- introspection -------------------------------------------------------------------
    def schema_info(self, schema: str | None = None) -> dict:
        schema = schema or self.search_path
        q = f"""
          SELECT c.table_name, c.column_name, c.data_type, c.is_nullable = 'YES',
                 col_description(format('%I.%I', c.table_schema, c.table_name)::regclass, c.ordinal_position),
                 obj_description(format('%I.%I', c.table_schema, c.table_name)::regclass, 'pg_class')
          FROM information_schema.columns c
          WHERE c.table_schema = '{schema}'
          ORDER BY c.table_name, c.ordinal_position"""
        info: dict = {}
        for t, col, typ, nullable, ccom, tcom in self.query(q, 10000)[1]:
            tb = info.setdefault(t, {"columns": {}, "comment": tcom, "pk": [], "fks": []})
            tb["columns"][col] = {"type": typ, "nullable": nullable, "comment": ccom}
        cons = f"""
          SELECT conrelid::regclass::text, contype, pg_get_constraintdef(oid)
          FROM pg_constraint WHERE connamespace = '{schema}'::regnamespace AND contype IN ('p','f')"""
        for rel, kind, d in self.query(cons, 10000)[1]:
            t = rel.split(".")[-1]
            if t not in info:
                continue
            if kind == "p":
                info[t]["pk"] = [x.strip() for x in re.search(r"\((.*?)\)", d).group(1).split(",")]
            else:
                info[t]["fks"].append(d)
        return info

    def schema(self, schema: str | None = None) -> dict[str, dict[str, str]]:
        """{table: {column: type}} - the shape SQLGlot's qualifier wants."""
        return {t: {c: v["type"] for c, v in tb["columns"].items()} for t, tb in self.schema_info(schema).items()}

    def low_cardinality_values(self, info: dict, max_distinct: int = 12) -> dict:
        """Distinct values of short text columns, so the model can write status = 'completed'
        instead of guessing 'complete' or 'Completed'."""
        out = {}
        for t, tb in info.items():
            for c, v in tb["columns"].items():
                if v["type"] != "text" or c in ("name", "email"):
                    continue
                vals = self.query(f'SELECT DISTINCT "{c}" FROM "{t}" WHERE "{c}" IS NOT NULL ORDER BY 1 '
                                  f"LIMIT {max_distinct + 1}", max_distinct + 1)[1]
                if len(vals) <= max_distinct:
                    out[(t, c)] = [r[0] for r in vals]
        return out

    def fingerprint(self, schema: str = "public") -> dict:
        """Row count and order-independent content hash per table. Used to prove a run changed nothing."""
        out = {}
        for t in sorted(self.schema_info(schema)):
            n, h = self.query(f'SELECT count(*), coalesce(sum(hashtext(x::text)::bigint), 0) FROM {schema}."{t}" x')[1][0]
            out[t] = [int(n), int(h)]
        return out


def schema_text(info: dict, level: str = "full", values: dict | None = None) -> str:
    """Render the schema for a prompt.

    names: table(col, col, ...) only                                   -> the zero_shot system
    full : types, NOT NULL, keys, foreign keys, comments, known values -> schema_prompt and later
    """
    lines = []
    for t, tb in info.items():
        if level == "names":
            lines.append(f"{t}({', '.join(tb['columns'])})")
            continue
        head = f"TABLE {t}" + (f"  -- {tb['comment']}" if tb.get("comment") else "")
        lines.append(head)
        for c, v in tb["columns"].items():
            bits = [f"  {c} {v['type']}"]
            if not v["nullable"]:
                bits.append("NOT NULL")
            if c in tb.get("pk", []):
                bits.append("PRIMARY KEY" if len(tb["pk"]) == 1 else "PK part")
            note = v.get("comment") or ""
            if values and (t, c) in values:
                note = (note + "; " if note else "") + "values: " + ", ".join(map(str, values[(t, c)]))
            lines.append(" ".join(bits) + (f"  -- {note}" if note else ""))
        for fk in tb.get("fks", []):
            lines.append(f"  {fk}")
    return "\n".join(lines)
