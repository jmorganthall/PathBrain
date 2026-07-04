"""Small rank-correlation helpers — Spearman via Pearson-over-ranks.

Shared by the settings→outcome field-sensitivity map and the campaign-drift check
(``drift.py``). Spearman is monotonic and magnitude-blind, matching how the crown
itself ranks profiles, and is robust to the absolute scale of either variable (so a
time axis in raw seconds or ordinals gives the same answer)."""
from __future__ import annotations


def rank(vals: list[float]) -> list[float]:
    """Average (tie-aware) 1-based ranks — the basis of Spearman's rank correlation."""
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0  # mean 1-based rank shared by the tie group
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def pearson(a: list[float], b: list[float]) -> float | None:
    """Pearson correlation; ``None`` when undefined (n<3 or a constant column)."""
    n = len(a)
    if n < 3:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 0 or vb <= 0:
        return None
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    return cov / ((va ** 0.5) * (vb ** 0.5))


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman rank correlation (Pearson over ranks) — monotonic, magnitude-blind."""
    return pearson(rank(xs), rank(ys))
