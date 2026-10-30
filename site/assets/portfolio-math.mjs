// Black-Litterman and long-only mean-variance optimization, for the dashboard's
// "try your own views" panel. A port of ballast.construction (Python); the test
// suite runs both on the same inputs and requires them to agree.
//
// Conventions match the Python code: returns are annual decimals, `pi` is the
// implied excess return (over the risk-free rate), absolute views are total
// returns (the risk-free rate is subtracted), relative views are differences.

const dot = (a, b) => a.reduce((s, x, i) => s + x * b[i], 0);
const matVec = (M, v) => M.map((row) => dot(row, v));
const transpose = (M) => M[0].map((_, j) => M.map((row) => row[j]));
const matMul = (A, B) => { const Bt = transpose(B); return A.map((row) => Bt.map((col) => dot(row, col))); };

/** Inverse of a small square matrix (Gauss-Jordan with partial pivoting). */
export function invert(M) {
  const n = M.length;
  const A = M.map((row, i) => [...row, ...row.map((_, j) => (i === j ? 1 : 0))]);
  for (let c = 0; c < n; c++) {
    let p = c;
    for (let r = c + 1; r < n; r++) if (Math.abs(A[r][c]) > Math.abs(A[p][c])) p = r;
    if (Math.abs(A[p][c]) < 1e-300) throw new Error("singular matrix");
    [A[c], A[p]] = [A[p], A[c]];
    const piv = A[c][c];
    for (let j = 0; j < 2 * n; j++) A[c][j] /= piv;
    for (let r = 0; r < n; r++) {
      if (r === c) continue;
      const f = A[r][c];
      if (f !== 0) for (let j = 0; j < 2 * n; j++) A[r][j] -= f * A[c][j];
    }
  }
  return A.map((row) => row.slice(n));
}

/**
 * views: [{long: "NVDA", short: "SPY" | null, value: 0.05}]
 * Returns {P, Q} with Q in excess-of-risk-free terms.
 */
export function viewMatrices(views, symbols, riskFree) {
  const P = [], Q = [];
  for (const v of views) {
    const i = symbols.indexOf(v.long), j = v.short ? symbols.indexOf(v.short) : -1;
    if (i < 0 || (v.short && j < 0)) throw new Error(`unknown asset in view ${v.long}${v.short ? "-" + v.short : ""}`);
    if (i === j) throw new Error("a relative view needs two different assets");
    if (!Number.isFinite(v.value)) throw new Error("view return must be a number");
    const row = symbols.map(() => 0);
    row[i] = 1;
    if (j >= 0) row[j] = -1;
    P.push(row);
    Q.push(j >= 0 ? v.value : v.value - riskFree);
  }
  return { P, Q };
}

/** He-Litterman posterior mean of excess returns (Woodbury form, Omega = diag(P tau S P')). */
export function posteriorMean(pi, cov, tau, P, Q) {
  if (!P || P.length === 0) return pi.slice();
  const prior = cov.map((row) => row.map((x) => tau * x));
  const priorPt = matMul(prior, transpose(P));                 // n x k
  const viewPrior = matMul(P, priorPt);                        // k x k
  const A = viewPrior.map((row, i) => row.map((x, j) => (i === j ? 2 * x : x)));   // + Omega
  const surprise = Q.map((q, k) => q - dot(P[k], pi));
  const gainSurprise = matVec(invert(A), surprise);
  return pi.map((p, i) => p + dot(priorPt[i], gainSurprise));
}

export function describe(w, mu, cov, rf) {
  const ret = dot(w, mu);
  const vol = Math.sqrt(Math.max(dot(w, matVec(cov, w)), 0));
  return { weights: w, expectedReturn: ret, volatility: vol, sharpe: vol > 0 ? (ret - rf) / vol : NaN };
}

/**
 * Euclidean projection onto {w : sum w = 1, 0 <= w <= cap}: w_i = clip(v_i - t, 0, cap), where
 * the shift t makes the weights sum to 1. The sum is piecewise linear in t with kinks at v_i and
 * v_i - cap, so t is found exactly by locating the segment that crosses 1 and interpolating.
 */
export function projectCappedSimplex(v, cap) {
  const total = (t) => v.reduce((s, x) => s + Math.min(Math.max(x - t, 0), cap), 0);
  const kinks = [...v, ...v.map((x) => x - cap)].sort((a, b) => a - b);
  let t = kinks[0];
  for (let k = 0; k < kinks.length - 1; k++) {
    const s0 = total(kinks[k]), s1 = total(kinks[k + 1]);
    if (s0 >= 1 && s1 <= 1) {
      t = s0 === s1 ? kinks[k] : kinks[k] + ((s0 - 1) / (s0 - s1)) * (kinks[k + 1] - kinks[k]);
      break;
    }
  }
  return v.map((x) => Math.min(Math.max(x - t, 0), cap));
}

function largestEigenvalue(S) {
  let v = S.map(() => 1);
  let lambda = 0;
  for (let k = 0; k < 200; k++) {
    const w = matVec(S, v);
    const norm = Math.hypot(...w);
    if (norm === 0) return 0;
    v = w.map((x) => x / norm);
    lambda = norm;
  }
  return lambda;
}

/**
 * Minimize 1/2 w'Sw - lambda mu'w over the capped simplex (long-only, sum 1, w <= cap):
 * the efficient portfolio for risk tolerance lambda. Accelerated projected gradient
 * (FISTA with restart), step 1/L.
 */
export function efficientPortfolio(mu, cov, lambda, cap, start = null, L = null) {
  const n = mu.length;
  L = L || largestEigenvalue(cov) || 1;
  let w = start ? start.slice() : projectCappedSimplex(mu.map(() => 1 / n), cap);
  let y = w.slice(), t = 1;
  const f = (x) => 0.5 * dot(x, matVec(cov, x)) - lambda * dot(mu, x);
  for (let k = 0; k < 20000; k++) {
    const g = matVec(cov, y).map((x, i) => x - lambda * mu[i]);
    const next = projectCappedSimplex(y.map((x, i) => x - g[i] / L), cap);
    if (f(next) > f(w)) {
      // From y = w (t = 1) a projected-gradient step of 1/L cannot raise f, so a rise is only
      // rounding: converged. Otherwise momentum overshot: restart from w (O'Donoghue-Candes).
      if (t === 1) break;
      y = w.slice(); t = 1; continue;
    }
    const tNext = (1 + Math.sqrt(1 + 4 * t * t)) / 2;
    const moved = Math.max(...next.map((x, i) => Math.abs(x - w[i])));
    y = next.map((x, i) => x + ((t - 1) / tNext) * (x - w[i]));
    w = next; t = tNext;
    if (moved < 1e-12) break;
  }
  return w;
}

/** Efficient portfolios along a grid of risk tolerances, from minimum variance to maximum return. */
export function frontierPath(mu, cov, cap, points = 80) {
  const out = [];
  const L = largestEigenvalue(cov) || 1;
  let prev = null;
  for (let k = 0; k < points; k++) {
    const lambda = k === 0 ? 0 : Math.pow(10, -4 + (6 * k) / (points - 1));
    prev = efficientPortfolio(mu, cov, lambda, cap, prev, L);
    out.push({ lambda, weights: prev });
  }
  return out;
}

/** Long-only maximum-Sharpe portfolio with a cap per asset: scan the frontier, then refine. */
export function maxSharpe(mu, cov, rf, cap, path = null) {
  if (mu.every((m) => m <= rf)) throw new Error("no asset is expected to beat the risk-free rate");
  if (cap * mu.length < 1 - 1e-12) throw new Error("the cap is too small for weights to sum to 100%");
  path = path || frontierPath(mu, cov, cap);
  const L = largestEigenvalue(cov) || 1;
  const sharpe = (w) => describe(w, mu, cov, rf).sharpe;
  let best = 0;
  path.forEach((p, k) => { if (sharpe(p.weights) > sharpe(path[best].weights)) best = k; });
  // golden-section search on log(lambda) between the neighbours of the best grid point
  const logL = (k) => (path[k].lambda > 0 ? Math.log10(path[k].lambda) : -6);
  let a = logL(Math.max(best - 1, 0)), b = logL(Math.min(best + 1, path.length - 1));
  const at = (x) => efficientPortfolio(mu, cov, Math.pow(10, x), cap, path[best].weights, L);
  const g = (Math.sqrt(5) - 1) / 2;
  let c = b - g * (b - a), d = a + g * (b - a);
  let wc = at(c), wd = at(d);
  for (let k = 0; k < 50 && b - a > 1e-9; k++) {
    if (sharpe(wc) >= sharpe(wd)) { b = d; d = c; wd = wc; c = b - g * (b - a); wc = at(c); }
    else { a = c; c = d; wc = wd; d = a + g * (b - a); wd = at(d); }
  }
  const candidates = [wc, wd, path[best].weights];
  return candidates.reduce((x, y) => (sharpe(y) > sharpe(x) ? y : x));
}

/**
 * Everything the views panel shows, from the snapshot's optimization block.
 * opt: {symbols, covariance, risk_free, tau, max_weight, assets: [{implied_return, ...}]}
 */
export function blackLitterman(opt, views) {
  const { symbols, covariance: cov, risk_free: rf, tau, max_weight: cap } = opt;
  const pi = opt.assets.map((a) => a.implied_return - rf);
  const { P, Q } = viewMatrices(views, symbols, rf);
  const mu = posteriorMean(pi, cov, tau, P, Q).map((x) => x + rf);
  const path = frontierPath(mu, cov, cap);
  const best = describe(maxSharpe(mu, cov, rf, cap, path), mu, cov, rf);
  const frontier = path.map((p) => describe(p.weights, mu, cov, rf));
  return { expected: mu, best, frontier };
}
