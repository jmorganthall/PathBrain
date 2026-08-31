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


def _solve_linear(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Solve ``a·x = b`` by Gaussian elimination with partial pivoting; ``None`` if singular.

    ``a`` is modified in place — callers pass throwaway normal-equation matrices.
    """
    k = len(b)
    for col in range(k):
        pivot = max(range(col, k), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            return None
        if pivot != col:
            a[col], a[pivot] = a[pivot], a[col]
            b[col], b[pivot] = b[pivot], b[col]
        inv = 1.0 / a[col][col]
        for r in range(col + 1, k):
            f = a[r][col] * inv
            if f == 0.0:
                continue
            for c in range(col, k):
                a[r][c] -= f * a[col][c]
            b[r] -= f * b[col]
    x = [0.0] * k
    for r in range(k - 1, -1, -1):
        s = b[r] - sum(a[r][c] * x[c] for c in range(r + 1, k))
        x[r] = s / a[r][r]
    return x


def ols_residual_ss(rows: list[list[float]], ys: list[float]) -> float | None:
    """Residual sum of squares of the least-squares fit ``ys ~ rows`` (no intercept).

    The building block of the weather variance decomposition: callers demean within
    whatever group structure they need (an intercept per group), then ask how much
    squared variation the covariate columns leave behind. Pure Python by the same rule
    as everything else that produces a user-facing number here — deterministic, no
    fitted black box, and small enough to audit.

    Numerically: columns are rescaled to unit RMS before forming the normal equations
    (covariates arrive in wildly different units — ms vs fractions), and a tiny ridge
    proportional to the diagonal keeps a collinear design solvable instead of failing;
    at 1e-8 of the trace it is far below anything that could move a reported R².
    ``None`` only when the system is genuinely unsolvable.
    """
    n = len(ys)
    if n == 0 or len(rows) != n:
        return None
    width = len(rows[0]) if rows else 0
    # A constant (here: all-zero, since callers demean) column carries no information and
    # would zero a normal-equation pivot — drop it rather than fail. A design that is ALL
    # such columns legitimately explains nothing: the whole variance is residual.
    active = [
        j for j in range(width)
        if (sum(r[j] * r[j] for r in rows) / n) ** 0.5 > 0
    ]
    if not active:
        return sum(y * y for y in ys)
    k = len(active)
    scales = [(sum(r[j] * r[j] for r in rows) / n) ** 0.5 for j in active]
    xtx = [[0.0] * k for _ in range(k)]
    xty = [0.0] * k
    for i in range(n):
        xi = [rows[i][j] / scales[a] for a, j in enumerate(active)]
        yi = ys[i]
        for a in range(k):
            va = xi[a]
            xty[a] += va * yi
            row = xtx[a]
            for b in range(a, k):
                row[b] += va * xi[b]
    for a in range(k):
        for b in range(a + 1, k):
            xtx[b][a] = xtx[a][b]
    ridge = 1e-8 * (sum(xtx[j][j] for j in range(k)) / k)
    for j in range(k):
        xtx[j][j] += ridge
    beta = _solve_linear(xtx, xty)
    if beta is None:
        return None
    ss = 0.0
    for i in range(n):
        pred = sum(rows[i][j] / scales[a] * beta[a] for a, j in enumerate(active))
        d = ys[i] - pred
        ss += d * d
    return ss
