"""Tests for the shared statistics primitives (``pathbrain.stats``)."""
from __future__ import annotations

from pathbrain.stats import ols_residual_ss, spearman


def test_exact_linear_fit_leaves_no_residual():
    rows = [[float(i), float(i * i % 7)] for i in range(40)]
    ys = [3.0 * r[0] - 2.0 * r[1] for r in rows]
    assert ols_residual_ss(rows, ys) < 1e-6


def test_unrelated_covariates_leave_the_variance_behind():
    # A deterministic pseudo-random outcome with no relation to the columns: the fit may
    # shave a sliver in-sample, but nearly all the squared variation must survive.
    rows = [[float(i), float((i * 13) % 11)] for i in range(60)]
    ys = [float((i * 7919) % 97) - 48.0 for i in range(60)]
    sst = sum(y * y for y in ys)
    ssr = ols_residual_ss(rows, ys)
    assert ssr / sst > 0.9


def test_collinear_columns_are_solvable_not_fatal():
    # x2 = 2·x1 exactly — the ridge keeps the normal equations solvable and the fit exact.
    rows = [[float(i), 2.0 * i] for i in range(30)]
    ys = [5.0 * i for i in range(30)]
    assert ols_residual_ss(rows, ys) < 1e-6


def test_no_columns_means_the_whole_variance_is_residual():
    assert ols_residual_ss([[] for _ in range(3)], [1.0, 2.0, 3.0]) == 14.0


def test_degenerate_inputs_return_none_or_zero():
    assert ols_residual_ss([], []) is None
    assert ols_residual_ss([[1.0]], [1.0, 2.0]) is None  # row/target length mismatch


def test_spearman_still_behaves():  # guard the aliased import surface this module shares
    assert abs(spearman([1.0, 2.0, 3.0, 4.0], [10.0, 20.0, 30.0, 40.0]) - 1.0) < 1e-9
