"""Implied volatility smile from a listed option chain.

Method (standard for equity index/ETF smiles):
1. Mid price = (bid + ask) / 2; quotes with a missing or crossed market are dropped.
2. The forward is implied from put-call parity at the strikes nearest the money,
   F = K + e^{rT} (C - P), taking the median across those strikes. This avoids
   having to estimate the dividend yield separately.
3. Each strike uses its out-of-the-money option (puts below F, calls above),
   which are the liquid ones, and implied volatility is solved with Black-76.

Listed SPY options are American. For out-of-the-money options the early-exercise
premium is small, so treating them as European is the usual approximation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ballast.derivatives.black_scholes import PricingError, implied_vol_black76

NEW_YORK = ZoneInfo("America/New_York")
PARITY_STRIKES = 5
MAX_RELATIVE_SPREAD = 0.5


@dataclass(frozen=True)
class Chain:
    symbol: str
    expiry: date
    spot: float
    snapshot_at: datetime
    calls: pd.DataFrame        # columns: strike, bid, ask, lastPrice, volume, openInterest, impliedVolatility
    puts: pd.DataFrame


def years_to_expiry(snapshot_at: datetime, expiry: date) -> float:
    """Calendar time to the 4pm New York close on expiry day, in years (ACT/365)."""
    close = datetime(expiry.year, expiry.month, expiry.day, 16, 0, tzinfo=NEW_YORK)
    return (close - snapshot_at).total_seconds() / (365.0 * 86400.0)


def _with_mid(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in ("bid", "ask"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out["mid"] = (out.bid + out.ask) / 2
    ok = (out.bid > 0) & (out.ask >= out.bid) & ((out.ask - out.bid) / out["mid"] <= MAX_RELATIVE_SPREAD)
    return out[ok]


def implied_forward(calls: pd.DataFrame, puts: pd.DataFrame, spot: float, T: float, r: float) -> float:
    both = _with_mid(calls)[["strike", "mid"]].merge(_with_mid(puts)[["strike", "mid"]], on="strike", suffixes=("_c", "_p"))
    if both.empty:
        raise PricingError("no strikes with both a call and a put quote: cannot imply the forward")
    near = both.iloc[(both.strike - spot).abs().argsort()[:PARITY_STRIKES]]
    forwards = near.strike + math.exp(r * T) * (near.mid_c - near.mid_p)
    return float(np.median(forwards))


def smile(chain: Chain, r: float) -> tuple[float, float, pd.DataFrame]:
    """Returns (T, forward, table) with one out-of-the-money quote per strike."""
    T = years_to_expiry(chain.snapshot_at, chain.expiry)
    if T <= 0:
        raise PricingError("expiry has passed")
    F = implied_forward(chain.calls, chain.puts, chain.spot, T, r)
    otm_puts = _with_mid(chain.puts).query("strike < @F").assign(option_type="put")
    otm_calls = _with_mid(chain.calls).query("strike >= @F").assign(option_type="call")
    table = pd.concat([otm_puts, otm_calls]).sort_values("strike")
    ivs = []
    for row in table.itertuples():
        try:
            ivs.append(implied_vol_black76(row.option_type, row.mid, F, row.strike, T, r))
        except (PricingError, ValueError):
            ivs.append(np.nan)
    table = table.assign(model_iv=ivs, moneyness=table.strike / F).dropna(subset=["model_iv"])
    return T, F, table.reset_index(drop=True)


def smile_summary(table: pd.DataFrame) -> dict[str, float]:
    """ATM volatility and skew, interpolated in moneyness (K / F)."""
    m, v = table.moneyness.to_numpy(), table.model_iv.to_numpy()
    out = {}
    for label, k in (("atm", 1.0), ("m90", 0.9), ("m95", 0.95), ("m105", 1.05), ("m110", 1.1)):
        out[label] = float(np.interp(k, m, v)) if m.min() <= k <= m.max() else float("nan")
    out["skew_90_110"] = out["m90"] - out["m110"]
    if "impliedVolatility" in table:
        vendor = pd.to_numeric(table["impliedVolatility"], errors="coerce")
        out["median_abs_diff_vs_vendor"] = float(np.nanmedian(np.abs(table.model_iv - vendor)))
    return out


def fetch_chain(symbol: str, expiry: date | None = None, min_days: int = 20) -> Chain:
    """Download one expiry from Yahoo Finance (imported lazily so tests stay offline)."""
    import yfinance as yf

    t = yf.Ticker(symbol)
    expiries = [date.fromisoformat(e) for e in t.options]
    if not expiries:
        raise PricingError(f"Yahoo returned no option expiries for {symbol}")
    today = datetime.now(NEW_YORK).date()
    if expiry is None:
        candidates = [e for e in expiries if (e - today).days >= min_days]
        if not candidates:
            raise PricingError(f"no expiry at least {min_days} days out")
        expiry = candidates[0]
    elif expiry not in expiries:
        raise PricingError(f"{expiry} is not a listed expiry for {symbol}")
    oc = t.option_chain(expiry.isoformat())
    spot = None
    if isinstance(getattr(oc, "underlying", None), dict):
        spot = oc.underlying.get("regularMarketPrice")
    if spot is None:
        spot = float(t.history(period="5d").Close.iloc[-1])
    return Chain(symbol.upper(), expiry, float(spot), datetime.now(timezone.utc), oc.calls, oc.puts)
