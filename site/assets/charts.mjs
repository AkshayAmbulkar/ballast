// Small SVG chart kit for the dashboard. No dependencies.
// Mark specs: 2px lines, <=24px bars with a 4px rounded data end, >=8px markers with a
// 2px surface ring, hairline solid grid, crosshair + tooltip on hover and on arrow keys,
// and a table view under every chart. All text is inserted with textContent.

const SVG = "http://www.w3.org/2000/svg";
const tooltipEl = () => document.getElementById("tooltip");

export function el(tag, attrs = {}, parent = null, text = null) {
  const node = tag.startsWith("svg:") ? document.createElementNS(SVG, tag.slice(4)) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.setAttribute("class", v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  if (text !== null) node.textContent = text;
  if (parent) parent.appendChild(node);
  return node;
}

// ---------- scales and ticks ----------

function niceStep(range, count) {
  const raw = range / Math.max(count, 1);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const f = raw / mag;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * mag;
}

export function niceTicks(min, max, count = 5) {
  if (min === max) { const d = Math.abs(min) || 1; min -= d / 2; max += d / 2; }
  const step = niceStep(max - min, count);
  const lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = lo; v <= hi + step / 2; v += step) ticks.push(Math.abs(v) < step / 1e9 ? 0 : v);
  return ticks;
}

function timeTicks(t0, t1, maxTicks) {
  const months = (t1.getUTCFullYear() - t0.getUTCFullYear()) * 12 + t1.getUTCMonth() - t0.getUTCMonth();
  const every = [1, 2, 3, 6, 12, 24].find((k) => months / k <= maxTicks) || 24;
  const out = [];
  let y = t0.getUTCFullYear(), m = t0.getUTCMonth() + 1;
  if (every >= 12) { y += m > 1 ? 1 : 0; m = 1; } else m = Math.ceil(m / every) * every;
  for (;;) {
    while (m > 12) { m -= 12; y += 1; }
    const d = new Date(Date.UTC(y, m - 1, 1));
    if (d > t1) break;
    if (d >= t0) out.push(d);
    m += every;
  }
  return out;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const timeTickLabel = (d, i, all) => {
  const showYear = i === 0 || d.getUTCMonth() === 0 || all[i - 1].getUTCFullYear() !== d.getUTCFullYear();
  return d.getUTCMonth() === 0 && all.length > 4 ? String(d.getUTCFullYear())
    : MONTHS[d.getUTCMonth()] + (showYear ? " " + d.getUTCFullYear() : "");
};

const textWidth = (s, px = 12) => String(s).length * px * 0.56;

// ---------- frame: title, legend, body, table toggle ----------

function frame(figure, opts) {
  figure.replaceChildren();
  if (opts.title) el("p", { class: "chart-title" }, figure, opts.title);
  if (opts.subtitle) el("p", { class: "chart-sub" }, figure, opts.subtitle);
  if (opts.legend && opts.legend.length > 1) {
    const ul = el("ul", { class: "legend" }, figure);
    for (const item of opts.legend) {
      const li = el("li", {}, ul);
      const key = el("span", { class: `key ${item.kind || "line"}` }, li);
      const color = `var(--${cssVarFor(item.className)})`;
      if (item.kind === "rect" || item.kind === "dot") key.style.background = color;
      else key.style.borderTopColor = color;
      el("span", {}, li, item.name);
    }
  }
  const body = el("div", { class: "chart-body" }, figure);
  if (opts.table) {
    const btn = el("button", { type: "button", class: "table-toggle", "aria-expanded": "false" }, figure, "Show table");
    const holder = el("div", { class: "chart-table", hidden: true }, figure);
    btn.addEventListener("click", () => {
      const open = holder.hidden;
      holder.hidden = !open;
      btn.setAttribute("aria-expanded", String(open));
      btn.textContent = open ? "Hide table" : "Show table";
      if (open && !holder.firstChild) holder.appendChild(dataTable(opts.table.columns, opts.table.rows));
    });
  }
  return body;
}

const cssVarFor = (cls) => ({ "c-s1": "s1", "c-s2": "s2", "c-hazard": "hazard", "c-neutral": "neutral-mark",
  "c-pos": "div-pos", "c-neg": "div-neg" }[cls] || "s1");

/** A plain table. columns: [{label, align?: "text"}]; rows: arrays of strings/numbers. */
export function dataTable(columns, rows, { caption = null, totalRow = false } = {}) {
  const table = el("table");
  if (caption) el("caption", {}, table, caption);
  const tr = el("tr", {}, el("thead", {}, table));
  for (const c of columns) el("th", { scope: "col", class: c.align === "text" ? "text" : null }, tr, c.label);
  const tbody = el("tbody", {}, table);
  rows.forEach((row, r) => {
    const tr2 = el("tr", { class: totalRow && r === rows.length - 1 ? "total" : null }, tbody);
    row.forEach((cell, i) => {
      const td = el("td", { class: columns[i] && columns[i].align === "text" ? "text" : null }, tr2);
      if (cell instanceof Node) td.appendChild(cell); else td.textContent = cell ?? "";
    });
  });
  return table;
}

// ---------- tooltip ----------

function showTooltip(content, clientX, clientY) {
  const tip = tooltipEl();
  tip.replaceChildren();
  if (content.head) el("div", { class: "tooltip-head" }, tip, content.head);
  for (const row of content.rows) {
    const r = el("div", { class: "tooltip-row" }, tip);
    const key = el("span", { class: "key" }, r);
    key.style.borderTopColor = `var(--${cssVarFor(row.className)})`;
    if (!row.className) key.style.visibility = "hidden";
    el("strong", {}, r, row.value);
    el("span", {}, r, row.label);
  }
  tip.hidden = false;
  const { innerWidth: W, innerHeight: H } = window;
  const box = tip.getBoundingClientRect();
  let x = clientX + 16, y = clientY + 16;
  if (x + box.width > W - 8) x = clientX - box.width - 16;
  if (y + box.height > H - 8) y = clientY - box.height - 16;
  tip.style.left = `${Math.max(8, x)}px`;
  tip.style.top = `${Math.max(8, y)}px`;
}
const hideTooltip = () => { tooltipEl().hidden = true; };

// ---------- bar path with a rounded data end ----------

function barPath(x, y0, y1, w, horizontal = false) {
  // vertical: x = left, y0 = baseline, y1 = data end. horizontal: x = top, y0/y1 along x.
  const len = Math.abs(y1 - y0);
  const r = Math.min(4, w / 2, len);
  const dir = y1 < y0 ? -1 : 1;
  if (!horizontal) {
    if (r < 1) return `M${x},${y0}V${y1}H${x + w}V${y0}Z`;
    return `M${x},${y0}V${y1 - dir * r}Q${x},${y1} ${x + r},${y1}H${x + w - r}Q${x + w},${y1} ${x + w},${y1 - dir * r}V${y0}Z`;
  }
  if (r < 1) return `M${y0},${x}H${y1}V${x + w}H${y0}Z`;
  return `M${y0},${x}H${y1 - dir * r}Q${y1},${x} ${y1},${x + r}V${x + w - r}Q${y1},${x + w} ${y1 - dir * r},${x + w}H${y0}Z`;
}

// ---------- x/y chart: lines, areas, bars and dots on one shared x ----------

/**
 * opts: {
 *   title, subtitle, height, legend: [{name, className, kind}],
 *   x: {type: "time" | "band" | "linear", values: [...], format?, label?},
 *   y: {format, zero?: bool, min?, max?, label?},
 *   layers: [
 *     {kind: "line", values, className, area?, markers?, endLabel?: string},
 *     {kind: "bars", values, className | (i, v) => className, group?: [index, count]},
 *     {kind: "dots", points: [{i? , x?, y, className, label?, strong?}]},
 *   ],
 *   tooltip: (i) => ({head, rows: [{className, value, label}]}) or for linear x: (point) => ...,
 *   table: {columns, rows},
 * }
 */
export function xyChart(figure, opts) {
  const body = frame(figure, opts);
  const draw = () => renderXY(body, opts);
  draw();
  observeWidth(body, draw);
}

function observeWidth(node, draw) {
  let last = node.clientWidth;
  const ro = new ResizeObserver(() => {
    if (Math.abs(node.clientWidth - last) > 2) { last = node.clientWidth; draw(); }
  });
  ro.observe(node);
}

function renderXY(body, opts) {
  body.replaceChildren();
  const width = Math.max(body.clientWidth || 600, 280);
  const height = opts.height || 260;
  const xs = opts.x.values;
  const n = xs.length;

  // y domain from every layer
  const ys = [];
  for (const L of opts.layers) {
    if (L.kind === "dots") L.points.forEach((p) => ys.push(p.y));
    else L.values.forEach((v) => v !== null && v !== undefined && Number.isFinite(v) && ys.push(v));
  }
  let yMin = opts.y.min ?? Math.min(...ys), yMax = opts.y.max ?? Math.max(...ys);
  if (opts.y.zero !== false && opts.layers.some((L) => L.kind === "bars")) { yMin = Math.min(yMin, 0); yMax = Math.max(yMax, 0); }
  if (opts.y.zero) { yMin = Math.min(yMin, 0); yMax = Math.max(yMax, 0); }
  const ticks = niceTicks(yMin, yMax, height < 180 ? 4 : 5);
  const [t0, t1] = [ticks[0], ticks[ticks.length - 1]];
  const yStep = ticks.length > 1 ? ticks[1] - ticks[0] : 1;
  const yFmt0 = opts.y.format || ((v) => String(v));
  const yFmt = (v) => yFmt0(v, yStep);

  const endLabels = opts.layers.filter((L) => L.kind === "line" && L.endLabel);
  const left = Math.max(...ticks.map((t) => textWidth(yFmt(t)))) + 10;
  const right = endLabels.length ? Math.max(...endLabels.map((L) => textWidth(L.endLabel))) + 14 : 12;
  const top = 8, bottom = 26;
  const pw = width - left - right, ph = height - top - bottom;
  const Y = (v) => top + ph - ((v - t0) / (t1 - t0)) * ph;

  let X, bandW = 0, xTicks = [];
  if (opts.x.type === "time") {
    const tt0 = xs[0].getTime(), tt1 = xs[n - 1].getTime();
    X = (i) => left + (n === 1 ? pw / 2 : ((xs[i].getTime() - tt0) / (tt1 - tt0)) * pw);
    bandW = pw / Math.max(n, 1);
    xTicks = timeTicks(xs[0], xs[n - 1], Math.max(2, Math.floor(pw / 90))).map((d, i, all) => ({
      x: left + ((d.getTime() - tt0) / (tt1 - tt0)) * pw, label: timeTickLabel(d, i, all) }));
  } else if (opts.x.type === "band") {
    bandW = pw / n;
    X = (i) => left + (i + 0.5) * bandW;
    const every = Math.ceil(n / Math.max(1, Math.floor(pw / 46)));
    xTicks = xs.map((v, i) => ({ x: X(i), label: opts.x.format ? opts.x.format(v) : String(v) })).filter((_, i) => i % every === 0);
  } else {
    const xt = niceTicks(opts.x.min ?? Math.min(...xs), opts.x.max ?? Math.max(...xs), Math.max(2, Math.floor(pw / 80)));
    const [x0, x1] = [xt[0], xt[xt.length - 1]];
    const XL = (v) => left + ((v - x0) / (x1 - x0)) * pw;
    X = (i) => XL(xs[i]);
    X.value = XL;
    const xStep = xt.length > 1 ? xt[1] - xt[0] : 1;
    xTicks = xt.map((v) => ({ x: XL(v), label: opts.x.format ? opts.x.format(v, xStep) : String(v) }));
  }

  const svg = el("svg:svg", { viewBox: `0 0 ${width} ${height}`, width, height, role: "img",
    "aria-label": opts.ariaLabel || opts.title || "chart", tabindex: "0" }, body);
  const g = el("svg:g", {}, svg);
  for (const t of ticks) {
    const y = Math.round(Y(t)) + 0.5;
    el("svg:line", { class: t === 0 ? "baseline" : "grid", x1: left, x2: left + pw, y1: y, y2: y }, g);
    el("svg:text", { class: "tick", x: left - 8, y: y + 4, "text-anchor": "end" }, g, yFmt(t));
  }
  for (const t of xTicks) el("svg:text", { class: "tick", x: t.x, y: height - 8, "text-anchor": "middle" }, g, t.label);
  if (opts.x.label) el("svg:text", { class: "tick", x: left + pw, y: height - 8, "text-anchor": "end" }, g, "");

  const zeroY = Y(Math.max(t0, Math.min(0, t1)));
  const marksG = el("svg:g", { class: opts.animate ? "bars-in" : null }, svg);
  const markerNodes = [];

  for (const L of opts.layers) {
    if (L.kind === "bars") {
      const [gi, gc] = L.group || [0, 1];
      const slot = opts.x.type === "band" ? Math.min(24 * gc + 2 * (gc - 1), bandW * 0.72) : bandW;
      const w = Math.max(opts.x.type === "time" ? Math.max(bandW - 1, 0.6) : (slot - 2 * (gc - 1)) / gc, 0.6);
      L.values.forEach((v, i) => {
        if (v === null || v === undefined || !Number.isFinite(v)) return;
        const cx = X(i) - (opts.x.type === "band" ? slot / 2 : w / 2) + gi * (w + 2);
        const cls = typeof L.className === "function" ? L.className(i, v) : L.className;
        el("svg:path", { class: `bar ${cls}`, d: barPath(cx, zeroY, Y(v), w) }, marksG);
      });
    } else if (L.kind === "line") {
      let d = "", pen = false, firstI = null, lastI = null;
      L.values.forEach((v, i) => {
        if (v === null || v === undefined || !Number.isFinite(v)) { pen = false; return; }
        d += `${pen ? "L" : "M"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`;
        pen = true; if (firstI === null) firstI = i; lastI = i;
      });
      if (L.area && firstI !== null) {
        el("svg:path", { class: `area ${L.className}`, d: `${d}L${X(lastI).toFixed(1)},${zeroY}L${X(firstI).toFixed(1)},${zeroY}Z` }, marksG);
      }
      el("svg:path", { class: `line ${L.className}`, d }, marksG);
      if (L.markers) L.values.forEach((v, i) => Number.isFinite(v) && el("svg:circle", { class: `dot ${L.className}`, cx: X(i), cy: Y(v), r: 4 }, marksG));
      if (L.endLabel && lastI !== null) {
        el("svg:text", { class: "data-label strong", x: X(lastI) + 8, y: Y(L.values[lastI]) + 4 }, marksG, L.endLabel);
      }
      markerNodes.push({ layer: L, node: el("svg:circle", { class: `dot ${L.className}`, r: 4, cx: -99, cy: -99, visibility: "hidden" }, svg) });
    } else if (L.kind === "dots") {
      const placed = [];
      const overlaps = (b) => placed.some((q) => b.x0 < q.x1 && q.x0 < b.x1 && b.y0 < q.y1 && q.y0 < b.y1);
      for (const p of L.points) {
        const px = p.x !== undefined ? X.value(p.x) : X(p.i);
        el("svg:circle", { class: `dot ${p.className}`, cx: px, cy: Y(p.y), r: p.r || 4.5 }, marksG);
        if (!p.label) continue;
        const w = textWidth(p.label), anchor = px > left + pw - w - 12 ? "end" : "start";
        const x0 = anchor === "end" ? px - 9 - w : px + 9;
        // try beside the dot, then below, then above; a label that fits nowhere is left to the tooltip and table
        for (const dy of [p.dy ?? 4, 18, -10, 32]) {
          const box = { x0, x1: x0 + w, y0: Y(p.y) + dy - 11, y1: Y(p.y) + dy + 3 };
          if (overlaps(box)) continue;
          placed.push(box);
          el("svg:text", { class: `data-label${p.strong ? " strong" : ""}`, x: anchor === "end" ? px - 9 : px + 9,
            y: Y(p.y) + dy, "text-anchor": anchor }, marksG, p.label);
          break;
        }
      }
    }
  }

  if (!opts.tooltip) return;
  const cross = el("svg:line", { class: "crosshair", y1: top, y2: top + ph, x1: -9, x2: -9, visibility: "hidden" }, svg);
  const hit = el("svg:rect", { class: "hit", x: left, y: top, width: pw, height: ph }, svg);
  let current = null;

  const showAt = (i, clientX, clientY) => {
    if (i === null) return;
    current = i;
    if (opts.x.type !== "linear") {
      const x = X(i);
      cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible");
      for (const m of markerNodes) {
        const v = m.layer.values[i];
        if (Number.isFinite(v)) { m.node.setAttribute("cx", x); m.node.setAttribute("cy", Y(v)); m.node.setAttribute("visibility", "visible"); }
        else m.node.setAttribute("visibility", "hidden");
      }
    }
    const content = opts.tooltip(i);
    if (!content) { hideTooltip(); return; }
    if (clientX === undefined) {
      const box = svg.getBoundingClientRect();
      const px = opts.x.type === "linear" ? pointPos(i).x : X(i);
      clientX = box.left + (px / width) * box.width; clientY = box.top + top;
    }
    showTooltip(content, clientX, clientY);
  };
  const hide = () => {
    current = null; cross.setAttribute("visibility", "hidden"); hideTooltip();
    markerNodes.forEach((m) => m.node.setAttribute("visibility", "hidden"));
  };

  // linear x: nearest point among the tooltip-able points (opts.points: [{x, y}])
  const pts = opts.x.type === "linear" ? (opts.hoverPoints || []) : [];
  const pointPos = (k) => ({ x: X.value(pts[k].x), y: Y(pts[k].y) });

  const indexFromEvent = (ev) => {
    const box = svg.getBoundingClientRect();
    const px = ((ev.clientX - box.left) / box.width) * width;
    const py = ((ev.clientY - box.top) / box.height) * height;
    if (opts.x.type === "linear") {
      let best = null, bd = Infinity;
      pts.forEach((_, k) => { const p = pointPos(k); const d = Math.hypot(p.x - px, p.y - py); if (d < bd) { bd = d; best = k; } });
      return bd <= 28 ? best : null;
    }
    if (opts.x.type === "band") return Math.max(0, Math.min(n - 1, Math.floor((px - left) / bandW)));
    let best = 0, bd = Infinity;
    for (let i = 0; i < n; i++) { const d = Math.abs(X(i) - px); if (d < bd) { bd = d; best = i; } }
    return best;
  };
  hit.addEventListener("pointermove", (ev) => { const i = indexFromEvent(ev); if (i === null) hide(); else showAt(i, ev.clientX, ev.clientY); });
  hit.addEventListener("pointerleave", hide);
  svg.addEventListener("blur", hide);
  svg.addEventListener("keydown", (ev) => {
    const count = opts.x.type === "linear" ? pts.length : n;
    if (!count) return;
    if (ev.key === "ArrowRight" || ev.key === "ArrowLeft") {
      ev.preventDefault();
      const step = ev.shiftKey ? 10 : 1;
      const next = current === null ? (ev.key === "ArrowRight" ? 0 : count - 1)
        : Math.max(0, Math.min(count - 1, current + (ev.key === "ArrowRight" ? step : -step)));
      showAt(next);
    } else if (ev.key === "Escape") hide();
  });
}

// ---------- horizontal bars (category labels on the left) ----------

/**
 * opts: {title, subtitle, items: [{label, value, className?, valueLabel?, sub?}], format, table}
 * Positive and negative values grow from one baseline; labels sit outside the bar end.
 */
export function hbarChart(figure, opts) {
  const body = frame(figure, opts);
  const draw = () => {
    body.replaceChildren();
    const width = Math.max(body.clientWidth || 500, 280);
    const items = opts.items;
    const rowH = 34, top = 4, bottom = 22;
    const height = top + bottom + rowH * items.length;
    const labelW = Math.min(Math.max(...items.map((it) => textWidth(it.label, 13))) + 12, width * 0.4);
    const valueLabels = items.map((it) => it.valueLabel ?? opts.format(it.value));
    const pad = Math.max(...valueLabels.map((s) => textWidth(s))) + 10;
    const vals = items.map((it) => it.value);
    const span = Math.max(...vals.map(Math.abs)) || 1;
    const lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
    // a value under 3% of the largest on the other side of zero is drawn at the baseline, not given its own axis
    const ticks = niceTicks(-lo > 0.03 * span ? lo : 0, hi > 0.03 * span ? hi : 0, Math.max(2, Math.floor((width - labelW) / 110)));
    const [t0, t1] = [ticks[0], ticks[ticks.length - 1]];
    const hasNeg = t0 < 0, hasPos = t1 > 0;
    const plotL = labelW + (hasNeg ? pad : 0), plotR = width - (hasPos ? pad : 8);
    const Xv = (v) => plotL + ((v - t0) / (t1 - t0)) * (plotR - plotL);
    const svg = el("svg:svg", { viewBox: `0 0 ${width} ${height}`, width, height, role: "img",
      "aria-label": opts.title || "bar chart", tabindex: "0" }, body);
    for (const t of ticks) {
      const x = Math.round(Xv(t)) + 0.5;
      el("svg:line", { class: t === 0 ? "baseline" : "grid", x1: x, x2: x, y1: top, y2: height - bottom }, svg);
      el("svg:text", { class: "tick", x, y: height - 6, "text-anchor": "middle" }, svg, opts.format(t, true));
    }
    items.forEach((it, k) => {
      const yMid = top + rowH * k + rowH / 2;
      el("svg:text", { class: "data-label strong", x: 0, y: yMid + 4 }, svg, it.label);
      const bw = 14;
      const cls = it.className || (it.value < 0 ? "c-neg" : "c-pos");
      const end = Math.min(Math.max(Xv(it.value), plotL), plotR);
      const bar = el("svg:path", { class: `bar ${cls}`, d: barPath(yMid - bw / 2, Xv(0), end, bw, true) }, svg);
      const leftward = it.value < 0 && hasNeg;
      const lx = leftward ? Xv(it.value) - 6 : Math.max(Xv(it.value), Xv(0)) + 6;
      el("svg:text", { class: "data-label", x: lx, y: yMid + 4, "text-anchor": leftward ? "end" : "start" }, svg, valueLabels[k]);
      if (opts.tooltip) {
        const hitR = el("svg:rect", { class: "hit", x: 0, y: yMid - rowH / 2, width, height: rowH }, svg);
        hitR.addEventListener("pointermove", (ev) => { bar.classList.add("active"); showTooltip(opts.tooltip(k), ev.clientX, ev.clientY); });
        hitR.addEventListener("pointerleave", () => { bar.classList.remove("active"); hideTooltip(); });
      }
    });
  };
  draw();
  observeWidth(body, draw);
}
