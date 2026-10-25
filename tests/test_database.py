"""Schema, constraints, migrations, loaders and seeding."""

from __future__ import annotations

import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ballast.config import MIGRATIONS_DIR
from ballast.db import MigrationError, migrate
from ballast.market_data.load import load_prices, load_rates
from ballast.portfolio.seed import SeedError, seed_instruments, seed_trades
from conftest import D, insert_instrument, insert_portfolio


# ---------- migrations ----------

def test_migrations_are_idempotent(engine):
    assert migrate(engine) == []


def test_edited_migration_is_detected(engine, tmp_path: Path):
    for f in MIGRATIONS_DIR.glob("*.sql"):
        shutil.copy(f, tmp_path / f.name)
    first = sorted(tmp_path.glob("*.sql"))[0]
    first.write_text(first.read_text() + "\n-- sneaky edit\n")
    with pytest.raises(MigrationError, match=first.name):
        migrate(engine, tmp_path)


# ---------- constraints ----------

@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO instruments (symbol, name, asset_class) VALUES ('aapl', 'x', 'equity')",   # lowercase
        "INSERT INTO instruments (symbol, name, asset_class) VALUES ('ZZZ', 'x', 'crypto')",    # bad class
    ],
)
def test_instrument_constraints(db, sql):
    with pytest.raises(IntegrityError):
        with db.begin() as conn:
            conn.execute(text(sql))


@pytest.mark.parametrize("qty, price, fees", [("0", "10", "0"), ("5", "-1", "0"), ("5", "10", "-2")])
def test_trade_constraints(db, qty, price, fees):
    with db.begin() as conn:
        iid = insert_instrument(conn, "AAA")
        pid = insert_portfolio(conn)
    with pytest.raises(IntegrityError):
        with db.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO trades (portfolio_id, instrument_id, trade_ref, trade_date, quantity, price, fees) "
                    "VALUES (:p, :i, 'X', '2024-01-02', :q, :px, :f)"
                ),
                {"p": pid, "i": iid, "q": D(qty), "px": D(price), "f": D(fees)},
            )


def test_duplicate_trade_ref_rejected(db):
    with db.begin() as conn:
        iid = insert_instrument(conn, "AAA")
        pid = insert_portfolio(conn)
        sql = text(
            "INSERT INTO trades (portfolio_id, instrument_id, trade_ref, trade_date, quantity, price) "
            "VALUES (:p, :i, 'DUP', '2024-01-02', 1, 10)"
        )
        conn.execute(sql, {"p": pid, "i": iid})
    with pytest.raises(IntegrityError):
        with db.begin() as conn:
            conn.execute(sql, {"p": pid, "i": iid})


def test_rate_series_reference_data_present(engine):
    with engine.connect() as conn:
        tenors = conn.execute(
            text("SELECT tenor_months FROM rate_series WHERE curve = 'UST_CMT' ORDER BY tenor_months")
        ).scalars().all()
    assert tenors == [1, 3, 6, 12, 24, 36, 60, 84, 120, 240, 360]


# ---------- loaders (network replaced by fakes) ----------

def _fake_yahoo(symbol, start, repair):
    idx = pd.date_range("2024-06-06", periods=4, freq="B", tz="America/New_York")
    return pd.DataFrame(
        {
            "Close": [120.0, 121.0, 122.0, 123.0],
            "Adj Close": [119.0, 120.0, 121.0, 122.0],
            "Volume": [10, 10, 10, 10],
            "Dividends": [0.0, 0.01, 0.0, 0.0],
            "Stock Splits": [0.0, 0.0, 10.0, 0.0],
        },
        index=idx,
    )


def test_load_prices_stores_as_traded_and_is_idempotent(db):
    with db.begin() as conn:
        insert_instrument(conn, "NVDX")
    assert load_prices(db, fetch=_fake_yahoo) == {"NVDX": 4}
    assert load_prices(db, fetch=_fake_yahoo) == {"NVDX": 4}  # re-run: upsert, no duplicates
    with db.connect() as conn:
        closes = conn.execute(text("SELECT close FROM prices ORDER BY price_date")).scalars().all()
        actions = conn.execute(text("SELECT action_type, value FROM corporate_actions ORDER BY ex_date")).all()
        jumps = conn.execute(text("SELECT count(*) FROM dq_unexplained_price_jumps")).scalar()
    assert closes == [D(1200), D(1210), D(122), D(123)]
    assert [(a, v) for a, v in actions] == [("cash_dividend", D("0.1")), ("split", D(10))]
    assert jumps == 0  # the split explains the price drop


def test_missing_split_is_caught_by_data_quality_view(db):
    with db.begin() as conn:
        iid = insert_instrument(conn, "BAD")
        conn.execute(
            text("INSERT INTO prices (instrument_id, price_date, close, source) VALUES "
                 "(:i, '2024-06-07', 1200, 't'), (:i, '2024-06-10', 120, 't')"),
            {"i": iid},
        )
    with db.connect() as conn:
        rows = conn.execute(text("SELECT symbol, price_date FROM dq_unexplained_price_jumps")).all()
    assert [(s, d) for s, d in rows] == [("BAD", date(2024, 6, 10))]


def test_load_rates(db):
    def fake_fred(series_id, api_key, start):
        return [(date(2024, 7, 3), Decimal("4.36")), (date(2024, 7, 5), Decimal("4.28"))]

    counts = load_rates(db, "k" * 32, series_ids=["DGS10", "SOFR"], fetch=fake_fred)
    assert counts == {"DGS10": 2, "SOFR": 2}
    with db.connect() as conn:
        curve = conn.execute(text("SELECT tenor_months, value_pct FROM treasury_cmt_curve ORDER BY obs_date")).all()
    assert [(t, v) for t, v in curve] == [(120, D("4.36")), (120, D("4.28"))]


# ---------- seeding ----------

def test_seed_trades_prices_blank_rows_at_close(db, tmp_path: Path):
    (tmp_path / "instruments.csv").write_text("symbol,name,asset_class\naaa,Alpha Corp,equity\n")
    (tmp_path / "trades.csv").write_text(
        "trade_ref,portfolio,trade_date,symbol,quantity,price,fees,note\n"
        "R1,Demo,2024-01-02,AAA,10,,0,at close\n"
        "R2,Demo,2024-01-03,AAA,-4,11.25,1.5,\n"
    )
    seed_instruments(db, tmp_path / "instruments.csv")
    with db.begin() as conn:
        iid = conn.execute(text("SELECT instrument_id FROM instruments WHERE symbol = 'AAA'")).scalar_one()
        conn.execute(
            text("INSERT INTO prices (instrument_id, price_date, close, source) VALUES "
                 "(:i, '2024-01-02', 10.5, 't'), (:i, '2024-01-03', 11, 't')"),
            {"i": iid},
        )
    assert seed_trades(db, tmp_path / "trades.csv") == 2
    assert seed_trades(db, tmp_path / "trades.csv") == 2  # idempotent on trade_ref
    with db.connect() as conn:
        trades = conn.execute(text("SELECT trade_ref, quantity, price, fees, note FROM trades ORDER BY trade_ref")).all()
    assert [tuple(t) for t in trades] == [
        ("R1", D(10), D("10.5"), D(0), "at close; priced at close"),
        ("R2", D(-4), D("11.25"), D("1.5"), None),
    ]


def test_seed_trades_without_price_data_fails_cleanly(db, tmp_path: Path):
    with db.begin() as conn:
        insert_instrument(conn, "AAA")
    (tmp_path / "trades.csv").write_text(
        "trade_ref,portfolio,trade_date,symbol,quantity,price,fees,note\nR1,Demo,2024-01-06,AAA,10,,0,\n"
    )
    with pytest.raises(SeedError, match="no AAA close on 2024-01-06"):
        seed_trades(db, tmp_path / "trades.csv")
    with db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM portfolios")).scalar() == 0  # rolled back
