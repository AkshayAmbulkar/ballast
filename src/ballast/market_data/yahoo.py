"""Yahoo Finance prices and corporate actions, converted to as-traded terms.

Yahoo's "Close" is back-adjusted for every split up to today, and so are its
dividend amounts and volumes. "Adj Close" is additionally adjusted for
dividends. Ballast stores *as-traded* values, so this module undoes the split
adjustment: each value on day t is multiplied by the product of all split
ratios with an ex-date after t.

Because Yahoo adjusts for splits up to *today*, history must always be fetched
through the present. Fetching an old window on its own would miss later splits
and leave prices in the wrong units.

While the U.S. market is open, Yahoo's bar for today holds intraday prices, not a
close. drop_unfinished_session removes that bar (after the split conversion, which
needs today's split information) so an intraday price is never stored as a close.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pandas as pd

REQUIRED_COLUMNS = ("Close", "Adj Close", "Volume")
NEW_YORK = ZoneInfo("America/New_York")
SESSION_SETTLED = time(16, 30)   # New York time: the 4:00 pm close has printed and daily bars are final


class YahooError(RuntimeError):
    pass


@dataclass(frozen=True)
class AsTradedHistory:
    prices: pd.DataFrame      # columns: price_date, close, adj_close, volume
    dividends: pd.DataFrame   # columns: ex_date, value  (as-traded cash per share)
    splits: pd.DataFrame      # columns: ex_date, value  (new shares per old share)


def _to_decimal(x: float, places: int) -> Decimal:
    return Decimal(str(round(float(x), places)))


def later_split_factor(split_ratios: pd.Series) -> pd.Series:
    """For each date t, the product of split ratios on dates strictly after t.

    `split_ratios` is indexed by date (sorted ascending) and holds the split
    ratio on split dates and 0 or NaN on all other dates (Yahoo's convention).
    """
    ratio = split_ratios.fillna(0.0)
    ratio = ratio.where(ratio > 0, 1.0)
    inclusive = ratio.iloc[::-1].cumprod().iloc[::-1]           # product over dates >= t
    return inclusive.shift(-1, fill_value=1.0)                   # product over dates  > t


def to_as_traded(raw: pd.DataFrame) -> AsTradedHistory:
    """Convert yfinance `history(auto_adjust=False, actions=True)` output."""
    missing = [c for c in REQUIRED_COLUMNS if c not in raw.columns]
    if missing:
        raise YahooError(f"Yahoo history is missing columns {missing}")

    df = raw.copy()
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)  # keep the exchange's local calendar date
    df.index = idx.normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = df[df["Close"].notna() & (df["Close"] > 0)]

    splits = df["Stock Splits"] if "Stock Splits" in df.columns else pd.Series(0.0, index=df.index)
    dividends = df["Dividends"] if "Dividends" in df.columns else pd.Series(0.0, index=df.index)
    factor = later_split_factor(splits)

    dates = [d.date() for d in df.index]

    prices = pd.DataFrame(
        {
            "price_date": dates,
            "close": [_to_decimal(v, 4) for v in df["Close"] * factor],
            "adj_close": [None if pd.isna(v) or v <= 0 else _to_decimal(v, 6) for v in df["Adj Close"]],
            "volume": [None if pd.isna(v) else int(round(v)) for v in df["Volume"] / factor],
        }
    )

    div_mask = dividends.fillna(0) > 0
    div_df = pd.DataFrame(
        {
            "ex_date": [d for d, m in zip(dates, div_mask) if m],
            "value": [_to_decimal(v, 6) for v in (dividends * factor)[div_mask]],
        }
    )

    split_mask = splits.fillna(0) > 0
    split_df = pd.DataFrame(
        {
            "ex_date": [d for d, m in zip(dates, split_mask) if m],
            "value": [_to_decimal(v, 8) for v in splits[split_mask]],
        }
    )

    return AsTradedHistory(prices=prices, dividends=div_df, splits=split_df)


def drop_unfinished_session(history: AsTradedHistory, now: datetime | None = None) -> AsTradedHistory:
    """Remove today's price row until the New York session has closed (dividends and splits are kept:
    they are known in advance and take effect on their ex-date either way)."""
    now_ny = (now or datetime.now(timezone.utc)).astimezone(NEW_YORK)
    if now_ny.time() >= SESSION_SETTLED:
        return history
    keep = history.prices.price_date < now_ny.date()
    return replace(history, prices=history.prices[keep].reset_index(drop=True))


def fetch_history(symbol: str, start: date, repair: bool = True) -> pd.DataFrame:
    """Download daily history from `start` through today (see module docstring)."""
    import yfinance as yf  # imported lazily so tests never touch the network

    raw = yf.Ticker(symbol).history(
        start=start.isoformat(),
        interval="1d",
        auto_adjust=False,
        actions=True,
        repair=repair,
    )
    if raw is None or raw.empty:
        raise YahooError(f"No Yahoo data returned for {symbol}. Check the ticker symbol.")
    return raw
