"""Idempotent loaders: every write is an upsert, so re-running a load is safe."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import Connection, Engine, text

from ballast.market_data import fred, yahoo

DEFAULT_HISTORY_START = date(2022, 1, 1)
MARKET_PRICED_CLASSES = ("equity", "etf")


def upsert_prices(conn: Connection, instrument_id: int, history: yahoo.AsTradedHistory, source: str) -> int:
    rows = [
        {"iid": instrument_id, "d": r.price_date, "c": r.close, "a": r.adj_close, "v": r.volume, "s": source}
        for r in history.prices.itertuples(index=False)
    ]
    if rows:
        conn.execute(
            text(
                """
                INSERT INTO prices (instrument_id, price_date, close, adj_close, volume, source)
                VALUES (:iid, :d, :c, :a, :v, :s)
                ON CONFLICT (instrument_id, price_date) DO UPDATE
                SET close = EXCLUDED.close, adj_close = EXCLUDED.adj_close,
                    volume = EXCLUDED.volume, source = EXCLUDED.source, loaded_at = now()
                """
            ),
            rows,
        )
    return len(rows)


def upsert_corporate_actions(conn: Connection, instrument_id: int, history: yahoo.AsTradedHistory, source: str) -> int:
    rows = [
        {"iid": instrument_id, "d": r.ex_date, "t": "cash_dividend", "v": r.value, "s": source}
        for r in history.dividends.itertuples(index=False)
    ] + [
        {"iid": instrument_id, "d": r.ex_date, "t": "split", "v": r.value, "s": source}
        for r in history.splits.itertuples(index=False)
    ]
    if rows:
        conn.execute(
            text(
                """
                INSERT INTO corporate_actions (instrument_id, ex_date, action_type, value, source)
                VALUES (:iid, :d, :t, :v, :s)
                ON CONFLICT (instrument_id, ex_date, action_type) DO UPDATE
                SET value = EXCLUDED.value, source = EXCLUDED.source, loaded_at = now()
                """
            ),
            rows,
        )
    return len(rows)


def upsert_rate_observations(conn: Connection, series_id: str, observations: list[tuple[date, Decimal]]) -> int:
    rows = [{"sid": series_id, "d": d, "v": v} for d, v in observations]
    if rows:
        conn.execute(
            text(
                """
                INSERT INTO rate_observations (series_id, obs_date, value_pct)
                VALUES (:sid, :d, :v)
                ON CONFLICT (series_id, obs_date) DO UPDATE
                SET value_pct = EXCLUDED.value_pct, loaded_at = now()
                """
            ),
            rows,
        )
    return len(rows)


def load_prices(
    engine: Engine,
    symbols: list[str] | None = None,
    start: date = DEFAULT_HISTORY_START,
    repair: bool = True,
    fetch=None,
) -> dict[str, int]:
    """Fetch and store prices + corporate actions for market-priced instruments.

    Each symbol is loaded in its own transaction, so one bad ticker does not
    roll back the others. Returns {symbol: number of price rows}.
    `fetch` can be swapped out in tests to avoid network calls.
    """
    fetch = fetch or yahoo.fetch_history
    with engine.connect() as conn:
        instruments = conn.execute(
            text("SELECT instrument_id, symbol FROM instruments WHERE asset_class = ANY(:classes) ORDER BY symbol"),
            {"classes": list(MARKET_PRICED_CLASSES)},
        ).all()

    wanted = {s.upper() for s in symbols} if symbols else None
    result: dict[str, int] = {}
    for instrument_id, symbol in instruments:
        if wanted is not None and symbol not in wanted:
            continue
        history = yahoo.drop_unfinished_session(yahoo.to_as_traded(fetch(symbol, start, repair)))
        with engine.begin() as conn:
            result[symbol] = upsert_prices(conn, instrument_id, history, source="yahoo")
            upsert_corporate_actions(conn, instrument_id, history, source="yahoo")
    return result


def load_rates(
    engine: Engine,
    api_key: str,
    series_ids: list[str] | None = None,
    start: date = DEFAULT_HISTORY_START,
    fetch=None,
) -> dict[str, int]:
    """Fetch and store FRED rate series listed in rate_series."""
    fetch = fetch or fred.fetch_series
    with engine.connect() as conn:
        all_series = [r[0] for r in conn.execute(text("SELECT series_id FROM rate_series ORDER BY series_id"))]

    result: dict[str, int] = {}
    for series_id in all_series:
        if series_ids and series_id not in series_ids:
            continue
        observations = fetch(series_id, api_key, start)
        with engine.begin() as conn:
            result[series_id] = upsert_rate_observations(conn, series_id, observations)
    return result
