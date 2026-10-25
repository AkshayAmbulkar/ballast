"""Brinson-Fachler performance attribution with Carino linking.

For one period, segment s, portfolio weight w_s and return r_s, benchmark weight
W_s and return b_s, and total benchmark return B = sum W_s b_s:

  allocation   A_s = (w_s - W_s)(b_s - B)       did we overweight segments that beat the benchmark?
  selection    S_s = W_s (r_s - b_s)            did our holdings beat their segment benchmark?
  interaction  I_s = (w_s - W_s)(r_s - b_s)

Summed over segments, A + S + I = R - B exactly, where R = sum w_s r_s.

Over many periods, returns compound, so daily effects do not add up to the
cumulative active return. Carino (1999) scales each period's effects by
k_t / K, with k_t = [ln(1+R_t) - ln(1+B_t)] / (R_t - B_t) and K the same for
the cumulative returns, which makes the linked effects add up to R - B exactly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd


def brinson_fachler(w, r, W, b) -> dict[str, np.ndarray]:
    w, r, W, b = (np.asarray(x, dtype=float) for x in (w, r, W, b))
    B = float(W @ b)
    return {
        "allocation": (w - W) * (b - B),
        "selection": W * (r - b),
        "interaction": (w - W) * (r - b),
    }


def _carino_k(R: float, B: float) -> float:
    if abs(R - B) < 1e-15:
        return 1.0 / (1.0 + R)
    return (math.log1p(R) - math.log1p(B)) / (R - B)


@dataclass(frozen=True)
class AttributionResult:
    portfolio_return: float
    benchmark_return: float
    effects: pd.DataFrame          # segments x [allocation, selection, interaction, total]


def link(daily: list[dict], segments: list[str]) -> AttributionResult:
    """Carino-link daily Brinson-Fachler effects.

    Each item of `daily`: {"w": [...], "r": [...], "W": [...], "b": [...]} aligned with `segments`.
    """
    R_t = np.array([float(np.dot(d["w"], d["r"])) for d in daily])
    B_t = np.array([float(np.dot(d["W"], d["b"])) for d in daily])
    R = float(np.prod(1 + R_t) - 1)
    B = float(np.prod(1 + B_t) - 1)
    K = _carino_k(R, B)
    total = {k: np.zeros(len(segments)) for k in ("allocation", "selection", "interaction")}
    for d, Rt, Bt in zip(daily, R_t, B_t):
        scale = _carino_k(Rt, Bt) / K
        for k, v in brinson_fachler(d["w"], d["r"], d["W"], d["b"]).items():
            total[k] += scale * v
    df = pd.DataFrame(total, index=segments)
    df["total"] = df.sum(axis=1)
    return AttributionResult(R, B, df)
