"""End-to-end: `ballast bootstrap` on the real seed files, with synthetic market data
standing in for Yahoo and FRED so the test runs offline and deterministically."""

from __future__ import annotations

import zlib
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text

from ballast import cli
from ballast.market_data import fred, yahoo
from ballast.portfolio.pnl import reference_pnl

NVDA_SPLIT_DATE = pd.Timestamp("2024-06-10", tz="America/New_York")


def synthetic_yahoo(symbol: str, start, repair):
    """Business-day random walk. Like real Yahoo data, the NVDA series is continuous
    (already split-adjusted) and the split appears only in the Stock Splits column."""
    idx = pd.bdate_range(pd.Timestamp(start), "2025-10-24", tz="America/New_York")
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
    days = pd.bdate_range(pd.Timestamp(start), "2025-10-24")
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
    assert "Portfolio bond DV01" in out

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
    # 300 units held from 2025-03-03; coupons of 2.125 per unit paid 2025-05-15 and 2025-11-15
    # (the synthetic data ends 2025-10-24, so only May falls inside the window).
    assert bond.coupons == Decimal("637.5")
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
