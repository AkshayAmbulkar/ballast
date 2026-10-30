"""The static dashboard in site/: its in-browser maths agrees with the Python engine,
its scripts parse, and it loads nothing from other websites.

The JavaScript checks need Node.js (preinstalled on GitHub's runners) and are
skipped when it is not available.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from ballast.construction.black_litterman import View, equilibrium_returns, posterior, view_matrices
from ballast.construction.optimize import describe, max_sharpe

SITE = Path(__file__).resolve().parents[1] / "site"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")


def _universe(seed: int):
    """Eight assets with one-factor correlations and implied returns, like the seed portfolio."""
    rng = np.random.default_rng(seed)
    vol = rng.uniform(0.12, 0.45, 8)
    beta = rng.uniform(-0.2, 0.95, 8)
    C = (np.outer(beta, beta) + np.eye(8) * 0.05) / 1.05
    np.fill_diagonal(C, 1.0)
    cov = C * np.outer(vol, vol)
    w = rng.dirichlet(np.ones(8))
    return cov, w


def _run_node(script: str) -> dict:
    out = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
@pytest.mark.parametrize("seed, views", [
    (1, []),
    (2, ["S4-S5:0.05"]),
    (3, ["S0:0.12", "S2-S7:-0.03"]),
    (4, ["S6:0.30"]),
])
def test_browser_black_litterman_matches_python(seed, views):
    cov, w_now = _universe(seed)
    symbols = [f"S{i}" for i in range(8)]
    rf, tau, cap, delta = 0.04, 0.05, 0.35, 2.5
    pi = equilibrium_returns(cov, w_now, delta)
    parsed = [View.parse(v) for v in views]
    if parsed:
        P, Q = view_matrices(parsed, symbols)
        mu = rf + posterior(pi, cov, tau, P, Q - rf * P.sum(axis=1)).mean
    else:
        mu = rf + pi
    w = max_sharpe(mu, cov, rf, cap)
    expected = describe(w, mu, cov, rf)

    opt = {"symbols": symbols, "covariance": cov.tolist(), "risk_free": rf, "tau": tau, "max_weight": cap,
           "assets": [{"implied_return": float(rf + p)} for p in pi]}
    js_views = [{"long": v.long, "short": v.short, "value": v.expected} for v in parsed]
    got = _run_node(
        f"import {{ blackLitterman }} from {json.dumps((SITE / 'assets' / 'portfolio-math.mjs').as_uri())};\n"
        f"const r = blackLitterman({json.dumps(opt)}, {json.dumps(js_views)});\n"
        "console.log(JSON.stringify({ mu: r.expected, w: r.best.weights, sharpe: r.best.sharpe }));"
    )
    assert np.allclose(got["mu"], mu, atol=1e-12)
    assert np.allclose(got["w"], w, atol=1e-6)
    assert got["sharpe"] == pytest.approx(expected.sharpe, abs=1e-9)
    assert sum(got["w"]) == pytest.approx(1.0) and max(got["w"]) <= cap + 1e-9 and min(got["w"]) >= 0


@needs_node
def test_browser_math_rejects_bad_views():
    got = _run_node(
        f"import {{ viewMatrices }} from {json.dumps((SITE / 'assets' / 'portfolio-math.mjs').as_uri())};\n"
        "const errs = [];\n"
        "for (const v of [{long:'A', short:'A', value:0.1}, {long:'Z', short:null, value:0.1}, {long:'A', short:null, value:NaN}]) {\n"
        "  try { viewMatrices([v], ['A','B'], 0.04); errs.push(null); } catch (e) { errs.push(e.message); }\n"
        "}\n"
        "console.log(JSON.stringify({ errs }));"
    )
    assert all(got["errs"])


@needs_node
@pytest.mark.parametrize("module", sorted(p.name for p in (SITE / "assets").glob("*.mjs")))
def test_scripts_parse(module):
    out = subprocess.run([NODE, "--check", str(SITE / "assets" / module)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


def test_site_loads_nothing_from_other_websites():
    html = (SITE / "index.html").read_text()
    assert "Content-Security-Policy" in html and "script-src 'self'" in html
    resources = re.findall(r'<(?:script|link|img|iframe)[^>]+(?:src|href)="([^"]+)"', html)
    assert resources and all(not r.startswith(("http:", "https:", "//")) for r in resources), resources
    css = (SITE / "assets" / "style.css").read_text()
    assert all(not u.startswith(("http", "//")) for u in re.findall(r'url\("?([^")]+)', css))
    for js in (SITE / "assets").glob("*.mjs"):
        text = js.read_text()
        assert not re.search(r'(?:fetch|import)\s*\(\s*["\'`]https?:', text), js.name
        assert not re.search(r'^import .* from ["\']https?:', text, re.M), js.name
        assert "innerHTML" not in text and "eval(" not in text, js.name
