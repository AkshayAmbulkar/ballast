"""Value-at-Risk and Expected Shortfall: historical, parametric and Monte Carlo.

All figures are 1-day, reported as positive losses in dollars.

Definitions for a set of n scenario P&Ls at confidence a (e.g. 0.99):
* k = floor(n (1 - a)), the number of scenarios allowed to lose more than VaR.
* VaR = the (k+1)-th worst loss, so exactly k scenarios lose more
  (500 scenarios at 99%: VaR is the 6th-worst loss).
* ES  = the average of the k worst losses, the losses beyond VaR.

Parametric (variance-covariance, delta-normal): portfolio P&L ~ N(0, s' S s),
with s the dollar sensitivities to each factor and S the factor covariance:
VaR = z_a sigma and ES = sigma phi(z_a) / (1 - a).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.stats import norm

EWMA_LAMBDA = 0.94      # RiskMetrics daily decay


@dataclass(frozen=True)
class TailRisk:
    var: float
    es: float
    scenarios: int


def tail_risk(pnl, confidence: float) -> TailRisk:
    x = np.sort(np.asarray(pnl, dtype=float))
    n = len(x)
    if n == 0:
        raise ValueError("no scenarios")
    k = int(np.floor(n * (1.0 - confidence) + 1e-9))
    var = -x[min(k, n - 1)]
    es = -x[:k].mean() if k > 0 else var
    return TailRisk(float(var), float(es), n)


def ewma_covariance(X, lam: float = EWMA_LAMBDA) -> np.ndarray:
    """Zero-mean exponentially weighted covariance (RiskMetrics): S_t = lam S_{t-1} + (1 - lam) x_t x_t'.

    Equivalent closed form used here: weights (1 - lam) lam^(n-1-t), normalised to sum to 1.
    """
    X = np.asarray(X, dtype=float)
    n = X.shape[0]
    w = (1.0 - lam) * lam ** np.arange(n - 1, -1, -1)
    w /= w.sum()
    return (X * w[:, None]).T @ X


def parametric(sensitivities, covariance, confidence: float) -> tuple[TailRisk, float]:
    s = np.asarray(sensitivities, dtype=float)
    sigma = float(np.sqrt(max(s @ covariance @ s, 0.0)))
    z = norm.ppf(confidence)
    return TailRisk(z * sigma, sigma * norm.pdf(z) / (1.0 - confidence), 0), sigma


def _factor_root(covariance) -> np.ndarray:
    """A matrix L with L L' = covariance, robust to tiny negative eigenvalues."""
    vals, vecs = np.linalg.eigh((covariance + covariance.T) / 2.0)
    return vecs * np.sqrt(np.clip(vals, 0.0, None))


def monte_carlo_factors(covariance, n: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    L = _factor_root(np.asarray(covariance, dtype=float))
    return rng.standard_normal((n, L.shape[0])) @ L.T
