"""End-to-end: `ballast bootstrap` on the real seed files, with synthetic market data
standing in for Yahoo so the test runs offline and deterministically."""

from __future__ import annotations

import zlib

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text

from ballast import cli
from ballast.market_data import yahoo
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


@pytest.fixture
def cli_db(db, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db.url.render_as_string(hide_password=False))
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.setattr(yahoo, "fetch_history", synthetic_yahoo)
    return db


def test_bootstrap_runs_end_to_end(cli_db, capsys):
    assert cli.main(["bootstrap", "--start", "2023-06-01"]) == 0
    out = capsys.readouterr().out
    assert "Ballast Core" in out
    assert "FAIL" not in out
    assert "FRED_API_KEY is not set" in out  # rates skipped gracefully without a key

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
