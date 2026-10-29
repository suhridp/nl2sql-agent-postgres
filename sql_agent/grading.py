"""Execution-accuracy grading: does the predicted query return the same answer as the gold query?

Two levels, both reported:
  strict  - same number of columns; columns may be in any order; rows compared as a multiset
            (or as a sequence when the question asks for an order).
  lenient - the prediction may carry extra columns (e.g. a name next to the asked-for ID), as long
            as every gold column is present with identical values.

Numbers are compared at 6 significant digits. If every number in the prediction has at most two
decimals (the model rounded), they are also compared after rounding the gold to two decimals.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter
from decimal import Decimal


def _num(v):
    return isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)


def canon(v, mode: str = "sig"):
    if v is None:
        return None
    if _num(v):
        f = float(v)
        if mode == "dp2":
            return round(f, 2) + 0.0
        return float(f"{f:.6g}") + 0.0
    if isinstance(v, (dt.datetime, dt.date)):
        if isinstance(v, dt.datetime) and v.time() == dt.time(0):
            return v.date().isoformat()
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return str(v)


def _rounded_to_2dp(rows) -> bool:
    for r in rows:
        for v in r:
            if _num(v) and not isinstance(v, int):
                if isinstance(v, Decimal):
                    if -v.as_tuple().exponent > 2 and v != v.quantize(Decimal("0.01")):
                        return False
                elif abs(v * 100 - round(v * 100)) > 1e-9:
                    return False
    return True


def _columns(rows, ncols, mode):
    return [[canon(r[j], mode) for r in rows] for j in range(ncols)]


def _sig(col):
    return tuple(sorted(Counter(map(repr, col)).items()))


def _match(gold, pred, *, ordered: bool, lenient: bool, mode: str) -> bool:
    if not gold and not pred:
        return True
    if len(gold) != len(pred):
        return False
    gn = len(gold[0]) if gold else 0
    pn = len(pred[0]) if pred else 0
    if pn < gn or (not lenient and pn != gn):
        return False
    gcols, pcols = _columns(gold, gn, mode), _columns(pred, pn, mode)
    psig = [_sig(c) for c in pcols]
    # assign each gold column to a distinct prediction column with the same multiset of values,
    # trying every consistent assignment (columns with equal signatures are interchangeable)
    choices = []
    for gc in gcols:
        s = _sig(gc)
        cands = [j for j in range(pn) if psig[j] == s]
        if not cands:
            return False
        choices.append(cands)

    def rows_for(assign):
        return [tuple(pcols[j][i] for j in assign) for i in range(len(pred))]

    gold_rows = [tuple(c[i] for c in gcols) for i in range(len(gold))]

    def search(k, used, assign):
        if k == len(choices):
            pr = rows_for(assign)
            return pr == gold_rows if ordered else Counter(map(repr, pr)) == Counter(map(repr, gold_rows))
        for j in choices[k]:
            if j not in used:
                if search(k + 1, used | {j}, assign + [j]):
                    return True
        return False

    return search(0, frozenset(), [])


def results_match(gold, pred, *, ordered: bool = False, lenient: bool = False) -> bool:
    gold, pred = list(gold or []), list(pred or [])
    if _match(gold, pred, ordered=ordered, lenient=lenient, mode="sig"):
        return True
    if pred and _rounded_to_2dp(pred):
        return _match(gold, pred, ordered=ordered, lenient=lenient, mode="dp2")
    return False


def same_result(a, b, *, ordered: bool = False) -> bool:
    """Strict equality of two result sets, used by the optimizer to accept a rewrite."""
    if len(a) != len(b):
        return False
    ca = [tuple(canon(v) for v in r) for r in a]
    cb = [tuple(canon(v) for v in r) for r in b]
    return ca == cb if ordered else Counter(map(repr, ca)) == Counter(map(repr, cb))
