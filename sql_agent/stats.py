"""Shared with agent-safety-eval (move both into common/ in a monorepo).

Statistics helpers. Every reported rate carries uncertainty.

* ``wilson``            - CI for a proportion, treating trials as independent.
* ``cluster_bootstrap`` - CI that resamples *tasks*, because trials of the same task are correlated.
                          This is the honest interval when the question is "would this hold on other
                          tasks like these?". It is usually wider than Wilson.
* ``mcnemar_exact``     - paired test for two configurations run on the same (task, variant, trial)
                          cells. Uses only the discordant pairs.
"""
from __future__ import annotations

import math
import random
from collections import defaultdict


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def cluster_bootstrap(values: list[float], clusters: list[str], iters: int = 2000, seed: int = 0,
                      alpha: float = 0.05) -> tuple[float, float, float]:
    """Mean and percentile CI, resampling clusters (tasks) with replacement."""
    if not values:
        return (0.0, 0.0, 0.0)
    groups = defaultdict(list)
    for v, c in zip(values, clusters, strict=True):
        groups[c].append(v)
    keys = sorted(groups)
    sums = {k: (sum(groups[k]), len(groups[k])) for k in keys}
    rng = random.Random(seed)
    stats = []
    for _ in range(iters):
        s = n = 0
        for k in rng.choices(keys, k=len(keys)):
            s += sums[k][0]
            n += sums[k][1]
        stats.append(s / n)
    stats.sort()
    lo = stats[int(alpha / 2 * iters)]
    hi = stats[min(iters - 1, int((1 - alpha / 2) * iters))]
    return (sum(values) / len(values), lo, hi)


def binom_two_sided(k: int, n: int) -> float:
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(0, min(k, n - k) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def mcnemar_exact(pairs: list[tuple[bool, bool]]) -> dict:
    """pairs = [(a, b)] for matched cells. b01 = a false & b true, b10 = a true & b false."""
    b10 = sum(1 for a, b in pairs if a and not b)
    b01 = sum(1 for a, b in pairs if b and not a)
    return {"n": len(pairs), "a_only": b10, "b_only": b01, "p": binom_two_sided(min(b10, b01), b10 + b01)}


def holm(pvals: dict) -> dict:
    """Holm-Bonferroni adjusted p-values for a family of comparisons."""
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m, out, running = len(items), {}, 0.0
    for i, (k, p) in enumerate(items):
        running = max(running, min(1.0, (m - i) * p))
        out[k] = running
    return out
