"""A portfolio frozen at one date, revalued under many scenarios at once.

Each position type is reduced to a fast, exact-enough revaluation:
* Stocks and ETFs: P&L = market value x simple return.
* Bonds and swaps: the value per unit is linear in discount factors at known
  dates (a bond is a sum of cash flows; a SOFR swap's floating leg telescopes to
  realized growth minus the final discount factor). So every scenario needs only
  one vectorized curve bootstrap, followed by a matrix product.
* Options: priced on a grid of 41 spot levels from -35% to +35% (log scale)
  around today's spot, then interpolated with a cubic spline. Volatility,
  rates and dividend yield are held at today's values, and no time passes
  (an instantaneous shock). Moves beyond the grid are clipped to its edge;
  stress tests reprice options directly instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
from scipy.interpolate import CubicSpline

from ballast.derivatives.store import OptionContract, OptionInputs, option_inputs, option_value
from ballast.derivatives.swaps import realized_growth
from ballast.fixed_income.batch import BatchCurveBuilder
from ballast.risk.market import MarketData, RiskDataError

DAYS_PER_YEAR = 365.25
LADDER_HALF_WIDTH = 0.35
LADDER_POINTS = 41
BP = 1e-4


@dataclass(frozen=True)
class RateInstrument:
    symbol: str
    kind: str                 # "bond" or "swap"
    quantity: float
    constant: float           # value per unit not depending on the curve
    times: np.ndarray         # years from the snapshot date
    weights: np.ndarray       # value per unit per unit of DF(time)


@dataclass
class OptionPosition:
    symbol: str
    underlying: str
    quantity: float
    contract: OptionContract
    inputs: OptionInputs
    spline: CubicSpline
    base_value: float

    def pnl(self, underlying_returns: np.ndarray) -> np.ndarray:
        u = np.clip(np.log1p(underlying_returns), -LADDER_HALF_WIDTH, LADDER_HALF_WIDTH)
        return self.quantity * (self.spline(u) - self.base_value)

    def pnl_direct(self, underlying_return: float) -> float:
        i = self.inputs
        shocked = option_value(self.contract, i.spot * (1 + underlying_return), i.T, i.r, i.q, i.sigma)
        return self.quantity * (shocked - option_value(self.contract, i.spot, i.T, i.r, i.q, i.sigma))

    @property
    def dollar_delta(self) -> float:
        """Value change per unit simple return of the underlying (dV/du at u = 0)."""
        return self.quantity * float(self.spline(0.0, 1))


@dataclass
class Snapshot:
    as_of: date
    market_value: float
    equity_symbols: list[str]
    stock_values: np.ndarray            # market value per equity symbol (stocks/ETFs only)
    rates: list[RateInstrument]
    options: list[OptionPosition]
    tenor_months: tuple[int, ...]
    par0: np.ndarray
    builder: BatchCurveBuilder | None
    _df0: np.ndarray | None = None
    unpriced: tuple[str, ...] = ()

    # ---------- revaluation ----------

    def _rate_pnl(self, par_shifts: np.ndarray) -> dict[str, np.ndarray]:
        n = par_shifts.shape[0]
        out = {"bonds": np.zeros(n), "swaps": np.zeros(n)}
        if not self.rates:
            return out
        df = self.builder.discount_factors(self.par0 + par_shifts)
        offset = 0
        for inst in self.rates:
            k = len(inst.times)
            change = (df[:, offset:offset + k] - self._df0[offset:offset + k]) @ inst.weights
            out["bonds" if inst.kind == "bond" else "swaps"] += inst.quantity * change
            offset += k
        return out

    def pnl_by_bucket(self, equity_returns: np.ndarray, par_shifts: np.ndarray) -> dict[str, np.ndarray]:
        """Scenario P&L by asset class. equity_returns: (n, n_symbols); par_shifts: (n, n_tenors) in decimals."""
        equity_returns = np.atleast_2d(equity_returns)
        par_shifts = np.atleast_2d(par_shifts)
        out = {"equity": equity_returns @ self.stock_values}
        out.update(self._rate_pnl(par_shifts))
        opt = np.zeros(equity_returns.shape[0])
        for o in self.options:
            opt += o.pnl(equity_returns[:, self.equity_symbols.index(o.underlying)])
        out["options"] = opt
        return out

    def pnl(self, equity_returns: np.ndarray, par_shifts: np.ndarray) -> np.ndarray:
        return sum(self.pnl_by_bucket(equity_returns, par_shifts).values())

    # ---------- linear sensitivities (for parametric VaR) ----------

    def sensitivities(self) -> np.ndarray:
        """$ change per unit change in each factor: equity returns first, then par yields (per 1.00)."""
        equity = self.stock_values.copy()
        for o in self.options:
            equity[self.equity_symbols.index(o.underlying)] += o.dollar_delta
        m = len(self.tenor_months)
        rates = np.zeros(m)
        if self.rates:
            bumps = np.vstack([np.eye(m) * BP, -np.eye(m) * BP])
            zero_eq = np.zeros((2 * m, len(self.equity_symbols)))
            p = self.pnl_by_bucket(zero_eq, bumps)
            total = p["bonds"] + p["swaps"]
            rates = (total[:m] - total[m:]) / (2 * BP)
        return np.concatenate([equity, rates])


def _rate_instruments(md: MarketData, positions, on: date) -> tuple[list[RateInstrument], list[str]]:
    out, missing = [], []
    for row in positions.itertuples():
        if row.asset_class == "treasury":
            bond = md.bonds.get(row.symbol)
            if bond is None:
                missing.append(row.symbol)
                continue
            flows = bond.cashflows_after(on)
            out.append(
                RateInstrument(row.symbol, "bond", row.quantity, 0.0,
                               np.array([(d - on).days / DAYS_PER_YEAR for d, _ in flows]),
                               np.array([a for _, a in flows]))
            )
        elif row.asset_class == "swap":
            swap = md.swaps.get(row.symbol)
            if swap is None:
                missing.append(row.symbol)
                continue
            rem = [(s, e) for s, e in swap.periods() if e > on]
            times = [(e - on).days / DAYS_PER_YEAR for _, e in rem]
            weights = [100.0 * swap.fixed_rate * (e - s).days / 360.0 for s, e in rem]
            weights[-1] += 100.0                                   # + DF(final payment)
            constant = 0.0
            if rem[0][0] < on:                                      # current period has started
                constant = -100.0 * realized_growth(swap, md.fixings, on)
            else:                                                   # forward-starting: - DF(start)
                times.append((rem[0][0] - on).days / DAYS_PER_YEAR)
                weights.append(-100.0)
            out.append(RateInstrument(row.symbol, "swap", row.quantity, constant, np.array(times), np.array(weights)))
    return out, missing


def build_snapshot(md: MarketData, on: date, ladder_steps: int = 200) -> Snapshot:
    pos = md.positions[md.positions.as_of_date == on]
    if pos.empty:
        raise RiskDataError(f"No positions on {on}")
    par0 = md.par.loc[on].to_numpy(dtype=float)
    if np.isnan(par0).any():
        raise RiskDataError(f"No complete Treasury curve as of {on}")

    stock_values = np.zeros(len(md.equity_symbols))
    for row in pos[pos.asset_class.isin(["equity", "etf"])].itertuples():
        stock_values[md.equity_symbols.index(row.symbol)] += row.market_value

    rates, missing = _rate_instruments(md, pos, on)
    builder, df0 = None, None
    if rates:
        times = np.concatenate([r.times for r in rates])
        builder = BatchCurveBuilder(np.array(md.tenor_months) / 12.0, times)
        df0 = builder.discount_factors(par0)[0]

    options = []
    for row in pos[pos.asset_class == "option"].itertuples():
        c = md.options.get(row.symbol)
        inputs = option_inputs(c, md.underlying(c.underlying_id), md.curves, on) if c else None
        if inputs is None:
            missing.append(row.symbol)
            continue
        u = np.linspace(-LADDER_HALF_WIDTH, LADDER_HALF_WIDTH, LADDER_POINTS)
        values = [option_value(c, inputs.spot * np.exp(x), inputs.T, inputs.r, inputs.q, inputs.sigma, steps=ladder_steps)
                  for x in u]
        spline = CubicSpline(u, values)
        options.append(OptionPosition(row.symbol, c.underlying, row.quantity, c, inputs, spline, float(spline(0.0))))

    return Snapshot(
        as_of=on, market_value=float(pos.market_value.sum()), equity_symbols=md.equity_symbols,
        stock_values=stock_values, rates=rates, options=options, tenor_months=md.tenor_months,
        par0=par0, builder=builder, _df0=df0, unpriced=tuple(missing),
    )
