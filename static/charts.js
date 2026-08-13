/* Theta Desk chart library -- dependency-free SVG.
 *
 * Every chart here follows the same contract:
 *   - hairline grid, thin marks, generous padding
 *   - a legend whenever there are two or more series
 *   - a crosshair + tooltip layer, and a keyboard-reachable table twin
 *   - colours come from CSS custom properties, so light/dark swap in one place
 *
 * Palette slots are assigned by entity, never by rank, so filtering a series
 * never repaints the survivors.
 */
(function (global) {
  "use strict";

  const NS = "http://www.w3.org/2000/svg";
  const fmt = {
    num: (v, d = 2) => (v === null || v === undefined || isNaN(v) ? "--" : Number(v).toFixed(d)),
    pct: (v, d = 1) => (v === null || v === undefined || isNaN(v) ? "--" : (v * 100).toFixed(d) + "%"),
    usd: (v, d = 2) => (v === null || v === undefined || isNaN(v) ? "--" : "$" + Number(v).toFixed(d)),
    compact(v) {
      if (v === null || v === undefined || isNaN(v)) return "--";
      const a = Math.abs(v), s = v < 0 ? "-" : "";
      if (a >= 1e12) return s + (a / 1e12).toFixed(2) + "T";
      if (a >= 1e9) return s + (a / 1e9).toFixed(2) + "B";
      if (a >= 1e6) return s + (a / 1e6).toFixed(2) + "M";
      if (a >= 1e3) return s + (a / 1e3).toFixed(1) + "k";
      return s + a.toFixed(0);
    },
    date(d) { return typeof d === "string" ? d.slice(5) : d; },
  };

  function el(tag, attrs, parent) {
    const n = document.createElementNS(NS, tag);
    if (attrs) for (const k in attrs) if (attrs[k] !== null && attrs[k] !== undefined) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }
  function h(tag, cls, parent, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    if (parent) parent.appendChild(n);
    return n;
  }
  function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  /* ---------------------------------------------------------------- scales */
  function linear(d0, d1, r0, r1) {
    if (d1 === d0) { d1 = d0 + 1; }
    const m = (r1 - r0) / (d1 - d0);
    const f = (v) => r0 + (v - d0) * m;
    f.invert = (p) => d0 + (p - r0) / m;
    f.domain = [d0, d1];
    f.range = [r0, r1];
    return f;
  }

  function niceTicks(lo, hi, count) {
    if (!isFinite(lo) || !isFinite(hi)) return [0, 1];
    if (lo === hi) { lo -= 1; hi += 1; }
    const span = hi - lo;
    const raw = span / Math.max(count, 2);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    const step = (norm >= 7.5 ? 10 : norm >= 3.5 ? 5 : norm >= 1.5 ? 2 : 1) * mag;
    const out = [];
    for (let t = Math.ceil(lo / step) * step; t <= hi + step * 1e-9; t += step) out.push(+t.toFixed(10));
    return out;
  }

  /* ------------------------------------------------------------- framework */
  class Frame {
    constructor(host, opts) {
      opts = opts || {};
      this.host = typeof host === "string" ? document.querySelector(host) : host;
      this.opts = opts;
      this.m = Object.assign({ t: 14, r: 16, b: 30, l: 54 }, opts.margin || {});
      this.host.innerHTML = "";
      this.host.classList.add("chart-host");

      this.fig = h("figure", "chart", this.host);
      if (opts.title || opts.subtitle) {
        const head = h("figcaption", "chart-head", this.fig);
        const left = h("div", "", head);
        if (opts.title) h("h4", "chart-title", left, opts.title);
        if (opts.subtitle) h("p", "chart-sub", left, opts.subtitle);
        this.headRight = h("div", "chart-head-right", head);
      }
      this.legendEl = h("div", "chart-legend", this.fig);
      this.plotWrap = h("div", "chart-plot", this.fig);
      this.svg = el("svg", { class: "chart-svg", preserveAspectRatio: "none" }, this.plotWrap);
      this.tip = h("div", "chart-tip", this.plotWrap);
      this.tip.hidden = true;

      this.height = opts.height || 240;
      this.plotWrap.style.height = this.height + "px";
      this.resize();
      if (global.ResizeObserver) {
        this._ro = new ResizeObserver(() => this.onResize());
        this._ro.observe(this.plotWrap);
      }
    }
    resize() {
      const r = this.plotWrap.getBoundingClientRect();
      this.w = Math.max(r.width || this.host.clientWidth || 600, 200);
      this.hgt = this.height;
      this.svg.setAttribute("viewBox", `0 0 ${this.w} ${this.hgt}`);
      this.svg.setAttribute("width", this.w);
      this.svg.setAttribute("height", this.hgt);
      this.iw = this.w - this.m.l - this.m.r;
      this.ih = this.hgt - this.m.t - this.m.b;
    }
    onResize() {
      const prev = this.w;
      this.resize();
      if (Math.abs(prev - this.w) > 2 && this._redraw) this._redraw();
    }
    clear() {
      this.svg.innerHTML = "";
      this.g = el("g", { transform: `translate(${this.m.l},${this.m.t})` }, this.svg);
      // Restore the plot box in case the previous draw showed an empty state.
      const prev = this.plotWrap.querySelector(".chart-empty");
      if (prev) prev.remove();
      this.plotWrap.style.height = this.height + "px";
      this.svg.style.display = "";
    }

    /* An empty state is taller than the plot box it replaces.  Appending it
     * inside a fixed-height container made it overflow across the card below,
     * and because clear() only wiped the SVG, a redraw stacked a second copy.
     * Collapse the box, hide the canvas, and keep exactly one message. */
    empty(msg) {
      const prev = this.plotWrap.querySelector(".chart-empty");
      if (prev) prev.remove();
      this.svg.style.display = "none";
      this.plotWrap.style.height = "auto";
      this.legendEl.hidden = true;
      h("p", "chart-empty", this.plotWrap, msg);
    }

    grid(x, y, opts) {
      opts = opts || {};
      const gc = "var(--grid)";
      const yt = opts.yTicks || niceTicks(y.domain[0], y.domain[1], opts.yCount || 5);
      yt.forEach((t) => {
        const py = y(t);
        if (py < -1 || py > this.ih + 1) return;
        el("line", { x1: 0, x2: this.iw, y1: py, y2: py, stroke: gc, "stroke-width": 1 }, this.g);
        el("text", { x: -8, y: py + 4, "text-anchor": "end", class: "axis-label" }, this.g)
          .textContent = (opts.yFmt || ((v) => fmt.num(v, 0)))(t);
      });
      if (opts.xTicks) {
        opts.xTicks.forEach((t) => {
          const px = x(t.v !== undefined ? t.v : t);
          if (px < -1 || px > this.iw + 1) return;
          if (opts.xGrid !== false)
            el("line", { x1: px, x2: px, y1: 0, y2: this.ih, stroke: gc, "stroke-width": 1 }, this.g);
          el("text", { x: px, y: this.ih + 20, "text-anchor": "middle", class: "axis-label" }, this.g)
            .textContent = t.label !== undefined ? t.label : (opts.xFmt || String)(t);
        });
      }
      el("line", { x1: 0, x2: this.iw, y1: this.ih, y2: this.ih, stroke: "var(--axis)", "stroke-width": 1 }, this.g);
    }

    zeroLine(y) {
      if (y.domain[0] < 0 && y.domain[1] > 0) {
        el("line", { x1: 0, x2: this.iw, y1: y(0), y2: y(0), stroke: "var(--axis)", "stroke-width": 1 }, this.g);
      }
    }

    legend(items) {
      this.legendEl.innerHTML = "";
      if (!items || items.length < 2) { this.legendEl.hidden = items && items.length ? false : true; }
      else this.legendEl.hidden = false;
      (items || []).forEach((it) => {
        const s = h("span", "legend-item", this.legendEl);
        const sw = h("span", "legend-swatch", s);
        sw.style.background = it.color;
        if (it.dash) sw.style.background = `repeating-linear-gradient(90deg, ${it.color} 0 4px, transparent 4px 7px)`;
        h("span", "", s, it.label);
      });
    }

    showTip(html, px, py) {
      this.tip.innerHTML = html;
      this.tip.hidden = false;
      const w = this.tip.offsetWidth, hh = this.tip.offsetHeight;
      let left = px + 14, top = py - hh - 10;
      if (left + w > this.w - 4) left = px - w - 14;
      if (left < 2) left = 2;
      if (top < 2) top = py + 16;
      this.tip.style.left = left + "px";
      this.tip.style.top = top + "px";
    }
    hideTip() { this.tip.hidden = true; }

    /* Table twin -- every chart is readable without colour. */
    table(cols, rows) {
      if (!this.headRight) return;
      let wrap = this.fig.querySelector(".chart-table");
      if (!wrap) {
        const btn = h("button", "btn-mini", this.headRight, "Table");
        wrap = h("div", "chart-table", this.fig);
        wrap.hidden = true;
        btn.addEventListener("click", () => {
          wrap.hidden = !wrap.hidden;
          btn.textContent = wrap.hidden ? "Table" : "Chart";
          this.plotWrap.hidden = !wrap.hidden;
        });
      }
      wrap.innerHTML = "";
      const t = h("table", "data-table", wrap);
      const tr = h("tr", "", h("thead", "", t));
      cols.forEach((c) => h("th", "", tr, c));
      const tb = h("tbody", "", t);
      rows.slice(0, 400).forEach((r) => {
        const row = h("tr", "", tb);
        r.forEach((v) => h("td", "", row, v === null || v === undefined ? "--" : String(v)));
      });
    }

    /* Shared pointer plumbing: bigger-than-the-mark hit area. */
    interact(onMove, onLeave) {
      const rect = el("rect", { x: 0, y: 0, width: this.iw, height: this.ih, fill: "transparent" }, this.g);
      rect.style.cursor = "crosshair";
      const handle = (ev) => {
        const b = this.svg.getBoundingClientRect();
        const sx = (ev.clientX - b.left) * (this.w / b.width) - this.m.l;
        const sy = (ev.clientY - b.top) * (this.hgt / b.height) - this.m.t;
        onMove(sx, sy, ev);
      };
      rect.addEventListener("pointermove", handle);
      rect.addEventListener("pointerdown", handle);
      rect.addEventListener("pointerleave", () => { this.hideTip(); if (onLeave) onLeave(); });
      return rect;
    }
  }

  const SERIES = (i) => cssVar("--series-" + (((i) % 8) + 1), "#2a78d6");
  const UP = () => cssVar("--up", "#0ca30c");
  const DOWN = () => cssVar("--down", "#d03b3b");

  /* ------------------------------------------------------------ line chart */
  function lineChart(host, cfg) {
    const f = new Frame(host, cfg);
    f._redraw = draw;
    draw();
    function draw() {
      f.clear();
      // Copy each series: `dots` is defaulted below, and mutating the caller's
      // config means the second render sees different input from the first.
      const series = cfg.series.filter((s) => s && s.points && s.points.length)
        .map((s) => Object.assign({}, s));
      if (!series.length) { f.empty(cfg.emptyMessage || "No data yet"); return; }
      // One point draws no line -- say so rather than showing bare axes that
      // read as a rendering failure.
      if (Math.max(...series.map((s) => s.points.length)) < 2 && !cfg.allowSingle) {
        f.empty(cfg.emptyMessage ||
          "Only one observation so far — this chart fills in as history accumulates.");
        return;
      }
      // Few points: show the marks, or the reader sees a bare segment.
      series.forEach((s) => { if (s.points.length <= 6 && s.dots === undefined) s.dots = true; });
      const xs = [], ys = [];
      series.forEach((s) => s.points.forEach((p) => { xs.push(p.x); ys.push(p.y); }));
      let [x0, x1] = [Math.min(...xs), Math.max(...xs)];
      let [y0, y1] = cfg.yDomain || [Math.min(...ys), Math.max(...ys)];
      if (cfg.refs) cfg.refs.forEach((r) => { if (r.y !== undefined) { y0 = Math.min(y0, r.y); y1 = Math.max(y1, r.y); } });
      const pad = (y1 - y0) * 0.10 || Math.abs(y1 || 1) * 0.1;
      y0 -= pad; y1 += pad;
      if (cfg.yZero) y0 = Math.min(y0, 0);
      const x = linear(x0, x1, 0, f.iw), y = linear(y0, y1, f.ih, 0);

      const xTicks = (cfg.xTicks || niceTicks(x0, x1, 5)).map((v) =>
        typeof v === "object" ? v : { v, label: (cfg.xFmt || ((n) => fmt.num(n, 0)))(v) });
      f.grid(x, y, { yFmt: cfg.yFmt, xTicks, yCount: cfg.yCount, xGrid: cfg.xGrid });
      f.zeroLine(y);

      (cfg.refs || []).forEach((r) => {
        if (r.y !== undefined) {
          el("line", { x1: 0, x2: f.iw, y1: y(r.y), y2: y(r.y), stroke: r.color || "var(--muted)", "stroke-width": 1, "stroke-dasharray": "3 3" }, f.g);
          el("text", { x: f.iw - 2, y: y(r.y) - 5, "text-anchor": "end", class: "axis-label" }, f.g).textContent = r.label;
        }
        if (r.x !== undefined) {
          el("line", { x1: x(r.x), x2: x(r.x), y1: 0, y2: f.ih, stroke: r.color || "var(--muted)", "stroke-width": 1, "stroke-dasharray": "3 3" }, f.g);
          if (r.label) el("text", { x: x(r.x) + 4, y: 12, class: "axis-label" }, f.g).textContent = r.label;
        }
      });

      series.forEach((s, i) => {
        const color = s.color || SERIES(i);
        const d = s.points.map((p, j) => `${j ? "L" : "M"}${x(p.x).toFixed(2)},${y(p.y).toFixed(2)}`).join("");
        if (s.area) {
          el("path", {
            d: d + `L${x(s.points[s.points.length - 1].x).toFixed(2)},${y(Math.max(y0, 0)).toFixed(2)}L${x(s.points[0].x).toFixed(2)},${y(Math.max(y0, 0)).toFixed(2)}Z`,
            fill: color, opacity: 0.10,
          }, f.g);
        }
        el("path", {
          d, fill: "none", stroke: color, "stroke-width": s.width || 2,
          "stroke-dasharray": s.dash || null, "stroke-linejoin": "round", "stroke-linecap": "round",
        }, f.g);
        if (s.dots) s.points.forEach((p) => {
          el("circle", { cx: x(p.x), cy: y(p.y), r: 4, fill: color, stroke: "var(--surface)", "stroke-width": 2 }, f.g);
        });
        // Selective direct label: the endpoint only.
        if (s.label && cfg.directLabels !== false && s.points.length) {
          const last = s.points[s.points.length - 1];
          el("text", { x: x(last.x) - 4, y: y(last.y) - 7, "text-anchor": "end", class: "series-label" }, f.g)
            .textContent = s.label;
        }
      });

      f.legend(series.map((s, i) => ({ label: s.label || ("Series " + (i + 1)), color: s.color || SERIES(i), dash: !!s.dash })));

      const cross = el("line", { y1: 0, y2: f.ih, stroke: "var(--muted)", "stroke-width": 1, opacity: 0 }, f.g);
      const marks = series.map((s, i) => el("circle", { r: 5, fill: s.color || SERIES(i), stroke: "var(--surface)", "stroke-width": 2, opacity: 0 }, f.g));
      f.interact((sx) => {
        if (sx < 0 || sx > f.iw) return;
        const xv = x.invert(sx);
        cross.setAttribute("x1", sx); cross.setAttribute("x2", sx); cross.setAttribute("opacity", 0.5);
        let rows = "";
        series.forEach((s, i) => {
          let best = null, bd = Infinity;
          s.points.forEach((p) => { const d = Math.abs(p.x - xv); if (d < bd) { bd = d; best = p; } });
          if (!best) { marks[i].setAttribute("opacity", 0); return; }
          marks[i].setAttribute("cx", x(best.x)); marks[i].setAttribute("cy", y(best.y)); marks[i].setAttribute("opacity", 1);
          rows += `<div class="tip-row"><span class="tip-dot" style="background:${s.color || SERIES(i)}"></span>${s.label || ""}<b>${(cfg.tipFmt || cfg.yFmt || ((v) => fmt.num(v, 2)))(best.y)}</b></div>`;
        });
        const head = (cfg.xFmt || ((n) => fmt.num(n, 2)))(xv);
        f.showTip(`<div class="tip-head">${head}</div>${rows}`, sx + f.m.l, f.m.t + 10);
      }, () => { cross.setAttribute("opacity", 0); marks.forEach((m) => m.setAttribute("opacity", 0)); });

      if (cfg.table !== false) {
        const allX = [...new Set(xs)].sort((a, b) => a - b);
        f.table([cfg.xLabel || "x", ...series.map((s, i) => s.label || "s" + i)],
          allX.map((xv) => [(cfg.xFmt || String)(xv),
          ...series.map((s) => { const p = s.points.find((q) => q.x === xv); return p ? (cfg.yFmt || ((v) => fmt.num(v, 4)))(p.y) : "--"; })]));
      }
    }
    return f;
  }

  /* ------------------------------------------------- candlestick + volume */
  function priceChart(host, cfg) {
    const f = new Frame(host, Object.assign({ margin: { t: 14, r: 56, b: 30, l: 54 } }, cfg));
    f._redraw = draw;
    draw();
    function draw() {
      f.clear();
      const bars = cfg.bars || [];
      if (!bars.length) { f.empty("No price history"); return; }
      const volH = cfg.volume === false ? 0 : Math.round(f.ih * 0.22);
      const gap = volH ? 12 : 0;
      const priceH = f.ih - volH - gap;

      const lows = bars.map((b) => b.low), highs = bars.map((b) => b.high);
      let y0 = Math.min(...lows), y1 = Math.max(...highs);
      (cfg.overlays || []).forEach((o) => o.values.forEach((v) => { if (v != null) { y0 = Math.min(y0, v); y1 = Math.max(y1, v); } }));
      (cfg.levels || []).forEach((l) => { if (l.price) { y0 = Math.min(y0, l.price); y1 = Math.max(y1, l.price); } });
      const pad = (y1 - y0) * 0.06;
      y0 -= pad; y1 += pad;

      const x = linear(0, bars.length - 1, 0, f.iw);
      const y = linear(y0, y1, priceH, 0);
      const bw = Math.max(Math.min(f.iw / bars.length * 0.62, 11), 1);

      // x ticks at month boundaries
      const xTicks = [];
      let lastMonth = null;
      bars.forEach((b, i) => {
        const m = b.date.slice(0, 7);
        if (m !== lastMonth) { lastMonth = m; xTicks.push({ v: i, label: b.date.slice(2, 7) }); }
      });
      const step = Math.ceil(xTicks.length / 7) || 1;

      f.grid(x, y, {
        yFmt: (v) => fmt.num(v, v > 100 ? 0 : 2),
        xTicks: xTicks.filter((_, i) => i % step === 0), yCount: 5,
      });

      (cfg.levels || []).forEach((l) => {
        el("line", { x1: 0, x2: f.iw, y1: y(l.price), y2: y(l.price), stroke: l.color || "var(--muted)", "stroke-width": 1, "stroke-dasharray": "4 4", opacity: 0.9 }, f.g);
        el("text", { x: f.iw + 4, y: y(l.price) + 4, class: "axis-label" }, f.g).textContent = l.label;
      });

      (cfg.overlays || []).forEach((o, i) => {
        const pts = [];
        o.values.forEach((v, j) => { if (v != null) pts.push(`${pts.length ? "L" : "M"}${x(j).toFixed(1)},${y(v).toFixed(1)}`); });
        if (pts.length) el("path", { d: pts.join(""), fill: "none", stroke: o.color || SERIES(i), "stroke-width": 1.6, opacity: 0.95 }, f.g);
      });

      const up = UP(), down = DOWN();
      bars.forEach((b, i) => {
        const rising = b.close >= b.open;
        const c = rising ? up : down;
        const px = x(i);
        el("line", { x1: px, x2: px, y1: y(b.high), y2: y(b.low), stroke: c, "stroke-width": 1 }, f.g);
        const yo = y(b.open), yc = y(b.close);
        el("rect", {
          x: px - bw / 2, y: Math.min(yo, yc), width: bw, height: Math.max(Math.abs(yc - yo), 1),
          fill: rising ? "none" : c, stroke: c, "stroke-width": 1, rx: 1,
        }, f.g);
      });

      if (volH) {
        const vmax = Math.max(...bars.map((b) => b.volume || 0)) || 1;
        const vy = linear(0, vmax, f.ih, f.ih - volH);
        el("line", { x1: 0, x2: f.iw, y1: f.ih, y2: f.ih, stroke: "var(--axis)", "stroke-width": 1 }, f.g);
        bars.forEach((b, i) => {
          const v = b.volume || 0;
          const rising = b.close >= b.open;
          el("rect", {
            x: x(i) - bw / 2, y: vy(v), width: bw, height: Math.max(f.ih - vy(v), 0.5),
            fill: rising ? up : down, opacity: 0.45, rx: 1,
          }, f.g);
        });
        el("text", { x: -8, y: f.ih - volH + 10, "text-anchor": "end", class: "axis-label" }, f.g)
          .textContent = fmt.compact(vmax);
        el("text", { x: 2, y: f.ih - volH - 3, class: "axis-label" }, f.g).textContent = "Volume";
      }

      const items = [{ label: "Up day", color: up }, { label: "Down day", color: down }]
        .concat((cfg.overlays || []).map((o, i) => ({ label: o.label, color: o.color || SERIES(i) })));
      f.legend(items);

      const cross = el("line", { y1: 0, y2: f.ih, stroke: "var(--muted)", "stroke-width": 1, opacity: 0 }, f.g);
      f.interact((sx) => {
        const i = Math.round(Math.max(0, Math.min(bars.length - 1, x.invert(sx))));
        const b = bars[i];
        cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("opacity", 0.5);
        const chg = i > 0 ? (b.close / bars[i - 1].close - 1) * 100 : 0;
        let extra = "";
        (cfg.overlays || []).forEach((o, k) => {
          if (o.values[i] != null) extra += `<div class="tip-row"><span class="tip-dot" style="background:${o.color || SERIES(k)}"></span>${o.label}<b>${fmt.num(o.values[i], 2)}</b></div>`;
        });
        f.showTip(
          `<div class="tip-head">${b.date}</div>
           <div class="tip-row">O<b>${fmt.num(b.open, 2)}</b></div>
           <div class="tip-row">H<b>${fmt.num(b.high, 2)}</b></div>
           <div class="tip-row">L<b>${fmt.num(b.low, 2)}</b></div>
           <div class="tip-row">C<b>${fmt.num(b.close, 2)}</b> <i class="${chg >= 0 ? "pos" : "neg"}">${chg >= 0 ? "+" : ""}${fmt.num(chg, 2)}%</i></div>
           <div class="tip-row">Vol<b>${fmt.compact(b.volume)}</b></div>${extra}`,
          x(i) + f.m.l, f.m.t + 8);
      }, () => cross.setAttribute("opacity", 0));

      f.table(["Date", "Open", "High", "Low", "Close", "Volume"],
        bars.slice(-120).reverse().map((b) => [b.date, fmt.num(b.open, 2), fmt.num(b.high, 2),
        fmt.num(b.low, 2), fmt.num(b.close, 2), fmt.compact(b.volume)]));
    }
    return f;
  }

  /* ------------------------------------------------------------ bar chart */
  function barChart(host, cfg) {
    const f = new Frame(host, cfg);
    f._redraw = draw;
    draw();
    function draw() {
      f.clear();
      const groups = cfg.groups || [];
      if (!groups.length) { f.empty("No data"); return; }
      const keys = cfg.keys || ["value"];
      const vals = [];
      groups.forEach((g) => keys.forEach((k) => { if (g[k] != null) vals.push(g[k]); }));
      let y0 = Math.min(0, ...vals), y1 = Math.max(0, ...vals);
      const pad = (y1 - y0) * 0.08 || 1;
      y0 -= pad; y1 += pad;
      const y = linear(y0, y1, f.ih, 0);
      const bandW = f.iw / groups.length;
      const inner = Math.min(bandW * 0.72, 26);
      const bw = keys.length > 1 ? (inner - 2 * (keys.length - 1)) / keys.length : inner;

      const xTickEvery = Math.ceil(groups.length / (cfg.xTickCount || 8));
      const xTicks = groups.map((g, i) => ({ v: i, label: i % xTickEvery === 0 ? g.label : "" }))
        .filter((t) => t.label !== "");
      const xs = linear(0, Math.max(groups.length - 1, 1), bandW / 2, f.iw - bandW / 2);
      f.grid(xs, y, { yFmt: cfg.yFmt || ((v) => fmt.compact(v)), xTicks, xGrid: false });
      f.zeroLine(y);

      groups.forEach((g, gi) => {
        keys.forEach((k, ki) => {
          const v = g[k];
          if (v == null) return;
          let color = cfg.colors ? cfg.colors[ki] : SERIES(ki);
          if (cfg.diverging) color = v >= 0 ? cfg.posColor || SERIES(0) : cfg.negColor || SERIES(7);
          const x0 = xs(gi) - inner / 2 + ki * (bw + 2);
          const top = v >= 0 ? y(v) : y(0);
          const hh = Math.max(Math.abs(y(v) - y(0)), 1);
          el("rect", { x: x0, y: top, width: Math.max(bw, 1), height: hh, fill: color, rx: Math.min(3, bw / 2), opacity: cfg.opacity || 0.9 }, f.g);
        });
      });

      if (cfg.refs) cfg.refs.forEach((r) => {
        const px = typeof r.index === "number" ? xs(r.index) : null;
        if (px === null) return;
        el("line", { x1: px, x2: px, y1: 0, y2: f.ih, stroke: r.color || "var(--text-secondary)", "stroke-width": 1.5, "stroke-dasharray": "4 3" }, f.g);
        el("text", { x: px + 4, y: 11, class: "axis-label strong" }, f.g).textContent = r.label;
      });

      if (keys.length > 1 || cfg.legendItems)
        f.legend(cfg.legendItems || keys.map((k, i) => ({ label: cfg.keyLabels ? cfg.keyLabels[i] : k, color: cfg.colors ? cfg.colors[i] : SERIES(i) })));
      else if (cfg.diverging)
        f.legend([{ label: cfg.posLabel || "Positive", color: cfg.posColor || SERIES(0) },
        { label: cfg.negLabel || "Negative", color: cfg.negColor || SERIES(7) }]);
      else f.legend([]);

      f.interact((sx) => {
        const gi = Math.round(xs.invert(sx));
        if (gi < 0 || gi >= groups.length) { f.hideTip(); return; }
        const g = groups[gi];
        let rows = "";
        keys.forEach((k, ki) => {
          let color = cfg.colors ? cfg.colors[ki] : SERIES(ki);
          if (cfg.diverging) color = (g[k] || 0) >= 0 ? cfg.posColor || SERIES(0) : cfg.negColor || SERIES(7);
          rows += `<div class="tip-row"><span class="tip-dot" style="background:${color}"></span>${cfg.keyLabels ? cfg.keyLabels[ki] : k}<b>${(cfg.tipFmt || cfg.yFmt || fmt.compact)(g[k])}</b></div>`;
        });
        if (g.extra) rows += `<div class="tip-row muted">${g.extra}</div>`;
        f.showTip(`<div class="tip-head">${g.label}</div>${rows}`, xs(gi) + f.m.l, f.m.t + 8);
      });

      f.table([cfg.xLabel || "Bucket", ...keys.map((k, i) => cfg.keyLabels ? cfg.keyLabels[i] : k)],
        groups.map((g) => [g.label, ...keys.map((k) => (cfg.yFmt || fmt.compact)(g[k]))]));
    }
    return f;
  }

  /* --------------------------------------------------------- payoff chart */
  function payoffChart(host, cfg) {
    const f = new Frame(host, Object.assign({ margin: { t: 16, r: 20, b: 30, l: 60 } }, cfg));
    f._redraw = draw;
    draw();
    function draw() {
      f.clear();
      const curves = (cfg.curves || []).filter((c) => c.points && c.points.length);
      if (!curves.length) { f.empty("No payoff"); return; }
      const xs = [], ys = [];
      curves.forEach((c) => c.points.forEach((p) => { xs.push(p.spot); ys.push(p.pnl); }));
      const x0 = Math.min(...xs), x1 = Math.max(...xs);
      let y0 = Math.min(...ys), y1 = Math.max(...ys);
      const pad = (y1 - y0) * 0.12 || 1;
      y0 -= pad; y1 += pad;
      const x = linear(x0, x1, 0, f.iw), y = linear(y0, y1, f.ih, 0);

      f.grid(x, y, {
        yFmt: (v) => (v >= 0 ? "+" : "") + fmt.num(v * 100, 0),
        xTicks: niceTicks(x0, x1, 6).map((v) => ({ v, label: fmt.num(v, 0) })),
      });

      const zeroY = y(0);
      const main = curves[0];
      // Two-tone fill against the zero line: profit vs loss is polarity, so the
      // diverging/status pair is the correct encoding here, not a series hue.
      const clipId = "clip" + Math.random().toString(36).slice(2, 8);
      const defs = el("defs", {}, f.g);
      const cp = el("clipPath", { id: clipId }, defs);
      const dMain = main.points.map((p, j) => `${j ? "L" : "M"}${x(p.spot).toFixed(2)},${y(p.pnl).toFixed(2)}`).join("");
      el("path", { d: dMain + `L${f.iw},${zeroY}L0,${zeroY}Z` }, cp);
      el("rect", { x: 0, y: 0, width: f.iw, height: Math.max(zeroY, 0), fill: UP(), opacity: 0.14, "clip-path": `url(#${clipId})` }, f.g);
      el("rect", { x: 0, y: zeroY, width: f.iw, height: Math.max(f.ih - zeroY, 0), fill: DOWN(), opacity: 0.14, "clip-path": `url(#${clipId})` }, f.g);

      el("line", { x1: 0, x2: f.iw, y1: zeroY, y2: zeroY, stroke: "var(--axis)", "stroke-width": 1 }, f.g);

      if (cfg.spot) {
        el("line", { x1: x(cfg.spot), x2: x(cfg.spot), y1: 0, y2: f.ih, stroke: "var(--text-secondary)", "stroke-width": 1.5 }, f.g);
        el("text", { x: x(cfg.spot) + 4, y: 12, class: "axis-label strong" }, f.g).textContent = "spot " + fmt.num(cfg.spot, 2);
      }
      (cfg.breakevens || []).forEach((b) => {
        el("line", { x1: x(b), x2: x(b), y1: 0, y2: f.ih, stroke: "var(--muted)", "stroke-width": 1, "stroke-dasharray": "3 3" }, f.g);
        el("text", { x: x(b), y: f.ih - 4, "text-anchor": "middle", class: "axis-label" }, f.g).textContent = "BE " + fmt.num(b, 2);
      });
      (cfg.markers || []).forEach((mk) => {
        if (mk.spot == null) return;
        const px = x(mk.spot);
        if (px < 0 || px > f.iw) return;
        el("circle", { cx: px, cy: y(mk.pnl != null ? mk.pnl : 0), r: 5, fill: mk.color || "var(--text-primary)", stroke: "var(--surface)", "stroke-width": 2 }, f.g);
        el("text", { x: px, y: y(mk.pnl != null ? mk.pnl : 0) - 10, "text-anchor": "middle", class: "series-label" }, f.g).textContent = mk.label;
      });

      curves.forEach((c, i) => {
        const d = c.points.map((p, j) => `${j ? "L" : "M"}${x(p.spot).toFixed(2)},${y(p.pnl).toFixed(2)}`).join("");
        el("path", {
          d, fill: "none", stroke: c.color || (i === 0 ? "var(--text-primary)" : SERIES(i)),
          "stroke-width": i === 0 ? 2.2 : 1.6, "stroke-dasharray": c.dash || null,
          "stroke-linejoin": "round",
        }, f.g);
      });

      f.legend(curves.map((c, i) => ({
        label: c.label, color: c.color || (i === 0 ? "var(--text-primary)" : SERIES(i)), dash: !!c.dash,
      })));

      const cross = el("line", { y1: 0, y2: f.ih, stroke: "var(--muted)", "stroke-width": 1, opacity: 0 }, f.g);
      f.interact((sx) => {
        if (sx < 0 || sx > f.iw) return;
        const sv = x.invert(sx);
        cross.setAttribute("x1", sx); cross.setAttribute("x2", sx); cross.setAttribute("opacity", 0.5);
        let rows = "";
        curves.forEach((c, i) => {
          let best = null, bd = Infinity;
          c.points.forEach((p) => { const dd = Math.abs(p.spot - sv); if (dd < bd) { bd = dd; best = p; } });
          if (best) {
            const dollars = best.pnl * 100 * (cfg.qty || 1);
            rows += `<div class="tip-row"><span class="tip-dot" style="background:${c.color || (i === 0 ? "var(--text-primary)" : SERIES(i))}"></span>${c.label}<b class="${dollars >= 0 ? "pos" : "neg"}">${dollars >= 0 ? "+" : ""}$${fmt.num(dollars, 0)}</b></div>`;
          }
        });
        const movePct = cfg.spot ? ((sv / cfg.spot - 1) * 100) : null;
        f.showTip(`<div class="tip-head">${fmt.num(sv, 2)}${movePct !== null ? ` <i>${movePct >= 0 ? "+" : ""}${fmt.num(movePct, 1)}%</i>` : ""}</div>${rows}`, sx + f.m.l, f.m.t + 8);
      }, () => cross.setAttribute("opacity", 0));

      f.table(["Spot", ...curves.map((c) => c.label + " P&L ($)")],
        main.points.filter((_, i) => i % 4 === 0).map((p) => [fmt.num(p.spot, 2),
        ...curves.map((c) => {
          const q = c.points.reduce((a, b) => Math.abs(b.spot - p.spot) < Math.abs(a.spot - p.spot) ? b : a);
          return fmt.num(q.pnl * 100 * (cfg.qty || 1), 0);
        })]));
    }
    return f;
  }

  /* ----------------------------------------------------------- sparkline */
  function sparkline(host, values, opts) {
    opts = opts || {};
    const node = typeof host === "string" ? document.querySelector(host) : host;
    const vals = (values || []).filter((v) => v != null && !isNaN(v));
    node.innerHTML = "";
    if (vals.length < 2) return;
    const w = opts.width || 84, hh = opts.height || 22;
    const svg = el("svg", { width: w, height: hh, viewBox: `0 0 ${w} ${hh}`, class: "spark" }, node);
    const lo = Math.min(...vals), hi = Math.max(...vals);
    const x = linear(0, vals.length - 1, 1, w - 1), y = linear(lo, hi, hh - 2, 2);
    const d = vals.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
    const color = opts.color || (vals[vals.length - 1] >= vals[0] ? UP() : DOWN());
    el("path", { d, fill: "none", stroke: color, "stroke-width": 1.4, "stroke-linejoin": "round" }, svg);
    el("circle", { cx: x(vals.length - 1), cy: y(vals[vals.length - 1]), r: 2, fill: color }, svg);
  }

  global.TDCharts = { lineChart, priceChart, barChart, payoffChart, sparkline, fmt, SERIES, UP, DOWN, cssVar };
})(window);

