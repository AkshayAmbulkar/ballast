"""Black-Litterman expected returns (He and Litterman, 1999).

1. Reverse optimization: the returns that make the reference weights optimal for
   a mean-variance investor with risk aversion delta,  pi = delta S w_ref.
2. Views: P mu = Q + error, error ~ N(0, Omega). Each row of P is a portfolio
   (an absolute view on one asset, or long one asset and short another), and Q
   the return expected for it. Following He and Litterman, Omega is diagonal
   with Omega_kk = (P tau S P')_kk, so each view is as uncertain as the prior.
3. Posterior, in the textbook form
   mu_BL = [(tau S)^-1 + P' Omega^-1 P]^-1 [(tau S)^-1 pi + P' Omega^-1 Q],
   computed with the algebraically identical update form (Woodbury identity)
   mu_BL = pi + tau S P' (P tau S P' + Omega)^-1 (Q - P pi)
   M     = tau S - tau S P' (P tau S P' + Omega)^-1 P tau S   (uncertainty of mu_BL)
   which stays numerically stable as views become certain (Omega -> 0).
   The posterior covariance of returns is S + M.

Checked against He and Litterman's published example in the tests.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


class ViewError(ValueError):
    pass


@dataclass(frozen=True)
class View:
    long: str
    short: str | None
    expected: float          # annual, decimal

    @classmethod
    def parse(cls, text: str) -> "View":
        """'NVDA:0.25' (NVDA returns 25%/yr) or 'NVDA-SPY:0.05' (NVDA beats SPY by 5%/yr)."""
        try:
            assets, value = text.rsplit(":", 1)
            expected = float(value)
        except ValueError:
            raise ViewError(f"cannot read view {text!r}; use SYMBOL:0.10 or LONG-SHORT:0.05") from None
        if "-" in assets:
            long, short = assets.split("-", 1)
            return cls(long.strip().upper(), short.strip().upper(), expected)
        return cls(assets.strip().upper(), None, expected)


def equilibrium_returns(cov, reference_weights, delta: float) -> np.ndarray:
    return delta * np.asarray(cov, dtype=float) @ np.asarray(reference_weights, dtype=float)


def view_matrices(views: list[View], symbols: list[str]) -> tuple[np.ndarray, np.ndarray]:
    P = np.zeros((len(views), len(symbols)))
    for k, v in enumerate(views):
        for s in (v.long, v.short):
            if s is not None and s not in symbols:
                raise ViewError(f"{s} is not in the universe {symbols}")
        if v.long == v.short:
            raise ViewError(f"a relative view needs two different assets, got {v.long}-{v.short}")
        if not np.isfinite(v.expected) or abs(v.expected) > 10:
            raise ViewError(f"view return {v.expected} is outside -1000%..+1000% a year")
        P[k, symbols.index(v.long)] = 1.0
        if v.short:
            P[k, symbols.index(v.short)] = -1.0
    return P, np.array([v.expected for v in views])


@dataclass(frozen=True)
class Posterior:
    mean: np.ndarray
    covariance: np.ndarray       # S + M
    uncertainty: np.ndarray      # M


def posterior(pi, cov, tau: float, P=None, Q=None, omega=None) -> Posterior:
    pi, cov = np.asarray(pi, dtype=float), np.asarray(cov, dtype=float)
    prior = tau * cov
    if P is None or len(P) == 0:
        return Posterior(pi.copy(), cov + prior, prior)
    P, Q = np.atleast_2d(np.asarray(P, dtype=float)), np.asarray(Q, dtype=float)
    view_prior = P @ prior @ P.T
    if omega is None:
        omega = np.diag(np.diag(view_prior))
    gain = prior @ P.T @ np.linalg.inv(view_prior + omega)       # tau S P' (P tau S P' + Omega)^-1
    mean = pi + gain @ (Q - P @ pi)
    M = prior - gain @ P @ prior
    return Posterior(mean, cov + M, M)


def optimal_weights(mean, covariance, delta: float) -> np.ndarray:
    """Unconstrained mean-variance weights (delta S)^-1 mu; they need not sum to 1."""
    return np.linalg.solve(delta * np.asarray(covariance, dtype=float), np.asarray(mean, dtype=float))
