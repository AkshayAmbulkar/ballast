"""End-to-end: `ballast bootstrap` on the real seed files, with synthetic market data
standing in for Yahoo and FRED so the test runs offline and deterministically."""

from __future__ import annotations

import zlib
from datetime import date
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text

from ballast import cli
from ballast.market_data import fred, yahoo
from ballast.portfolio.pnl import reference_pnl

NVDA_SPLIT_DATE = pd.Timestamp("2024-06-10", tz="America/New_York")
END = "2026-10-23"


def synthetic_yahoo(symbol: str, start, repair):
    """Business-day random walk. Like real Yahoo data, the NVDA series is continuous
    (already split-adjusted) and the split appears only in the Stock Splits column."""
    idx = pd.bdate_range(pd.Timestamp(start), END, tz="America/New_York")
    rng = np.random.default_rng(zlib.crc32(symbol.encode()))
    close = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.012, len(idx))))
    splits = np.zeros(len(idx))
    dividends = np.zeros(len(idx))
    dividends[::63] = 0.25                                   # roughly quarterly
    if symbol == "NVDA":
        splits[idx.get_loc(NVDA_SPLIT_DATE)] = 10.0
    return pd.DataFrame(
        {"Close": close, "Adj Close": close, "Volume": 1_000_000, "Dividends": dividends, "Stock Splits": splits},
        index=idx,
    )


TENOR_MONTHS = {"DGS1MO": 1, "DGS3MO": 3, "DGS6MO": 6, "DGS1": 12, "DGS2": 24, "DGS3": 36,
                "DGS5": 60, "DGS7": 84, "DGS10": 120, "DGS20": 240, "DGS30": 360}


def synthetic_fred(series_id: str, api_key: str, start):
    """Upward-sloping curve that drifts slowly; SOFR tracks the 1-month bill."""
    days = pd.bdate_range(pd.Timestamp(start), END)
    rng = np.random.default_rng(zlib.crc32(series_id.encode()))
    level = np.cumsum(rng.normal(0, 0.01, len(days)))           # percent points
    months = TENOR_MONTHS.get(series_id, 1)
    base = 3.8 + 0.6 * np.log1p(months / 12)
    return [(d.date(), Decimal(f"{base + x:.2f}")) for d, x in zip(days, level)]


@pytest.fixture
def cli_db(db, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db.url.render_as_string(hide_password=False))
    monkeypatch.setenv("FRED_API_KEY", "k" * 32)
    monkeypatch.setattr(yahoo, "fetch_history", synthetic_yahoo)
    monkeypatch.setattr(fred, "fetch_series", synthetic_fred)
    return db


def test_bootstrap_runs_end_to_end(cli_db, capsys):
    assert cli.main(["bootstrap", "--start", "2023-06-01"]) == 0
    out = capsys.readouterr().out
    assert "Ballast Core" in out
    assert "FAIL" not in out
    assert "DV01 ($ gained per 1bp parallel fall)" in out

    with cli_db.connect() as conn:
        nvda = conn.execute(
            text(
                """
                SELECT pd.as_of_date, pd.quantity FROM positions_daily pd
                JOIN instruments i USING (instrument_id)
                WHERE i.symbol = 'NVDA' AND pd.as_of_date IN ('2024-06-07', '2024-06-10', '2024-07-01')
                ORDER BY pd.as_of_date
                """
            )
        ).all()
        pid = conn.execute(text("SELECT portfolio_id FROM portfolios WHERE name = 'Ballast Core'")).scalar_one()
        sql_total = conn.execute(
            text("SELECT SUM(daily_pnl) FROM position_pnl_daily WHERE portfolio_id = :p"), {"p": pid}
        ).scalar_one()

    # 20 shares bought pre-split -> 200 after the 10-for-1 split -> 150 after selling 50.
    assert [int(q) for _, q in nvda] == [20, 200, 150]
    # SQL views and the independent day-by-day simulation agree exactly on the real seed portfolio.
    assert sql_total == sum(r.daily_pnl for r in reference_pnl(cli_db, pid))


def test_check_fails_on_bad_data(cli_db, capsys):
    assert cli.main(["bootstrap", "--start", "2023-06-01"]) == 0
    with cli_db.begin() as conn:
        conn.execute(text("UPDATE trades SET price = price * 2 WHERE trade_ref = 'BC-0001'"))
    assert cli.main(["check"]) == 1
    assert "dq_trade_price_outliers" in capsys.readouterr().out


def test_bonds_are_valued_and_pay_coupons(cli_db, capsys):
    assert cli.main(["bootstrap", "--start", "2023-06-01"]) == 0
    with cli_db.connect() as conn:
        bond = conn.execute(
            text(
                """
                SELECT SUM(p.income) AS coupons, MIN(p.quantity), MAX(p.quantity), COUNT(*)
                FROM position_pnl_daily p JOIN instruments i USING (instrument_id)
                WHERE i.symbol = 'UST-4.25-2034'
                """
            )
        ).one()
        equity_days = conn.execute(
            text("SELECT COUNT(DISTINCT price_date) FROM prices p JOIN instruments i USING (instrument_id) "
                 "WHERE i.symbol = 'SPY' AND price_date >= '2025-03-03'")
        ).scalar_one()
    # 300 units held from 2025-03-03; coupons of 2.125 per unit on 2025-05-15, 2025-11-15 and
    # 2026-05-15 fall inside the synthetic data window (which ends 2026-10-23).
    assert bond.coupons == Decimal("1912.5")
    assert (bond[1], bond[2]) == (300, 300)
    assert bond[3] == equity_days        # bonds valued on the same calendar as equities


def test_bootstrap_without_fred_key_stops_with_clear_message(cli_db, monkeypatch, capsys):
    monkeypatch.delenv("FRED_API_KEY")
    assert cli.main(["bootstrap", "--start", "2023-06-01"]) == 1
    assert "FRED_API_KEY is not set" in capsys.readouterr().out


def test_curve_and_rate_sensitivity_commands(cli_db, capsys):
    assert cli.main(["bootstrap", "--start", "2023-06-01"]) == 0
    capsys.readouterr()
    assert cli.main(["curve"]) == 0
    out = capsys.readouterr().out
    assert "30Y" in out and "discount_factor" in out
    assert cli.main(["rate-sensitivity", "--symbol", "TLT", "--series", "DGS20"]) == 0
    assert "Empirical duration" in capsys.readouterr().out


def test_derivatives_are_marked_and_flow_through_pnl(cli_db, capsys):
    assert cli.main(["bootstrap", "--start", "2023-06-01"]) == 0
    out = capsys.readouterr().out
    assert "Delta-adjusted equity exposure" in out
    assert "SOFR swaps" in out
    with cli_db.connect() as conn:
        k, source = conn.execute(text("SELECT fixed_rate, fixed_rate_source FROM swap_terms")).one()
        swap_trade = conn.execute(
            text("SELECT t.price, p.close FROM trades t JOIN prices p ON p.instrument_id = t.instrument_id "
                 "AND p.price_date = t.trade_date WHERE t.trade_ref = 'BC-0018'")
        ).one()
        option_marks = conn.execute(
            text("SELECT i.symbol, COUNT(*), MIN(p.close) FROM prices p JOIN instruments i USING (instrument_id) "
                 "WHERE i.asset_class = 'option' GROUP BY i.symbol ORDER BY 1")
        ).all()
        swap_value_sign = conn.execute(
            text("SELECT COUNT(*) FILTER (WHERE close < 0), COUNT(*) FILTER (WHERE close > 0) FROM prices p "
                 "JOIN instruments i USING (instrument_id) WHERE i.asset_class = 'swap'")
        ).one()
    assert k is not None and source.startswith("par on 2026-09-01")
    assert swap_trade == (Decimal(0), Decimal(0))        # booked at market: worth exactly zero on day one
    assert [s for s, _, _ in option_marks] == ["NVDA-20270115-C250", "SPY-20261218-P700"]
    assert all(n > 100 and low >= 0 for _, n, low in option_marks)
    assert sum(swap_value_sign) > 0                       # marked after inception, values can be either sign


def test_pay_fixed_swap_offsets_bond_dv01(cli_db, capsys):
    from ballast.fixed_income.analytics import rates_risk_report

    assert cli.main(["bootstrap", "--start", "2023-06-01"]) == 0
    report = rates_risk_report(cli_db, "Ballast Core")
    bond_dv01 = float(report.bonds.dv01_usd.sum())
    swap_dv01 = float(report.swaps.dv01_usd.sum())
    assert bond_dv01 > 0 > swap_dv01
    assert abs(report.total_dv01) < bond_dv01
    gross = bond_dv01 - swap_dv01
    assert report.key_rates.loc["Total"].sum() == pytest.approx(report.total_dv01, abs=1e-5 * gross)


def test_sofr_check_matches_published_averages(db, monkeypatch, capsys):
    """Feed published-style averages computed with the NY Fed rules: every day must match exactly."""
    from ballast.derivatives.sofr import sofr_average

    monkeypatch.setenv("DATABASE_URL", db.url.render_as_string(hide_password=False))
    rng = np.random.default_rng(3)
    days = [d.date() for d in pd.bdate_range("2025-01-02", "2025-06-30")]
    fixings = {d: round(4.30 + float(x), 2) / 100 for d, x in zip(days, np.cumsum(rng.normal(0, 0.01, len(days))))}
    pubs = [d for d in days if (d - days[0]).days >= 31]
    rows = [{"s": "SOFR", "d": d, "v": Decimal(f"{r * 100:.2f}")} for d, r in fixings.items()]
    rows += [{"s": "SOFR30DAYAVG", "d": p, "v": Decimal(f"{sofr_average(fixings, p) * 100:.5f}")} for p in pubs]
    with db.begin() as conn:
        conn.execute(text("INSERT INTO rate_observations (series_id, obs_date, value_pct) VALUES (:s, :d, :v)"), rows)
    assert cli.main(["sofr-check"]) == 0
    out = capsys.readouterr().out
    assert f"Exact match at the published 5 decimals: {len(pubs)} of {len(pubs)} (100.0%)" in out


def test_vol_smile_recovers_known_volatilities(cli_db, monkeypatch, capsys):
    """Synthetic chain priced with a known skew; the smile must recover it, and the forward."""
    import math
    from datetime import datetime, timezone

    from ballast.derivatives import chains
    from ballast.derivatives.black_scholes import black76_price

    assert cli.main(["bootstrap", "--start", "2023-06-01"]) == 0
    capsys.readouterr()

    snapshot = datetime(2026, 10, 23, 18, 0, tzinfo=timezone.utc)
    expiry = date(2026, 11, 27)
    spot, F_true = 600.0, 601.5
    vol = lambda k: 0.18 + 0.25 * (1 - k / F_true)            # downward skew
    T = chains.years_to_expiry(snapshot, expiry)

    def make(kind):
        rows = []
        for k in np.arange(500.0, 701.0, 5.0):
            r = TRUE_RATE[0]
            p = black76_price(kind, F_true, k, T, r, vol(k))
            rows.append({"strike": k, "bid": p * 0.99, "ask": p * 1.01, "lastPrice": p,
                         "volume": 10, "openInterest": 100, "impliedVolatility": vol(k)})
        return pd.DataFrame(rows)

    TRUE_RATE = [0.0]

    def fake_fetch(symbol, expiry_arg, min_days):
        return chains.Chain("SPY", expiry, spot, snapshot, make("call"), make("put"))

    # Price the synthetic chain with exactly the rate the command will use.
    from ballast.fixed_income.store import par_curve_on
    real_par_curve_on = par_curve_on

    def fixed_curve(engine, on):
        found = real_par_curve_on(engine, date(2026, 10, 23))
        TRUE_RATE[0] = float(found[1].build().zero_rate(T))
        return found

    monkeypatch.setattr(cli, "par_curve_on", fixed_curve)
    monkeypatch.setattr(chains, "fetch_chain", fake_fetch)
    fixed_curve(cli_db, None)
    assert cli.main(["vol-smile", "--symbol", "SPY"]) == 0
    out = capsys.readouterr().out
    assert "forward (put-call parity) 601.50" in out
    with cli_db.connect() as conn:
        got = conn.execute(text("SELECT strike, model_iv FROM option_quotes ORDER BY strike")).all()
    assert len(got) == 41
    assert max(abs(iv - vol(float(k))) for k, iv in got) < 1e-6


def test_risk_commands_run_and_store_results(cli_db, capsys):
    assert cli.main(["bootstrap", "--start", "2022-01-03"]) == 0
    out = capsys.readouterr().out
    assert "Value-at-Risk" in out and "diversification benefit" in out
    assert "Stress tests" in out and "rates_2022" in out
    assert cli.main(["backtest"]) == 0
    bt = capsys.readouterr().out
    assert "kupiec_p" in bt and "Largest losses in the backtest" in bt
    with cli_db.connect() as conn:
        methods = conn.execute(text("SELECT method FROM var_results ORDER BY method")).scalars().all()
        n_bt = conn.execute(text("SELECT COUNT(*) FROM var_backtest")).scalar_one()
        years = conn.execute(text("SELECT COUNT(*) FROM var_backtest_by_year")).scalar_one()
    assert methods == ["historical", "monte_carlo", "parametric"]
    assert n_bt > 1000 and years > 0
