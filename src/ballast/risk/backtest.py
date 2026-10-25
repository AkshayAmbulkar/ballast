"""VaR backtesting statistics.

* Kupiec (1995) proportion-of-failures test: are exceptions as frequent as the
  confidence level implies? LR_pof = -2 ln[(1-p)^(n-x) p^x] + 2 ln[(1-x/n)^(n-x) (x/n)^x],
  chi-squared with 1 degree of freedom under the null.
* Christoffersen (1998) independence test: do exceptions cluster, i.e. is an
  exception more likely the day after an exception? Chi-squared with 1 degree of freedom.
  Conditional coverage combines both: LR_cc = LR_pof + LR_ind, 2 degrees of freedom.
* Basel traffic light (Basel Committee, 1996) for 250 days at 99%: green 0-4
  exceptions, yellow 5-9, red 10 or more.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from scipy.stats import chi2


def _xlogy(x: float, y: float) -> float:
    return 0.0 if x == 0 else x * math.log(y)


@dataclass(frozen=True)
class TestResult:
    statistic: float
    p_value: float
    dof: int

    def rejects(self, level: float = 0.05) -> bool:
        return self.p_value < level


def kupiec_pof(exceptions: int, observations: int, p: float) -> TestResult:
    x, n = exceptions, observations
    if not 0 <= x <= n or n == 0:
        raise ValueError("need 0 <= exceptions <= observations and observations > 0")
    phat = x / n
    log_null = _xlogy(n - x, 1 - p) + _xlogy(x, p)
    log_alt = _xlogy(n - x, 1 - phat) + _xlogy(x, phat)
    lr = max(-2.0 * (log_null - log_alt), 0.0)
    return TestResult(lr, float(chi2.sf(lr, 1)), 1)


def christoffersen_independence(hits: list[bool]) -> TestResult:
    n00 = n01 = n10 = n11 = 0
    for a, b in zip(hits[:-1], hits[1:]):
        if not a and not b:
            n00 += 1
        elif not a and b:
            n01 += 1
        elif a and not b:
            n10 += 1
        else:
            n11 += 1
    total = n00 + n01 + n10 + n11
    if total == 0:
        return TestResult(0.0, 1.0, 1)
    pi = (n01 + n11) / total
    pi0 = n01 / (n00 + n01) if n00 + n01 else 0.0
    pi1 = n11 / (n10 + n11) if n10 + n11 else 0.0
    log_null = _xlogy(n00 + n10, 1 - pi) + _xlogy(n01 + n11, pi)
    log_alt = _xlogy(n00, 1 - pi0) + _xlogy(n01, pi0) + _xlogy(n10, 1 - pi1) + _xlogy(n11, pi1)
    lr = max(-2.0 * (log_null - log_alt), 0.0)
    return TestResult(lr, float(chi2.sf(lr, 1)), 1)


def conditional_coverage(exceptions: int, observations: int, p: float, hits: list[bool]) -> TestResult:
    lr = kupiec_pof(exceptions, observations, p).statistic + christoffersen_independence(hits).statistic
    return TestResult(lr, float(chi2.sf(lr, 2)), 2)


def basel_zone(exceptions_in_250_days: int) -> str:
    if exceptions_in_250_days <= 4:
        return "green"
    if exceptions_in_250_days <= 9:
        return "yellow"
    return "red"
