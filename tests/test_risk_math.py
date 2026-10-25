"""Risk math: VaR and ES definitions, backtest statistics, and the vectorized revaluation."""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pytest
from scipy.stats import binom, norm

from ballast.derivatives.store import OptionContract, option_value
from ballast.derivatives.swaps import OISSwap, pv_per_100
from ballast.fixed_income.batch import BatchCurveBuilder
from ballast.fixed_income.bonds import Bond, price_from_curve
from ballast.fixed_income.curve import bootstrap_par_curve
from ballast.risk.backtest import basel_zone, christoffersen_independence, conditional_coverage, kupiec_pof
from ballast.risk.var import ewma_covariance, monte_carlo_factors, parametric, tail_risk

TENORS = np.array([1, 3, 6, 12, 24, 36, 60, 84, 120, 240, 360]) / 12
PAR = np.array([0.043, 0.0425, 0.042, 0.0405, 0.0395, 0.0398, 0.0405, 0.0415, 0.0425, 0.0455, 0.046])


# ---------- VaR / ES definitions ----------

def test_historical_var_is_the_sixth_worst_of_500():
    pnl = np.arange(1, 501, dtype=float)          # losses -1 ... -500 after negation below
    r = tail_risk(-pnl, 0.99)
    assert r.var == 495.0                          # 6th-worst loss: exactly 5 scenarios lose more
    assert r.es == np.mean([500, 499, 498, 497, 496])
    assert r.scenarios == 500


def test_parametric_normal_constants():
    """Standard normal: 99% VaR = 2.326 sigma, 99% ES = 2.665 sigma; 95%: 1.645 and 2.063."""
    s, cov = np.array([1.0]), np.array([[1.0]])
    r99, sigma = parametric(s, cov, 0.99)
    r95, _ = parametric(s, cov, 0.95)
    assert sigma == 1.0
    assert (round(r99.var, 3), round(r99.es, 3)) == (2.326, 2.665)
    assert (round(r95.var, 3), round(r95.es, 3)) == (1.645, 2.063)


def test_monte_carlo_matches_parametric_for_a_linear_portfolio():
    cov = np.array([[0.0004, 0.0001], [0.0001, 0.0009]])
    s = np.array([100_000.0, -50_000.0])
    exact, _ = parametric(s, cov, 0.99)
    mc = tail_risk(monte_carlo_factors(cov, 400_000, seed=1) @ s, 0.99)
    assert mc.var == pytest.approx(exact.var, rel=0.01)
    assert mc.es == pytest.approx(exact.es, rel=0.015)


def test_ewma_covariance_matches_riskmetrics_recursion():
    """S_t = lam S_(t-1) + (1 - lam) x_t x_t'. After 300 days the starting value's weight
    (0.94^299, about 1e-8) is negligible, so the recursion and the closed form agree."""
    rng = np.random.default_rng(0)
    X = rng.normal(0, 0.01, (300, 3))
    lam = 0.94
    S = np.outer(X[0], X[0])
    for x in X[1:]:
        S = lam * S + (1 - lam) * np.outer(x, x)
    assert np.allclose(ewma_covariance(X, lam), S, rtol=1e-6)


# ---------- backtest statistics ----------

@pytest.mark.parametrize("x,n,p", [(0, 250, 0.01), (2, 250, 0.01), (5, 500, 0.01), (12, 250, 0.01), (30, 500, 0.05)])
def test_kupiec_matches_binomial_likelihood_ratio(x, n, p):
    """Independent check: the binomial coefficients cancel, so LR = 2 [log L(x/n) - log L(p)]."""
    lr = 2 * (binom.logpmf(x, n, x / n) - binom.logpmf(x, n, p))
    assert kupiec_pof(x, n, p).statistic == pytest.approx(lr, abs=1e-9)


def test_kupiec_is_zero_when_exceptions_match_expectation():
    assert kupiec_pof(5, 500, 0.01).statistic == pytest.approx(0.0, abs=1e-12)
    assert not kupiec_pof(5, 500, 0.01).rejects()
    assert kupiec_pof(15, 500, 0.01).rejects()


def test_christoffersen_detects_clustered_exceptions():
    spread = [i % 50 == 0 for i in range(500)]              # 10 isolated exceptions
    clustered = [200 <= i < 210 for i in range(500)]        # 10 consecutive exceptions
    assert not christoffersen_independence(spread).rejects()
    assert christoffersen_independence(clustered).rejects()
    assert conditional_coverage(10, 500, 0.01, clustered).dof == 2


def test_basel_traffic_light_zones():
    assert [basel_zone(x) for x in (0, 4, 5, 9, 10, 15)] == ["green", "green", "yellow", "yellow", "red", "red"]


# ---------- vectorized revaluation ----------

def test_batch_bootstrap_matches_scalar_curve():
    rng = np.random.default_rng(1)
    scenarios = PAR + rng.normal(0, 0.003, (100, 11))
    times = np.concatenate([[0.0, 0.01, 0.5, 1.0, 29.99, 30.0, 31.5], rng.uniform(0, 30, 40)])
    batch = BatchCurveBuilder(TENORS, times).discount_factors(scenarios)
    scalar = np.array([bootstrap_par_curve(TENORS, p).df(times) for p in scenarios])
    assert np.abs(batch - scalar).max() < 1e-14


def test_bond_and_swap_are_linear_in_discount_factors():
    """The representations used for VaR give exactly the same values as the pricers."""
    on = date(2026, 10, 23)
    curve = bootstrap_par_curve(TENORS, PAR)
    bond = Bond(0.0425, date(2024, 11, 15), date(2034, 11, 15))
    flows = bond.cashflows_after(on)
    t = np.array([(d - on).days / 365.25 for d, _ in flows])
    assert float(np.sum([a for _, a in flows] * curve.df(t))) == pytest.approx(price_from_curve(bond, curve, on).dirty, abs=1e-12)

    swap = OISSwap(date(2026, 9, 1), date(2036, 9, 1), 0.0412)
    growth = 1.0057
    rem = [(s, e) for s, e in swap.periods() if e > on]
    times = np.array([(e - on).days / 365.25 for _, e in rem])
    weights = np.array([100 * 0.0412 * (e - s).days / 360 for s, e in rem])
    weights[-1] += 100
    linear = -100 * growth + float(weights @ curve.df(times))
    assert linear == pytest.approx(pv_per_100(swap, curve, on, growth), abs=1e-12)


def test_option_price_grid_interpolation_is_accurate():
    from scipy.interpolate import CubicSpline

    c = OptionContract(1, "X", 2, "NVDA", "call", 250.0, date(2027, 1, 15), True)
    S, T, r, q, s = 210.96, 84 / 365, 0.037, 0.0047, 0.359
    u = np.linspace(-0.35, 0.35, 41)
    spline = CubicSpline(u, [option_value(c, S * math.exp(x), T, r, q, s, steps=200) for x in u])
    for x in (-0.17, -0.05, 0.013, 0.08, 0.21):
        direct = option_value(c, S * math.exp(x), T, r, q, s, steps=200)
        assert float(spline(x)) == pytest.approx(direct, abs=2e-3)
