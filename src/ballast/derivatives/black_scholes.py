"""Black-Scholes-Merton (with a continuous dividend yield) and Black-76.

Units used throughout:
* T in years, r and q continuously compounded decimals, sigma a decimal (0.20 = 20%).
* Greeks are per unit of the underlying: vega per 1.00 change in volatility,
  rho per 1.00 change in r, theta per year. Divide vega and rho by 100 for
  "per point", and theta by 365 for "per calendar day".
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from scipy.optimize import brentq
from scipy.stats import norm

CALL, PUT = "call", "put"


class PricingError(ValueError):
    pass


def _check(option_type: str, S: float, K: float, T: float, sigma: float) -> None:
    if option_type not in (CALL, PUT):
        raise PricingError(f"option_type must be 'call' or 'put', got {option_type!r}")
    if S <= 0 or K <= 0:
        raise PricingError("spot and strike must be positive")
    if T <= 0 or sigma <= 0:
        raise PricingError("T and sigma must be positive (use intrinsic value at expiry)")


def _d1_d2(S: float, K: float, T: float, r: float, q: float, sigma: float) -> tuple[float, float]:
    vol_sqrt_t = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / vol_sqrt_t
    return d1, d1 - vol_sqrt_t


def bs_price(option_type: str, S: float, K: float, T: float, r: float, sigma: float, q: float = 0.0) -> float:
    _check(option_type, S, K, T, sigma)
    d1, d2 = _d1_d2(S, K, T, r, q, sigma)
    df_r, df_q = math.exp(-r * T), math.exp(-q * T)
    if option_type == CALL:
        return S * df_q * norm.cdf(d1) - K * df_r * norm.cdf(d2)
    return K * df_r * norm.cdf(-d2) - S * df_q * norm.cdf(-d1)


@dataclass(frozen=True)
class Greeks:
    price: float
    delta: float
    gamma: float
    vega: float     # per 1.00 change in sigma
    theta: float    # per year
    rho: float      # per 1.00 change in r


def bs_greeks(option_type: str, S: float, K: float, T: float, r: float, sigma: float, q: float = 0.0) -> Greeks:
    _check(option_type, S, K, T, sigma)
    d1, d2 = _d1_d2(S, K, T, r, q, sigma)
    df_r, df_q = math.exp(-r * T), math.exp(-q * T)
    pdf_d1 = norm.pdf(d1)
    sqrt_t = math.sqrt(T)
    gamma = df_q * pdf_d1 / (S * sigma * sqrt_t)
    vega = S * df_q * pdf_d1 * sqrt_t
    decay = -S * df_q * pdf_d1 * sigma / (2 * sqrt_t)
    if option_type == CALL:
        price = S * df_q * norm.cdf(d1) - K * df_r * norm.cdf(d2)
        delta = df_q * norm.cdf(d1)
        theta = decay - r * K * df_r * norm.cdf(d2) + q * S * df_q * norm.cdf(d1)
        rho = K * T * df_r * norm.cdf(d2)
    else:
        price = K * df_r * norm.cdf(-d2) - S * df_q * norm.cdf(-d1)
        delta = df_q * (norm.cdf(d1) - 1.0)
        theta = decay + r * K * df_r * norm.cdf(-d2) - q * S * df_q * norm.cdf(-d1)
        rho = -K * T * df_r * norm.cdf(-d2)
    return Greeks(price, delta, gamma, vega, theta, rho)


def black76_price(option_type: str, F: float, K: float, T: float, r: float, sigma: float) -> float:
    """Option on a forward F, discounted at r: e^{-rT} [F N(d1) - K N(d2)] for a call."""
    _check(option_type, F, K, T, sigma)
    vol_sqrt_t = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / vol_sqrt_t
    d2 = d1 - vol_sqrt_t
    df = math.exp(-r * T)
    if option_type == CALL:
        return df * (F * norm.cdf(d1) - K * norm.cdf(d2))
    return df * (K * norm.cdf(-d2) - F * norm.cdf(-d1))


def implied_vol_black76(option_type: str, price: float, F: float, K: float, T: float, r: float) -> float:
    """Volatility that reproduces `price` under Black-76. Raises PricingError if no solution exists."""
    df = math.exp(-r * T)
    intrinsic = df * max((F - K) if option_type == CALL else (K - F), 0.0)
    upper = df * (F if option_type == CALL else K)
    if not (intrinsic < price < upper):
        raise PricingError(f"price {price:.6f} outside no-arbitrage bounds ({intrinsic:.6f}, {upper:.6f})")
    return brentq(lambda s: black76_price(option_type, F, K, T, r, s) - price, 1e-6, 10.0, xtol=1e-12)


def implied_vol_bs(option_type: str, price: float, S: float, K: float, T: float, r: float, q: float = 0.0) -> float:
    """Implied volatility under Black-Scholes-Merton (same as Black-76 with F = S e^{(r-q)T})."""
    return implied_vol_black76(option_type, price, S * math.exp((r - q) * T), K, T, r)
