"""Fixed-income math, checked against textbook examples and exact identities."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from ballast.fixed_income.bonds import (
    Bond,
    cashflow_yield_risk,
    price_from_curve,
    yield_from_dirty_price,
    yield_risk,
)
from ballast.fixed_income.curve import (
    BondQuote,
    CurveError,
    DiscountCurve,
    bootstrap_from_bond_prices,
    bootstrap_par_curve,
)
from ballast.fixed_income.risk import ParCurve, curve_risk, historical_shifts, key_rate_dv01s, scenario_shifts
from ballast.fixed_income.schedule import coupon_dates, coupon_period
from ballast.fixed_income.sensitivity import empirical_duration

CMT_MONTHS = (1, 3, 6, 12, 24, 36, 60, 84, 120, 240, 360)


# ---------- textbook benchmarks: Hull, Options, Futures, and Other Derivatives, ch. 4 ----------

def test_hull_bootstrap_zero_rates():
    """Hull's bootstrap example: five bonds, published zero rates 10.127%, 10.469%,
    10.536%, 10.681%, 10.808% (continuous compounding)."""
    quotes = [
        BondQuote(0.25, 0.0, 97.5),
        BondQuote(0.50, 0.0, 94.9),
        BondQuote(1.00, 0.0, 90.0),
        BondQuote(1.50, 8.0, 96.0),
        BondQuote(2.00, 12.0, 101.6),
    ]
    t, r = bootstrap_from_bond_prices(quotes)
    assert list(t) == [0.25, 0.5, 1.0, 1.5, 2.0]
    assert np.round(r * 100, 3).tolist() == [10.127, 10.469, 10.536, 10.681, 10.808]


def test_hull_duration_and_convexity():
    """Hull's duration example: 3-year 10% bond, semiannual coupons, yield 12% continuous.
    Published: price 94.213, duration 2.653 years, convexity 7.570."""
    times = np.arange(1, 7) / 2.0
    flows = np.array([5, 5, 5, 5, 5, 105], dtype=float)
    risk = cashflow_yield_risk(times, flows, 0.12, compounding=None)
    assert round(risk.price, 3) == 94.213
    assert round(risk.macaulay_duration, 3) == 2.653
    assert round(risk.convexity, 3) == 7.570


# ---------- coupon schedules and accrued interest ----------

def test_coupon_dates_mid_month():
    assert coupon_dates(date(2024, 11, 15), date(2026, 11, 15)) == [
        date(2025, 5, 15), date(2025, 11, 15), date(2026, 5, 15), date(2026, 11, 15)
    ]


def test_coupon_dates_end_of_month_rule():
    assert coupon_dates(date(2025, 8, 31), date(2027, 8, 31)) == [
        date(2026, 2, 28), date(2026, 8, 31), date(2027, 2, 28), date(2027, 8, 31)
    ]
    # Leap year: February coupon on the 29th, and the 31st is never lost to drift.
    assert coupon_dates(date(2027, 8, 31), date(2028, 8, 31))[0] == date(2028, 2, 29)


def test_coupon_period_on_coupon_date_starts_new_period():
    assert coupon_period(date(2024, 11, 15), date(2034, 11, 15), 2, date(2025, 5, 15)) == (
        date(2025, 5, 15), date(2025, 11, 15)
    )


def test_accrued_interest_actual_actual():
    """4.25% note, coupons 15 May / 15 Nov, valued 3 Mar 2025:
    108 days accrued of a 181-day period -> 2.125 x 108 / 181 = 1.267956."""
    bond = Bond(0.0425, date(2024, 11, 15), date(2034, 11, 15))
    assert bond.accrued_interest(date(2025, 3, 3)) == pytest.approx(2.125 * 108 / 181, abs=1e-12)
    assert bond.accrued_interest(date(2025, 5, 15)) == 0.0


def test_cashflows_exclude_coupon_paid_on_valuation_date():
    bond = Bond(0.04, date(2024, 11, 15), date(2026, 11, 15))
    flows = bond.cashflows_after(date(2025, 5, 15))
    assert flows[0] == (date(2025, 11, 15), 2.0)
    assert flows[-1] == (date(2026, 11, 15), 102.0)


# ---------- yield, duration, convexity ----------

def test_yield_price_round_trip():
    bond = Bond(0.0425, date(2024, 11, 15), date(2034, 11, 15))
    on = date(2025, 3, 3)
    dirty = yield_risk(bond, on, 0.0437).price
    assert yield_from_dirty_price(bond, on, dirty) == pytest.approx(0.0437, abs=1e-12)


def test_par_bond_on_coupon_date_prices_at_100():
    bond = Bond(0.05, date(2025, 1, 15), date(2035, 1, 15))
    assert yield_risk(bond, date(2025, 7, 15), 0.05).price == pytest.approx(100.0, abs=1e-10)


def test_analytic_dv01_and_convexity_match_finite_differences():
    bond = Bond(0.0425, date(2024, 11, 15), date(2034, 11, 15))
    on, y, h = date(2025, 3, 3), 0.045, 1e-4
    base = yield_risk(bond, on, y)
    up, down = yield_risk(bond, on, y + h).price, yield_risk(bond, on, y - h).price
    assert base.dv01 == pytest.approx((down - up) / 2, rel=1e-6)
    assert base.convexity == pytest.approx((up + down - 2 * base.price) / (base.price * h * h), rel=1e-4)


# ---------- par-curve bootstrap ----------

def _upward_curve() -> ParCurve:
    yields = (0.0430, 0.0425, 0.0420, 0.0405, 0.0395, 0.0398, 0.0405, 0.0415, 0.0425, 0.0455, 0.0460)
    return ParCurve(CMT_MONTHS, yields)


def _inverted_curve() -> ParCurve:  # roughly mid-2023
    yields = (0.0540, 0.0545, 0.0550, 0.0535, 0.0490, 0.0465, 0.0430, 0.0415, 0.0400, 0.0420, 0.0395)
    return ParCurve(CMT_MONTHS, yields)


@pytest.mark.parametrize("par", [_upward_curve(), _inverted_curve()])
def test_par_bonds_reprice_to_100_at_every_grid_point(par):
    curve = par.build()
    grid_par = np.interp(np.arange(1, 61) / 2, par.tenors_years, par.par_yields)
    for n, y in enumerate(grid_par, start=1):
        times = np.arange(1, n + 1) / 2
        flows = np.full(n, 100 * y / 2)
        flows[-1] += 100
        assert float(np.sum(flows * curve.df(times))) == pytest.approx(100.0, abs=1e-10)


def test_bills_use_bond_equivalent_simple_interest():
    curve = _upward_curve().build()
    assert curve.df(1 / 12) == pytest.approx(1 / (1 + 0.0430 / 12), abs=1e-14)
    assert curve.df(3 / 12) == pytest.approx(1 / (1 + 0.0425 / 4), abs=1e-14)


def test_flat_par_curve_gives_flat_zero_curve():
    par = ParCurve((6, 12, 60, 120, 360), (0.04,) * 5)
    z = par.build().zero_rate(np.arange(1, 61) / 2, compounding=2)
    assert np.allclose(z, 0.04, atol=1e-12)


@pytest.mark.parametrize("par", [_upward_curve(), _inverted_curve()])
def test_discount_factors_decrease_and_forwards_are_positive(par):
    curve = par.build()
    t = np.linspace(0.01, 30, 3000)
    assert np.all(np.diff(curve.df(t)) < 0)
    assert np.all(curve.forward_rate(t[:-1], t[1:]) > 0)


def test_log_linear_interpolation_gives_constant_forward_between_knots():
    curve = DiscountCurve(np.array([1.0, 2.0]), np.array([0.96, 0.92]))
    f1 = curve.forward_rate(1.1, 1.3)
    f2 = curve.forward_rate(1.6, 1.9)
    assert f1 == pytest.approx(f2, abs=1e-14)
    assert f1 == pytest.approx(np.log(0.96 / 0.92), abs=1e-14)


def test_invalid_inputs_raise():
    with pytest.raises(CurveError):
        bootstrap_par_curve([0.5, 0.5], [0.04, 0.04])
    with pytest.raises(CurveError):
        DiscountCurve(np.array([1.0, 0.5]), np.array([0.9, 0.95]))


# ---------- curve-based risk ----------

def _bond_price_fn(bond: Bond, on: date):
    return lambda curve: price_from_curve(bond, curve, on).dirty


def test_effective_duration_equals_modified_duration_on_flat_curve():
    """On a flat par curve, a bond valued on a coupon date has curve-based effective
    duration equal to its yield-based modified duration."""
    bond = Bond(0.04, date(2025, 1, 15), date(2035, 1, 15))
    on = date(2025, 1, 15)
    par = ParCurve((6, 12, 60, 120, 360), (0.04,) * 5)
    # Value cash flows on exact half-year times so the curve and the street convention agree.
    curve_risk_result = curve_risk(
        lambda c: float(np.sum(np.r_[np.full(19, 2.0), 102.0] * c.df(np.arange(1, 21) / 2))), par
    )
    street = yield_risk(bond, on, 0.04)
    assert curve_risk_result.price == pytest.approx(100.0, abs=1e-10)
    assert curve_risk_result.effective_duration == pytest.approx(street.modified_duration, rel=1e-6)


def test_key_rate_dv01s_sum_to_parallel_dv01():
    bond = Bond(0.0425, date(2024, 11, 15), date(2034, 11, 15))
    fn, par = _bond_price_fn(bond, date(2026, 10, 23)), _upward_curve()
    krd = key_rate_dv01s(fn, par)
    assert krd.sum() == pytest.approx(curve_risk(fn, par).dv01, rel=1e-6)


def test_key_rates_beyond_maturity_have_zero_exposure():
    bond = Bond(0.04, date(2025, 11, 15), date(2030, 11, 15))   # 5-year cash flows at most
    krd = key_rate_dv01s(_bond_price_fn(bond, date(2025, 11, 15)), _upward_curve())
    tenor_years = np.array(CMT_MONTHS) / 12
    assert np.all(krd[tenor_years > 5] == 0.0)
    assert krd[tenor_years == 5][0] > 0


def test_full_revaluation_close_to_duration_convexity_approximation():
    bond = Bond(0.0425, date(2024, 11, 15), date(2034, 11, 15))
    fn, par = _bond_price_fn(bond, date(2026, 10, 23)), _upward_curve()
    risk = curve_risk(fn, par)
    shocked = fn(par.shifted(scenario_shifts("parallel_up_100", par)).build())
    dy = 0.01
    approx = risk.price * (-risk.effective_duration * dy + 0.5 * risk.effective_convexity * dy * dy)
    assert shocked - risk.price == pytest.approx(approx, rel=2e-3)
    assert shocked < risk.price


def test_historical_shift_is_difference_of_par_yields():
    a, b = _upward_curve(), _inverted_curve()
    shifts = historical_shifts(a, b)
    assert shifts[0] == pytest.approx((0.0540 - 0.0430) / 1e-4)
    assert np.allclose(np.array(a.shifted(shifts).par_yields), b.par_yields, atol=1e-15)


# ---------- empirical duration ----------

def test_empirical_duration_recovers_known_sensitivity():
    rng = np.random.default_rng(7)
    dy = rng.normal(0, 0.0008, 1000)
    returns = 0.0001 - 16.0 * dy + rng.normal(0, 0.002, 1000)
    est = empirical_duration(returns, dy)
    assert est.duration == pytest.approx(16.0, abs=0.3)
    assert 0.8 < est.r_squared < 1.0
    assert est.observations == 1000
