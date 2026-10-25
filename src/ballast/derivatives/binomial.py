"""Binomial trees for European and American options.

`binomial_price` takes the up and down factors directly, so textbook trees can
be reproduced exactly; `crr_price` uses the Cox-Ross-Rubinstein calibration
u = e^{sigma sqrt(dt)}, d = 1/u. The risk-neutral up probability is
p = (e^{(r-q) dt} - d) / (u - d). American options are checked for early
exercise at every node.
"""

from __future__ import annotations

import math

import numpy as np

from ballast.derivatives.black_scholes import CALL, PUT, PricingError


def binomial_price(
    option_type: str,
    S: float,
    K: float,
    T: float,
    r: float,
    steps: int,
    u: float,
    d: float,
    q: float = 0.0,
    american: bool = False,
) -> float:
    if option_type not in (CALL, PUT):
        raise PricingError(f"option_type must be 'call' or 'put', got {option_type!r}")
    if steps < 1 or T <= 0:
        raise PricingError("need at least one step and T > 0")
    dt = T / steps
    growth = math.exp((r - q) * dt)
    p = (growth - d) / (u - d)
    if not 0.0 < p < 1.0:
        raise PricingError(f"risk-neutral probability {p:.4f} outside (0, 1): tree is not arbitrage-free")
    discount = math.exp(-r * dt)
    sign = 1.0 if option_type == CALL else -1.0

    j = np.arange(steps + 1)
    spot = S * u ** (steps - j) * d ** j            # node 0 = all up moves
    value = np.maximum(sign * (spot - K), 0.0)
    for n in range(steps - 1, -1, -1):
        value = discount * (p * value[:-1] + (1.0 - p) * value[1:])
        if american:
            spot = S * u ** (n - np.arange(n + 1)) * d ** np.arange(n + 1)
            value = np.maximum(value, sign * (spot - K))
    return float(value[0])


def crr_price(
    option_type: str,
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    steps: int = 500,
    q: float = 0.0,
    american: bool = False,
) -> float:
    if sigma <= 0:
        raise PricingError("sigma must be positive")
    u = math.exp(sigma * math.sqrt(T / steps))
    return binomial_price(option_type, S, K, T, r, steps, u, 1.0 / u, q=q, american=american)
