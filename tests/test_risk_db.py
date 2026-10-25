"""Risk engine against the database: VaR equals an independent calculation for a
portfolio simple enough to compute by hand, and stress tests use actual factor moves."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text

from ballast.risk.engine import compute_var, run_backtest
from ballast.risk.market import load_market_data
from ballast.risk.snapshot import build_snapshot
from ballast.risk.stress import Episode, run_episode
from conftest import insert_instrument, insert_portfolio, insert_trade

DAYS = [d.date() for d in pd.bdate_range("2023-01-02", "2025-06-30")]
TENORS = {"DGS1MO": 1, "DGS3MO": 3, "DGS6MO": 6, "DGS1": 12, "DGS2": 24, "DGS3": 36,
          "DGS5": 60, "DGS7": 84, "DGS10": 120, "DGS20": 240, "DGS30": 360}


@pytest.fixture
def equity_book(db):
    """One stock, 100 shares bought on the first day; a flat 4% curve that never moves."""
    rng = np.random.default_rng(11)
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.015, len(DAYS))))
    with db.begin() as conn:
        iid = insert_instrument(conn, "AAA")
        conn.execute(
            text("INSERT INTO prices (instrument_id, price_date, close, adj_close, source) VALUES (:i, :d, :c, :c, 't')"),
            [{"i": iid, "d": d, "c": Decimal(f"{c:.6f}")} for d, c in zip(DAYS, closes)],
        )
        conn.execute(
            text("INSERT INTO rate_observations (series_id, obs_date, value_pct) VALUES (:s, :d, 4.0)"),
            [{"s": s, "d": d} for s in TENORS for d in DAYS],
        )
        pid = insert_portfolio(conn, "Book")
        insert_trade(conn, pid, iid, "T1", DAYS[0], "100", f"{closes[0]:.6f}")
    return db, pd.Series([float(Decimal(f"{c:.6f}")) for c in closes], index=DAYS)


def test_historical_var_matches_independent_calculation(equity_book):
    engine, closes = equity_book
    md = load_market_data(engine, "Book")
    on = DAYS[-1]
    res = compute_var(md, on, 0.99, lookback=500, sims=20_000, seed=3)

    returns = (closes / closes.shift(1) - 1).dropna().tail(500).to_numpy()
    pnl = np.sort(100 * closes.iloc[-1] * returns)
    assert res.methods["historical"].var == pytest.approx(-pnl[5], abs=1e-6)          # 6th-worst of 500
    assert res.methods["historical"].es == pytest.approx(-pnl[:5].mean(), abs=1e-6)

    # Linear portfolio, rates flat: parametric and Monte Carlo agree with each other.
    assert res.methods["monte_carlo"].var == pytest.approx(res.methods["parametric"].var, rel=0.03)
    assert res.market_value == pytest.approx(100 * closes.iloc[-1], abs=1e-6)


def test_backtest_uses_previous_close_and_actual_pnl(equity_book):
    engine, closes = equity_book
    md = load_market_data(engine, "Book")
    days = run_backtest(md, 0.99, lookback=250)
    assert days, "expected some backtest days"
    first = days[0]
    i = DAYS.index(first.date)
    # P&L of 100 shares from the previous close to this close.
    assert first.actual_pnl == pytest.approx(100 * (closes.iloc[i] - closes.iloc[i - 1]), abs=1e-6)
    returns = (closes / closes.shift(1) - 1).loc[:DAYS[i - 1]].dropna().tail(250).to_numpy()
    expected_var = -np.sort(100 * closes.iloc[i - 1] * returns)[2]                     # floor(250 x 1%) = 2
    assert first.var["historical"] == pytest.approx(expected_var, abs=1e-6)


def test_stress_episode_applies_actual_moves(equity_book):
    engine, closes = equity_book
    md = load_market_data(engine, "Book")
    snap = build_snapshot(md, DAYS[-1])
    ep = Episode("test", date(2024, 3, 1), date(2024, 3, 15), "test")
    r = run_episode(md, snap, ep)
    move = closes.loc[date(2024, 3, 15)] / closes.loc[date(2024, 3, 1)] - 1
    assert r.pnl["equity"] == pytest.approx(100 * closes.iloc[-1] * move, abs=1e-6)
    assert r.pnl["bonds"] == 0 and r.pnl["options"] == 0
    assert r.ten_year_change_bp == 0
