"""Scripted stand-in "models" that validate the harness without an API key.

They are NOT evidence about any real model. They exist so that every path in the pipeline (repair,
result check, clarification, refusal, guardrails, grading) runs and is tested on real PostgreSQL.

  oracle - behaves perfectly: gold SQL, asks on ambiguous questions, declines unanswerable ones,
           refuses adversarial ones.
  noisy  - makes realistic first-attempt mistakes on about half the answerable questions (some raise
           errors, some silently return wrong answers), repairs to the gold query when told about an
           error, never asks for clarification, invents columns for unanswerable questions, and
           complies with adversarial requests so the guardrails are exercised.
"""
from __future__ import annotations

import hashlib
import json
import re

from .providers import Turn

LOUD = [
    ("typo_column", r"\bstatus\b", "order_status"),
    ("typo_column", r"\border_date\b", "orderdate"),
    ("typo_column", r"\bcategory\b", "product_category"),
    ("wrong_table", r"\border_items\b", "order_item"),
    ("ambiguous_column", r"oi\.order_id = o\.order_id", "order_id = order_id"),
    ("syntax", r"^SELECT\b", "SELEC"),
]
SILENT = [
    ("wrong_literal_case", r"'completed'", "'Completed'"),
    ("dropped_filter", r" AND o\.status = 'completed'| WHERE o\.status = 'completed'(?= GROUP| AND|$)", ""),
    ("wrong_year", r"2025-", "2024-"),
]


def _h(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest()[:8], 16)


class ScriptedLLM:
    kind = "scripted"

    def __init__(self, bench: dict, policy: str = "oracle"):
        self.policy = policy
        self.model = policy
        self.items = {}
        for kind, items in bench.items():
            for it in items:
                self.items[it["question"]] = {**it, "_kind": kind}

    @property
    def label(self):
        return f"scripted:{self.policy}"

    def corruption(self, item) -> tuple[str, str] | None:
        """Deterministic first-attempt mistake for the noisy policy, or None."""
        h = _h(item["id"])
        if h % 10 >= 5:
            return None
        pool = LOUD if (h // 10) % 2 == 0 else SILENT
        applicable = [(n, re.sub(p, r, item["sql"], count=1)) for n, p, r in pool]
        applicable = [(n, b) for n, b in applicable if b != item["sql"]]
        specific = [a for a in applicable if a[0] != "syntax"]  # a typo in SELECT is the fallback mistake
        applicable = specific or applicable
        return applicable[(h // 20) % len(applicable)] if applicable else None

    def complete(self, system, prompt, *, sample_key="", max_tokens=0) -> Turn:
        m = re.search(r"^Question: (.*)$", prompt, re.M)
        item = self.items.get(m.group(1).strip()) if m else None
        if item is None:
            return Turn(text='{"cannot_answer": "unknown question"}')
        kind = item["_kind"]
        repair = "Your previous reply produced" in prompt
        check = "If that is the right answer" in prompt
        if self.policy == "oracle":
            reply = {"answerable": {"sql": item.get("sql")}, "ambiguous": {"clarify": item.get("why", "Which metric?")},
                     "unanswerable": {"cannot_answer": item.get("why", "not in the schema")},
                     "adversarial": {"refuse": "this request would change data or access the system"}}[kind]
            if check:
                reply = {"confirm": True}
            return Turn(text=json.dumps(reply), in_tokens=len(prompt) // 4, out_tokens=30)
        # noisy
        if kind == "answerable":
            c = self.corruption(item)
            if repair or check or c is None:
                reply = {"sql": item["sql"]}
            else:
                reply = {"sql": c[1]}
        elif kind == "ambiguous":
            reply = {"sql": "SELECT name FROM products ORDER BY list_price DESC LIMIT 10"}
        elif kind == "unanswerable":
            reply = {"sql": "SELECT avg(age) FROM customers"} if not repair else {"cannot_answer": "no such data"}
        else:
            reply = {"sql": item.get("attack_sql", "SELECT 1")}
        return Turn(text=json.dumps(reply), in_tokens=len(prompt) // 4, out_tokens=40)
