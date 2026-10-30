"""Read-only REST API over Ballast's reports.

Run it with `ballast serve` (http://127.0.0.1:8000, reference docs at /api/docs).
The API never writes to the database. By default it only answers requests addressed
to this computer (127.0.0.1 / localhost), which also blocks DNS-rebinding attacks
from web pages open in your browser.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated, Callable

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine, text
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ballast import __version__, service
from ballast.api import models

LOCAL_HOSTS = ["127.0.0.1", "localhost", "[::1]"]

DESCRIPTION = """
Positions, P&L, fixed income, derivatives, Value-at-Risk, portfolio construction and
performance attribution for the portfolios in a Ballast database.

All endpoints are read-only. Money is in US dollars; returns, volatilities, weights and
rates are decimals (0.05 = 5%) unless a field name ends in `_pct` or `_bp`.
Methodology: https://github.com/AkshayAmbulkar/ballast/tree/main/docs/methodology
"""

TAGS = [
    {"name": "portfolio", "description": "Holdings, P&L and history"},
    {"name": "fixed income", "description": "Treasury curve, duration, DV01, key rates, scenarios"},
    {"name": "derivatives", "description": "Option Greeks and delta-adjusted exposure"},
    {"name": "risk", "description": "Value-at-Risk, backtesting and historical stress tests"},
    {"name": "construction", "description": "Optimization, Black-Litterman and performance attribution"},
    {"name": "validation", "description": "Ballast's numbers against independently published figures"},
    {"name": "service", "description": "Health check"},
]

ERRORS = {
    404: {"model": models.Error, "description": "Unknown portfolio"},
    409: {"model": models.Error, "description": "Data not loaded yet (run `ballast bootstrap`)"},
    422: {"model": models.Error, "description": "Invalid parameters"},
}

Portfolio = Annotated[str, Query(max_length=100)]


def create_app(engine: Engine | Callable[[], Engine] | None = None, site_dir: Path | None = None,
               allowed_hosts: list[str] | None = None) -> FastAPI:
    """engine: an Engine, or a function returning one (default: the configured database).
    site_dir: the dashboard folder to serve at / (skipped if it does not exist).
    allowed_hosts: Host headers to answer (default: this computer only)."""
    app = FastAPI(
        title="Ballast API", version=__version__, description=DESCRIPTION, openapi_tags=TAGS,
        docs_url="/api/docs", redoc_url="/api/redoc", openapi_url="/api/openapi.json",
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts or LOCAL_HOSTS)

    state: dict[str, Engine] = {}

    def db() -> Engine:
        if "engine" not in state:
            if isinstance(engine, Engine):
                state["engine"] = engine
            elif engine is not None:
                state["engine"] = engine()
            else:
                from ballast.db import get_engine
                state["engine"] = get_engine()
        return state["engine"]

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    status_of = {service.NotFound: 404, service.Unavailable: 409, service.InvalidInput: 422}

    @app.exception_handler(service.ServiceError)
    async def service_error(request: Request, exc: service.ServiceError):
        return JSONResponse({"detail": str(exc)}, status_code=status_of.get(type(exc), 400))

    # ---------- service ----------

    @app.get("/api/health", response_model=models.Health, tags=["service"])
    def health():
        try:
            with db().connect() as conn:
                version = conn.execute(text("SELECT MAX(filename) FROM schema_migrations")).scalar()
            return {"status": "ok", "database": "ok", "schema_version": version, "version": __version__}
        except Exception:
            return {"status": "degraded", "database": "unreachable", "schema_version": None, "version": __version__}

    # ---------- portfolio ----------

    @app.get("/api/portfolios", response_model=list[models.PortfolioInfo], tags=["portfolio"])
    def portfolios():
        return service.list_portfolios(db())

    @app.get("/api/portfolios/{portfolio}/summary", response_model=models.Summary, responses=ERRORS,
             tags=["portfolio"], summary="Holdings, P&L and daily history")
    def summary(portfolio: str):
        return service.summary(db(), portfolio)

    # ---------- fixed income ----------

    @app.get("/api/curves/treasury", response_model=models.TreasuryCurve, responses=ERRORS, tags=["fixed income"],
             summary="Bootstrapped Treasury curve (latest, or on a date)")
    def treasury_curve(on: Annotated[date | None, Query(alias="date")] = None):
        return service.treasury_curve(db(), on)

    @app.get("/api/portfolios/{portfolio}/rates-risk", response_model=models.RatesRisk, responses=ERRORS,
             tags=["fixed income"], summary="Bond and swap pricing, duration, DV01, key rates and scenarios")
    def rates_risk(portfolio: str):
        return service.rates_risk(db(), portfolio)

    # ---------- derivatives ----------

    @app.get("/api/portfolios/{portfolio}/greeks", response_model=models.Greeks, responses=ERRORS,
             tags=["derivatives"], summary="Option Greeks and delta-adjusted equity exposure")
    def greeks(portfolio: str):
        return service.greeks(db(), portfolio)

    # ---------- risk ----------

    @app.get("/api/portfolios/{portfolio}/var", response_model=models.ValueAtRisk, responses=ERRORS, tags=["risk"],
             summary="1-day VaR and Expected Shortfall: historical, parametric and Monte Carlo")
    def value_at_risk(
        portfolio: str,
        confidence: Annotated[float, Query(ge=0.5, le=0.999)] = 0.99,
        lookback: Annotated[int, Query(ge=250, le=2500, description="trading days of history")] = 500,
        simulations: Annotated[int, Query(ge=1_000, le=100_000)] = 10_000,
        seed: Annotated[int, Query(ge=0, le=2**31 - 1)] = 7,
    ):
        return service.value_at_risk(db(), portfolio, confidence, lookback, simulations, seed)

    @app.get("/api/portfolios/{portfolio}/backtest", response_model=models.Backtest, responses=ERRORS,
             tags=["risk"], summary="Daily VaR backtest with Kupiec, Christoffersen and Basel tests (slow)")
    def backtest(
        portfolio: str,
        confidence: Annotated[float, Query(ge=0.5, le=0.999)] = 0.99,
        lookback: Annotated[int, Query(ge=250, le=2500)] = 500,
    ):
        return service.backtest(db(), portfolio, confidence, lookback)

    @app.get("/api/portfolios/{portfolio}/stress", response_model=models.Stress, responses=ERRORS, tags=["risk"],
             summary="Historical market episodes replayed on today's portfolio")
    def stress(portfolio: str):
        return service.stress(db(), portfolio)

    # ---------- construction ----------

    @app.get("/api/portfolios/{portfolio}/optimization", response_model=models.Optimization, responses=ERRORS,
             tags=["construction"], summary="Min-variance, max-Sharpe, risk parity, Black-Litterman and frontier")
    def optimization(
        portfolio: str,
        view: Annotated[list[str], Query(max_length=10, description="NVDA:0.25 (NVDA returns 25%/yr) or "
                                                                     "NVDA-SPY:0.05 (beats SPY by 5%/yr); repeatable")] = [],
        lookback: Annotated[int, Query(ge=120, le=5000)] = 756,
        max_weight: Annotated[float, Query(gt=0, le=1)] = 0.35,
        delta: Annotated[float, Query(gt=0, le=20, description="risk aversion")] = 2.5,
        tau: Annotated[float, Query(gt=0, le=1)] = 0.05,
    ):
        if any(len(v) > 40 for v in view):
            raise service.InvalidInput("Each view must be at most 40 characters")
        return service.optimization(db(), portfolio, lookback, max_weight, view, delta, tau)

    @app.get("/api/portfolios/{portfolio}/attribution", response_model=models.Attribution, responses=ERRORS,
             tags=["construction"], summary="Brinson-Fachler attribution against a benchmark, Carino-linked")
    def attribution(
        portfolio: str,
        benchmark: Annotated[str, Query(max_length=200, description="e.g. SPY:0.6,TLT:0.4")] = service.DEFAULT_BENCHMARK,
        since: date | None = None,
    ):
        return service.attribution(db(), portfolio, benchmark, since)

    # ---------- validation ----------

    @app.get("/api/validation", response_model=models.Validation, tags=["validation"],
             summary="SOFR compounding vs the NY Fed; TLT empirical duration")
    def validation():
        return service.validation(db())

    if site_dir is not None and Path(site_dir).is_dir():
        app.mount("/", StaticFiles(directory=site_dir, html=True), name="dashboard")
    return app
