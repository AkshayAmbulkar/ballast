"""REST API, dashboard export and their safety rules, on one bootstrapped database
(the real seed portfolio with the synthetic market data from test_cli)."""

from __future__ import annotations

import json
from decimal import Decimal

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from ballast import cli, service
from ballast.api import models
from ballast.api.app import create_app
from ballast.export import ExportRefused, check_public_portfolio, compact, export_site, find_private
from ballast.market_data import fred, yahoo
from test_cli import synthetic_fred, synthetic_yahoo

P = "/api/portfolios/Ballast%20Core"


@pytest.fixture(scope="module")
def loaded(engine, tmp_path_factory):
    """Bootstrap once for the whole module (about 30 seconds)."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("BALLAST_CONFIG_DIR", str(tmp_path_factory.mktemp("config")))
        mp.setenv("DATABASE_URL", engine.url.render_as_string(hide_password=False))
        mp.setenv("FRED_API_KEY", "k" * 32)
        mp.setattr(yahoo, "fetch_history", synthetic_yahoo)
        mp.setattr(fred, "fetch_series", synthetic_fred)
        from conftest import TABLES
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
        assert cli.main(["bootstrap", "--start", "2023-06-01"]) == 0
    return engine


@pytest.fixture(scope="module")
def client(loaded):
    return TestClient(create_app(loaded, allowed_hosts=["testserver"]))


def test_health_and_portfolio_list(client):
    h = client.get("/api/health").json()
    assert h["status"] == "ok" and h["schema_version"] == "009_construction.sql"
    assert [p["name"] for p in client.get("/api/portfolios").json()] == ["Ballast Core"]


def test_summary_agrees_with_the_database(client, loaded):
    s = client.get(f"{P}/summary").json()
    with loaded.connect() as conn:
        mv, cum = conn.execute(text(
            "SELECT market_value, cumulative_pnl FROM portfolio_pnl_daily ORDER BY as_of_date DESC LIMIT 1")).one()
    assert s["market_value"] == pytest.approx(float(mv), abs=1e-6)
    assert s["lifetime_pnl"] == pytest.approx(float(cum), abs=1e-6)
    assert s["history"][-1]["cumulative_pnl"] == pytest.approx(s["lifetime_pnl"])
    assert sum(h["weight"] for h in s["holdings"]) == pytest.approx(1.0)
    assert sum(h["market_value"] for h in s["holdings"]) == pytest.approx(s["market_value"])
    assert sum(c["weight"] for c in s["by_asset_class"]) == pytest.approx(1.0)
    assert {h["symbol"] for h in s["holdings"]} >= {"SPY", "TLT", "UST-4.25-2034", "SOFR-OIS-10Y-20260901"}


def test_every_report_endpoint_answers(client):
    for path in ("/api/curves/treasury", f"{P}/rates-risk", f"{P}/greeks", f"{P}/var", f"{P}/stress",
                 f"{P}/optimization", f"{P}/attribution", "/api/validation"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.text)
    rr = client.get(f"{P}/rates-risk").json()
    assert rr["total_dv01"] == pytest.approx(rr["bond_dv01"] + rr["swap_dv01"])
    assert sum(rr["key_rates"]["total"]) == pytest.approx(rr["total_dv01"], rel=0.05)
    assert len(client.get(f"{P}/greeks").json()["options"]) == 2


def test_risk_numbers_are_consistent(client):
    v99 = client.get(f"{P}/var").json()
    v95 = client.get(f"{P}/var", params={"confidence": 0.95, "simulations": 2000}).json()
    by = lambda v: {m["method"]: m for m in v["methods"]}
    for m in ("historical", "parametric", "monte_carlo"):
        assert by(v99)[m]["es"] >= by(v99)[m]["var"] > by(v95)[m]["var"] > 0
    assert v99["diversification_benefit"] == pytest.approx(v99["sum_of_standalone"] - by(v99)["historical"]["var"])
    bt = client.get(f"{P}/backtest").json()
    for t in bt["tests"]:
        hits = sum(d["pnl"] < -d[f"var_{t['method']}"] for d in bt["days"])
        assert t["exceptions"] == hits and t["observations"] == len(bt["days"])


def test_construction_endpoints(client):
    o = client.get(f"{P}/optimization").json()
    port = {p["name"]: p for p in o["portfolios"]}
    for p in port.values():
        assert sum(p["weights"].values()) == pytest.approx(1.0, abs=1e-6)
    # With no views, Black-Litterman returns are the implied returns that make today's
    # weights optimal, so max Sharpe returns today's weights wherever the cap allows.
    cur, best = np.array(list(port["current"]["weights"].values())), np.array(list(port["max_sharpe"]["weights"].values()))
    if cur.max() <= o["max_weight"]:
        assert best == pytest.approx(cur, abs=1e-4)
    assert len(o["covariance"]) == len(o["symbols"]) == len(o["assets"])
    with_view = client.get(f"{P}/optimization", params={"view": ["NVDA-SPY:0.05", "GLD:0.06"]}).json()
    assert with_view["views"] == ["NVDA-SPY:0.05", "GLD:0.06"]
    a = client.get(f"{P}/attribution").json()
    assert a["totals"]["total"] == pytest.approx(a["active_return"], abs=1e-12)
    assert a["growth"][-1]["portfolio"] - 1 == pytest.approx(a["portfolio"]["total_return"])


@pytest.mark.parametrize("path, params, status", [
    ("/api/portfolios/Nope/summary", {}, 404),
    (f"{P}/optimization", {"view": "NVDA:abc"}, 422),
    (f"{P}/optimization", {"view": "ZZZZ:0.1"}, 422),
    (f"{P}/optimization", {"view": "NVDA-NVDA:0.05"}, 422),
    (f"{P}/optimization", {"view": "NVDA:nan"}, 422),
    (f"{P}/optimization", {"view": ["NVDA:0.1"] * 11}, 422),
    (f"{P}/optimization", {"max_weight": 0.05}, 422),
    (f"{P}/attribution", {"benchmark": "SPY:0.5"}, 422),
    (f"{P}/attribution", {"benchmark": "SPY"}, 422),
    (f"{P}/var", {"confidence": 1.5}, 422),
    (f"{P}/var", {"simulations": 10_000_000}, 422),
])
def test_bad_requests_get_clear_errors(client, path, params, status):
    r = client.get(path, params=params)
    assert r.status_code == status
    assert "detail" in r.json() and "Traceback" not in r.text


def test_api_only_reads(client, loaded):
    routes = [r for r in client.app.routes if getattr(r, "methods", None)]
    assert all(r.methods <= {"GET", "HEAD"} for r in routes)
    count = lambda: [loaded.connect().execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
                     for t in ("trades", "optimization_runs", "var_results", "attribution_daily", "prices")]
    before = count()
    client.get(f"{P}/optimization")
    client.get(f"{P}/var", params={"simulations": 1000})
    client.get(f"{P}/attribution")
    assert count() == before


def test_rejects_requests_for_other_hosts_and_sets_security_headers(loaded):
    c = TestClient(create_app(loaded))           # default: only this computer's names
    assert c.get("/api/health", headers={"host": "evil.example"}).status_code == 400
    r = c.get("/api/health", headers={"host": "127.0.0.1:8000"})
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"


def test_serves_the_dashboard(loaded, tmp_path):
    (tmp_path / "index.html").write_text("<h1>Ballast</h1>")
    c = TestClient(create_app(loaded, tmp_path, allowed_hosts=["testserver"]))
    assert "Ballast" in c.get("/").text and c.get("/api/health").status_code == 200


def test_serve_refuses_network_addresses_unless_asked(capsys):
    assert cli.main(["serve", "--host", "0.0.0.0"]) == 1
    assert "Refusing" in capsys.readouterr().out
    assert cli._is_loopback("127.0.0.1") and cli._is_loopback("::1") and cli._is_loopback("localhost")
    assert not cli._is_loopback("0.0.0.0") and not cli._is_loopback("192.168.1.5")


# ---------- dashboard export ----------

def test_export_writes_valid_public_files(loaded, tmp_path):
    res = export_site(loaded, out_dir=tmp_path)
    body = res.snapshot_path.read_text()
    snap = json.loads(body)
    assert models.Summary.model_validate(snap["summary"])
    assert models.Optimization.model_validate(snap["optimization"])
    assert models.Backtest.model_validate(snap["backtest"])
    assert set(snap["var"]) == {"99", "95"}
    assert find_private(body) == [] and find_private(res.openapi_path.read_text()) == []
    paths = json.loads(res.openapi_path.read_text())["paths"]
    assert "/api/portfolios/{portfolio}/optimization" in paths and len(paths) == 12


def test_export_site_command(loaded, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", loaded.url.render_as_string(hide_password=False))
    assert cli.main(["export-site", "--out", str(tmp_path)]) == 0
    assert "data as of" in capsys.readouterr().out
    assert (tmp_path / "snapshot.json").stat().st_size > 10_000 and (tmp_path / "openapi.json").exists()
    assert cli.main(["export-site", "--out", str(tmp_path), "--portfolio", "Nope"]) == 1
    assert "Not exported" in capsys.readouterr().out


def test_export_refuses_portfolios_that_are_not_the_public_sample(loaded, tmp_path):
    check_public_portfolio(loaded, "Ballast Core")          # the seed ledger exactly: allowed
    with loaded.begin() as conn:
        conn.execute(text(
            "INSERT INTO trades (portfolio_id, instrument_id, trade_ref, trade_date, quantity, price) "
            "SELECT p.portfolio_id, i.instrument_id, 'MY-REAL-1', '2025-05-01', 7, 100 "
            "FROM portfolios p, instruments i WHERE p.name = 'Ballast Core' AND i.symbol = 'AAPL'"))
    try:
        with pytest.raises(ExportRefused, match="MY-REAL-1"):
            export_site(loaded, out_dir=tmp_path)
        assert not (tmp_path / "snapshot.json").exists()
    finally:
        with loaded.begin() as conn:
            conn.execute(text("DELETE FROM trades WHERE trade_ref = 'MY-REAL-1'"))
    with loaded.begin() as conn:
        conn.execute(text("UPDATE trades SET quantity = quantity + 1 WHERE trade_ref = 'BC-0001'"))
    try:
        with pytest.raises(ExportRefused):
            check_public_portfolio(loaded, "Ballast Core")
    finally:
        with loaded.begin() as conn:
            conn.execute(text("UPDATE trades SET quantity = quantity - 1 WHERE trade_ref = 'BC-0001'"))


def test_private_text_is_detected(monkeypatch):
    fake_key = "ab12" * 8                             # built at runtime: the repository scan flags key-shaped literals
    monkeypatch.setenv("FRED_API_KEY", fake_key)
    assert "FRED API key" in find_private('{"x": "%s"}' % fake_key)
    address = "me" + "@" + "example.com"          # assembled so the repository scan does not flag this file
    assert "email address" in find_private('{"note": "%s"}' % address)
    assert "user folder path" in find_private('{"file": "/Users/someone/Projects/ballast"}')
    assert "user folder path" in find_private('{"file": "/home/someone/ballast"}')
    assert find_private('{"symbol": "SPY", "url": "https://github.com/AkshayAmbulkar/ballast"}') == []


def test_compact_keeps_ten_significant_digits():
    assert compact({"a": [1 / 3, 0.0, 12345678901234.5, None, "x"]}) == {
        "a": [0.3333333333, 0.0, 12345678900000.0, None, "x"]}
    assert service.clean({"n": np.int64(3), "f": np.float64("nan"), "d": Decimal("1.5")}) == {"n": 3, "f": None, "d": 1.5}
