"""Minimal ctypes binding to the system libpq, used only when neither psycopg2 nor psycopg is installed.

It supports what this project needs: connect, run one statement, read a text-format result, report
errors with their SQLSTATE. Values are converted from text by type OID, matching psycopg2's types
for the columns in this schema (int, numeric -> Decimal, float, bool, date, timestamp, text).
"""
from __future__ import annotations

import ctypes
import ctypes.util
import datetime as dt
from decimal import Decimal

_lib = None


def _load():
    global _lib
    if _lib is None:
        name = ctypes.util.find_library("pq") or "libpq.so.5"
        lib = ctypes.CDLL(name)
        vp, cp, i = ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int
        sig = {
            "PQconnectdb": ([cp], vp), "PQstatus": ([vp], i), "PQerrorMessage": ([vp], cp), "PQfinish": ([vp], None),
            "PQexec": ([vp, cp], vp), "PQresultStatus": ([vp], i), "PQresultErrorMessage": ([vp], cp),
            "PQresultErrorField": ([vp, i], cp), "PQntuples": ([vp], i), "PQnfields": ([vp], i),
            "PQfname": ([vp, i], cp), "PQftype": ([vp, i], ctypes.c_uint), "PQgetvalue": ([vp, i, i], cp),
            "PQgetisnull": ([vp, i, i], i), "PQclear": ([vp], None), "PQtransactionStatus": ([vp], i),
        }
        for fn, (args, res) in sig.items():
            f = getattr(lib, fn)
            f.argtypes, f.restype = args, res
        _lib = lib
    return _lib


class LibpqError(Exception):
    def __init__(self, msg: str, sqlstate: str = ""):
        super().__init__(msg)
        self.sqlstate = sqlstate


def _ts(s: str):
    return dt.datetime.fromisoformat(s)


CONVERT = {16: lambda s: s == "t", 20: int, 21: int, 23: int, 26: int, 700: float, 701: float, 1700: Decimal,
           1082: dt.date.fromisoformat, 1114: _ts, 1184: _ts}
PGRES_COMMAND_OK, PGRES_TUPLES_OK = 1, 2
PG_DIAG_SQLSTATE = ord("C")


class Connection:
    def __init__(self, dsn: str):
        self.lib = _load()
        self.conn = self.lib.PQconnectdb(dsn.encode())
        if self.lib.PQstatus(self.conn) != 0:
            msg = (self.lib.PQerrorMessage(self.conn) or b"").decode(errors="replace").strip()
            self.lib.PQfinish(self.conn)
            self.conn = None
            raise LibpqError(msg or "connection failed", "08001")

    @property
    def closed(self) -> bool:
        return self.conn is None or self.lib.PQstatus(self.conn) != 0

    def execute(self, sql: str):
        """Run one statement. Returns (columns, rows) for result sets, (None, None) for commands."""
        res = self.lib.PQexec(self.conn, sql.encode())
        try:
            st = self.lib.PQresultStatus(res)
            if st not in (PGRES_COMMAND_OK, PGRES_TUPLES_OK):
                msg = (self.lib.PQresultErrorMessage(res) or b"").decode(errors="replace").strip()
                code = (self.lib.PQresultErrorField(res, PG_DIAG_SQLSTATE) or b"").decode()
                raise LibpqError(msg.removeprefix("ERROR:").strip().splitlines()[0] if msg else "error", code)
            if st == PGRES_COMMAND_OK:
                return None, None
            nf, nt = self.lib.PQnfields(res), self.lib.PQntuples(res)
            cols = [self.lib.PQfname(res, j).decode() for j in range(nf)]
            conv = [CONVERT.get(self.lib.PQftype(res, j), str) for j in range(nf)]
            rows = []
            for r in range(nt):
                row = []
                for j in range(nf):
                    if self.lib.PQgetisnull(res, r, j):
                        row.append(None)
                    else:
                        row.append(conv[j](self.lib.PQgetvalue(res, r, j).decode()))
                rows.append(tuple(row))
            return cols, rows
        finally:
            self.lib.PQclear(res)

    def in_failed_transaction(self) -> bool:
        return self.lib.PQtransactionStatus(self.conn) in (2, 3)  # INTRANS / INERROR

    def close(self):
        if self.conn is not None:
            self.lib.PQfinish(self.conn)
            self.conn = None
