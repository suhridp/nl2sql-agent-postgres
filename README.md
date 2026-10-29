# Natural-Language-to-SQL Agent with Verified Query Optimization

**Status: In development.** The agent, benchmark, guardrails, optimizer and reporting are built and
tested on PostgreSQL 16. The **guardrail and optimizer studies are real measurements** (they need no
model). **Accuracy results for real LLMs are pending**; the agent numbers below come from scripted
stand-ins and only show that the pipeline and grading work.

An agent that turns business questions into PostgreSQL, checks the SQL before it runs, executes it
read-only, repairs its own errors, asks when a question is ambiguous, refuses requests to change
anything, and then tries to make the query faster. It only accepts a faster version after showing it
returns identical rows on two databases and is reliably faster across repeated, interleaved timings.

```bash
docker compose up -d                       # PostgreSQL 16 + the retail database (~30 s first time)
pip install -r requirements.txt
python -m pytest -q                        # 31 tests against real PostgreSQL, ~80 s
python -m sql_agent run configs/smoke.yaml # benchmark + both studies, ~3 min, no API key
open results/smoke/dashboard.html
```

---

## Headline results (real, PostgreSQL 16.15, warm cache, one machine)

### 1. Query optimization: a statistical gate beats trusting the planner

16 correct but naively written queries (non-index-friendly date filters, correlated subqueries,
`NOT IN`, forced CTE materialization, and some the planner already handles). Each policy's choice is
re-timed against the original in a **separate confirmation run**, so winners aren't graded on the
timings that picked them.

| policy | rewritten | geometric-mean speedup (16 queries) | rewrites slower than original | timed runs |
|---|---|---|---|---|
| never rewrite | 0 | 1.00x | 0 | 0 |
| SQLGlot rewrite if >=10% faster (phase 1) | 5 | 2.40x | 1 | 320 |
| trust the planner: lowest EXPLAIN cost | 9 | 3.10x | **2** (one about 5x slower) | 0 |
| time only the cheapest candidate, then gate | 6 | 3.44x | 0 | 320 |
| **time every verified candidate, then gate** | **7** | **3.68x** | **0** | 460 |

- **Real wins:** index-friendly date ranges (2–36x), correlated subqueries rewritten as joins
  (33–91x), removing a forced `MATERIALIZED` CTE (about 200x).
- **The planner's cost estimate is not enough.** Choosing the lowest-cost version would have been
  more than 10% slower than the best verified version on 4 of 16 queries. On `o09` a rewrite with
  *half* the estimated cost runs **about 5x slower**, while on `o07` a rewrite with a *higher* cost runs
  1.5x faster. This is the gap between estimated cost and measured time that plan-aware, top-k
  optimization research is about. Timing only the planner's top-1 candidate kept most of the gain
  with 30% fewer timed runs.
- **Safety of rewrites:** a `NOT IN -> NOT EXISTS` rewrite was refused on a nullable column (where it
  changes results). A test shows a wrong `LEFT JOIN -> JOIN` rewrite that passes on the main data and
  is caught only by the edge-case database.

### 2. Guardrails: each layer alone misses something, together they stop all 34 attacks

| layer, tested alone | attacks stopped |
|---|---|
| SQLGlot validator | 32 / 34 |
| EXPLAIN cost check | 28 / 34 |
| SELECT-only database role (read-only mode off) | 28 / 34 |
| execution path without validator (read-only transaction + SELECT-only cursor + role + timeout) | 31 / 34 |
| **full stack** | **34 / 34** |

Every table's content hash is identical before and after. What slipped past single layers:

- **The validator** lets through valid but huge `SELECT`s, such as a 145-billion-row cross join.
  The cost check catches them.
- **Database privileges** don't stop session tampering (`SET statement_timeout = 0`) or reads of
  `pg_stat_activity` and `pg_settings`.
- **A real gap the study found:** without the validator, `SELECT set_config('statement_timeout', '0',
  false)` runs and would switch off the timeout for later queries. It's now fixed by re-applying
  limits inside every transaction, with a regression test.

### 3. Agent benchmark (harness validation, scripted stand-ins)

78 answerable questions (20 easy, 30 medium, 20 hard, 8 extra), each with hand-written, executed and
checked gold SQL. Plus 10 ambiguous, 8 unanswerable and 12 adversarial requests. The `noisy`
stand-in makes realistic first-attempt mistakes. Some raise errors (typo'd column, wrong table,
ambiguous column, syntax); others silently return wrong answers (`'Completed'` vs `'completed'`, a
dropped filter, the wrong year).

| system | execution accuracy [95% CI] | errors recovered | silently wrong | harmful SQL ran |
|---|---|---|---|---|
| zero_shot (table/column names only) | 65% [55, 76] | 0/19 | 8 | 0 |
| schema_prompt (types, keys, comments, known values) | 65% [55, 76] | 0/19 | 8 | 0 |
| + validation + EXPLAIN check + repair loop | 90% [82, 96] | 19/19 | 8 | 0 |
| + second look at empty/NULL results | 96% [91, 100] | 19/19 | 3 | 0 |

This shows the machinery works: the repair loop recovers every error the database reports, and the
result check catches silent mistakes that produce empty or NULL answers. It says nothing about any
model: a real model's errors and its gap between zero_shot and schema_prompt are what the pilot run
measures.

---

## Design

```
question -> prompt (schema at chosen detail) -> model -> {sql | clarify | cannot_answer | refuse}
   -> SQLGlot validation -> EXPLAIN cost check -> read-only cursor (timeout, row cap)
   -> result check (empty / NULL answers get one second look) -> repair loop (up to 3) on any failure
   -> optimizer: rule-based + SQLGlot (+ LLM) rewrites -> validate -> equal rows on main AND edge DB
      -> interleaved timing -> accept only if the 95% CI of the paired speedup is above 1.05x
```

**Database.** Seven-table retail schema (customers, products, orders, order lines, payments,
returns, reviews) with about 1.4M deterministic, skewed rows, so join order and index choice matter.
Every table and column carries a `COMMENT`; that documentation (for example the definition of
revenue) is what separates `schema_prompt` from `zero_shot`. A hand-built `edge` schema with the same
tables holds NULLs in every nullable column, customers with no orders, an order with no lines,
exact ties and year-boundary dates, to catch rewrites and answers that are right only by
coincidence.

**Safety layers.**
1. Validator: single statement, SELECT or set operations only, no DML/DDL/`SET`/`COPY`/`INTO`/
   locking anywhere (including data-modifying CTEs), no sleep/file/admin/settings functions, no
   system catalogs, known tables and columns.
2. EXPLAIN cost check before execution.
3. Execution through `BEGIN READ ONLY` plus a server-side cursor: only a query that can be declared
   as a cursor runs at all. Timeout and search path are re-applied with `SET LOCAL` in every
   transaction.
4. SELECT-only `analyst` role, no TEMP privilege, read-only by default, 15 s statement timeout.

**Grading.** Execution accuracy on result sets: column order and row order are ignored unless the
question asks for an order. Numbers are compared at 6 significant digits, or at 2 decimals when the
model rounded. Strict mode needs the same columns; lenient mode allows extra columns. Truncated
results count as wrong. Paired McNemar tests compare consecutive systems.

**Optimizer rules** (each correct under the stated condition; see `sql_agent/optimizer.py`):
index-friendly date ranges, HAVING to WHERE, IN to EXISTS, NOT IN to NOT EXISTS (only if both
columns are NOT NULL), correlated aggregate subquery to LEFT JOIN of a grouped table, OR to UNION ALL
with `IS NOT TRUE` guards, CTE materialization flip, DISTINCT join to EXISTS, and SQLGlot's own
optimizer. With `llm_rewrites: true` a model also proposes rewrites, which go through exactly the
same checks.

## Running with a real model

```bash
export ANTHROPIC_API_KEY=...
python -m sql_agent ask "Which channel brought in the most revenue in 2025?"   # one question, verbose
python -m sql_agent run configs/pilot.yaml --dry-run                          # 540 trials
python -m sql_agent run configs/pilot.yaml                                    # 1 model, all systems
python -m sql_agent run configs/main.yaml                                     # 2 models x 3 trials + studies
```

A free local open-weight model works through any OpenAI-compatible server (`configs/local.yaml`,
Ollama). Fill in `budget.prices` to enforce a dollar cap. Responses are cached per trial, so reruns
are free and exact. Other commands: `python -m sql_agent optimize "<SELECT ...>"` optimizes any
query you give it, and `python -m sql_agent guardrails` re-runs the attack study.

**Spider/BIRD.** `python -m sql_agent.spider --spider-dir <spider> --dbs concert_singer,...` copies
SQLite databases into PostgreSQL schemas and transpiles gold SQL with SQLGlot. It keeps a question
only if SQLite and PostgreSQL return the same answer, and lists the rest with reasons. This is tested
on a Spider-format fixture (it correctly drops a question where SQLite's case-insensitive `LIKE`
differs from PostgreSQL) but has **not yet been run on Spider itself**; download it and check its
licence (CC BY-SA 4.0).

## Repository layout

```
db/            schema.sql (documented), data.sql (deterministic, skewed), edge.sql, init.sh, docker entrypoint
bench/         questions.yaml (78 + 10 + 8 + 12), attacks.yaml (34), optimize.yaml (16)
sql_agent/
  db.py          driver (psycopg2 / psycopg / libpq fallback), read-only cursor path, introspection, fingerprints
  validate.py    static guardrails with a reason code per rule
  agent.py       pipeline and the five systems compared
  grading.py     strict and lenient execution accuracy
  optimizer.py   rewrite rules, two-database equivalence, interleaved timing, bootstrap gate
  studies.py     optimizer policies with confirmation runs; per-layer guardrail study
  scripted.py    oracle / noisy stand-ins (harness validation only)
  spider.py      Spider-format port with per-question verification
  runner.py      YAML experiments, threaded, resumable, per-trial caching, spend cap
  analysis.py, dashboard.py   report.md, figures, self-contained dashboard
  providers.py, stats.py      shared with the agent-safety project (move to common/ in a monorepo)
tests/         31 tests, real PostgreSQL; validator and grading tests run without a database
```

## Limitations
- One synthetic schema and 78 questions. Results describe this benchmark.
- Timings are warm-cache and server-side on one machine. Cold-cache behaviour, concurrency and
  larger data can change which plan wins.
- Rewrite equivalence is checked on two databases, not proven. The edge database makes coincidental
  equivalence less likely; it doesn't rule it out.
- The optimization set was written to contain known anti-patterns. Real agent SQL may offer fewer
  opportunities; the `optimized` system's rewrite count measures that.
- The scripted stand-ins' mistakes were designed by the author. Real model errors will look different.

## Roadmap
1. Pilot on one model, then the main run on 2+ models with 3 trials per cell and model-proposed rewrites.
2. Run the Spider port on a Spider dev subset and report execution accuracy next to the custom set.
3. Cold-cache timing (restart plus OS cache drop between rounds) on a dedicated machine.
4. Learn when timing is worth it: predict, from plan features, whether the cheapest candidate is
   the fastest, and time more candidates only when it probably isn't.
5. Run the agent-safety project's injection attacks against this agent (for example, instructions
   hidden in product names).

## AI assistance
Much of this code was written with an AI assistant. The design decisions, the correctness condition
of every rewrite rule, and known limitations are written down here and in `docs/design.md` so each
part can be explained and changed without it.
