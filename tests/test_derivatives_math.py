"""Derivatives math, checked against Hull's textbook examples and the New York Fed's
published SOFR Index worked example."""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pytest

from ballast.derivatives.binomial import binomial_price, crr_price
from ballast.derivatives.black_scholes import (
    PricingError,
    black76_price,
    bs_greeks,
    bs_price,
    implied_vol_black76,
    implied_vol_bs,
)
from ballast.derivatives.sofr import FixingError, compound_factor, sofr_average, sofr_index
from ballast.derivatives.swaps import OISSwap, annuity, par_rate, payments, pv_per_100, realized_growth
from ballast.fixed_income.risk import ParCurve


# ---------- Black-Scholes: Hull, Options, Futures, and Other Derivatives ----------

def test_hull_black_scholes_example():
    """S=42, K=40, r=10%, sigma=20%, T=0.5: published call 4.76, put 0.81."""
    assert round(bs_price("call", 42, 40, 0.5, 0.10, 0.20), 2) == 4.76
    assert round(bs_price("put", 42, 40, 0.5, 0.10, 0.20), 2) == 0.81


def test_hull_greeks_example():
    """S=49, K=50, r=5%, sigma=20%, T=20 weeks: published price 2.40, delta 0.522,
    gamma 0.066, vega 12.1, theta -4.31 per year, rho 8.91."""
    g = bs_greeks("call", 49, 50, 20 / 52, 0.05, 0.20)
    assert round(g.price, 2) == 2.40
    assert round(g.delta, 3) == 0.522
    assert round(g.gamma, 3) == 0.066
    assert round(g.vega, 1) == 12.1
    assert round(g.theta, 2) == -4.31
    assert round(g.rho, 2) == 8.91


@pytest.mark.parametrize("q", [0.0, 0.015])
def test_put_call_parity(q):
    S, K, T, r, sigma = 100.0, 95.0, 0.75, 0.04, 0.25
    c, p = bs_price("call", S, K, T, r, sigma, q), bs_price("put", S, K, T, r, sigma, q)
    assert c - p == pytest.approx(S * math.exp(-q * T) - K * math.exp(-r * T), abs=1e-12)


@pytest.mark.parametrize("option_type", ["call", "put"])
def test_greeks_match_finite_differences(option_type):
    S, K, T, r, sigma, q = 100.0, 105.0, 0.5, 0.04, 0.3, 0.01
    g = bs_greeks(option_type, S, K, T, r, sigma, q)
    f = lambda **kw: bs_price(option_type, kw.get("S", S), K, kw.get("T", T), kw.get("r", r), kw.get("sigma", sigma), q)
    h = 1e-4
    assert g.delta == pytest.approx((f(S=S + h) - f(S=S - h)) / (2 * h), rel=1e-6)
    assert g.gamma == pytest.approx((f(S=S + h) - 2 * f() + f(S=S - h)) / h**2, rel=1e-4)
    assert g.vega == pytest.approx((f(sigma=sigma + h) - f(sigma=sigma - h)) / (2 * h), rel=1e-6)
    assert g.rho == pytest.approx((f(r=r + h) - f(r=r - h)) / (2 * h), rel=1e-6)
    assert g.theta == pytest.approx(-(f(T=T + h) - f(T=T - h)) / (2 * h), rel=1e-6)


def test_black76_equals_black_scholes_on_the_forward():
    S, K, T, r, q, sigma = 100.0, 110.0, 1.0, 0.05, 0.02, 0.22
    F = S * math.exp((r - q) * T)
    assert black76_price("call", F, K, T, r, sigma) == pytest.approx(bs_price("call", S, K, T, r, sigma, q), abs=1e-12)


@pytest.mark.parametrize("option_type,K", [("call", 90.0), ("call", 130.0), ("put", 70.0), ("put", 100.0)])
def test_implied_vol_round_trip(option_type, K):
    S, T, r, q, sigma = 100.0, 0.4, 0.045, 0.012, 0.37
    price = bs_price(option_type, S, K, T, r, sigma, q)
    assert implied_vol_bs(option_type, price, S, K, T, r, q) == pytest.approx(sigma, abs=1e-9)


def test_implied_vol_rejects_prices_outside_arbitrage_bounds():
    with pytest.raises(PricingError):
        implied_vol_black76("call", 0.5, 100.0, 90.0, 0.5, 0.0)   # below intrinsic (10)
    with pytest.raises(PricingError):
        implied_vol_black76("put", 120.0, 100.0, 110.0, 0.5, 0.0)  # above the strike


# ---------- Binomial trees ----------

def test_hull_two_step_american_put():
    """u=1.2, d=0.8, r=5%, two 1-year steps, S=50, K=52: published European put 4.1923
    and American put 5.0894. Hull rounds p to 0.6282; the exact p gives 4.1927 and 5.0896,
    so the comparison allows for that rounding."""
    euro = binomial_price("put", 50, 52, 2.0, 0.05, 2, 1.2, 0.8)
    amer = binomial_price("put", 50, 52, 2.0, 0.05, 2, 1.2, 0.8, american=True)
    assert euro == pytest.approx(4.1923, abs=5e-4)
    assert amer == pytest.approx(5.0894, abs=5e-4)


def test_hull_american_put_crr_tree_and_convergence():
    """S=K=50, r=10%, sigma=40%, T=5 months: published 4.49 with 5 steps, and
    4.263, 4.272, 4.278, 4.283 with 30, 50, 100, 500 steps."""
    price = lambda n: crr_price("put", 50, 50, 5 / 12, 0.10, 0.40, steps=n, american=True)
    assert round(price(5), 2) == 4.49
    assert [round(price(n), 3) for n in (30, 50, 100, 500)] == [4.263, 4.272, 4.278, 4.283]


def test_european_tree_converges_to_black_scholes():
    tree = crr_price("call", 42, 40, 0.5, 0.10, 0.20, steps=2000)
    assert tree == pytest.approx(bs_price("call", 42, 40, 0.5, 0.10, 0.20), abs=1e-3)


def test_american_call_without_dividends_is_never_exercised_early():
    euro = crr_price("call", 100, 95, 1.0, 0.05, 0.3, steps=400)
    amer = crr_price("call", 100, 95, 1.0, 0.05, 0.3, steps=400, american=True)
    assert amer == pytest.approx(euro, abs=1e-12)


def test_american_put_worth_at_least_european():
    euro = crr_price("put", 100, 110, 1.0, 0.05, 0.3, steps=400)
    amer = crr_price("put", 100, 110, 1.0, 0.05, 0.3, steps=400, american=True)
    assert amer > euro


# ---------- SOFR: New York Fed worked example ----------

NYFED_FIXINGS = {
    date(2018, 4, 2): 0.0180,
    date(2018, 4, 3): 0.0183,
    date(2018, 4, 4): 0.0174,
    date(2018, 4, 5): 0.0175,
    date(2018, 4, 6): 0.0175,   # Friday: applies for 3 calendar days
}


def test_nyfed_sofr_index_worked_example():
    """Published index values 1.00005000, 1.00010084, 1.00014917, 1.00019779, 1.00034365
    for publication dates 3, 4, 5, 6 and 9 April 2018."""
    base = date(2018, 4, 2)
    pubs = [date(2018, 4, 3), date(2018, 4, 4), date(2018, 4, 5), date(2018, 4, 6), date(2018, 4, 9)]
    got = [round(sofr_index(NYFED_FIXINGS, base, p), 8) for p in pubs]
    assert got == [1.00005000, 1.00010084, 1.00014917, 1.00019779, 1.00034365]


def test_average_window_starting_on_saturday_uses_fridays_rate_for_two_days():
    fixings = {date(2024, 3, 1) + timedelta(days=i): 0.05 for i in range(40)}
    fixings = {d: r for d, r in fixings.items() if d.weekday() < 5}
    fixings[date(2024, 3, 8)] = 0.06            # Friday
    publication = date(2024, 3, 9) + timedelta(days=30)   # window starts Saturday 9 March
    expected = (1 + 0.06 * 2 / 360)
    d = date(2024, 3, 11)
    while d < publication:
        run = 3 if d.weekday() == 4 else 1
        run = min(run, (publication - d).days)
        expected *= 1 + 0.05 * run / 360
        d += timedelta(days=run)
    assert sofr_average(fixings, publication) == pytest.approx((expected - 1) * 12, abs=1e-15)


def test_constant_sofr_average_equals_its_compounded_rate():
    fixings = {date(2024, 1, 1) + timedelta(days=i): 0.0533 for i in range(60)}
    pub = date(2024, 2, 15)
    assert sofr_average(fixings, pub) == pytest.approx(((1 + 0.0533 / 360) ** 30 - 1) * 12, abs=1e-13)


def test_stale_fixings_are_rejected():
    with pytest.raises(FixingError):
        compound_factor({date(2024, 1, 2): 0.05}, date(2024, 1, 2), date(2024, 2, 1))


# ---------- SOFR OIS swap ----------

FLAT = ParCurve((6, 12, 60, 120, 360), (0.04,) * 5)


def test_swap_at_par_rate_is_worth_zero():
    curve, on = FLAT.build(), date(2026, 9, 1)
    s = OISSwap(on, date(2036, 9, 1), 0.0)
    k = par_rate(s, curve, on)
    assert pv_per_100(OISSwap(on, date(2036, 9, 1), k), curve, on, 1.0) == pytest.approx(0.0, abs=1e-12)


def test_off_market_swap_value_is_rate_difference_times_annuity():
    curve, on = FLAT.build(), date(2026, 9, 1)
    k_par = par_rate(OISSwap(on, date(2031, 9, 1), 0.0), curve, on)
    s = OISSwap(on, date(2031, 9, 1), k_par + 0.0050)
    assert pv_per_100(s, curve, on, 1.0) == pytest.approx(100 * 0.0050 * annuity(s, curve, on), abs=1e-12)


def test_floating_leg_telescopes_to_period_by_period_sum():
    """Mid-period valuation: realized growth plus projected forwards equals A - DF(T)."""
    curve = ParCurve((6, 12, 60, 120, 360), (0.039, 0.040, 0.042, 0.044, 0.047)).build()
    swap = OISSwap(date(2026, 3, 2), date(2031, 3, 3), 0.042)
    on, growth = date(2026, 10, 23), 1.0213
    periods = [(s, e) for s, e in swap.periods() if e > on]
    t = lambda d: (d - on).days / 365.25
    # Current period pays N(A/DF(e0) - 1); each future period pays N(DF(s)/DF(e) - 1). Discount each payment.
    explicit = growth / float(curve.df(t(periods[0][1]))) - 1.0
    explicit *= float(curve.df(t(periods[0][1])))
    for s, e in periods[1:]:
        explicit += (float(curve.df(t(s))) / float(curve.df(t(e))) - 1.0) * float(curve.df(t(e)))
    fixed = 0.042 * annuity(swap, curve, on)
    assert pv_per_100(swap, curve, on, growth) == pytest.approx(100 * (fixed - explicit), abs=1e-10)


def test_swap_payment_with_constant_sofr():
    fixings = {date(2026, 9, 1) + timedelta(days=i): 0.04 for i in range(400)}
    swap = OISSwap(date(2026, 9, 1), date(2028, 9, 1), 0.041)
    (pay_date, amount), = payments(swap, fixings, date(2027, 9, 1))
    assert pay_date == date(2027, 9, 1)
    growth = (1 + 0.04 / 360) ** 365
    assert amount == pytest.approx(100 * (0.041 * 365 / 360 - (growth - 1)), abs=1e-12)


def test_realized_growth_is_one_on_a_period_start():
    swap = OISSwap(date(2026, 9, 1), date(2028, 9, 1), 0.04)
    assert realized_growth(swap, {date(2026, 8, 31): 0.04}, date(2026, 9, 1)) == 1.0


def test_pay_fixed_swap_gains_when_rates_rise():
    on = date(2026, 9, 1)
    k = par_rate(OISSwap(on, date(2036, 9, 1), 0.0), FLAT.build(), on)
    swap = OISSwap(on, date(2036, 9, 1), k)
    up = pv_per_100(swap, FLAT.shifted(25).build(), on, 1.0)
    assert -up > 0            # receive-fixed loses, so pay-fixed (negative quantity) gains


def test_report_greeks_match_analytic_black_scholes_for_european_options():
    from ballast.derivatives.analytics import numeric_greeks
    from ballast.derivatives.store import OptionContract

    S, K, T, r, q, sigma = 600.0, 580.0, 0.3, 0.042, 0.012, 0.19
    for kind in ("call", "put"):
        c = OptionContract(1, "X", 2, "SPY", kind, K, date(2027, 1, 15), american=False)
        got = numeric_greeks(c, S, T, r, q, sigma)
        exact = bs_greeks(kind, S, K, T, r, sigma, q)
        assert (got.price, got.delta, got.gamma) == (exact.price, exact.delta, exact.gamma)
        assert got.vega_point == pytest.approx(exact.vega / 100, abs=1e-15)
        assert got.theta_day == pytest.approx(exact.theta / 365, abs=1e-15)
        assert got.rho_pct == pytest.approx(exact.rho / 100, abs=1e-15)


def test_american_tree_greeks_are_close_to_european_for_an_otm_put():
    """Early exercise matters little for an out-of-the-money put, so tree Greeks
    should sit close to the Black-Scholes ones, with a slightly larger put delta."""
    from ballast.derivatives.analytics import numeric_greeks
    from ballast.derivatives.store import OptionContract

    S, K, T, r, q, sigma = 750.0, 700.0, 0.15, 0.04, 0.012, 0.16
    amer = numeric_greeks(OptionContract(1, "X", 2, "SPY", "put", K, date(2026, 12, 18), True), S, T, r, q, sigma)
    euro = bs_greeks("put", S, K, T, r, sigma, q)
    assert amer.price >= euro.price
    assert amer.delta == pytest.approx(euro.delta, abs=0.02)
    assert amer.vega_point == pytest.approx(euro.vega / 100, rel=0.05)
    assert amer.gamma == pytest.approx(euro.gamma, rel=0.10)


def test_american_greeks_use_control_variate_so_gamma_is_not_tree_noise():
    """Regression for a bug found on real data: an out-of-the-money NVDA call whose
    plain-tree gamma was 2.3x the true value. Early exercise is worthless here
    (tiny dividend), so the American Greeks must match Black-Scholes closely."""
    from ballast.derivatives.analytics import numeric_greeks
    from ballast.derivatives.store import OptionContract

    S, K, T, r, q, sigma = 210.96, 250.0, 84 / 365, 0.037, 0.0047, 0.359
    amer = numeric_greeks(OptionContract(1, "X", 2, "NVDA", "call", K, date(2027, 1, 15), True), S, T, r, q, sigma)
    euro = bs_greeks("call", S, K, T, r, sigma, q)
    assert amer.price == pytest.approx(euro.price, abs=1e-6)
    assert amer.delta == pytest.approx(euro.delta, rel=1e-3)
    assert amer.gamma == pytest.approx(euro.gamma, rel=1e-2)
    assert amer.vega_point == pytest.approx(euro.vega / 100, rel=1e-3)
