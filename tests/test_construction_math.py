"""Portfolio construction and attribution, checked against closed forms and He and Litterman (1999)."""

from __future__ import annotations

import numpy as np
import pytest

from ballast.construction.attribution import brinson_fachler, link
from ballast.construction.black_litterman import (
    View,
    ViewError,
    equilibrium_returns,
    optimal_weights,
    posterior,
    view_matrices,
)
from ballast.construction.optimize import (
    OptimizationError,
    describe,
    frontier,
    max_sharpe,
    min_variance,
    risk_contributions,
    risk_parity,
)


def _cov(seed=0, n=6):
    rng = np.random.default_rng(seed)
    A = rng.normal(0, 0.2, (n, n))
    return A @ A.T / n + np.diag(rng.uniform(0.01, 0.04, n))


# ---------- mean-variance against closed forms ----------

def test_unconstrained_min_variance_matches_closed_form():
    S = _cov()
    ones = np.ones(len(S))
    exact = np.linalg.solve(S, ones) / (ones @ np.linalg.solve(S, ones))
    assert np.allclose(min_variance(S, long_only=False), exact, atol=1e-7)


def test_unconstrained_tangency_matches_closed_form():
    S, rf = _cov(1), 0.04
    mu = np.array([0.08, 0.10, 0.06, 0.12, 0.07, 0.09])
    raw = np.linalg.solve(S, mu - rf)
    exact = raw / raw.sum()
    assert np.allclose(max_sharpe(mu, S, rf, long_only=False), exact, atol=1e-6)


def test_constraints_are_respected():
    S = _cov(2)
    mu = np.array([0.05, 0.15, 0.06, 0.20, 0.07, 0.09])
    w = max_sharpe(mu, S, 0.03, max_weight=0.30)
    assert w.sum() == pytest.approx(1.0, abs=1e-9)
    assert w.min() >= -1e-9 and w.max() <= 0.30 + 1e-9
    with pytest.raises(OptimizationError):
        min_variance(S, max_weight=0.10)              # 6 x 10% < 100%


def test_frontier_starts_at_min_variance_and_volatility_rises():
    S = _cov(3)
    mu = np.array([0.05, 0.15, 0.06, 0.20, 0.07, 0.09])
    pts = [describe(w, mu, S, 0.0) for w in frontier(mu, S, points=8)]
    gmv = describe(min_variance(S), mu, S, 0.0)
    assert pts[0].volatility == pytest.approx(gmv.volatility, rel=1e-6)
    assert all(b.volatility >= a.volatility - 1e-9 for a, b in zip(pts, pts[1:]))
    assert all(b.expected_return > a.expected_return for a, b in zip(pts, pts[1:]))


def test_max_sharpe_is_the_best_point_on_the_frontier():
    S, rf = _cov(4), 0.03
    mu = np.array([0.05, 0.15, 0.06, 0.20, 0.07, 0.09])
    best = describe(max_sharpe(mu, S, rf), mu, S, rf).sharpe
    assert all(describe(w, mu, S, rf).sharpe <= best + 1e-8 for w in frontier(mu, S, points=25))


def _equity_like_universe(rng):
    """Eight assets with vols and one-factor correlations like the seed portfolio, and the
    returns implied by its weights. Regression input: with a 35% cap the highest-return
    portfolio is a corner (30/35/35), which the frontier once failed to reach."""
    vol = np.array([26.8, 21.5, 23.1, 26.2, 46.7, 15.2, 13.0, 22.3]) / 100
    beta = np.array([0.8, 0.1, 0.6, 0.8, 0.7, 0.95, -0.2, 0.4]) + rng.normal(0, 0.05, 8)
    C = (np.outer(beta, beta) + np.eye(8) * 0.05) / 1.05
    np.fill_diagonal(C, 1.0)
    S = C * np.outer(vol, vol)
    w = np.array([8.3, 12.3, 9.6, 7.6, 15.2, 36.5, 7.0, 3.5]) / 100
    return S, 0.043 + equilibrium_returns(S, w / w.sum(), 2.5)


def test_capped_frontier_reaches_the_highest_attainable_return():
    rng = np.random.default_rng(0)
    for _ in range(100):
        S, mu = _equity_like_universe(rng)
        pts = frontier(mu, S, points=8, max_weight=0.35)
        top = np.sort(mu)[::-1][:3] @ np.array([0.35, 0.35, 0.30])   # the corner portfolio's return
        assert pts[-1] @ mu == pytest.approx(top, abs=1e-8)
        assert all(w.max() <= 0.35 + 1e-9 and w.min() >= -1e-9 for w in pts)
        assert np.all(np.diff([w @ mu for w in pts]) > 0)


# ---------- risk parity ----------

def test_risk_parity_converges_on_hard_covariances():
    """Vols from 0.5% to 150%, correlations up to 0.995, uneven budgets, up to 30 assets."""
    rng = np.random.default_rng(1)
    for _ in range(100):
        n = int(rng.integers(2, 30))
        vol = np.exp(rng.uniform(np.log(0.005), np.log(1.5), n))
        rho = rng.choice([0.0, 0.5, 0.95, 0.995])
        C = rho + (1 - rho) * np.corrcoef(rng.normal(size=(n, 3 * n)))
        np.fill_diagonal(C, 1.0)
        S = C * np.outer(vol, vol)
        budgets = rng.dirichlet(np.full(n, 0.5)) + 1e-4
        rc = risk_contributions(risk_parity(S, budgets), S)
        assert np.allclose(rc, budgets / budgets.sum(), atol=1e-10)
    for _ in range(100):
        S, _ = _equity_like_universe(rng)
        assert np.allclose(risk_contributions(risk_parity(S), S), 1 / 8, atol=1e-10)


def test_risk_parity_with_uncorrelated_assets_is_inverse_volatility():
    vols = np.array([0.10, 0.20, 0.40])
    w = risk_parity(np.diag(vols**2))
    assert np.allclose(w, (1 / vols) / (1 / vols).sum(), atol=1e-8)


def test_risk_parity_equalises_risk_contributions():
    S = _cov(5)
    rc = risk_contributions(risk_parity(S), S)
    assert np.allclose(rc, 1 / len(S), atol=1e-8)
    budgets = np.array([0.4, 0.2, 0.1, 0.1, 0.1, 0.1])
    assert np.allclose(risk_contributions(risk_parity(S, budgets), S), budgets, atol=1e-8)


# ---------- Black-Litterman: He and Litterman (1999) ----------

HL_SYMBOLS = ["AUS", "CAN", "FRA", "GER", "JAP", "UK", "USA"]
HL_CORR = np.array([
    [1.000, 0.488, 0.478, 0.515, 0.439, 0.512, 0.491],
    [0.488, 1.000, 0.664, 0.655, 0.310, 0.608, 0.779],
    [0.478, 0.664, 1.000, 0.861, 0.355, 0.783, 0.668],
    [0.515, 0.655, 0.861, 1.000, 0.354, 0.777, 0.653],
    [0.439, 0.310, 0.355, 0.354, 1.000, 0.405, 0.306],
    [0.512, 0.608, 0.783, 0.777, 0.405, 1.000, 0.652],
    [0.491, 0.779, 0.668, 0.653, 0.306, 0.652, 1.000],
])
HL_VOL = np.array([16.0, 20.3, 24.8, 27.1, 21.0, 20.0, 18.7]) / 100
HL_W = np.array([1.6, 2.2, 5.2, 5.5, 11.6, 12.4, 61.5]) / 100
HL_COV = np.outer(HL_VOL, HL_VOL) * HL_CORR
DELTA, TAU = 2.5, 0.05


def _germany_view():
    """'Germany will outperform the rest of Europe (France and UK, cap-weighted) by 5% a year.'"""
    P = np.array([[0, 0, -5.2 / 17.6, 1, 0, -12.4 / 17.6, 0]])
    return P, np.array([0.05])


def test_he_litterman_equilibrium_returns():
    """Published: 3.9, 6.9, 8.4, 9.0, 4.3, 6.8, 7.6 (percent)."""
    pi = equilibrium_returns(HL_COV, HL_W, DELTA)
    assert np.round(pi * 100, 1).tolist() == [3.9, 6.9, 8.4, 9.0, 4.3, 6.8, 7.6]


def test_he_litterman_posterior_returns_and_weights():
    """Published posterior returns 4.3, 7.6, 9.3, 11.0, 4.5, 7.0, 8.1 and optimal weights
    1.5, 2.1, about -4, 35.4, 11.0, -9.5, 58.6 (percent). France computes to -3.97; it is
    compared within 0.1 point to allow for the paper's rounding."""
    P, Q = _germany_view()
    post = posterior(equilibrium_returns(HL_COV, HL_W, DELTA), HL_COV, TAU, P, Q)
    assert np.round(post.mean * 100, 1).tolist() == [4.3, 7.6, 9.3, 11.0, 4.5, 7.0, 8.1]
    w = optimal_weights(post.mean, post.covariance, DELTA) * 100
    others = [0, 1, 3, 4, 5, 6]
    assert np.round(w[others], 1).tolist() == [1.5, 2.1, 35.4, 11.0, -9.5, 58.6]
    assert w[2] == pytest.approx(-3.95, abs=0.1)


def test_views_only_move_weights_of_assets_in_the_view():
    """He and Litterman's key property: w* = w_eq / (1 + tau) + P' lambda."""
    P, Q = _germany_view()
    post = posterior(equilibrium_returns(HL_COV, HL_W, DELTA), HL_COV, TAU, P, Q)
    shift = optimal_weights(post.mean, post.covariance, DELTA) - HL_W / (1 + TAU)
    untouched = [0, 1, 4, 6]                     # Australia, Canada, Japan, USA
    assert np.allclose(shift[untouched], 0.0, atol=1e-12)


def test_no_views_returns_equilibrium_and_certain_views_hold_exactly():
    pi = equilibrium_returns(HL_COV, HL_W, DELTA)
    assert np.allclose(posterior(pi, HL_COV, TAU).mean, pi)
    P, Q = _germany_view()
    certain = posterior(pi, HL_COV, TAU, P, Q, omega=np.zeros((1, 1)))
    assert (P @ certain.mean)[0] == pytest.approx(0.05, abs=1e-14)
    vague = posterior(pi, HL_COV, TAU, P, Q, omega=np.array([[1e6]]))
    assert np.allclose(vague.mean, pi, atol=1e-8)


def test_reverse_optimization_recovers_reference_weights():
    pi = equilibrium_returns(HL_COV, HL_W, DELTA)
    assert np.allclose(optimal_weights(pi, HL_COV, DELTA), HL_W)


def test_view_parsing():
    P, Q = view_matrices([View.parse("nvda-spy:0.05"), View.parse("GLD:0.02")], ["GLD", "NVDA", "SPY"])
    assert P.tolist() == [[0, 1, -1], [1, 0, 0]] and Q.tolist() == [0.05, 0.02]
    with pytest.raises(ViewError):
        View.parse("NVDA")
    with pytest.raises(ViewError):
        view_matrices([View.parse("TSLA:0.1")], ["SPY"])
    with pytest.raises(ViewError, match="two different assets"):
        view_matrices([View.parse("SPY-SPY:0.05")], ["SPY"])
    with pytest.raises(ViewError):
        view_matrices([View.parse("SPY:inf")], ["SPY"])


# ---------- attribution ----------

def test_brinson_fachler_hand_example():
    """Two segments. Portfolio 70/30 returning 5% and 1%; benchmark 60/40 returning 4% and 2%.
    B = 3.2%, R = 3.8%. Allocation: 0.1 x 0.8% + (-0.1) x (-1.2%) = 0.2%.
    Selection: 0.6 x 1% + 0.4 x (-1%) = 0.2%. Interaction: 0.1 x 1% + (-0.1) x (-1%) = 0.2%."""
    e = brinson_fachler([0.7, 0.3], [0.05, 0.01], [0.6, 0.4], [0.04, 0.02])
    assert e["allocation"].sum() == pytest.approx(0.002, abs=1e-15)
    assert e["selection"].sum() == pytest.approx(0.002, abs=1e-15)
    assert e["interaction"].sum() == pytest.approx(0.002, abs=1e-15)


def test_single_period_effects_add_up_to_active_return():
    rng = np.random.default_rng(9)
    w, W = rng.dirichlet(np.ones(4)), rng.dirichlet(np.ones(4))
    r, b = rng.normal(0, 0.02, 4), rng.normal(0, 0.02, 4)
    total = sum(v.sum() for v in brinson_fachler(w, r, W, b).values())
    assert total == pytest.approx(w @ r - W @ b, abs=1e-15)


def test_carino_linking_adds_up_to_cumulative_active_return():
    rng = np.random.default_rng(10)
    days = [{"w": rng.dirichlet(np.ones(3)), "r": rng.normal(0.0005, 0.01, 3),
             "W": np.array([0.6, 0.4, 0.0]), "b": rng.normal(0.0004, 0.01, 3)} for _ in range(500)]
    res = link(days, ["equity", "rates", "gold"])
    R = np.prod([1 + d["w"] @ d["r"] for d in days]) - 1
    B = np.prod([1 + d["W"] @ d["b"] for d in days]) - 1
    assert (res.portfolio_return, res.benchmark_return) == (pytest.approx(R, abs=1e-12), pytest.approx(B, abs=1e-12))
    assert res.effects["total"].sum() == pytest.approx(R - B, abs=1e-12)
