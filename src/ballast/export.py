"""`ballast export-site`: write the dashboard's data files.

The dashboard in site/ is a static web page (no server), so it can be published
for free on GitHub Pages. It reads two files this command writes:

* site/data/snapshot.json: every report for one portfolio (service.snapshot)
* site/data/openapi.json: the REST API reference, shown on the dashboard's API tab

Because whatever is in site/ becomes public, the export refuses to run when:
* the portfolio's trades differ from the public seed ledger (data/seed/trades.csv),
  so a portfolio of real trades loaded from data/private/ is never published by accident;
* the output contains anything that looks private: a path on this computer, an
  email address, the database URL or the FRED API key.
"""

from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Engine, text

from ballast import service
from ballast.config import PROJECT_ROOT, SEED_DIR, get_settings

SITE_DIR = PROJECT_ROOT / "site"
SIGNIFICANT_DIGITS = 10


class ExportRefused(RuntimeError):
    pass


def _seed_ledger(path: Path, portfolio: str) -> set[tuple]:
    with path.open(newline="", encoding="utf-8") as f:
        return {
            (r["trade_ref"].strip(), r["symbol"].strip().upper(), date.fromisoformat(r["trade_date"].strip()),
             Decimal(r["quantity"].strip()))
            for r in csv.DictReader(f) if r["portfolio"].strip() == portfolio
        }


def check_public_portfolio(engine: Engine, portfolio: str, seed_trades: Path = SEED_DIR / "trades.csv") -> None:
    """Raise ExportRefused unless the portfolio holds exactly the public seed trades."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT t.trade_ref, i.symbol, t.trade_date, t.quantity
                FROM trades t JOIN portfolios p USING (portfolio_id) JOIN instruments i USING (instrument_id)
                WHERE p.name = :n
                """
            ),
            {"n": portfolio},
        ).all()
    loaded = {(ref, sym, d, Decimal(q).normalize()) for ref, sym, d, q in rows}
    public = {(ref, sym, d, q.normalize()) for ref, sym, d, q in _seed_ledger(seed_trades, portfolio)}
    if not loaded:
        raise ExportRefused(f"Portfolio {portfolio!r} has no trades. Run `ballast bootstrap` first.")
    if loaded != public:
        extra = sorted(r[0] for r in loaded - public)
        raise ExportRefused(
            f"Portfolio {portfolio!r} does not match the public seed ledger {seed_trades.name} "
            f"(differing trades: {', '.join(extra[:5]) or 'some seed trades are missing'}). "
            "The dashboard is published openly, so only the public sample portfolio can be exported. "
            "Use --allow-private only if you have checked that these trades may be made public."
        )


def private_markers() -> list[tuple[str, str]]:
    """(label, literal text) pairs that must never appear in published files."""
    s = get_settings()
    markers = [("path on this computer", str(PROJECT_ROOT)), ("home folder", str(Path.home()))]
    if s.database_url:
        markers.append(("database URL", s.database_url))
    if s.fred_api_key:
        markers.append(("FRED API key", s.fred_api_key))
    return [(label, m) for label, m in markers if m and len(m) >= 6]


EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
USER_PATH = re.compile(r"(/Users/|/home/|[A-Za-z]:\\\\Users\\\\)[^\"\s/\\\\]+")


def find_private(text_: str) -> list[str]:
    found = [label for label, m in private_markers() if m in text_]
    if EMAIL.search(text_):
        found.append("email address")
    if USER_PATH.search(text_):
        found.append("user folder path")
    return found


def compact(obj, digits: int = SIGNIFICANT_DIGITS):
    """Round floats to `digits` significant digits (smaller files; far below display precision)."""
    if isinstance(obj, dict):
        return {k: compact(v, digits) for k, v in obj.items()}
    if isinstance(obj, list):
        return [compact(v, digits) for v in obj]
    if isinstance(obj, float) and math.isfinite(obj) and obj != 0.0:
        return float(f"{obj:.{digits}g}")
    return obj


@dataclass(frozen=True)
class ExportResult:
    snapshot_path: Path
    openapi_path: Path
    snapshot_bytes: int
    as_of: str


def export_site(engine: Engine, portfolio: str = service.DEFAULT_PORTFOLIO, out_dir: Path | None = None,
                benchmark: str = service.DEFAULT_BENCHMARK, allow_private: bool = False) -> ExportResult:
    out_dir = out_dir or SITE_DIR / "data"
    if not allow_private:
        check_public_portfolio(engine, portfolio)
    from ballast.api.app import create_app      # FastAPI is only needed here, not for the rest of the CLI

    snap = compact(service.snapshot(engine, portfolio, benchmark))
    files = {
        "snapshot.json": json.dumps(snap, separators=(",", ":"), allow_nan=False),
        "openapi.json": json.dumps(create_app(engine).openapi(), indent=1, allow_nan=False),
    }
    for name, body in files.items():
        leaks = find_private(body)
        if leaks:
            raise ExportRefused(f"Not writing {name}: it would contain {', '.join(sorted(set(leaks)))}.")
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (out_dir / name).write_text(body + "\n", encoding="utf-8")
    return ExportResult(out_dir / "snapshot.json", out_dir / "openapi.json", len(files["snapshot.json"]),
                        snap["summary"]["as_of"])
