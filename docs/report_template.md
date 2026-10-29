# [Title, e.g. "Measure, don't trust the planner: verified rewrites for an LLM SQL agent"]

*Mini-paper skeleton. Fill only with numbers from a committed run directory.*

## Abstract (<=150 words)
Agent design, benchmark size, models, the two or three main findings with intervals, main limitation.

## 1. Questions
(a) How much do schema documentation, validation-and-repair and result checks add to execution
accuracy? (b) Do layered guardrails stop destructive or abusive SQL, and which layer does what?
(c) Can rewrites be accepted safely and only when they help, and how much does the planner's cost
estimate mislead?

## 2. Setup
Database and edge twin; benchmark composition by difficulty; systems table; models, temperature,
trials; grading rules; statistics (question-clustered bootstrap, McNemar with Holm).

## 3. Results
3.1 Accuracy by system and difficulty (figure: accuracy.png) with paired tests.
3.2 Behaviour: clarification on ambiguous, abstention on unanswerable, refusals, false clarifications.
3.3 Repair and result check: first-attempt error types, recovery rate, silent errors left.
3.4 Guardrails: per-layer matrix and what each layer uniquely stops.
3.5 Optimizer: policy table, every-rewrite figure, cost-vs-time figure, the o07/o09 examples.
3.6 Cost: tokens, calls and latency per question; optimizer overhead.

## 4. Failure analysis
Hand-review wrong answers by mechanism (wrong join path, wrong filter, convention ignored, wrong
aggregation grain, misread question). Quote 2-3 short examples.

## 5. Limitations and next steps
From README and design notes.

## 6. Reproducibility
Commit hash, `python -m sql_agent run configs/main.yaml`, cached responses.
