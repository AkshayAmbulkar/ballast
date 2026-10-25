"""Conversion of Yahoo's split-adjusted history back to as-traded values."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from ballast.market_data.yahoo import YahooError, later_split_factor, to_as_traded


def _raw(closes, splits=None, dividends=None, tz="America/New_York"):
    """Build a frame shaped like yfinance history(auto_adjust=False, actions=True)."""
    n = len(closes)
    idx = pd.date_range("2024-06-05", periods=n, freq="B", tz=tz)
    return pd.DataFrame(
        {
            "Open": closes,
            "High": closes,
            "Low": closes,
            "Close": closes,
            "Adj Close": [c * 0.99 for c in closes],
            "Volume": [1_000_000] * n,
            "Dividends": dividends or [0.0] * n,
            "Stock Splits": splits or [0.0] * n,
        },
        index=idx,
    )


def test_later_split_factor_single_split():
    s = pd.Series([0.0, 0.0, 10.0, 0.0])
    assert later_split_factor(s).tolist() == [10.0, 10.0, 1.0, 1.0]


def test_later_split_factor_two_splits():
    s = pd.Series([0.0, 2.0, 0.0, 3.0, 0.0])
    assert later_split_factor(s).tolist() == [6.0, 3.0, 3.0, 1.0, 1.0]


def test_ten_for_one_split_restores_as_traded_prices():
    # Yahoo shows pre-split days divided by 10; day 3 is the ex-date.
    raw = _raw([120.789, 121.0, 121.5, 122.0], splits=[0.0, 0.0, 10.0, 0.0])
    h = to_as_traded(raw)
    assert h.prices["close"].tolist() == [Decimal("1207.89"), Decimal("1210"), Decimal("121.5"), Decimal("122")]
    assert h.prices["volume"].tolist() == [100_000, 100_000, 1_000_000, 1_000_000]
    assert h.splits["value"].tolist() == [Decimal("10")]
    assert h.splits["ex_date"].tolist() == [date(2024, 6, 7)]


def test_dividend_before_split_is_unadjusted():
    raw = _raw([50.0, 50.0, 25.0], splits=[0.0, 0.0, 2.0], dividends=[0.0, 0.25, 0.0])
    h = to_as_traded(raw)
    assert h.dividends["value"].tolist() == [Decimal("0.5")]   # 0.25 split-adjusted -> 0.50 as paid
    assert h.dividends["ex_date"].tolist() == [date(2024, 6, 6)]


def test_adj_close_is_kept_as_published():
    h = to_as_traded(_raw([100.0, 101.0]))
    assert h.prices["adj_close"].tolist() == [Decimal("99"), Decimal("99.99")]


def test_timezone_aware_index_keeps_exchange_date():
    h = to_as_traded(_raw([10.0, 11.0], tz="America/New_York"))
    assert h.prices["price_date"].tolist() == [date(2024, 6, 5), date(2024, 6, 6)]


def test_missing_columns_raise():
    with pytest.raises(YahooError):
        to_as_traded(pd.DataFrame({"Close": [1.0]}, index=pd.date_range("2024-01-02", periods=1)))
