"""Response schemas for the REST API. They document every field in the OpenAPI reference
and check that each response has the shape the dashboard expects.

Units: money in US dollars; returns, volatilities, weights and rates as decimals
(0.05 = 5%) unless a field name ends in _pct (percent) or _bp (basis points).
"""

from __future__ import annotations

from datetime import date as Date

from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Error(Model):
    detail: str


class Health(Model):
    status: str = Field(description="ok, or degraded when the database cannot be reached")
    database: str
    schema_version: str | None = Field(description="latest applied migration")
    version: str


class PortfolioInfo(Model):
    name: str
    as_of: Date | None
    market_value: float | None


# ---------- summary ----------

class Holding(Model):
    symbol: str
    name: str
    asset_class: str
    segment: str | None = Field(description="attribution segment, when set explicitly")
    quantity: float = Field(description="shares; $100-face units for bonds; shares covered for options; "
                                        "notional/100 for swaps (negative = pay fixed)")
    price: float
    market_value: float
    weight: float
    day_pnl: float | None
    total_pnl: float = Field(description="lifetime P&L including income, net of fees")
    total_income: float = Field(description="dividends, coupons and swap payments received")


class AssetClassWeight(Model):
    asset_class: str
    market_value: float
    weight: float


class HistoryPoint(Model):
    date: Date
    market_value: float
    daily_pnl: float
    cumulative_pnl: float


class Summary(Model):
    portfolio: str
    as_of: Date
    market_value: float
    day_pnl: float | None
    lifetime_pnl: float | None
    lifetime_income: float | None
    inception: Date
    holdings: list[Holding]
    by_asset_class: list[AssetClassWeight]
    history: list[HistoryPoint]


# ---------- fixed income ----------

class CurvePoint(Model):
    tenor: str
    par_pct: float
    zero_bey_pct: float = Field(description="zero rate, semiannual bond-equivalent")
    zero_cc_pct: float = Field(description="zero rate, continuously compounded")
    discount_factor: float
    fwd_6m_bey_pct: float = Field(description="6-month forward rate starting at the tenor")


class TreasuryCurve(Model):
    date: Date
    points: list[CurvePoint]


class BondRisk(Model):
    symbol: str
    face: float
    clean: float
    accrued: float
    dirty: float
    ytm_pct: float
    market_value: float
    mod_duration: float
    eff_duration: float
    convexity: float
    dv01_usd: float = Field(description="$ gained per 1bp parallel fall in par yields")


class SwapRisk(Model):
    symbol: str
    notional: float
    direction: str
    fixed_pct: float
    par_today_pct: float
    pv_per_100: float
    market_value: float
    dv01_usd: float


class KeyRatePosition(Model):
    symbol: str
    dv01: list[float]


class KeyRates(Model):
    tenors: list[str]
    positions: list[KeyRatePosition]
    total: list[float]


class RateScenario(Model):
    scenario: str
    description: str
    full_reval_pnl: float
    key_rate_estimate: float
    convexity_effect: float


class RatesRisk(Model):
    as_of: Date | None
    curve_date: Date | None
    bonds: list[BondRisk]
    swaps: list[SwapRisk]
    key_rates: KeyRates | None
    scenarios: list[RateScenario]
    bond_dv01: float
    swap_dv01: float
    total_dv01: float


# ---------- derivatives ----------

class OptionRisk(Model):
    symbol: str
    underlying: str
    quantity: float
    spot: float
    vol_pct: float
    days: int
    price: float
    market_value: float
    delta: float
    delta_shares: float
    gamma_shares: float
    vega_usd: float = Field(description="$ per 1 volatility point")
    theta_usd_day: float
    rho_usd: float = Field(description="$ per 1% change in rates")


class Exposure(Model):
    underlying: str
    spot: float
    stock_shares: float
    option_delta_shares: float
    net_delta_shares: float
    stock_exposure_usd: float
    net_exposure_usd: float


class Greeks(Model):
    as_of: Date | None
    options: list[OptionRisk]
    exposure: list[Exposure]
    total_vega: float
    total_theta: float


# ---------- risk ----------

class TailMeasure(Model):
    method: str
    var: float = Field(description="1-day Value-at-Risk, a positive loss in $")
    es: float = Field(description="1-day Expected Shortfall, a positive loss in $")


class StandaloneRisk(Model):
    name: str
    var: float
    es: float


class ValueAtRisk(Model):
    as_of: Date
    confidence: float
    market_value: float
    lookback_days: int
    simulations: int
    methods: list[TailMeasure]
    standalone: list[StandaloneRisk]
    sum_of_standalone: float
    diversification_benefit: float
    unpriced: list[str]


class BacktestDay(Model):
    date: Date
    pnl: float
    var_historical: float
    var_parametric: float


class BacktestTest(Model):
    method: str
    exceptions: int
    observations: int
    expected: float
    rate: float
    kupiec_p: float | None
    independence_p: float | None
    conditional_coverage_p: float | None
    last250: int | None
    basel_zone: str | None


class Backtest(Model):
    confidence: float
    start: Date
    end: Date
    days: list[BacktestDay]
    tests: list[BacktestTest]


class StressEpisode(Model):
    name: str
    description: str
    start: Date
    end: Date
    spy_return: float | None
    ust10y_change_bp: float | None
    pnl: dict[str, float | None] = Field(description="$ P&L by asset class, plus total")


class Stress(Model):
    as_of: Date
    episodes: list[StressEpisode]


# ---------- construction and attribution ----------

class Asset(Model):
    symbol: str
    weight_now: float
    hist_return: float
    volatility: float
    implied_return: float = Field(description="total return that makes today's weights optimal")
    bl_return: float = Field(description="Black-Litterman posterior total return")
    risk_share: float = Field(description="share of today's portfolio variance")


class OptimizedPortfolio(Model):
    name: str
    expected_return: float
    volatility: float
    sharpe: float | None
    weights: dict[str, float]


class FrontierPoint(Model):
    expected_return: float
    volatility: float
    sharpe: float | None


class Optimization(Model):
    as_of: Date
    risk_free: float
    lookback_days: int
    returns_from: Date
    max_weight: float
    delta: float
    tau: float
    views: list[str]
    symbols: list[str]
    assets: list[Asset]
    covariance: list[list[float]] = Field(description="annualized, in the order of symbols")
    portfolios: list[OptimizedPortfolio]
    frontier: list[FrontierPoint]


class PeriodStats(Model):
    total_return: float
    annualized: float
    volatility: float


class Effect(Model):
    segment: str
    allocation: float
    selection: float
    interaction: float
    total: float


class EffectTotals(Model):
    allocation: float
    selection: float
    interaction: float
    total: float


class YearAttribution(Model):
    year: int
    portfolio_return: float
    benchmark_return: float
    allocation: float
    selection: float
    interaction: float


class GrowthPoint(Model):
    date: Date
    portfolio: float = Field(description="growth of $1")
    benchmark: float


class Attribution(Model):
    benchmark: str
    start: Date
    end: Date
    days: int
    portfolio: PeriodStats
    benchmark_stats: PeriodStats
    active_return: float
    active_annualized: float
    tracking_error: float
    information_ratio: float | None
    effects: list[Effect]
    totals: EffectTotals
    by_year: list[YearAttribution]
    growth: list[GrowthPoint]


# ---------- validation ----------

class SofrCheck(Model):
    days: int
    start: Date
    end: Date
    exact_matches: int
    largest_diff_bp: float


class DurationCheck(Model):
    symbol: str
    series: str
    start: Date
    end: Date
    observations: int
    duration: float
    r_squared: float | None


class Validation(Model):
    sofr_30day_average: SofrCheck | None
    tlt_empirical_duration: DurationCheck | None
