// Ballast dashboard: renders data/snapshot.json (written by `ballast export-site`).
import { dataTable, el, hbarChart, xyChart } from "./charts.mjs";
import { blackLitterman, describe, frontierPath } from "./portfolio-math.mjs";

const REPO = "https://github.com/AkshayAmbulkar/ballast";

// ---------- formatting ----------

const MINUS = "−";
const nf = (dp) => new Intl.NumberFormat("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
const sign = (v) => (v < 0 ? MINUS : "");
const fmt = {
  usd: (v, dp = 0) => (v == null ? "n/a" : `${sign(v)}$${nf(dp).format(Math.abs(v))}`),
  usdSigned: (v, dp = 0) => (v == null ? "n/a" : `${v > 0 ? "+" : sign(v)}$${nf(dp).format(Math.abs(v))}`),
  usdCompact: (v) => {
    if (v == null) return "n/a";
    const a = Math.abs(v);
    const body = a >= 1e6 ? `${nf(a >= 1e7 ? 0 : 1).format(a / 1e6)}M` : a >= 1e3 ? `${nf(a >= 1e4 ? 0 : 1).format(a / 1e3)}K` : nf(0).format(a);
    return `${sign(v)}$${body}`;
  },
  pct: (v, dp = 1) => (v == null ? "n/a" : `${sign(v)}${nf(dp).format(Math.abs(v * 100))}%`),
  pctSigned: (v, dp = 2) => (v == null ? "n/a" : `${v > 0 ? "+" : sign(v)}${nf(dp).format(Math.abs(v * 100))}%`),
  pp: (v, dp = 2) => (v == null ? "n/a" : `${v > 0 ? "+" : sign(v)}${nf(dp).format(Math.abs(v * 100))}`),
  num: (v, dp = 2) => (v == null ? "n/a" : `${sign(v)}${nf(dp).format(Math.abs(v))}`),
  int: (v) => (v == null ? "n/a" : nf(0).format(v)),
  p: (v) => (v == null ? "n/a" : v < 0.001 ? "<0.001" : nf(3).format(v)),
  date: (iso) => (iso ? new Date(`${iso.slice(0, 10)}T00:00:00Z`).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" }) : "n/a"),
};
const toDate = (iso) => new Date(`${iso}T00:00:00Z`);
// axis labels: as many decimals as the tick step needs, so labels never repeat
const decimalsFor = (step) => { let dp = 0; while (dp < 4 && Math.abs(Math.round(step * 10 ** dp) - step * 10 ** dp) > 1e-9) dp++; return dp; };
const pctAxis = (v, step = 0.01) => `${sign(v)}${nf(decimalsFor(step * 100)).format(Math.abs(v * 100))}%`;
const $ = (id) => document.getElementById(id);
const setText = (id, text) => { const n = $(id); if (n) n.textContent = text; };
const CLASS_NAMES = { equity: "Stocks", etf: "ETFs", treasury: "Treasuries", option: "Options", swap: "Swap" };
const PORTFOLIO_NAMES = { current: "Current", min_variance: "Minimum variance", max_sharpe: "Max Sharpe",
  risk_parity: "Risk parity", max_sharpe_hist: "Max Sharpe, historical means" };
const EPISODE_NAMES = { rates_2022: "2022 rate hikes", svb_2023: "SVB failure, 2023", yen_carry_2024: "Yen carry unwind, 2024",
  deepseek_2025: "DeepSeek, 2025", tariffs_2025: "Tariff shock, 2025" };
const SEGMENT_NAMES = { equity: "Equity", rates: "Rates", gold: "Gold" };
const label = (map, k) => map[k] || k;
const plural = (n, word) => `${fmt.int(n)} ${word}${n === 1 ? "" : "s"}`;

function table(id, columns, rows, opts) {
  const holder = $(id);
  holder.replaceChildren(rows.length ? dataTable(columns, rows, opts) : el("p", { class: "empty" }, null, "Nothing to show."));
}

function symbolCell(symbol, name) {
  const span = el("span");
  el("span", {}, span, symbol);
  if (name && name !== symbol) el("span", { class: "sub" }, span, name.replace(/ \(.*\)$/, ""));
  return span;
}

function barCell(text, fraction, negative = false) {
  const span = el("span");
  el("span", {}, span, text);
  const bar = el("span", { class: `inline-bar${negative ? " neg" : ""}` }, span);
  bar.style.width = `${Math.max(1, Math.round(Math.min(Math.abs(fraction), 1) * 60))}px`;
  return span;
}

// ---------- hero: the waterline ----------

function backtestLayers(bt, { both }) {
  const pnl = bt.days.map((d) => d.pnl);
  const hist = bt.days.map((d) => -d.var_historical);
  const exc = bt.days.map((d, i) => (d.pnl < -d.var_historical ? i : null)).filter((i) => i !== null);
  const layers = [
    { kind: "bars", values: pnl, className: (i, v) => (v < -bt.days[i].var_historical ? "c-hazard" : "c-neutral") },
    { kind: "line", values: hist, className: both ? "c-s1" : "c-hazard" },
  ];
  if (both) layers.push({ kind: "line", values: bt.days.map((d) => -d.var_parametric), className: "c-s2" });
  layers.push({ kind: "dots", points: exc.map((i) => ({ i, y: pnl[i], className: "c-hazard", r: 4 })) });
  return { layers, exceptions: exc.length };
}

function backtestTooltip(bt, both) {
  return (i) => {
    const d = bt.days[i];
    const rows = [
      { className: d.pnl < -d.var_historical ? "c-hazard" : "c-neutral", value: fmt.usdSigned(d.pnl), label: "P&L" },
      { className: both ? "c-s1" : "c-hazard", value: fmt.usd(d.var_historical), label: "VaR, historical" },
    ];
    if (both) rows.push({ className: "c-s2", value: fmt.usd(d.var_parametric), label: "VaR, parametric" });
    if (d.pnl < -d.var_historical) rows.push({ className: null, value: "Exception", label: "loss beyond historical VaR" });
    return { head: fmt.date(d.date), rows };
  };
}

function renderHero(s) {
  const sum = s.summary, v99 = s.var["99"], bt = s.backtest;
  const hist = v99.methods.find((m) => m.method === "historical");
  const t = bt.tests.find((x) => x.method === "historical");
  const counts = {};
  for (const h of sum.holdings) counts[h.asset_class] = (counts[h.asset_class] || 0) + 1;
  const consistent = t.kupiec_p != null && t.kupiec_p >= 0.05;
  setText("hero-text",
    `${sum.portfolio} holds ${fmt.usd(sum.market_value)} in stocks, Treasuries, options and a SOFR swap. ` +
    `On a 1-in-100 bad day it could lose ${fmt.usd(hist.var)}. In ${fmt.int(t.observations)} days of backtests, ` +
    `losses went past that line ${plural(t.exceptions, "time")} against ${nf(1).format(t.expected)} expected` +
    (consistent ? ", within what the Kupiec test accepts." : ", more than the Kupiec test accepts."));
  const { layers } = backtestLayers(bt, { both: false });
  xyChart($("hero-chart"), {
    height: 170, animate: true,
    legend: [{ name: "Daily P&L", className: "c-neutral", kind: "rect" },
      { name: "99% VaR, set at the previous close", className: "c-hazard" },
      { name: "Loss beyond VaR", className: "c-hazard", kind: "dot" }],
    x: { type: "time", values: bt.days.map((d) => toDate(d.date)) },
    y: { format: fmt.usdCompact },
    layers,
    tooltip: backtestTooltip(bt, false),
    ariaLabel: `Daily P&L against 99% historical VaR, ${fmt.date(bt.start)} to ${fmt.date(bt.end)}`,
    table: { columns: [{ label: "Date", align: "text" }, { label: "P&L" }, { label: "VaR" }, { label: "Exception" }],
      rows: bt.days.map((d) => [fmt.date(d.date), fmt.usdSigned(d.pnl), fmt.usd(d.var_historical), d.pnl < -d.var_historical ? "yes" : ""]) },
  });
  setText("hero-caption", `Each bar is one day's profit or loss, ${fmt.date(bt.start)} to ${fmt.date(bt.end)}. ` +
    "The magenta line is the 1-day 99% Value-at-Risk computed the evening before; dots mark the days a loss went past it.");
}

// ---------- overview ----------

function renderOverview(s) {
  const sum = s.summary;
  setText("overview-lede",
    `Positions and P&L are rebuilt every day from a ledger of trades since ${fmt.date(sum.inception)}. ` +
    `Lifetime P&L is ${fmt.usdSigned(sum.lifetime_pnl)}, including ${fmt.usd(sum.lifetime_income)} of dividends, coupons ` +
    `and swap payments; on ${fmt.date(sum.as_of)} the portfolio made ${fmt.usdSigned(sum.day_pnl)}.`);
  const h = sum.history;
  xyChart($("pnl-chart"), {
    title: "Lifetime P&L", subtitle: "Cumulative profit and loss in dollars, net of fees",
    height: 280,
    x: { type: "time", values: h.map((p) => toDate(p.date)) },
    y: { format: fmt.usdCompact, zero: true },
    layers: [{ kind: "line", values: h.map((p) => p.cumulative_pnl), className: "c-s1", area: true,
      endLabel: fmt.usdCompact(h[h.length - 1].cumulative_pnl) }],
    tooltip: (i) => ({ head: fmt.date(h[i].date), rows: [
      { className: "c-s1", value: fmt.usdSigned(h[i].cumulative_pnl), label: "lifetime P&L" },
      { className: null, value: fmt.usdSigned(h[i].daily_pnl), label: "that day" },
      { className: null, value: fmt.usd(h[i].market_value), label: "market value" }] }),
    table: { columns: [{ label: "Date", align: "text" }, { label: "Market value" }, { label: "Day P&L" }, { label: "Lifetime P&L" }],
      rows: h.map((p) => [fmt.date(p.date), fmt.usd(p.market_value), fmt.usdSigned(p.daily_pnl), fmt.usdSigned(p.cumulative_pnl)]) },
  });
  hbarChart($("class-chart"), {
    title: "Market value by asset class",
    subtitle: "The swap is worth about zero at inception; its risk shows up under Rates",
    items: sum.by_asset_class.map((c) => ({ label: label(CLASS_NAMES, c.asset_class), value: c.market_value, className: "c-s1",
      valueLabel: `${fmt.usdCompact(c.market_value)}, ${fmt.pct(c.weight)}` })),
    format: fmt.usdCompact,
    table: { columns: [{ label: "Asset class", align: "text" }, { label: "Market value" }, { label: "Weight" }],
      rows: sum.by_asset_class.map((c) => [label(CLASS_NAMES, c.asset_class), fmt.usd(c.market_value), fmt.pct(c.weight)]) },
  });
  const maxW = Math.max(...sum.holdings.map((x) => Math.abs(x.weight)));
  table("holdings-table",
    [{ label: "Instrument", align: "text" }, { label: "Class", align: "text" }, { label: "Quantity" }, { label: "Price" },
      { label: "Market value" }, { label: "Weight" }, { label: "Day P&L" }, { label: "Lifetime P&L" }],
    sum.holdings.map((x) => [symbolCell(x.symbol, x.name), label(CLASS_NAMES, x.asset_class), fmt.int(x.quantity),
      fmt.num(x.price, 2), fmt.usd(x.market_value), barCell(fmt.pct(x.weight), x.weight / maxW, x.weight < 0),
      fmt.usdSigned(x.day_pnl), fmt.usdSigned(x.total_pnl)]));
}

// ---------- rates ----------

function renderRates(s) {
  const rr = s.rates_risk, c = s.curve;
  setText("rates-lede",
    `The Treasury curve is bootstrapped every day from FRED's constant-maturity par yields. Bonds are priced off it and the ` +
    `swap uses it as a SOFR proxy. A 1 basis point fall in yields gains the book ${fmt.usd(rr.total_dv01, 2)}: ` +
    `${fmt.usd(rr.bond_dv01, 2)} from bonds, ${fmt.usdSigned(rr.swap_dv01, 2)} from the pay-fixed swap that hedges them.`);
  const pts = c.points;
  xyChart($("curve-chart"), {
    title: `Treasury curve, ${fmt.date(c.date)}`, subtitle: "Percent; zero rates on a semiannual bond-equivalent basis",
    height: 260,
    legend: [{ name: "Par yield (FRED)", className: "c-s1" }, { name: "Zero rate (bootstrapped)", className: "c-s2" }],
    x: { type: "band", values: pts.map((p) => p.tenor) },
    y: { format: (v, step) => pctAxis(v / 100, step / 100), zero: false },
    layers: [
      { kind: "line", values: pts.map((p) => p.par_pct), className: "c-s1", markers: true },
      { kind: "line", values: pts.map((p) => p.zero_bey_pct), className: "c-s2", markers: true },
    ],
    tooltip: (i) => ({ head: `${pts[i].tenor} tenor`, rows: [
      { className: "c-s1", value: `${nf(3).format(pts[i].par_pct)}%`, label: "par yield" },
      { className: "c-s2", value: `${nf(3).format(pts[i].zero_bey_pct)}%`, label: "zero rate" },
      { className: null, value: nf(5).format(pts[i].discount_factor), label: "discount factor" },
      { className: null, value: `${nf(3).format(pts[i].fwd_6m_bey_pct)}%`, label: "6-month forward" }] }),
    table: { columns: [{ label: "Tenor", align: "text" }, { label: "Par %" }, { label: "Zero %" }, { label: "Discount factor" }, { label: "6m forward %" }],
      rows: pts.map((p) => [p.tenor, nf(3).format(p.par_pct), nf(3).format(p.zero_bey_pct), nf(6).format(p.discount_factor), nf(3).format(p.fwd_6m_bey_pct)]) },
  });
  if (rr.key_rates) {
    const kr = rr.key_rates;
    xyChart($("keyrate-chart"), {
      title: "Key-rate DV01", subtitle: "Dollars gained per 1bp fall at each tenor, whole book",
      height: 260,
      x: { type: "band", values: kr.tenors },
      y: { format: (v) => fmt.usdCompact(v) },
      layers: [{ kind: "bars", values: kr.total, className: (i, v) => (v < 0 ? "c-neg" : "c-pos") }],
      tooltip: (i) => ({ head: `${kr.tenors[i]} key rate`, rows: [
        { className: kr.total[i] < 0 ? "c-neg" : "c-pos", value: fmt.usdSigned(kr.total[i], 2), label: "total" },
        ...kr.positions.filter((p) => Math.abs(p.dv01[i]) >= 0.005).map((p) => ({ className: null, value: fmt.usdSigned(p.dv01[i], 2), label: p.symbol }))] }),
      table: { columns: [{ label: "Position", align: "text" }, ...kr.tenors.map((t) => ({ label: t }))],
        rows: [...kr.positions.map((p) => [p.symbol, ...p.dv01.map((v) => fmt.num(v, 2))]), ["Total", ...kr.total.map((v) => fmt.num(v, 2))]] },
    });
  }
  table("bonds-table",
    [{ label: "Bond", align: "text" }, { label: "Face" }, { label: "Clean" }, { label: "Accrued" }, { label: "Dirty" }, { label: "Yield" },
      { label: "Market value" }, { label: "Mod. duration" }, { label: "Eff. duration" }, { label: "Convexity" }, { label: "DV01" }],
    rr.bonds.map((b) => [b.symbol, fmt.usd(b.face), nf(3).format(b.clean), nf(3).format(b.accrued), nf(3).format(b.dirty),
      `${nf(3).format(b.ytm_pct)}%`, fmt.usd(b.market_value), nf(2).format(b.mod_duration), nf(2).format(b.eff_duration),
      nf(1).format(b.convexity), fmt.usd(b.dv01_usd, 2)]));
  table("swaps-table",
    [{ label: "Swap", align: "text" }, { label: "Notional" }, { label: "Direction", align: "text" }, { label: "Fixed rate" },
      { label: "Par rate today" }, { label: "Market value" }, { label: "DV01" }],
    rr.swaps.map((w) => [w.symbol, fmt.usd(w.notional), w.direction, `${nf(4).format(w.fixed_pct)}%`, `${nf(4).format(w.par_today_pct)}%`,
      fmt.usdSigned(w.market_value, 2), fmt.usdSigned(w.dv01_usd, 2)]));
  table("scenarios-table",
    [{ label: "Scenario", align: "text" }, { label: "Full revaluation" }, { label: "Key-rate estimate" }, { label: "Convexity" }, { label: "Definition", align: "text" }],
    rr.scenarios.map((x) => [x.scenario.replace(/_/g, " "), fmt.usdSigned(x.full_reval_pnl), fmt.usdSigned(x.key_rate_estimate),
      fmt.usdSigned(x.convexity_effect), x.description]));
}

// ---------- options and swap ----------

function renderDerivatives(s) {
  const g = s.greeks;
  setText("derivatives-lede",
    "European options use exact Black-Scholes Greeks; American ones use a 500-step binomial tree with a control variate, " +
    "bumped and repriced. Volatility is realized, rates come from the Treasury curve and dividends from the trailing yield. " +
    `Book totals: vega ${fmt.usdSigned(g.total_vega, 2)} per volatility point, theta ${fmt.usdSigned(g.total_theta, 2)} per day.`);
  table("options-table",
    [{ label: "Option", align: "text" }, { label: "Quantity" }, { label: "Spot" }, { label: "Vol" }, { label: "Days" }, { label: "Price" },
      { label: "Market value" }, { label: "Delta" }, { label: "Delta, shares" }, { label: "Gamma, shares" }, { label: "Vega $" },
      { label: "Theta $/day" }, { label: "Rho $" }],
    g.options.map((o) => [o.symbol, fmt.int(o.quantity), nf(2).format(o.spot), `${nf(1).format(o.vol_pct)}%`, fmt.int(o.days),
      nf(3).format(o.price), fmt.usdSigned(o.market_value, 2), nf(3).format(o.delta), fmt.num(o.delta_shares, 1),
      fmt.num(o.gamma_shares, 3), fmt.num(o.vega_usd, 2), fmt.num(o.theta_usd_day, 2), fmt.num(o.rho_usd, 2)]));
  const ex = g.exposure;
  if (ex.length) {
    xyChart($("exposure-chart"), {
      title: "Equity exposure before and after options", subtitle: "Dollars of underlying; net = stock plus option delta",
      height: 240,
      legend: [{ name: "Stock only", className: "c-neutral", kind: "rect" }, { name: "Net of options", className: "c-s1", kind: "rect" }],
      x: { type: "band", values: ex.map((e) => e.underlying) },
      y: { format: fmt.usdCompact },
      layers: [
        { kind: "bars", values: ex.map((e) => e.stock_exposure_usd), className: "c-neutral", group: [0, 2] },
        { kind: "bars", values: ex.map((e) => e.net_exposure_usd), className: "c-s1", group: [1, 2] },
      ],
      tooltip: (i) => ({ head: ex[i].underlying, rows: [
        { className: "c-neutral", value: fmt.usd(ex[i].stock_exposure_usd), label: "stock only" },
        { className: "c-s1", value: fmt.usd(ex[i].net_exposure_usd), label: "net of options" },
        { className: null, value: fmt.num(ex[i].option_delta_shares, 1), label: "option delta, shares" }] }),
      table: { columns: [{ label: "Underlying", align: "text" }, { label: "Stock $" }, { label: "Net $" }],
        rows: ex.map((e) => [e.underlying, fmt.usd(e.stock_exposure_usd), fmt.usd(e.net_exposure_usd)]) },
    });
  }
  table("exposure-table",
    [{ label: "Underlying", align: "text" }, { label: "Spot" }, { label: "Stock shares" }, { label: "Option delta" }, { label: "Net shares" }],
    ex.map((e) => [e.underlying, nf(2).format(e.spot), fmt.int(e.stock_shares), fmt.num(e.option_delta_shares, 1), fmt.num(e.net_delta_shares, 1)]));
}

// ---------- risk ----------

function renderRisk(s) {
  const v99 = s.var["99"], v95 = s.var["95"], bt = s.backtest, st = s.stress;
  setText("risk-lede",
    `One-day risk of the whole book on ${fmt.date(v99.as_of)}, with every position fully revalued in each scenario: ` +
    `${v99.lookback_days} days of actual market moves (historical), an EWMA covariance (parametric, λ = 0.94) and ` +
    `${fmt.int(v99.simulations)} Monte Carlo draws. Expected Shortfall is the average loss beyond VaR.`);
  const names = { historical: "Historical", parametric: "Parametric", monte_carlo: "Monte Carlo" };
  table("var-table",
    [{ label: "Method", align: "text" }, { label: "99% VaR" }, { label: "99% ES" }, { label: "95% VaR" }, { label: "95% ES" }, { label: "99% VaR, % of value" }],
    v99.methods.map((m) => {
      const m95 = v95.methods.find((x) => x.method === m.method);
      return [names[m.method] || m.method, fmt.usd(m.var), fmt.usd(m.es), fmt.usd(m95.var), fmt.usd(m95.es), fmt.pct(m.var / v99.market_value, 2)];
    }));
  const { layers } = backtestLayers(bt, { both: true });
  xyChart($("backtest-chart"), {
    title: "VaR backtest", subtitle: `99% one-day VaR set at each previous close against the next day's P&L, ${fmt.date(bt.start)} to ${fmt.date(bt.end)}`,
    height: 300,
    legend: [{ name: "Daily P&L", className: "c-neutral", kind: "rect" }, { name: "Historical VaR", className: "c-s1" },
      { name: "Parametric VaR", className: "c-s2" }, { name: "Loss beyond historical VaR", className: "c-hazard", kind: "dot" }],
    x: { type: "time", values: bt.days.map((d) => toDate(d.date)) },
    y: { format: fmt.usdCompact },
    layers, tooltip: backtestTooltip(bt, true),
    table: { columns: [{ label: "Date", align: "text" }, { label: "P&L" }, { label: "Historical VaR" }, { label: "Parametric VaR" }],
      rows: bt.days.map((d) => [fmt.date(d.date), fmt.usdSigned(d.pnl), fmt.usd(d.var_historical), fmt.usd(d.var_parametric)]) },
  });
  table("backtest-table",
    [{ label: "Method", align: "text" }, { label: "Exceptions" }, { label: "Expected" }, { label: "Kupiec p" }, { label: "Christoffersen p" },
      { label: "Conditional coverage p" }, { label: "Latest 250 days", align: "text" }],
    bt.tests.map((t) => {
      const zone = el("span", { class: `status ${t.basel_zone || ""}` }, null,
        t.last250 == null ? "n/a" : `${t.last250}, ${t.basel_zone} zone`);
      return [names[t.method], `${t.exceptions} of ${fmt.int(t.observations)}`, nf(1).format(t.expected), fmt.p(t.kupiec_p),
        fmt.p(t.independence_p), fmt.p(t.conditional_coverage_p), zone];
    }));
  hbarChart($("stress-chart"), {
    title: "Historical stress tests", subtitle: "Each episode's actual market moves applied to today's book, $",
    items: st.episodes.map((e) => ({ label: label(EPISODE_NAMES, e.name), value: e.pnl.total })),
    format: fmt.usdCompact,
    tooltip: (k) => {
      const e = st.episodes[k];
      return { head: `${label(EPISODE_NAMES, e.name)}, ${fmt.date(e.start)} to ${fmt.date(e.end)}`, rows: [
        { className: e.pnl.total < 0 ? "c-neg" : "c-pos", value: fmt.usdSigned(e.pnl.total), label: "total" },
        ...["equity", "options", "bonds", "swaps"].map((k2) => ({ className: null, value: fmt.usdSigned(e.pnl[k2]), label: k2 }))] };
    },
    table: { columns: [{ label: "Episode", align: "text" }, { label: "Total P&L" }],
      rows: st.episodes.map((e) => [label(EPISODE_NAMES, e.name), fmt.usdSigned(e.pnl.total)]) },
  });
  const parts = v99.standalone.filter((x) => x.name !== "bonds" && x.name !== "swaps");
  const histVar = v99.methods.find((m) => m.method === "historical").var;
  hbarChart($("standalone-chart"), {
    title: "Diversification", subtitle: `99% historical VaR of each part alone and of the whole book; the parts add up to ` +
      `${fmt.usd(v99.sum_of_standalone)}, the book to ${fmt.usd(histVar)}`,
    items: [...parts.map((x) => ({ label: x.name.replace(/^./, (c) => c.toUpperCase()).replace(" (bonds + swaps)", ", bonds and swap"), value: x.var, className: "c-neutral" })),
      { label: "Whole book", value: histVar, className: "c-s1" }],
    format: fmt.usdCompact,
    table: { columns: [{ label: "Part", align: "text" }, { label: "99% VaR" }, { label: "99% ES" }],
      rows: [...v99.standalone.map((x) => [x.name, fmt.usd(x.var), fmt.usd(x.es)]), ["Whole book", fmt.usd(histVar), ""]] },
  });
  table("stress-table",
    [{ label: "Episode", align: "text" }, { label: "Dates", align: "text" }, { label: "SPY" }, { label: "10-year yield" }, { label: "Equity" },
      { label: "Options" }, { label: "Bonds" }, { label: "Swap" }, { label: "Total" }, { label: "What happened", align: "text" }],
    st.episodes.map((e) => [label(EPISODE_NAMES, e.name), `${fmt.date(e.start)} to ${fmt.date(e.end)}`, fmt.pctSigned(e.spy_return, 1),
      e.ust10y_change_bp == null ? "n/a" : `${e.ust10y_change_bp > 0 ? "+" : sign(e.ust10y_change_bp)}${nf(0).format(Math.abs(e.ust10y_change_bp))}bp`,
      fmt.usdSigned(e.pnl.equity), fmt.usdSigned(e.pnl.options), fmt.usdSigned(e.pnl.bonds), fmt.usdSigned(e.pnl.swaps),
      fmt.usdSigned(e.pnl.total), e.description]));
}

// ---------- construction ----------

let constructionState = null;

function renderConstruction(s) {
  const o = s.optimization;
  setText("construction-lede",
    `${o.symbols.length} stocks and ETFs, ${o.lookback_days} days of daily returns from ${fmt.date(o.returns_from)}. ` +
    `Implied returns are the ones that make today's weights optimal (reverse optimization, risk aversion ${o.delta}); ` +
    `every optimizer is long-only with at most ${fmt.pct(o.max_weight, 0)} in one asset, except risk parity, which needs no cap. ` +
    `Risk-free rate ${fmt.pct(o.risk_free, 2)} (3-month Treasury).`);
  document.querySelectorAll('[data-field="cap"]').forEach((n) => { n.textContent = fmt.pct(o.max_weight, 0); });
  const mu0 = o.assets.map((a) => a.bl_return);
  const baseFrontier = frontierPath(mu0, o.covariance, o.max_weight, 60).map((p) => describe(p.weights, mu0, o.covariance, o.risk_free));
  constructionState = { o, baseFrontier, views: [] };

  const weightsRows = o.portfolios.map((p) => [label(PORTFOLIO_NAMES, p.name),
    ...o.symbols.map((sym) => heatCell(p.weights[sym], o.max_weight)), fmt.pct(p.expected_return), fmt.pct(p.volatility), fmt.num(p.sharpe, 2)]);
  table("weights-table", [{ label: "Portfolio", align: "text" }, ...o.symbols.map((x) => ({ label: x })),
    { label: "Exp. return" }, { label: "Volatility" }, { label: "Sharpe" }], weightsRows);
  wireViews();
  updateViews();
}

function heatCell(w, cap) {
  const level = w < 0.005 ? 0 : Math.min(6, 1 + Math.floor((w / cap) * 5.999));
  const span = el("span", { class: level ? `heat l${level}` : "" }, null, fmt.pct(w));
  span.style.display = "inline-block"; span.style.minWidth = "3.6em"; span.style.padding = "0.15em 0.35em"; span.style.borderRadius = "3px";
  return span;
}

function frontierChart(o, base, mine, mineBest) {
  const points = o.portfolios.map((p) => ({ x: p.volatility, y: p.expected_return, className: p.name === "current" ? "c-s1" : "c-neutral",
    label: label(PORTFOLIO_NAMES, p.name), strong: p.name === "current" || p.name === "max_sharpe", name: p.name, sharpe: p.sharpe }));
  const hover = [...points];
  const layers = [{ kind: "line", values: base.map((p) => p.expectedReturn), className: "c-s1" }];
  const xs = base.map((p) => p.volatility);
  const legend = [{ name: "Frontier, implied returns", className: "c-s1" }];
  if (mine) {
    layers.push({ kind: "line", values: [], className: "c-s2" });
    legend.push({ name: "Frontier with your views", className: "c-s2" });
  }
  // one x domain for both frontiers: draw mine as a separate dots-free line via a merged x list
  const merged = mine ? mergeFrontiers(base, mine) : { x: xs, a: base.map((p) => p.expectedReturn), b: null };
  layers[0].values = merged.a;
  if (mine) layers[1].values = merged.b;
  const dotPoints = points.map((p) => ({ x: p.x, y: p.y, className: p.className, label: p.label, strong: p.strong, dy: p.name === "min_variance" ? 16 : 4 }));
  if (mineBest) {
    dotPoints.push({ x: mineBest.volatility, y: mineBest.expectedReturn, className: "c-s2", label: "Your max Sharpe", strong: true, dy: -8 });
    hover.push({ x: mineBest.volatility, y: mineBest.expectedReturn, name: "yours", label: "Max Sharpe with your views", sharpe: mineBest.sharpe });
  }
  layers.push({ kind: "dots", points: dotPoints });
  const allX = [...merged.x, ...dotPoints.map((p) => p.x)];
  xyChart($("frontier-chart"), {
    title: "Efficient frontier",
    subtitle: "Expected return against volatility, a year; dots are the optimized portfolios" + overCap(o),
    height: 320, legend,
    x: { type: "linear", values: merged.x, min: Math.min(...allX), max: Math.max(...allX), format: pctAxis },
    y: { format: pctAxis },
    layers,
    hoverPoints: hover,
    tooltip: (k) => {
      const p = hover[k];
      return { head: p.label, rows: [
        { className: null, value: fmt.pct(p.y), label: "expected return" },
        { className: null, value: fmt.pct(p.x), label: "volatility" },
        { className: null, value: fmt.num(p.sharpe, 2), label: "Sharpe ratio" }] };
    },
    table: { columns: [{ label: "Portfolio", align: "text" }, { label: "Exp. return" }, { label: "Volatility" }, { label: "Sharpe" }],
      rows: hover.map((p) => [p.label, fmt.pct(p.y), fmt.pct(p.x), fmt.num(p.sharpe, 2)]) },
  });
}

function overCap(o) {
  const cur = o.portfolios.find((p) => p.name === "current").weights;
  const over = o.symbols.filter((sym) => cur[sym] > o.max_weight + 1e-9);
  return over.length ? `. Today's portfolio holds more than ${fmt.pct(o.max_weight, 0)} in ${over.join(" and ")}, ` +
    "beyond the cap the frontier respects, so it can sit above it." : "";
}

function mergeFrontiers(a, b) {
  const rows = [...a.map((p) => ({ x: p.volatility, a: p.expectedReturn, b: null })), ...b.map((p) => ({ x: p.volatility, a: null, b: p.expectedReturn }))]
    .sort((u, v) => u.x - v.x);
  return { x: rows.map((r) => r.x), a: interpolateGaps(rows.map((r) => r.a), rows.map((r) => r.x)), b: interpolateGaps(rows.map((r) => r.b), rows.map((r) => r.x)) };
}

function interpolateGaps(values, xs) {
  // fill gaps inside a series by linear interpolation so it draws as one line; leave the ends empty
  const known = values.map((v, i) => (v === null ? null : i)).filter((i) => i !== null);
  const out = values.slice();
  for (let k = 0; k < known.length - 1; k++) {
    const i0 = known[k], i1 = known[k + 1];
    for (let i = i0 + 1; i < i1; i++) out[i] = values[i0] + ((xs[i] - xs[i0]) / (xs[i1] - xs[i0] || 1)) * (values[i1] - values[i0]);
  }
  return out;
}

function wireViews() {
  const { o } = constructionState;
  const rows = $("view-rows");
  const addRow = (v = { long: "NVDA", short: null, value: 0.05 }) => {
    if (constructionState.views.length >= 4) { setText("views-error", "Up to four views at a time."); return; }
    constructionState.views.push(v);
    drawRows();
  };
  const drawRows = () => {
    rows.replaceChildren();
    constructionState.views.forEach((v, k) => {
      const row = el("div", { class: "view-row" }, rows);
      const pick = (value, allowNone, labelText) => {
        const sel = el("select", { "aria-label": labelText }, row);
        if (allowNone) el("option", { value: "" }, sel, "returns");
        for (const sym of o.symbols) el("option", { value: sym }, sel, allowNone ? `beats ${sym} by` : sym);
        sel.value = value || "";
        return sel;
      };
      const a = pick(v.long, false, `View ${k + 1}: asset`);
      const b = pick(v.short, true, `View ${k + 1}: compared with`);
      const input = el("input", { type: "number", step: "0.5", min: "-50", max: "100", inputmode: "decimal",
        "aria-label": `View ${k + 1}: return, percent a year`, value: String(+(v.value * 100).toFixed(2)) }, row);
      el("span", {}, row, "% a year");
      const rm = el("button", { type: "button", class: "remove", "aria-label": `Remove view ${k + 1}` }, row, "Remove");
      const sync = () => {
        v.long = a.value; v.short = b.value || null; v.value = Number(input.value) / 100;
        updateViews();
      };
      a.addEventListener("change", sync); b.addEventListener("change", sync); input.addEventListener("input", sync);
      rm.addEventListener("click", () => { constructionState.views.splice(k, 1); drawRows(); updateViews(); });
    });
  };
  $("add-view").onclick = () => { addRow(); updateViews(); };
  $("preset-nvda").onclick = () => { addRow({ long: "NVDA", short: "SPY", value: 0.05 }); updateViews(); };
  $("preset-tlt").onclick = () => { addRow({ long: "TLT", short: null, value: 0.08 }); updateViews(); };
  $("clear-views").onclick = () => { constructionState.views = []; drawRows(); updateViews(); };
  constructionState.drawRows = drawRows;
}

function updateViews() {
  const { o, baseFrontier, views } = constructionState;
  setText("views-error", "");
  const valid = views.filter((v) => v.long && Number.isFinite(v.value) && v.long !== v.short);
  if (valid.length !== views.length) setText("views-error", "A relative view needs two different assets and a number.");
  let res = null;
  try {
    res = blackLitterman(o, valid.map((v) => ({ ...v })));
  } catch (err) {
    setText("views-error", err.message);
  }
  const current = o.portfolios.find((p) => p.name === "current");
  const best = res ? res.best : null;
  frontierChart(o, baseFrontier, valid.length && res ? res.frontier : null, valid.length && best ? best : null);
  const mine = best ? best.weights : o.portfolios.find((p) => p.name === "max_sharpe").weights;
  const mineW = Array.isArray(mine) ? mine : o.symbols.map((s) => mine[s]);
  xyChart($("views-chart"), {
    title: valid.length ? "Max-Sharpe weights with your views" : "Max-Sharpe weights with implied returns (add a view to change them)",
    subtitle: best ? `Expected return ${fmt.pct(best.expectedReturn)}, volatility ${fmt.pct(best.volatility)}, Sharpe ${fmt.num(best.sharpe, 2)}` : "",
    height: 240,
    legend: [{ name: "Today's weights", className: "c-neutral", kind: "rect" }, { name: valid.length ? "With your views" : "Max Sharpe", className: "c-s1", kind: "rect" }],
    x: { type: "band", values: o.symbols },
    y: { format: pctAxis, zero: true },
    layers: [
      { kind: "bars", values: o.symbols.map((s) => current.weights[s]), className: "c-neutral", group: [0, 2] },
      { kind: "bars", values: mineW, className: "c-s1", group: [1, 2] },
    ],
    tooltip: (i) => ({ head: o.symbols[i], rows: [
      { className: "c-neutral", value: fmt.pct(current.weights[o.symbols[i]]), label: "today" },
      { className: "c-s1", value: fmt.pct(mineW[i]), label: valid.length ? "with your views" : "max Sharpe" },
      { className: null, value: fmt.pct(res ? res.expected[i] : o.assets[i].bl_return), label: "expected return" }] }),
    table: { columns: [{ label: "Asset", align: "text" }, { label: "Today" }, { label: "Max Sharpe" }, { label: "Expected return" }],
      rows: o.symbols.map((s, i) => [s, fmt.pct(current.weights[s]), fmt.pct(mineW[i]), fmt.pct(res ? res.expected[i] : o.assets[i].bl_return)]) },
  });
  table("assets-table",
    [{ label: "Asset", align: "text" }, { label: "Weight now" }, { label: "Share of risk" }, { label: "Volatility" }, { label: "Historical return" },
      { label: "Implied return" }, { label: valid.length ? "With your views" : "Black-Litterman" }],
    o.assets.map((a, i) => [a.symbol, fmt.pct(a.weight_now), fmt.pct(a.risk_share), fmt.pct(a.volatility), fmt.pct(a.hist_return),
      fmt.pct(a.implied_return), fmt.pct(res ? res.expected[i] : a.bl_return)]));
}

// ---------- attribution ----------

function renderAttribution(s) {
  const a = s.attribution;
  setText("attribution-lede",
    `${fmt.date(a.start)} to ${fmt.date(a.end)} against a ${benchmarkText(a.benchmark)} benchmark rebalanced daily. ` +
    `The portfolio returned ${fmt.pctSigned(a.portfolio.total_return)} (${fmt.pctSigned(a.portfolio.annualized)} a year), ` +
    `the benchmark ${fmt.pctSigned(a.benchmark_stats.total_return)}. Active return ${fmt.pctSigned(a.active_annualized)} a year, ` +
    `tracking error ${fmt.pct(a.tracking_error, 2)}, information ratio ${fmt.num(a.information_ratio, 2)}.`);
  const g = a.growth;
  xyChart($("growth-chart"), {
    title: "Growth of $1", subtitle: "Daily returns compounded; the benchmark is rebalanced to its weights every day",
    height: 300,
    legend: [{ name: "Ballast Core", className: "c-s1" }, { name: `Benchmark, ${benchmarkText(a.benchmark)}`, className: "c-neutral" }],
    x: { type: "time", values: g.map((p) => toDate(p.date)) },
    y: { format: (v) => `$${nf(2).format(v)}`, zero: false },
    layers: [
      { kind: "line", values: g.map((p) => p.benchmark), className: "c-neutral", endLabel: `$${nf(2).format(g[g.length - 1].benchmark)}` },
      { kind: "line", values: g.map((p) => p.portfolio), className: "c-s1", endLabel: `$${nf(2).format(g[g.length - 1].portfolio)}` },
    ],
    tooltip: (i) => ({ head: fmt.date(g[i].date), rows: [
      { className: "c-s1", value: `$${nf(3).format(g[i].portfolio)}`, label: "portfolio" },
      { className: "c-neutral", value: `$${nf(3).format(g[i].benchmark)}`, label: "benchmark" }] }),
    table: { columns: [{ label: "Date", align: "text" }, { label: "Portfolio" }, { label: "Benchmark" }],
      rows: g.map((p) => [fmt.date(p.date), nf(4).format(p.portfolio), nf(4).format(p.benchmark)]) },
  });
  const maxAbs = Math.max(...a.effects.map((e) => Math.abs(e.total)), Math.abs(a.totals.total));
  table("effects-table",
    [{ label: "Segment", align: "text" }, { label: "Allocation" }, { label: "Selection" }, { label: "Interaction" }, { label: "Total, pp" }],
    [...a.effects.map((e) => [label(SEGMENT_NAMES, e.segment), fmt.pp(e.allocation), fmt.pp(e.selection), fmt.pp(e.interaction),
      barCell(fmt.pp(e.total), e.total / maxAbs, e.total < 0)]),
      ["Total", fmt.pp(a.totals.allocation), fmt.pp(a.totals.selection), fmt.pp(a.totals.interaction), fmt.pp(a.totals.total)]],
    { totalRow: true });
  table("years-table",
    [{ label: "Year", align: "text" }, { label: "Portfolio" }, { label: "Benchmark" }, { label: "Allocation" }, { label: "Selection" }, { label: "Interaction" }],
    a.by_year.map((y) => [String(y.year), fmt.pctSigned(y.portfolio_return), fmt.pctSigned(y.benchmark_return), fmt.pp(y.allocation),
      fmt.pp(y.selection), fmt.pp(y.interaction)]));
}

const benchmarkText = (b) => b.split(",").map((part) => { const [sym, w] = part.split(":"); return `${Math.round(Number(w) * 100)}% ${sym}`; }).join(" / ");

// ---------- model checks ----------

function renderChecks(s) {
  const list = $("checks-list");
  list.replaceChildren();
  const add = (name, result, how, link = null) => {
    const li = el("li", {}, list);
    el("h3", {}, li, name);
    const right = el("div", {}, li);
    el("p", { class: "result" }, right, result);
    const p = el("p", { class: "how" }, right, how);
    if (link) { p.append(" "); el("a", { href: link.href, rel: "noopener" }, p, link.text); }
  };
  const v = s.validation;
  if (v.sofr_30day_average) {
    const c = v.sofr_30day_average;
    add("SOFR compounding", `${fmt.int(c.exact_matches)} of ${fmt.int(c.days)} days match exactly`,
      `Ballast's 30-day average SOFR, compounded from daily fixings with the New York Fed's method, against the averages the New York Fed ` +
      `publishes (FRED series SOFR30DAYAVG), ${fmt.date(c.start)} to ${fmt.date(c.end)}, at the published 5 decimals. Largest difference ${nf(4).format(c.largest_diff_bp)}bp.`);
  }
  if (v.tlt_empirical_duration) {
    const d = v.tlt_empirical_duration;
    add("Bond ETF duration", `TLT: ${nf(2).format(d.duration)} years (R² ${nf(2).format(d.r_squared)})`,
      `Regression of TLT's daily total return on the daily change in the 20-year Treasury yield, ${fmt.int(d.observations)} days from ` +
      `${fmt.date(d.start)}. iShares publishes the fund's effective duration on its fund page for comparison.`,
      { href: "https://www.ishares.com/us/products/239454/ishares-20-year-treasury-bond-etf", text: "TLT fund page" });
  }
  const t = s.backtest.tests.find((x) => x.method === "historical");
  add("VaR backtest", `${plural(t.exceptions, "exception")} in ${fmt.int(t.observations)} days, ${nf(1).format(t.expected)} expected`,
    `Kupiec p = ${fmt.p(t.kupiec_p)}, Christoffersen p = ${fmt.p(t.independence_p)} (below 0.05 rejects the model). ` +
    (t.last250 == null ? "" : `Latest 250 days: ${plural(t.last250, "exception")}, Basel ${t.basel_zone} zone.`));
  const at = s.attribution;
  add("Attribution adds up", `Effects sum to ${fmt.pp(at.totals.total, 4)} pp; active return ${fmt.pp(at.active_return, 4)} pp`,
    "Brinson-Fachler allocation, selection and interaction, linked across days with Carino's method, must add up exactly to the " +
    "cumulative portfolio return minus the cumulative benchmark return.");
  const tests = `${REPO}/tree/main/tests`;
  add("Black-Litterman", "Reproduces He and Litterman (1999)",
    "Equilibrium returns, posterior returns and optimal weights of the paper's seven-country example, to the published decimal. " +
    "This page's in-browser optimizer is checked against the Python engine to six decimals.", { href: `${tests}/test_construction_math.py`, text: "Tests" });
  add("Option pricing", "Matches Hull's textbook examples",
    "Black-Scholes prices and Greeks, the two-step and many-step American put trees, put-call parity, and the European tree converging to Black-Scholes.",
    { href: `${tests}/test_derivatives_math.py`, text: "Tests" });
  add("Curve and bond maths", "Matches Hull's bootstrap and duration examples",
    "Zero rates bootstrapped from par yields, Actual/Actual accrued interest, yield-price round trips, and key-rate DV01s summing to the parallel DV01.",
    { href: `${tests}/test_fixed_income_math.py`, text: "Tests" });
  add("P&L", "SQL views equal an independent simulation",
    "Daily P&L from the database views (positions, trades, dividends, coupons, swap payments, splits) matches a separate Python simulation of every position.",
    { href: `${tests}/test_pnl.py`, text: "Tests" });
}

// ---------- API reference ----------

async function renderApi() {
  const holder = $("api-reference");
  let spec;
  try {
    const r = await fetch("data/openapi.json", { cache: "no-cache" });
    if (!r.ok) throw new Error(String(r.status));
    spec = await r.json();
  } catch {
    holder.replaceChildren(el("p", { class: "empty" }, null, "The API reference is not published with this snapshot. Run `ballast export-site`."));
    return;
  }
  holder.replaceChildren();
  const schemas = spec.components?.schemas || {};
  const resolve = (s) => (s && s.$ref ? schemas[s.$ref.split("/").pop()] : s);
  const typeOf = (s) => {
    s = s || {};
    if (s.$ref) return s.$ref.split("/").pop();
    if (s.anyOf) return s.anyOf.map(typeOf).filter((x) => x !== "null").join(" or ") + (s.anyOf.some((x) => x.type === "null") ? ", optional" : "");
    if (s.type === "array") return `list of ${typeOf(s.items)}`;
    if (s.type === "object" && s.additionalProperties) return `map of ${typeOf(s.additionalProperties)}`;
    return s.format === "date" ? "date" : s.type || "any";
  };
  const groups = {};
  for (const [path, ops] of Object.entries(spec.paths)) {
    for (const [method, op] of Object.entries(ops)) (groups[(op.tags || ["other"])[0]] ||= []).push({ path, method, op });
  }
  for (const tag of (spec.tags || []).map((t) => t.name).filter((t) => groups[t])) {
    el("h3", { class: "api-group" }, holder, tag.replace(/^./, (c) => c.toUpperCase()));
    for (const { path, method, op } of groups[tag]) {
      const d = el("details", { class: "endpoint" }, holder);
      const sm = el("summary", {}, d);
      el("span", { class: "method" }, sm, method.toUpperCase());
      el("span", { class: "path" }, sm, path);
      el("span", { class: "what" }, sm, op.summary || "");
      const params = op.parameters || [];
      if (params.length) {
        d.appendChild(dataTable([{ label: "Parameter", align: "text" }, { label: "In", align: "text" }, { label: "Type", align: "text" },
          { label: "Default", align: "text" }, { label: "Limits and notes", align: "text" }],
        params.map((p) => {
          const sch = p.schema || {};
          const lim = [sch.minimum != null && `≥ ${sch.minimum}`, sch.exclusiveMinimum != null && `> ${sch.exclusiveMinimum}`,
            sch.maximum != null && `≤ ${sch.maximum}`, sch.maxLength != null && `at most ${sch.maxLength} characters`,
            sch.maxItems != null && `at most ${sch.maxItems}`, p.description || sch.description].filter(Boolean).join("; ");
          return [p.name, p.in, typeOf(sch), sch.default != null ? JSON.stringify(sch.default) : "", lim];
        }), { caption: "Parameters" }));
      }
      const ok = op.responses?.["200"]?.content?.["application/json"]?.schema;
      const body = resolve(ok?.type === "array" ? ok.items : ok);
      if (body && body.properties) {
        d.appendChild(dataTable([{ label: "Field", align: "text" }, { label: "Type", align: "text" }, { label: "Description", align: "text" }],
          Object.entries(body.properties).map(([k, v]) => [k, typeOf(v), v.description || ""]),
          { caption: `Response: ${ok?.type === "array" ? "a list of " : ""}${(ok?.items || ok)?.$ref?.split("/").pop() || "object"}` }));
      }
    }
  }
}

// ---------- routing ----------

const RENDERERS = { overview: renderOverview, rates: renderRates, derivatives: renderDerivatives, risk: renderRisk,
  construction: renderConstruction, attribution: renderAttribution, checks: renderChecks, api: () => renderApi() };
const rendered = new Set();
let snapshot = null;

function activate(id, { scroll = false } = {}) {
  if (!RENDERERS[id]) id = "overview";
  for (const panel of document.querySelectorAll(".panel")) panel.hidden = panel.id !== id;
  for (const a of document.querySelectorAll(".tabs a")) {
    if (a.getAttribute("href") === `#${id}`) {
      a.setAttribute("aria-current", "page");
      a.scrollIntoView({ block: "nearest", inline: "nearest" });      // on a phone the tab row scrolls sideways
    } else a.removeAttribute("aria-current");
  }
  if (snapshot && !rendered.has(id)) {
    rendered.add(id);
    try { RENDERERS[id](snapshot); } catch (err) { showPanelError(id, err); }
  }
  if (scroll) {
    const tabs = document.querySelector(".tabs");
    // a tab click keeps the tabs in view; a link straight to a section scrolls down to it
    if (scroll === "always" || window.scrollY > tabs.offsetTop) window.scrollTo({ top: tabs.offsetTop });
  }
}

function showPanelError(id, err) {
  console.error(err);
  const panel = $(id);
  panel.appendChild(el("p", { class: "form-error" }, null, `This section could not be drawn: ${err.message}`));
}

function initTheme() {
  const btn = $("theme-toggle");
  let stored = null;
  try { stored = localStorage.getItem("ballast-theme"); } catch { /* storage unavailable */ }
  const systemDark = () => window.matchMedia("(prefers-color-scheme: dark)").matches;
  const apply = (theme) => {
    if (theme) document.documentElement.setAttribute("data-theme", theme); else document.documentElement.removeAttribute("data-theme");
    const dark = theme ? theme === "dark" : systemDark();
    btn.textContent = dark ? "Light mode" : "Dark mode";
    btn.setAttribute("aria-pressed", String(dark));
  };
  apply(stored);
  btn.addEventListener("click", () => {
    const dark = document.documentElement.getAttribute("data-theme") ? document.documentElement.getAttribute("data-theme") === "dark" : systemDark();
    const next = dark ? "light" : "dark";
    try { localStorage.setItem("ballast-theme", next); } catch { /* ignore */ }
    apply(next);
  });
}

async function main() {
  initTheme();
  document.querySelector(".tabs").addEventListener("click", (ev) => {
    const a = ev.target.closest("a");
    if (!a) return;
    ev.preventDefault();
    const id = a.getAttribute("href").slice(1);
    history.pushState(null, "", `#${id}`);
    activate(id, { scroll: true });
  });
  window.addEventListener("popstate", () => activate(location.hash.slice(1)));
  try {
    const r = await fetch("data/snapshot.json", { cache: "no-cache" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    snapshot = await r.json();
  } catch (err) {
    setText("hero-text", "No data has been published yet.");
    setText("hero-caption", "Run `ballast export-site` to write data/snapshot.json, then reload this page.");
    setText("as-of", "No snapshot");
    activate(location.hash.slice(1));
    return;
  }
  setText("as-of", `Data as of ${fmt.date(snapshot.summary.as_of)}, from Yahoo Finance and FRED`);
  const deepLink = Boolean(RENDERERS[location.hash.slice(1)]);
  setText("generated", `Snapshot generated ${snapshot.generated_at.replace("T", " ").replace("+00:00", " UTC")} by ballast export-site.`);
  renderHero(snapshot);
  activate(location.hash.slice(1), { scroll: deepLink ? "always" : false });
}

main();
