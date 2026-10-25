"""Test fixtures.

Database tests run against a real PostgreSQL database named by TEST_DATABASE_URL
(default: ballast_test on localhost). The database is created if it does not
exist, and its schema is rebuilt from the migrations at the start of each run.
Tests are skipped, not failed, when no PostgreSQL server is reachable.

Safety: because the test database is wiped, the suite refuses to run unless the
database name ends in "_test". Each test also runs with an empty, temporary
config folder, so tests never read your real secrets or call real APIs.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.exc import OperationalError

from ballast.config import get_settings
from ballast.db import migrate

TABLES = "trades, portfolios, corporate_actions, prices, rate_observations, zero_curves, bond_terms, instruments"


def is_safe_test_database(url: str) -> bool:
    name = make_url(url).database or ""
    return name.endswith("_test")


@pytest.fixture(autouse=True)
def isolated_config(tmp_path_factory, monkeypatch):
    """Point Ballast at an empty config folder and drop any real API key for every test."""
    monkeypatch.setenv("BALLAST_CONFIG_DIR", str(tmp_path_factory.mktemp("ballast-config")))
    monkeypatch.delenv("FRED_API_KEY", raising=False)


def _ensure_database(url: str) -> None:
    target = make_url(url)
    admin = create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": target.database}).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{target.database}"'))
    admin.dispose()


@pytest.fixture(scope="session")
def engine():
    url = get_settings().test_database_url
    if not is_safe_test_database(url):
        pytest.exit(
            "Refusing to run: the test suite wipes its database, so TEST_DATABASE_URL must name "
            f"a database ending in '_test' (got '{make_url(url).database}').",
            returncode=2,
        )
    try:
        _ensure_database(url)
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL not reachable for tests: {exc.orig}")
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    migrate(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine):
    """Empty tables before each test (reference data in rate_series is kept)."""
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    return engine


def D(x) -> Decimal:
    return Decimal(str(x))


def insert_instrument(conn, symbol: str, asset_class: str = "equity") -> int:
    return conn.execute(
        text("INSERT INTO instruments (symbol, name, asset_class) VALUES (:s, :s, :a) RETURNING instrument_id"),
        {"s": symbol, "a": asset_class},
    ).scalar_one()


def insert_prices(conn, instrument_id: int, closes: dict[date, str]) -> None:
    conn.execute(
        text("INSERT INTO prices (instrument_id, price_date, close, source) VALUES (:i, :d, :c, 'test')"),
        [{"i": instrument_id, "d": d, "c": D(c)} for d, c in closes.items()],
    )


def insert_action(conn, instrument_id: int, ex_date: date, action_type: str, value: str) -> None:
    conn.execute(
        text(
            "INSERT INTO corporate_actions (instrument_id, ex_date, action_type, value, source) "
            "VALUES (:i, :d, :t, :v, 'test')"
        ),
        {"i": instrument_id, "d": ex_date, "t": action_type, "v": D(value)},
    )


def insert_portfolio(conn, name: str = "Test") -> int:
    return conn.execute(
        text("INSERT INTO portfolios (name) VALUES (:n) RETURNING portfolio_id"), {"n": name}
    ).scalar_one()


def insert_trade(conn, portfolio_id, instrument_id, ref, trade_date, qty, price, fees="0") -> None:
    conn.execute(
        text(
            "INSERT INTO trades (portfolio_id, instrument_id, trade_ref, trade_date, quantity, price, fees) "
            "VALUES (:p, :i, :r, :d, :q, :px, :f)"
        ),
        {"p": portfolio_id, "i": instrument_id, "r": ref, "d": trade_date, "q": D(qty), "px": D(price), "f": D(fees)},
    )
