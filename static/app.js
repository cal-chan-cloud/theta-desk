/* Theta Desk front end. */
(function () {
  "use strict";
  const C = window.TDCharts, F = C.fmt;
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

  const state = {
    overview: null, symbol: null, chain: null, ideas: null, flow: null,
    perf: null, trades: null,
    current: "dashboard", sym: null,
    sort: { key: "opt_dollar_volume", dir: -1 },
    // Monotonic request tokens: switching symbols quickly must never let a
    // slow earlier response paint over a newer one.
    token: { symbol: 0, chain: 0 },
  };

  /* --------------------------------------------------------------- format */
  // Null must survive formatting as "--".  `(v || 0) * 100` silently turns a
  // missing metric into a confident "+0%", which is worse than showing nothing.
  const nz = (v) => (v === null || v === undefined || (typeof v === "number" && isNaN(v)));
  const pct = (v, d) => (nz(v) ? "--" : (v * 100).toFixed(d == null ? 1 : d) + "%");
  const sgn = (v, d, suffix) => nz(v) ? "--"
    : (v >= 0 ? "+" : "") + Number(v).toFixed(d == null ? 2 : d) + (suffix || "");
  const sgnPct = (v, d) => nz(v) ? "--" : sgn(v * 100, d == null ? 1 : d, "%");
  const cls = (v) => (nz(v) || v === 0 ? "" : v > 0 ? "pos" : "neg");
  const usd = (v, d) => nz(v) ? "--" : (v < 0 ? "-$" : "$") + Math.abs(v).toFixed(d == null ? 2 : d);
  const usdC = (v) => nz(v) ? "--" : (v < 0 ? "-$" : "$") + F.compact(Math.abs(v));

  function chip(v, fmtFn) {
    if (nz(v)) return `<span class="chip flat">--</span>`;
    const k = v > 0 ? "up" : v < 0 ? "down" : "flat";
    return `<span class="chip ${k}">${(fmtFn || sgnPct)(v)}</span>`;
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  /* ------------------------------------------------------------------ api */
  async function api(path, opts) {
    const r = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts));
    const body = await r.json().catch(() => ({ error: "malformed response from " + path }));
    if (!r.ok) throw new Error(body.error || ("HTTP " + r.status));
    return body;
  }
  let toastTimer = null;
  function toast(msg, kind) {
    $$(".toast").forEach((t) => t.remove());
    const t = document.createElement("div");
    t.className = "toast" + (kind === "err" ? " err" : "");
    t.textContent = msg;
    document.body.appendChild(t);
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.remove(), kind === "err" ? 6000 : 3400);
  }
  /* A render exception must never leave a blank tab: surface it where the
   * content should have been.  (An `Infinity` in one JSON payload once wiped
   * the entire Journal view with no visible error at all.) */
  function guard(hostSel, fn) {
    try { fn(); }
    catch (e) {
      const host = $(hostSel);
      if (host) host.insertAdjacentHTML("afterbegin",
        `<div class="card"><div class="empty"><div class="big">!</div>
         <h4>Could not render this view</h4><p>${esc(e.message)}</p></div></div>`);
      console.error(e);
    }
  }
  function skeleton(host, rows) {
    host.innerHTML = Array.from({ length: rows || 6 },
      () => `<div class="skel skel-row"></div>`).join("");
  }
  function emptyState(title, body, icon) {
    return `<div class="empty"><div class="big">${icon || "○"}</div>
      <h4>${esc(title)}</h4><p>${body}</p></div>`;
  }

  function tile(host, k, v, s, klass, opts) {
    opts = opts || {};
    const d = document.createElement("div");
    d.className = "tile" + (opts.accent ? " accent" : "");
    d.innerHTML = `<div class="k">${k}</div>` +
      `<div class="v ${klass || ""} ${opts.small ? "sm" : ""}">${v}</div>` +
      (s ? `<div class="s">${s}</div>` : "") +
      (opts.spark ? `<div class="spark-slot"></div>` : "");
    host.appendChild(d);
    if (opts.spark && opts.spark.length > 1)
      C.sparkline($(".spark-slot", d), opts.spark, { width: 120, height: 22 });
    return d;
  }

  /* --------------------------------------------------------------- routing */
  const VIEWS = ["dashboard", "trades", "symbol", "chain", "flow", "journal", "tools"];
  function show(view) {
    if (!VIEWS.includes(view)) view = "dashboard";
    state.current = view;
    $$("#tabs button").forEach((b) => b.setAttribute("aria-current", String(b.dataset.view === view)));
    $$("section.view").forEach((s) => s.classList.toggle("active", s.id === "view-" + view));
    const needsSym = view === "symbol" || view === "chain";
    history.replaceState(null, "", "#" + view + (needsSym && state.sym ? "/" + state.sym : ""));
    ({
      dashboard: loadOverview, trades: loadIdeas, symbol: () => loadSymbol(state.sym),
      chain: () => loadChain(state.sym), flow: loadFlow, journal: loadJournal, tools: loadStatus,
    }[view] || (() => { }))();
  }
  $("#tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-view]");
    if (b) show(b.dataset.view);
  });

  /* ----------------------------------------------------------- dashboard */
  const WL_COLS = [
    { k: "symbol", t: "Symbol", f: (r) => `<span class="sym">${r.symbol}</span>` },
    { k: "_spark", t: "30d", sortable: false, f: () => `<span class="spark-cell"></span>` },
    { k: "spot", t: "Spot", f: (r) => `<div class="cell-stack"><span>${F.num(r.spot, 2)}</span>
        <small class="${cls(r.change_pct)}">${sgn(r.change_pct, 2, "%")}</small></div>` },
    { k: "iv30", t: "IV 30d", f: (r) => pct(r.iv30) },
    { k: "vol_forecast", t: "Forecast", f: (r) => pct(r.vol_forecast) },
    {
      k: "vol_edge", t: "Vol edge", f: (r) => nz(r.vol_edge) ? "--"
        : `<div class="cell-stack"><span class="${cls(r.vol_edge)}">${sgn(r.vol_edge * 100, 0, "%")}</span>
           <span class="mbar"><i style="width:${Math.min(Math.abs(r.vol_edge) * 200, 100)}%;background:${r.vol_edge > 0 ? "var(--up)" : "var(--down)"}"></i></span></div>`
    },
    { k: "iv_rank", t: "IV rk", f: (r) => nz(r.iv_rank) ? `<span class="dim">--</span>` : F.num(r.iv_rank, 0) },
    { k: "hv_rank", t: "HV rk", f: (r) => F.num(r.hv_rank, 0) },
    { k: "term_slope", t: "Term", f: (r) => `<span class="${cls(r.term_slope)}">${sgn(r.term_slope, 2)}</span>` },
    { k: "skew_25", t: "Skew", f: (r) => nz(r.skew_25) ? "--" : sgn(r.skew_25 * 100, 1) },
    {
      k: "trend_score", t: "Trend", f: (r) => nz(r.trend_score) ? "--"
        : `<div class="cell-stack"><span class="${cls(r.trend_score)}">${sgn(r.trend_score, 2)}</span>
           <small>${esc(r.trend_label || "")}</small></div>`
    },
    { k: "rsi", t: "RSI", f: (r) => F.num(r.rsi, 0) },
    { k: "opt_volume", t: "Opt vol", f: (r) => F.compact(r.opt_volume) },
    { k: "opt_dollar_volume", t: "Premium", f: (r) => usdC(r.opt_dollar_volume) },
    { k: "pcr_vol", t: "P/C", f: (r) => nz(r.pcr_vol) ? "--" : `<span class="${r.pcr_vol > 1 ? "neg" : "pos"}">${F.num(r.pcr_vol, 2)}</span>` },
    { k: "gex_total", t: "GEX", f: (r) => `<span class="${cls(r.gex_total)}">${F.compact(r.gex_total)}</span>` },
    { k: "news_score", t: "News", f: (r) => `<span class="${cls(r.news_score)}">${sgn(r.news_score, 2)}</span>` },
    { k: "days_to_earnings", t: "Earn", f: (r) => (nz(r.days_to_earnings) || r.days_to_earnings < 0) ? `<span class="dim">--</span>` : `<span class="${r.days_to_earnings <= 7 ? "warn" : ""}">${r.days_to_earnings}d</span>` },
    { k: "regime", t: "Regime", f: (r) => `<span class="regime-chip ${r.vol_regime || ""}"><span class="dot2"></span>${esc(r.regime || "--")}</span>` },
  ];

  async function loadOverview(force) {
    if (state.overview && !force) { renderOverview(); return; }
    if (!state.overview) skeleton($("#marketTiles"), 8);
    try {
      state.overview = await api("/api/overview");
    } catch (e) {
      $("#marketTiles").innerHTML = emptyState("Cannot reach the desk", esc(e.message), "!");
      return;
    }
    populateSymbolPickers();
    guard("#view-dashboard", renderOverview);
  }

  function renderOverview() {
    const o = state.overview;
    if (!o) return;
    const st = o.market_state || "closed";
    $("#marketPill").innerHTML = `<span class="status-dot ${st}"></span><span class="v">${st}</span>`;
    const m = o.macro || {};
    $("#vixPill").innerHTML = `<span class="k">VIX</span><span class="v">${F.num(m.vix, 2)}</span>` +
      (nz(m.vix_rank) ? "" : `<span class="d muted">rk ${F.num(m.vix_rank, 0)}</span>`);
    $("#spxPill").innerHTML = `<span class="k">SPX</span><span class="v">${F.num(m.spx, 0)}</span>`;

    const t = $("#marketTiles"); t.innerHTML = "";
    const tot = o.totals || {};
    const pcr = tot.call_volume ? tot.put_volume / tot.call_volume : null;
    const spy = (o.symbols || []).find((s) => s.symbol === "SPY");
    tile(t, "S&P 500", F.num(m.spx, 2), spy ? chip(spy.change_pct / 100) + " SPY today" : "",
      "", { accent: true, spark: spy ? spy.spark : null });
    tile(t, "VIX", F.num(m.vix, 2), nz(m.vix_rank) ? "30-day implied on SPX" : `rank ${F.num(m.vix_rank, 0)} of the last year`);
    tile(t, "Risk-free 3m", pct(m.risk_free_3m, 2), "from ^IRX — used for every forward");
    tile(t, "Names covered", String((o.symbols || []).length), "with a full chain snapshot");
    tile(t, "Contracts traded", F.compact(tot.opt_volume), "across the watchlist today");
    tile(t, "Premium traded", usdC(tot.opt_dollar_volume), "volume × mid × 100");
    tile(t, "Put/call volume", F.num(pcr, 2), nz(pcr) ? "" : (pcr > 1 ? "put-heavy" : "call-heavy"),
      nz(pcr) ? "" : (pcr > 1 ? "neg" : "pos"));
    const lr = o.last_run || {};
    tile(t, "Last run", (lr.finished || "--").slice(11, 16) || "--",
      lr.stats ? `${lr.stats.symbols} names · ${F.compact(lr.stats.contracts)} contracts · ${lr.stats.elapsed}s` : "not run yet");

    // ---- table
    const head = $("#wlHead"); head.innerHTML = "";
    WL_COLS.forEach((c) => {
      const th = document.createElement("th");
      th.textContent = c.t;
      if (c.sortable === false) { th.style.cursor = "default"; head.appendChild(th); return; }
      if (state.sort.key === c.k) th.className = "sorted" + (state.sort.dir > 0 ? " asc" : "");
      th.addEventListener("click", () => {
        state.sort = { key: c.k, dir: state.sort.key === c.k ? -state.sort.dir : -1 };
        renderOverview();
      });
      head.appendChild(th);
    });

    let rows = (o.symbols || []).slice();
    const q = ($("#wlFilter").value || "").trim().toUpperCase();
    if (q) rows = rows.filter((r) => r.symbol.includes(q));
    if ($("#wlOnlyEarnings").checked) rows = rows.filter((r) => !nz(r.days_to_earnings) && r.days_to_earnings >= 0 && r.days_to_earnings <= 30);
    if ($("#wlOnlyRich").checked) rows = rows.filter((r) => r.vol_regime === "rich");
    if ($("#wlOnlyCheap").checked) rows = rows.filter((r) => r.vol_regime === "cheap");
    const sk = state.sort.key, sd = state.sort.dir;
    rows.sort((a, b) => {
      const x = a[sk], y = b[sk];
      if (nz(x) && nz(y)) return a.symbol.localeCompare(b.symbol);
      if (nz(x)) return 1;
      if (nz(y)) return -1;
      if (typeof x === "string") return sd * x.localeCompare(y);
      return sd * (x - y);
    });
    $("#wlCount").textContent = `${rows.length} of ${(o.symbols || []).length} names`;

    const body = $("#wlBody");
    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="${WL_COLS.length}">${emptyState(
        "No symbols match", "Clear the filters, or run the pipeline from the Tools tab.", "⌕")}</td></tr>`;
      return;
    }
    body.innerHTML = rows.map((r) =>
      `<tr class="clickable" data-sym="${r.symbol}">` +
      WL_COLS.map((c) => `<td>${c.f(r)}</td>`).join("") + "</tr>").join("");
    body.querySelectorAll("tr[data-sym]").forEach((tr, i) => {
      tr.addEventListener("click", () => { state.sym = tr.dataset.sym; show("symbol"); });
      const cell = $(".spark-cell", tr);
      if (cell && rows[i].spark && rows[i].spark.length > 1)
        C.sparkline(cell, rows[i].spark, { width: 72, height: 20 });
    });

    // ---- charts
    const byEdge = rows.filter((r) => r.iv30 && r.vol_forecast)
      .sort((a, b) => (b.vol_edge || 0) - (a.vol_edge || 0)).slice(0, 24);
    C.barChart("#chartIvVsForecast", {
      title: "Implied vs forecast volatility",
      subtitle: "30-day implied against the jump-robust forecast of realised vol. A taller blue bar means the market is charging more than the stock has been moving.",
      height: 250, groups: byEdge.map((r) => ({ label: r.symbol, iv: r.iv30 * 100, fc: r.vol_forecast * 100 })),
      keys: ["iv", "fc"], keyLabels: ["Implied 30d", "Forecast realised"],
      colors: [C.SERIES(0), C.SERIES(1)],
      yFmt: (v) => F.num(v, 0) + "%", xLabel: "Symbol", xTickCount: 24,
    });
    const byTrend = rows.filter((r) => !nz(r.trend_score)).sort((a, b) => b.trend_score - a.trend_score);
    C.barChart("#chartTrendVsVol", {
      title: "Composite trend score",
      subtitle: "Moving-average stack, MACD, RSI, ADX direction, momentum and relative strength — each normalised by the stock's own volatility so a 5% move in TLT is not read like a 5% move in MSTR.",
      height: 250, groups: byTrend.map((r) => ({ label: r.symbol, value: r.trend_score, extra: r.trend_label })),
      keys: ["value"], diverging: true, posColor: C.SERIES(0), negColor: C.SERIES(7),
      posLabel: "Uptrend", negLabel: "Downtrend",
      yFmt: (v) => F.num(v, 1), xLabel: "Symbol", xTickCount: 24,
    });

    $("#marketNews").innerHTML = (o.market_news || []).map(newsRow).join("") ||
      emptyState("No headlines cached", "The news stage runs with the pipeline.", "◷");
  }

  function newsRow(n) {
    const s = n.sentiment || 0;
    const k = s > 0.15 ? "up" : s < -0.15 ? "down" : "";
    const lab = s > 0.15 ? "positive" : s < -0.15 ? "negative" : "neutral";
    return `<div class="news-item ${k}"><div class="rail"></div><div>
      <div class="t"><a href="${esc(n.link || "#")}" target="_blank" rel="noopener">${esc(n.title)}</a></div>
      <div class="m"><span class="src">${esc(n.source || "")}</span>
      <span>${esc((n.published || "").replace("T", " ").slice(0, 16))}</span>
      <span class="${cls(s)}">${lab} ${sgn(s, 2)}</span>
      ${n.tags ? `<span class="dim">${esc(n.tags)}</span>` : ""}</div></div></div>`;
  }

  ["wlFilter", "wlOnlyEarnings", "wlOnlyRich", "wlOnlyCheap"].forEach((id) =>
    $("#" + id).addEventListener("input", () => guard("#view-dashboard", renderOverview)));

  /* -------------------------------------------------------------- symbols */
  function populateSymbolPickers() {
    const syms = (state.overview.symbols || []).map((s) => s.symbol);
    if ((!state.sym || !syms.includes(state.sym)) && syms.length) state.sym = syms[0];
    ["#symPick", "#chainSym"].forEach((sel) => {
      $(sel).innerHTML = syms.map((s) => `<option${s === state.sym ? " selected" : ""}>${s}</option>`).join("");
    });
  }
  $("#symPick").addEventListener("change", (e) => { state.sym = e.target.value; loadSymbol(state.sym); });
  $("#chainSym").addEventListener("change", (e) => { state.sym = e.target.value; loadChain(state.sym); });
  $("#symToChain").addEventListener("click", () => show("chain"));

  async function loadSymbol(sym) {
    if (!sym) { if (!state.overview) await loadOverview(); sym = state.sym; }
    if (!sym) return;
    $("#symPick").value = sym;
    const tok = ++state.token.symbol;
    if (!state.symbol || state.symbol.symbol !== sym) skeleton($("#symTiles"), 4);
    let d;
    try { d = await api("/api/symbol/" + encodeURIComponent(sym)); }
    catch (e) {
      if (tok !== state.token.symbol) return;
      $("#symTiles").innerHTML = emptyState("No data for " + esc(sym),
        esc(e.message) + " — run the pipeline for this name.", "!");
      return;
    }
    if (tok !== state.token.symbol) return;   // a newer request already won
    state.symbol = d;
    guard("#view-symbol", () => renderSymbol(d));
    try {
      const ch = await api("/api/chain/" + encodeURIComponent(sym));
      if (tok === state.token.symbol) renderSymbolChainCharts(ch, d);
    } catch (e) { /* chain charts are a bonus, not a requirement */ }
  }

  function renderSymbol(d) {
    const m = d.metrics, det = d.detail || {};
    $("#symAsof").textContent = "snapshot " + (m.ts || "").replace("T", " ").slice(0, 16) + " UTC";

    const t = $("#symTiles"); t.innerHTML = "";
    const spark = (d.bars || []).slice(-30).map((b) => b.close);
    tile(t, d.symbol + " spot", F.num(m.spot, 2),
      chip(det.change_pct / 100) + " today", "", { accent: true, spark });
    tile(t, "IV 30d", pct(m.iv30), `60d ${pct(m.iv60)} · 90d ${pct(m.iv90)}`);
    tile(t, "Forecast realised", pct(m.vol_forecast),
      `HV20 ${pct(m.hv20)} · jumps ${pct(m.jump_share, 0)} of variance`);
    tile(t, "Vol edge", sgnPct(m.vol_edge, 0),
      m.vol_regime ? "implied is " + m.vol_regime : "", cls(m.vol_edge));
    tile(t, "IV rank", nz(m.iv_rank) ? "--" : F.num(m.iv_rank, 0),
      m.iv_samples ? `${m.iv_samples} session${m.iv_samples === 1 ? "" : "s"} of history` : "building history");
    tile(t, "HV rank", F.num(m.hv_rank, 0), "vs 1y of realised vol");
    tile(t, "Trend", sgn(m.trend_score, 2), esc(m.trend_label || ""), cls(m.trend_score));
    tile(t, "25-delta skew", nz(m.skew_25) ? "--" : sgn(m.skew_25 * 100, 1, " pts"), "put IV minus call IV");
    tile(t, "Options volume", F.compact(m.opt_volume),
      `OI ${F.compact(m.opt_oi)} · P/C ${F.num(m.pcr_vol, 2)}`);
    tile(t, "Premium traded", usdC(m.opt_dollar_volume),
      nz(m.opt_rvol) ? "needs a few days of history" : `${F.num(m.opt_rvol, 1)}× its own average`);
    tile(t, "Net dealer gamma", F.compact(m.gex_total),
      nz(m.gamma_flip) ? "" : `flips at ${F.num(m.gamma_flip, 2)}`, cls(m.gex_total));
    tile(t, "Max pain", F.num(m.max_pain, 2), "~30-day expiry, where OI concentrates");
    if (m.earnings_date)
      tile(t, "Earnings", m.earnings_date,
        (nz(m.days_to_earnings) ? "" : m.days_to_earnings + " days") +
        (m.implied_earnings_move ? ` · market prices a ${pct(m.implied_earnings_move)} move` : ""),
        (!nz(m.days_to_earnings) && m.days_to_earnings <= 7) ? "warn" : "");

    // price chart
    const bars = d.bars || [];
    const closes = bars.map((b) => b.close);
    const ma = (n) => closes.map((_, i) => i < n - 1 ? null :
      closes.slice(i - n + 1, i + 1).reduce((a, b) => a + b, 0) / n);
    const levels = [];
    if (m.support) levels.push({ price: m.support, label: "S " + F.num(m.support, 2), color: C.SERIES(2) });
    if (m.resistance) levels.push({ price: m.resistance, label: "R " + F.num(m.resistance, 2), color: C.SERIES(1) });
    C.priceChart("#chartPrice", {
      title: d.symbol + " — daily price and volume",
      subtitle: "Support and resistance are pivot clusters weighted by touches and recency. Volume shares the x-axis in its own panel — never a second y-scale on the price plot.",
      height: 380, bars: bars.slice(-260),
      overlays: [
        { label: "SMA 20", values: ma(20).slice(-260), color: C.SERIES(0) },
        { label: "SMA 50", values: ma(50).slice(-260), color: C.SERIES(1) },
        { label: "SMA 200", values: ma(200).slice(-260), color: C.SERIES(2) },
      ],
      levels,
    });

    const exps = (d.expiries || []).filter((e) => e.dte > 0);
    const term = exps.filter((e) => e.dte <= 400);
    C.lineChart("#chartTerm", {
      title: "Volatility term structure",
      subtitle: "Model-free vol integrates the whole strike strip (VIX-style); ATM vol is just the money. A downward slope means an event is priced into the front.",
      height: 240,
      series: [
        { label: "Model-free", points: term.filter((e) => e.mfiv).map((e) => ({ x: e.dte, y: e.mfiv * 100 })), dots: true, color: C.SERIES(0) },
        { label: "ATM", points: term.filter((e) => e.atm_iv).map((e) => ({ x: e.dte, y: e.atm_iv * 100 })), dots: true, color: C.SERIES(1) },
      ],
      refs: m.vol_forecast ? [{ y: m.vol_forecast * 100, label: "forecast realised", color: C.SERIES(2) }] : [],
      xFmt: (v) => F.num(v, 0) + "d", yFmt: (v) => F.num(v, 0) + "%",
      xLabel: "Days to expiry", directLabels: false,
    });

    C.barChart("#chartExpMove", {
      title: "Expected move by expiry",
      subtitle: "One standard deviation implied by the model-free vol, as a per cent of spot. This is the range the options market is charging for.",
      height: 240,
      groups: exps.filter((e) => !nz(e.em_pct)).slice(0, 12).map((e) => ({
        label: e.expiry.slice(5), value: e.em_pct * 100,
        extra: `${F.num(e.dte, 0)} DTE · straddle ${F.num(e.straddle, 2)}`,
      })),
      keys: ["value"], colors: [C.SERIES(0)],
      yFmt: (v) => F.num(v, 1) + "%", xLabel: "Expiry", xTickCount: 12,
    });

    const gp = det.gamma_profile || [];
    C.lineChart("#chartGamma", {
      title: "Dealer gamma profile",
      subtitle: "Net gamma re-priced at each spot level. Above the flip dealers dampen moves; below it they amplify them.",
      height: 240,
      series: [{ label: "Net gamma exposure", points: gp.map((p) => ({ x: p.spot, y: p.gex })), color: C.SERIES(0), area: true }],
      refs: [
        { x: m.spot, label: "spot", color: "var(--text-secondary)" },
        m.gamma_flip ? { x: m.gamma_flip, label: "flip", color: C.SERIES(1) } : null,
      ].filter(Boolean),
      xFmt: (v) => F.num(v, 0), yFmt: (v) => F.compact(v),
      tipFmt: (v) => "$" + F.compact(v) + " per 1%",
      xLabel: "Spot", yZero: true, directLabels: false,
    });

    const ivh = (d.iv_history || []).filter((r) => r.iv30);
    C.lineChart("#chartIvHist", {
      title: "Implied vs realised — this desk's own history",
      subtitle: ivh.length < 5 ? "One point per session. IV rank becomes meaningful after roughly 40 of them."
        : "Each point is the last snapshot of that session.",
      height: 240,
      series: [
        { label: "IV 30d", points: ivh.map((r, i) => ({ x: i, y: r.iv30 * 100 })), color: C.SERIES(0) },
        { label: "HV 20d", points: ivh.filter((r) => r.hv20).map((r, i) => ({ x: i, y: r.hv20 * 100 })), color: C.SERIES(1) },
        { label: "Forecast", points: ivh.filter((r) => r.vol_forecast).map((r, i) => ({ x: i, y: r.vol_forecast * 100 })), color: C.SERIES(2), dash: "4 3" },
      ],
      xFmt: (v) => (ivh[Math.round(v)] || {}).asof_date || "",
      yFmt: (v) => F.num(v, 0) + "%", xLabel: "Session", directLabels: false,
      emptyMessage: "Only one snapshot so far. Run the pipeline daily and this becomes the IV-rank series.",
    });

    // expiry table
    const cols = [["Expiry", "expiry"], ["DTE", "dte"], ["Forward", "forward"], ["ATM IV", "atm_iv"],
    ["Model-free", "mfiv"], ["25d skew", "skew_25"], ["Exp move", "em_pct"], ["Straddle", "straddle"],
    ["Max pain", "max_pain"], ["Call wall", "call_wall"], ["Put wall", "put_wall"],
    ["P/C vol", "pcr_vol"], ["P/C OI", "pcr_oi"], ["Volume", "total_volume"], ["OI", "total_oi"]];
    $("#expTable").innerHTML = "<thead><tr>" + cols.map((c) => `<th>${c[0]}</th>`).join("") +
      "</tr></thead><tbody>" + exps.map((e) => "<tr>" + cols.map(([, k]) => {
        const v = e[k];
        if (k === "expiry") return `<td>${v}</td>`;
        if (k === "dte") return `<td>${F.num(v, 0)}</td>`;
        if (["atm_iv", "mfiv", "em_pct"].includes(k)) return `<td>${pct(v)}</td>`;
        if (k === "skew_25") return `<td class="${cls(v)}">${nz(v) ? "--" : sgn(v * 100, 1)}</td>`;
        if (["total_volume", "total_oi"].includes(k)) return `<td>${F.compact(v)}</td>`;
        return `<td>${F.num(v, 2)}</td>`;
      }).join("") + "</tr>").join("") + "</tbody>";

    const rv = det.realised || {};
    $("#volDetail").innerHTML = [
      ["Close-to-close 10d", pct(rv.hv10)], ["Close-to-close 20d", pct(rv.hv20)],
      ["Close-to-close 60d", pct(rv.hv60)],
      ["Yang-Zhang 20d", pct(rv.yz20)], ["Garman-Klass 20d", pct(rv.gk20)],
      ["Parkinson 20d", pct(rv.park20)], ["Rogers-Satchell 20d", pct(rv.rs20)],
      ["Bipower (jump-robust)", pct(rv.bp20)], ["Winsorised 20d", pct(rv.trim20)],
      ["EWMA (winsorised)", pct(rv.ewma)],
      ["Jump share of variance", pct(rv.jump_share, 0)],
      ["__split", ""],
      ["Forecast used", pct(m.vol_forecast), true],
      ["Implied 30d", pct(m.iv30), true],
      ["Variance risk premium", nz(m.vrp) ? "--" : sgn(m.vrp * 100, 1, " pts"), true],
    ].map(([k, v, strong]) => k === "__split" ? `<div class="kv-split"></div>`
      : `<dt class="${strong ? "strong" : ""}">${k}</dt><dd class="${strong ? "strong" : ""}">${v}</dd>`).join("");

    const tc = det.trend_components || {};
    $("#trendDetail").innerHTML = [
      ["MA stack", sgn(tc.ma_stack, 2)], ["Price vs 200d", sgn(tc.price_vs_200, 2)],
      ["MACD", sgn(tc.macd, 2)], ["RSI signal", sgn(tc.rsi, 2)], ["ADX direction", sgn(tc.adx_dir, 2)],
      ["Momentum", sgn(tc.roc, 2)], ["Relative strength", sgn(tc.rel_strength, 2)],
      ["__split", ""],
      ["Composite", sgn(m.trend_score, 2), true],
      ["ADX", F.num(m.adx, 1)], ["ATR %", pct(m.atr_pct)],
      ["52-week position", pct(det.pos_52w, 0)],
      ["Relative volume", nz(m.rvol) ? "--" : F.num(m.rvol, 2) + "×"],
      ["News score", `${sgn(m.news_score, 2)} (${m.news_count || 0} articles)`],
    ].map(([k, v, strong]) => k === "__split" ? `<div class="kv-split"></div>`
      : `<dt class="${strong ? "strong" : ""}">${k}</dt><dd class="${strong ? "strong" : ""}">${v}</dd>`).join("");

    $("#symNews").innerHTML = (d.news || []).slice(0, 18).map(newsRow).join("") ||
      emptyState("No news cached", "for " + esc(d.symbol), "◷");

    const ih = $("#symIdeas"); ih.innerHTML = "";
    (d.ideas || []).forEach((i, n) => ih.appendChild(ideaCard(i, n === 0)));
    if (!(d.ideas || []).length)
      ih.innerHTML = emptyState("No ideas today", "Nothing on this name cleared the liquidity, expectancy and sizing filters.", "○");
  }

  function renderSymbolChainCharts(ch, sym) {
    const strikes = (ch.strikes || []).filter((s) => (s.call && s.call.iv) || (s.put && s.put.iv));
    const spot = ch.spot;
    const smile = strikes.map((s) => {
      const src = s.strike >= spot ? s.call : s.put;
      const quote = s.strike >= spot ? (s.call || {}).iv_src : (s.put || {}).iv_src;
      return src && src.iv ? { strike: s.strike, iv: src.iv, quote } : null;
    }).filter(Boolean).filter((p) => p.strike > spot * 0.7 && p.strike < spot * 1.3);
    C.lineChart("#chartSmile", {
      title: "Volatility smile — " + ch.expiry,
      subtitle: "The fitted surface against the raw vendor quotes it was fitted to. Only out-of-the-money quotes inform the fit: a deep in-the-money option is nearly all intrinsic, so inverting its price for vol amplifies quote noise.",
      height: 240,
      series: [
        { label: "Fitted surface", points: smile.map((p) => ({ x: p.strike, y: p.iv * 100 })), color: C.SERIES(0) },
        { label: "Vendor quote", points: smile.filter((p) => p.quote).map((p) => ({ x: p.strike, y: p.quote * 100 })), color: C.SERIES(1), width: 1, dash: "3 3" },
      ],
      refs: [{ x: spot, label: "spot", color: "var(--text-secondary)" }],
      xFmt: (v) => F.num(v, 0), yFmt: (v) => F.num(v, 0) + "%", xLabel: "Strike", directLabels: false,
    });

    const oi = strikes.filter((s) => s.strike > spot * 0.82 && s.strike < spot * 1.18);
    C.barChart("#chartOI", {
      title: "Open interest by strike — " + ch.expiry,
      subtitle: "Where the existing positions sit. The largest strike on each side is the wall dealers hedge around.",
      height: 240,
      groups: oi.map((s) => ({
        label: String(s.strike),
        calls: (s.call || {}).open_interest || 0,
        puts: (s.put || {}).open_interest || 0,
        extra: `call vol ${F.compact((s.call || {}).volume)} · put vol ${F.compact((s.put || {}).volume)}`,
      })),
      keys: ["calls", "puts"], keyLabels: ["Call OI", "Put OI"],
      colors: [C.SERIES(0), C.SERIES(1)],
      refs: [{ index: oi.findIndex((s) => s.strike >= spot), label: "spot" }],
      yFmt: (v) => F.compact(v), xLabel: "Strike", xTickCount: 10,
    });
  }

  $("#symRefresh").addEventListener("click", async () => {
    if (!state.sym) return;
    toast("Refreshing " + state.sym + "…");
    try {
      await api("/api/refresh", { method: "POST", body: JSON.stringify({ symbols: [state.sym], fresh: true }) });
      setTimeout(() => { state.symbol = null; loadSymbol(state.sym); }, 9000);
    } catch (e) { toast(e.message, "err"); }
  });

  /* ---------------------------------------------------------------- ideas */
  async function loadIdeas(force) {
    if (state.ideas && !force) { guard("#view-trades", renderIdeas); return; }
    skeleton($("#ideaList"), 5);
    try { state.ideas = await api("/api/ideas"); }
    catch (e) { $("#ideaList").innerHTML = emptyState("Cannot load the board", esc(e.message), "!"); return; }
    $("#ideaDate").innerHTML = (state.ideas.dates || []).map((d) =>
      `<option${d === state.ideas.asof_date ? " selected" : ""}>${d}</option>`).join("");
    const strats = [...new Set((state.ideas.ideas || []).map((i) => i.label))].sort();
    $("#ideaStrat").innerHTML = `<option value="">all</option>` +
      strats.map((s) => `<option>${esc(s)}</option>`).join("");
    guard("#view-trades", renderIdeas);
  }

  function renderIdeas() {
    let list = (state.ideas.ideas || []).slice();
    const s = $("#ideaStrat").value, dir = $("#ideaDir").value;
    if (s) list = list.filter((i) => i.label === s);
    if (dir) list = list.filter((i) => i.direction === dir);
    if ($("#ideaDefinedOnly").checked) list = list.filter((i) => !nz(i.max_loss));
    const budget = (state.status && state.status.config)
      ? state.status.config.account_size * state.status.config.risk_per_trade_pct : 500;
    if ($("#ideaAffordable").checked) list = list.filter((i) => (i.risk_dollars || 0) <= budget * 1.05);

    const sort = $("#ideaSort").value;
    const key = {
      score: (i) => -i.score, ev_per_risk: (i) => -(i.ev_per_risk || 0),
      pop: (i) => -(i.pop || 0), risk: (i) => (i.risk_dollars || 1e9), dte: (i) => (i.dte || 0),
    }[sort] || ((i) => -i.score);
    list.sort((a, b) => key(a) - key(b));

    $("#ideaCount").textContent = `${list.length} idea${list.length === 1 ? "" : "s"}`;
    renderExposure(state.ideas.exposure || {});
    const host = $("#ideaList"); host.innerHTML = "";
    if (!list.length) {
      host.innerHTML = `<div class="card">${emptyState("Nothing matches",
        "Loosen the filters, or run the pipeline to build a fresh board.", "○")}</div>`;
      return;
    }
    list.forEach((i, n) => host.appendChild(ideaCard(i, n === 0)));
  }
  ["ideaStrat", "ideaDir", "ideaDefinedOnly", "ideaAffordable", "ideaSort"].forEach((id) =>
    $("#" + id).addEventListener("input", () => guard("#view-trades", renderIdeas)));
  $("#ideaDate").addEventListener("change", async (e) => {
    state.ideas = await api("/api/ideas?date=" + encodeURIComponent(e.target.value));
    guard("#view-trades", renderIdeas);
  });

  /* Board-level exposure. Per-idea scores are blind to it: nothing stops the
   * board being twenty bullish trades at once, and over 2026-08-13..19 every
   * board ran 15-20 bullish of 25 at roughly +3 net delta, which is where most
   * of the drawdown came from -- not from picking bad individual trades. */
  function renderExposure(x) {
    const host = $("#boardExposure");
    if (!host) return;
    if (!x || !x.n) { host.innerHTML = ""; return; }
    const mix = x.direction_mix || {};
    const parts = Object.keys(mix).sort().map((k) =>
      `<span class="badge ${k}">${k} ${mix[k]}</span>`).join(" ");
    host.innerHTML = `
      <div class="tiles" style="grid-template-columns:repeat(auto-fit,minmax(140px,1fr))">
        <div class="tile"><div class="k">Board direction</div>
          <div class="v sm ${x.bullish_pct >= 65 ? "warn" : ""}">${F.num(x.bullish_pct, 0)}% bullish</div>
          <div class="s">${parts}</div></div>
        <div class="tile"><div class="k">Net delta</div>
          <div class="v sm ${cls(x.net_delta)}">${sgn(x.net_delta, 2)}</div>
          <div class="s">taking every idea at 1×</div></div>
        <div class="tile"><div class="k">Net theta / vega</div>
          <div class="v sm">${usd(x.net_theta, 0)}<span class="dim"> / </span>${usd(x.net_vega, 0)}</div>
          <div class="s">per day · per vol point</div></div>
        <div class="tile"><div class="k">Credit vs debit</div>
          <div class="v sm">${x.credit}<span class="dim"> / </span>${x.debit}</div>
          <div class="s">short vs long premium</div></div>
        <div class="tile"><div class="k">Total risk</div>
          <div class="v sm">${usdC(x.total_risk)}</div>
          <div class="s">whole board at 1× each</div></div>
        <div class="tile"><div class="k">Repeated from ${x.prev_board || "prior board"}</div>
          <div class="v sm ${(x.repeat_from_prev || []).length > 6 ? "warn" : ""}">${(x.repeat_from_prev || []).length} names</div>
          <div class="s">${(x.repeat_from_prev || []).slice(0, 8).join(" ") || "none"}</div></div>
      </div>
      ${x.skew_warning ? `<ul class="warnings" style="margin-top:10px"><li>${esc(x.skew_warning)}</li></ul>` : ""}`;
  }

  function gauge(score) {
    const v = Math.max(0, Math.min(100, score || 0));
    const r = 19, circ = 2 * Math.PI * r;
    const color = v >= 75 ? "var(--up)" : v >= 60 ? "var(--series-1)" : v >= 45 ? "var(--warning)" : "var(--muted)";
    return `<div class="gauge">
      <svg width="46" height="46" viewBox="0 0 46 46" aria-hidden="true">
        <circle cx="23" cy="23" r="${r}" fill="none" stroke="var(--surface-3)" stroke-width="4"/>
        <circle cx="23" cy="23" r="${r}" fill="none" stroke="${color}" stroke-width="4"
                stroke-linecap="round" stroke-dasharray="${circ}"
                stroke-dashoffset="${circ * (1 - v / 100)}"/>
      </svg>
      <div class="val">${F.num(v, 0)}</div><div class="cap">score</div></div>`;
  }

  function probMeter(p) {
    if (nz(p)) return "";
    return `<span class="prob"><span class="track"><i style="width:${Math.round(p * 100)}%"></i></span>${pct(p, 0)}</span>`;
  }

  function ideaCard(i, openIt) {
    const card = document.createElement("div");
    card.className = "idea" + (openIt ? " open" : "");
    const risk = nz(i.max_loss) ? null : Math.abs(i.max_loss) * 100;
    const reward = nz(i.max_profit) ? null : i.max_profit * 100;
    const g = i.greeks || {};
    card.innerHTML = `
      <div class="idea-head">
        <div class="rankbox">${i.rank || "•"}</div>
        <div class="idea-title">
          <div class="l1">
            <span class="sym">${i.symbol}</span>
            <b>${esc(i.label)}</b>
            <span class="badge ${i.credit_debit}">${i.credit_debit}</span>
            <span class="badge ${i.direction}">${esc(i.direction)}</span>
            <span class="badge plain">${i.expiry} · ${F.num(i.dte, 0)}d</span>
            ${(i.warnings || []).length ? `<span class="badge warn">${i.warnings.length} caution${i.warnings.length > 1 ? "s" : ""}</span>` : ""}
          </div>
          <div class="l2">${esc(i.rationale || "")}</div>
        </div>
        <div class="idea-nums">
          <div class="big">${i.credit_debit === "credit" ? "credit" : "debit"} ${usd(Math.abs(i.entry_price) * 100, 0)}</div>
          <div class="sub">POP ${pct(i.pop, 0)} · risk ${risk == null ? "undefined" : usd(risk, 0)}</div>
        </div>
        ${gauge(i.score)}
      </div>
      <div class="idea-body">
        <div class="idea-cols">
          <div>
            <p class="section-title">Structure</p>
            <table class="legs">
              <thead><tr><th>Action</th><th>Expiry</th><th>Strike</th><th>Type</th><th>Price</th><th>IV</th><th>Delta</th><th>OI</th><th>Liq</th></tr></thead>
              <tbody>${(i.legs || []).map((l) => `<tr>
                <td><span class="act ${l.qty > 0 ? "buy" : "sell"}">${l.action} ${Math.abs(l.qty)}</span></td>
                <td>${l.is_stock ? "&mdash;" : l.expiry}</td>
                <td>${l.is_stock ? "&mdash;" : F.num(l.strike, 2)}</td>
                <td>${l.is_stock ? "100 shares" : (l.right === "C" ? "Call" : "Put")}</td>
                <td>${F.num(l.price, 2)}</td>
                <td>${l.is_stock ? "&mdash;" : pct(l.iv)}</td>
                <td>${F.num(l.delta, 3)}</td>
                <td>${l.is_stock ? "&mdash;" : F.compact(l.oi)}</td>
                <td>${l.is_stock ? "&mdash;" : F.num(l.liquidity, 0)}</td></tr>`).join("")}</tbody>
            </table>

            <div class="tiles" style="margin-top:14px;grid-template-columns:repeat(auto-fit,minmax(112px,1fr))">
              <div class="tile"><div class="k">Max profit</div><div class="v sm pos">${reward == null ? "unbounded" : usd(reward, 0)}</div></div>
              <div class="tile"><div class="k">Max loss</div><div class="v sm neg">${risk == null ? "undefined" : usd(risk, 0)}</div></div>
              <div class="tile"><div class="k">Expectancy</div><div class="v sm ${cls(i.ev)}">${usd(i.ev * 100, 0)}</div><div class="s">${F.num(i.ev_per_risk, 1)}% of risk</div></div>
              <div class="tile"><div class="k">Worst 5%</div><div class="v sm neg">${usd(i.cvar5 * 100, 0)}</div><div class="s">conditional tail</div></div>
              <div class="tile"><div class="k">Breakeven</div><div class="v sm">${(i.breakevens || []).map((b) => F.num(b, 2)).join(" / ") || "--"}</div></div>
              <div class="tile"><div class="k">Suggested size</div><div class="v sm">${i.qty}×</div><div class="s">${usd(i.risk_dollars, 0)} at risk</div></div>
            </div>

            <dl class="kv" style="margin-top:14px">
              <dt>Net delta / gamma</dt><dd>${F.num(g.delta, 3)} / ${F.num(g.gamma, 4)}</dd>
              <dt>Theta per day</dt><dd class="${cls(g.theta)}">${usd((g.theta || 0) * 100, 2)}</dd>
              <dt>Vega per vol point</dt><dd>${usd((g.vega || 0) * 100, 2)}</dd>
              <dt>Implied / forecast vol</dt><dd>${pct(i.atm_iv)} / ${pct(i.forecast_vol)}</dd>
              <dt>Execution cost</dt><dd class="${cls(i.ev_q)}">${usd((i.ev_q || 0) * 100, 2)}</dd>
              <dt>Liquidity score</dt><dd>${F.num(i.liquidity, 0)} / 100</dd>
            </dl>
            ${(i.warnings || []).length ? `<ul class="warnings">${i.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
          </div>

          <div>
            <p class="section-title">Take profit &amp; stop</p>
            <div class="ladder">${ladderHtml(i)}</div>
          </div>
        </div>
        <div class="idea-payoff">
          <div class="chart-host" id="payoff-${i.id}"></div>
          <div class="row" style="margin:14px 0 0">
            <button class="btn primary" data-log="${i.id}">Log this trade</button>
            <button class="btn" data-sym2="${i.symbol}">Open ${i.symbol}</button>
            <button class="btn" data-chain="${i.symbol}|${i.expiry}">View chain</button>
          </div>
        </div>
      </div>`;

    $(".idea-head", card).addEventListener("click", () => {
      card.classList.toggle("open");
      if (card.classList.contains("open") && !card.dataset.drawn) {
        card.dataset.drawn = "1";
        drawPayoff(i);
      }
    });
    card.addEventListener("click", (e) => {
      const lg = e.target.closest("[data-log]");
      if (lg) { e.stopPropagation(); logTrade(i); return; }
      const s2 = e.target.closest("[data-sym2]");
      if (s2) { e.stopPropagation(); state.sym = s2.dataset.sym2; show("symbol"); return; }
      const ch = e.target.closest("[data-chain]");
      if (ch) {
        e.stopPropagation();
        const [sy, ex] = ch.dataset.chain.split("|");
        state.sym = sy; show("chain"); loadChain(sy, ex);
      }
    });
    if (openIt) setTimeout(() => { card.dataset.drawn = "1"; drawPayoff(i); }, 30);
    return card;
  }

  function ladderHtml(i) {
    const rows = (i.targets || []).map((t) => rungHtml(t, ""));
    if (i.stop) rows.push(rungHtml(i.stop, "stop"));
    if (i.time_stop) rows.push(`<div class="rung time"><div class="name">TIME</div>
      <div class="detail">${esc(i.time_stop.desc)}</div>
      <div class="val">${i.time_stop.date}</div></div>`);
    return rows.join("");
  }
  function rungHtml(t, extra) {
    let detail = esc(t.desc || "");
    if (!nz(t.spot_up) && !nz(t.spot_down)) {
      detail += ` — spot to <b>${F.num(t.spot_down, 2)}</b> or <b>${F.num(t.spot_up, 2)}</b>`;
    } else if (!nz(t.spot)) {
      detail += ` — spot <b>${F.num(t.spot, 2)}</b> (${sgn(t.spot_move_pct, 1, "%")})`;
    } else if (t.date) {
      detail += ` — by <b>${t.date}</b>${nz(t.days) ? "" : ` (${t.days}d)`}`;
      if (t.hold_range) {
        const [lo, hi] = t.hold_range;
        detail += ` while spot holds ${lo ? F.num(lo, 2) : ""}${lo && hi ? "–" : ""}${hi ? F.num(hi, 2) : ""}`;
      }
    }
    const dollars = t.pnl * 100;
    return `<div class="rung ${extra}">
      <div class="name">${t.name}</div>
      <div class="detail">${detail}</div>
      <div class="val ${dollars >= 0 ? "pos" : "neg"}">${sgn(dollars, 0)}
        <small>${probMeter(t.prob) || "at expiry"} · exit @ ${F.num(Math.abs(t.spread_price), 2)}</small>
      </div></div>`;
  }

  async function drawPayoff(i) {
    const host = "#payoff-" + i.id;
    if (!$(host)) return;
    let p;
    try { p = await api("/api/idea/" + i.id + "/payoff"); }
    catch (e) { if ($(host)) $(host).innerHTML = `<p class="chart-empty">${esc(e.message)}</p>`; return; }
    if (!$(host)) return;
    const markers = [];
    (p.targets || []).forEach((t) => {
      if (!nz(t.spot)) markers.push({ spot: t.spot, pnl: t.pnl, label: t.name, color: C.UP() });
      if (!nz(t.spot_up) && t.spot_up !== t.spot) markers.push({ spot: t.spot_up, pnl: t.pnl, label: t.name, color: C.UP() });
    });
    if (p.stop && !nz(p.stop.spot)) markers.push({ spot: p.stop.spot, pnl: p.stop.pnl, label: "STOP", color: C.DOWN() });
    C.payoffChart(host, {
      title: "Profit and loss profile",
      subtitle: `Solid line is expiry; the thinner lines are what the spread is worth today and at ${p.halfway_date}. Dollars are per spread.`,
      height: 300, spot: p.spot, breakevens: p.breakevens, markers,
      curves: [
        { label: "At expiry", points: p.at_expiry },
        { label: "Halfway (" + p.halfway_date + ")", points: p.halfway, color: C.SERIES(1), dash: "5 3" },
        { label: "Today", points: p.now, color: C.SERIES(2), dash: "2 3" },
      ],
    });
  }

  async function logTrade(i) {
    const qty = prompt(`How many spreads of ${i.symbol} ${i.label}?\nSuggested size is ${i.qty} (risking about ${usd(i.risk_dollars, 0)}).`, i.qty);
    if (qty === null) return;
    const px = prompt(`Fill price per spread (${i.credit_debit}). The model assumed ${Math.abs(i.entry_price).toFixed(2)}.`,
      Math.abs(i.entry_price).toFixed(2));
    if (px === null) return;
    const val = parseFloat(px);
    if (isNaN(val)) { toast("That is not a number", "err"); return; }
    const signed = i.credit_debit === "credit" ? -Math.abs(val) : Math.abs(val);
    try {
      await api("/api/trades", {
        method: "POST",
        body: JSON.stringify({ from_idea: i.id, qty: parseInt(qty, 10) || 1, entry_price: signed }),
      });
      toast("Logged to the journal");
      state.perf = null; state.trades = null;
    } catch (e) { toast(e.message, "err"); }
  }

  /* ---------------------------------------------------------------- chain */
  async function loadChain(sym, expiry) {
    if (!sym) { if (!state.overview) await loadOverview(); sym = state.sym; }
    if (!sym) return;
    $("#chainSym").value = sym;
    const tok = ++state.token.chain;
    let d;
    try { d = await api("/api/chain/" + encodeURIComponent(sym) + (expiry ? "?expiry=" + encodeURIComponent(expiry) : "")); }
    catch (e) {
      if (tok !== state.token.chain) return;
      $("#chainTable").innerHTML = `<tbody><tr><td>${emptyState("No chain for " + esc(sym), esc(e.message), "!")}</td></tr></tbody>`;
      return;
    }
    if (tok !== state.token.chain) return;
    state.chain = d;
    $("#chainExp").innerHTML = (d.expiries || []).map((e) =>
      `<option value="${e.expiry}"${e.expiry === d.expiry ? " selected" : ""}>${e.expiry} · ${F.num(e.dte, 0)}d</option>`).join("");
    guard("#view-chain", renderChain);
  }
  $("#chainExp").addEventListener("change", (e) => loadChain(state.sym, e.target.value));
  ["chainRange", "chainCols"].forEach((id) =>
    $("#" + id).addEventListener("change", () => guard("#view-chain", renderChain)));

  function renderChain() {
    const d = state.chain;
    if (!d) return;
    const m = d.metrics || {};
    $("#chainMeta").textContent = `spot ${F.num(d.spot, 2)} · snapshot ${(d.asof || "").replace("T", " ").slice(0, 16)} UTC`;

    const t = $("#chainTiles"); t.innerHTML = "";
    tile(t, "ATM implied vol", pct(m.atm_iv), `model-free ${pct(m.mfiv)}`, "", { accent: true });
    tile(t, "Expected move", nz(m.expected_move) ? "--" : "±" + F.num(m.expected_move, 2), pct(m.em_pct) + " of spot");
    tile(t, "Straddle", F.num(m.straddle, 2), pct(m.straddle_pct) + " of spot");
    tile(t, "25-delta skew", nz(m.skew_25) ? "--" : sgn(m.skew_25 * 100, 1, " pts"),
      `25d put ${pct(m.iv_25p)} vs call ${pct(m.iv_25c)}`);
    // Annualising a forward-vs-spot gap over a few days amplifies quote noise
    // into a double-digit "dividend yield" — only show it once the tenor can
    // carry the number.
    const divTxt = (m.t_years || 0) >= 0.15 ? ` · implied div ${pct(m.div_yield, 2)}` : "";
    tile(t, "Forward", F.num(m.forward, 2),
      `${esc(m.parity_method || "")}${divTxt} · carry ${sgn((m.forward / d.spot - 1) * 100, 2, "%")}`);
    tile(t, "Max pain", F.num(m.max_pain, 2), `call wall ${F.num(m.call_wall, 2)} · put wall ${F.num(m.put_wall, 2)}`);
    tile(t, "Volume", F.compact(m.total_volume), `P/C ${F.num(m.pcr_vol, 2)}`);
    tile(t, "Open interest", F.compact(m.total_oi), `P/C ${F.num(m.pcr_oi, 2)}`);
    if (m.arb_flags) tile(t, "Surface warnings", esc(m.arb_flags), "quality flags on the fitted smile", "warn");

    const mode = $("#chainCols").value;
    const band = parseFloat($("#chainRange").value);
    const rows = (d.strikes || []).filter((s) => Math.abs(s.strike / d.spot - 1) <= band);
    const SETS = {
      core: [["iv", "IV", (v) => pct(v)], ["delta", "Delta", (v) => F.num(v, 3)],
      ["volume", "Vol", (v) => F.compact(v)], ["open_interest", "OI", (v) => F.compact(v)],
      ["bid", "Bid", (v) => F.num(v, 2)], ["ask", "Ask", (v) => F.num(v, 2)]],
      greeks: [["iv", "IV", (v) => pct(v)], ["delta", "Δ", (v) => F.num(v, 3)],
      ["gamma", "Γ", (v) => F.num(v, 4)], ["theta", "Θ", (v) => F.num(v, 3)],
      ["vega", "V", (v) => F.num(v, 3)], ["vanna", "Vanna", (v) => F.num(v, 4)],
      ["vomma", "Vomma", (v) => F.num(v, 4)], ["charm", "Charm", (v) => F.num(v, 4)],
      ["mid", "Mid", (v) => F.num(v, 2)]],
      flow: [["volume", "Vol", (v) => F.compact(v)], ["open_interest", "OI", (v) => F.compact(v)],
      ["vol_oi", "V/OI", (v) => F.num(v, 2)], ["mid", "Mid", (v) => F.num(v, 2)],
      ["spread_pct", "Spr", (v) => pct(v, 0)], ["liquidity", "Liq", (v) => F.num(v, 0)],
      ["extrinsic", "Extrin", (v) => F.num(v, 2)]],
    };
    const set = SETS[mode];
    const maxOI = Math.max(1, ...rows.map((s) => Math.max((s.call || {}).open_interest || 0, (s.put || {}).open_interest || 0)));

    const head =
      `<thead>
         <tr><th class="side-head calls" colspan="${set.length}">CALLS</th>
             <th class="side-head" style="border:0"></th>
             <th class="side-head puts" colspan="${set.length}">PUTS</th></tr>
         <tr>${set.map((c) => `<th>${c[1]}</th>`).reverse().join("")}
             <th style="text-align:center">Strike</th>
             ${set.map((c) => `<th>${c[1]}</th>`).join("")}</tr>
       </thead>`;

    const body = "<tbody>" + rows.map((s) => {
      const atm = Math.abs(s.strike / d.spot - 1) < 0.006;
      const cellsFor = (side, itm) => set.map(([k, , f]) => {
        const o = s[side] || {};
        const v = o[k];
        const klass = itm ? "itm" : "";
        if (k === "open_interest" && v) {
          const w = (v / maxOI) * 100;
          const color = side === "call" ? "var(--series-1)" : "var(--series-2)";
          return `<td class="${klass} bar-cell"><div class="fill ${side === "call" ? "right" : ""}" style="width:${w}%;background:${color}"></div><span>${f(v)}</span></td>`;
        }
        return `<td class="${klass}">${nz(v) ? "--" : f(v)}</td>`;
      });
      return `<tr class="${atm ? "atm" : ""}" data-strike="${s.strike}">` +
        cellsFor("call", s.strike < d.spot).reverse().join("") +
        `<td class="strike">${F.num(s.strike, 2)}</td>` +
        cellsFor("put", s.strike > d.spot).join("") + "</tr>";
    }).join("") + "</tbody>";

    $("#chainTable").innerHTML = head + body;
  }

  /* ----------------------------------------------------------------- flow */
  async function loadFlow(force) {
    if (state.flow && !force) { guard("#view-flow", renderFlow); return; }
    try { state.flow = await api("/api/flow"); } catch (e) { toast(e.message, "err"); return; }
    guard("#view-flow", renderFlow);
  }
  function renderFlow() {
    const f = state.flow;
    const L = f.leaders || [];
    $("#flowLeaders").innerHTML =
      "<thead><tr><th>Symbol</th><th>Spot</th><th>Contracts</th><th>Open interest</th><th>Premium traded</th>" +
      "<th>Rel volume</th><th>Call vol</th><th>Put vol</th><th>P/C</th><th>IV 30d</th></tr></thead><tbody>" +
      L.map((r) => `<tr class="clickable" data-sym="${r.symbol}">
        <td class="sym">${r.symbol}</td><td>${F.num(r.spot, 2)}</td>
        <td>${F.compact(r.volume)}</td><td>${F.compact(r.oi)}</td>
        <td>${usdC(r.dollar_volume)}</td>
        <td>${nz(r.rvol) ? `<span class="dim">--</span>` : F.num(r.rvol, 2) + "×"}</td>
        <td>${F.compact(r.call_volume)}</td><td>${F.compact(r.put_volume)}</td>
        <td class="${(r.pcr_vol || 0) > 1 ? "neg" : "pos"}">${F.num(r.pcr_vol, 2)}</td>
        <td>${pct(r.iv30)}</td></tr>`).join("") + "</tbody>";

    C.barChart("#chartCallPut", {
      title: "Call versus put contract volume",
      subtitle: "Raw contract counts by symbol, sorted by total activity. Puts outweighing calls is the classic hedging or bearish-positioning tell.",
      height: 260,
      groups: L.slice(0, 22).map((r) => ({ label: r.symbol, calls: r.call_volume, puts: r.put_volume })),
      keys: ["calls", "puts"], keyLabels: ["Calls", "Puts"],
      colors: [C.SERIES(0), C.SERIES(1)], yFmt: (v) => F.compact(v), xLabel: "Symbol", xTickCount: 22,
    });

    const U = f.unusual || [];
    $("#flowUnusual").innerHTML =
      "<thead><tr><th>Symbol</th><th>Contract</th><th>DTE</th><th>Vs spot</th><th>Volume</th>" +
      "<th>Open interest</th><th>Vol/OI</th><th>Premium</th><th>IV</th><th>Delta</th><th>Side</th></tr></thead><tbody>" +
      (U.length ? U.map((u) => `<tr class="clickable" data-sym="${u.symbol}">
        <td class="sym">${u.symbol}</td>
        <td>${u.expiry} ${u.right === "C" ? "Call" : "Put"} ${F.num(u.strike, 2)}</td>
        <td>${F.num(u.dte, 0)}</td>
        <td class="${cls(u.moneyness_pct)}">${sgn(u.moneyness_pct, 1, "%")}</td>
        <td>${F.compact(u.volume)}</td><td>${F.compact(u.open_interest)}</td>
        <td><b>${F.num(u.vol_oi, 1)}×</b></td>
        <td>${usdC(u.dollar_volume)}</td>
        <td>${pct(u.iv)}</td><td>${F.num(u.delta, 2)}</td>
        <td class="dim">${esc(u.side_hint || "--")}</td></tr>`).join("")
        : `<tr><td colspan="11">${emptyState("Nothing unusual", "No contract cleared the $25k premium and volume-over-OI filters in this snapshot.", "○")}</td></tr>`) +
      "</tbody>";

    $$("#view-flow tr[data-sym]").forEach((tr) =>
      tr.addEventListener("click", () => { state.sym = tr.dataset.sym; show("symbol"); }));
  }

  /* -------------------------------------------------------------- journal */
  async function loadJournal(force) {
    if (state.perf && !force) { guard("#view-journal", renderJournal); return; }
    try {
      state.perf = await api("/api/performance");
      state.trades = await api("/api/trades?status=" + $("#tradeStatus").value);
    } catch (e) { toast(e.message, "err"); return; }
    guard("#view-journal", renderJournal);
  }
  $("#tradeStatus").addEventListener("change", () => loadJournal(true));

  function renderJournal() {
    const p = state.perf.journal, sc = state.perf.scanner;
    const o = p.overall;
    const t = $("#journalTiles"); t.innerHTML = "";
    tile(t, "Realised P&L", usd(o.total, 0), `${o.n} closed trade${o.n === 1 ? "" : "s"}`, cls(o.total), { accent: true });
    tile(t, "Win rate", nz(o.win_rate) ? "--" : F.num(o.win_rate, 0) + "%",
      nz(o.avg_win) ? "" : `avg win ${usd(o.avg_win, 0)} · avg loss ${usd(o.avg_loss, 0)}`);
    tile(t, "Profit factor",
      nz(o.profit_factor) ? (o.no_losses ? "∞" : "--") : F.num(o.profit_factor, 2),
      o.no_losses ? "no losing trades yet" : "gross wins / gross losses");
    tile(t, "Expectancy", usd(o.expectancy, 0), "per trade", cls(o.expectancy));
    tile(t, "Max drawdown", usd(p.max_drawdown, 0), "peak to trough on closed P&L", p.max_drawdown < 0 ? "neg" : "");
    tile(t, "Open positions", String(p.open_count), `marked P&L ${usd(p.open_pnl, 0)}`, cls(p.open_pnl));
    const g = p.open_greeks || {};
    tile(t, "Book delta", F.num(g.delta, 1), `theta ${usd(g.theta, 0)}/day · vega ${usd(g.vega, 0)}`);
    tile(t, "Scanner ideas tracked", String(sc.n),
      sc.overall && sc.overall.n ? `marked P&L ${usd(sc.overall.total, 0)}` : "forward test starts after one day");

    const tr = (state.trades || {}).trades || [];
    $("#tradeTable").innerHTML =
      "<thead><tr><th>Sym</th><th>Structure</th><th>Opened</th><th>Expiry</th><th>Qty</th>" +
      "<th>Entry</th><th>Mark</th><th>P&L</th><th>P&L %</th><th>Status</th><th></th></tr></thead><tbody>" +
      (tr.length ? tr.map((r) => {
        const mk = r.mark || {};
        const pnl = r.status === "closed" ? r.pnl : mk.pnl;
        const pp = r.status === "closed" ? r.pnl_pct : mk.pnl_pct;
        return `<tr>
          <td class="sym">${r.symbol}</td><td>${esc((r.strategy || "").replace(/_/g, " "))}</td>
          <td>${r.opened_date}</td><td>${r.expiry || "--"}</td><td>${r.qty}</td>
          <td>${F.num(r.entry_price, 2)}</td>
          <td>${r.status === "closed" ? F.num(r.exit_price, 2) : F.num(mk.mark, 2)}</td>
          <td class="${cls(pnl)}">${usd(pnl, 0)}</td>
          <td class="${cls(pp)}">${sgn(pp, 1, "%")}</td>
          <td><span class="badge ${r.status === "open" ? "debit" : "plain"}">${r.status}</span></td>
          <td>${r.status === "open" ? `<button class="btn-mini" data-close="${r.id}">Close</button> ` : ""}
              <button class="btn-mini danger" data-del="${r.id}">Delete</button></td></tr>`;
      }).join("") : `<tr><td colspan="11">${emptyState("No trades logged",
        "Open an idea on the Daily Trades tab and press &ldquo;Log this trade&rdquo;.", "○")}</td></tr>`) +
      "</tbody>";

    C.lineChart("#chartEquity", {
      title: "Closed-trade equity curve",
      subtitle: "Cumulative realised P&L in dollars, in the order trades were closed.",
      height: 240, yZero: true,
      series: [{ label: "Cumulative P&L", points: (p.equity_curve || []).map((e, i) => ({ x: i, y: e.cum })), color: C.SERIES(0), area: true, dots: (p.equity_curve || []).length < 40 }],
      // Label by trade ordinal, not date: several trades closed on one day
      // would otherwise repeat the same date across every tick.
      xFmt: (v) => {
        const e = (p.equity_curve || [])[Math.round(v)];
        return e ? `#${Math.round(v) + 1} ${e.symbol}` : "";
      },
      yFmt: (v) => "$" + F.compact(v), xLabel: "Trade", directLabels: false,
      emptyMessage: "Close a trade and the realised equity curve starts here.",
    });

    const bs = Object.entries(p.by_strategy || {});
    C.barChart("#chartByStrategy", {
      title: "Realised P&L by structure",
      subtitle: "Which structures have actually paid, not which felt good.",
      height: 240,
      groups: bs.map(([k, v]) => ({ label: k.replace(/_/g, " "), value: v.total, extra: `${v.n} trades · ${F.num(v.win_rate, 0)}% wins` })),
      keys: ["value"], diverging: true, posColor: C.UP(), negColor: C.DOWN(),
      posLabel: "Profitable", negLabel: "Losing",
      yFmt: (v) => "$" + F.compact(v), xLabel: "Structure", xTickCount: 12,
    });

    const cal = sc.calibration || [];
    C.lineChart("#chartCalibration", {
      title: "Scanner calibration",
      subtitle: "Stated probability of profit against how often those ideas were actually in the money. Points on the diagonal mean the model's confidence is honest.",
      height: 260, allowSingle: true,
      series: [
        { label: "Perfect calibration", points: [{ x: 0, y: 0 }, { x: 100, y: 100 }], color: "var(--muted)", dash: "4 4", width: 1, dots: false },
        { label: "Observed", points: cal.map((c) => ({ x: c.predicted, y: c.realised })), color: C.SERIES(0), dots: true },
      ],
      yDomain: [0, 100], xTicks: [0, 25, 50, 75, 100],
      xFmt: (v) => F.num(v, 0) + "%", yFmt: (v) => F.num(v, 0) + "%",
      xLabel: "Predicted POP", directLabels: false,
    });

    if (sc.calibration_note) {
      const cal = $("#chartCalibration");
      if (cal) cal.insertAdjacentHTML("beforeend",
        `<p class="hint" style="margin-top:8px">${esc(sc.calibration_note)}</p>`);
    }
    const rows = sc.rows || [];
    $("#scannerTable").innerHTML =
      "<thead><tr><th>Date</th><th>Sym</th><th>Structure</th><th>Score</th><th>POP</th><th>P&L</th><th>Hit</th></tr></thead><tbody>" +
      (rows.length ? rows.slice(0, 60).map((r) => `<tr>
        <td>${r.asof_date}</td><td class="sym">${r.symbol}</td><td>${esc((r.strategy || "").replace(/_/g, " "))}</td>
        <td>${F.num(r.score, 0)}</td><td>${pct(r.pop, 0)}</td>
        <td class="${cls(r.pnl)}">${usd(r.pnl, 0)}</td><td>${esc(r.hit_target || "--")}</td></tr>`).join("")
        : `<tr><td colspan="7">${emptyState("Forward test is empty",
          "It fills in from the second pipeline run onward.", "◷")}</td></tr>`) + "</tbody>";
  }

  // Delegated ONCE at init.  Re-attaching inside renderJournal with {once:true}
  // stacked a fresh listener on every render, so a single click fired every
  // pending handler — deleting a trade twice, or opening two prompts.
  $("#tradeTable").addEventListener("click", async (e) => {
    const c = e.target.closest("[data-close]"), d = e.target.closest("[data-del]");
    try {
      if (c) {
        const px = prompt("Exit price per spread (debit positive, credit negative):");
        if (px === null) return;
        const val = parseFloat(px);
        if (isNaN(val)) { toast("That is not a number", "err"); return; }
        const reason = prompt("Reason (target / stop / time / manual):", "target") || "manual";
        await api("/api/trades/" + c.dataset.close, {
          method: "PATCH", body: JSON.stringify({ action: "close", exit_price: val, reason }),
        });
        toast("Position closed");
        loadJournal(true);
      } else if (d) {
        if (!confirm("Delete this trade and all of its marks?")) return;
        await api("/api/trades/" + d.dataset.del, { method: "DELETE" });
        toast("Trade deleted");
        loadJournal(true);
      }
    } catch (err) { toast(err.message, "err"); }
  });

  /* ---------------------------------------------------------------- tools */
  async function calc(solve) {
    const body = {
      spot: +$("#cSpot").value, strike: +$("#cStrike").value, days: +$("#cDays").value,
      rate: +$("#cRate").value / 100, dividend: +$("#cDiv").value / 100,
      right: $("#cRight").value,
    };
    if (solve) { body.price = +$("#cPrice").value; body.solve_iv = true; }
    else body.iv = +$("#cIv").value / 100;
    let r;
    try { r = await api("/api/calculator", { method: "POST", body: JSON.stringify(body) }); }
    catch (e) { toast(e.message, "err"); return; }
    if (solve) $("#cIv").value = (r.iv * 100).toFixed(2);
    const g = r.greeks, t = $("#calcOut"); t.innerHTML = "";
    tile(t, "Theoretical price", F.num(r.price, 3), "per share", "", { accent: true });
    tile(t, "Implied vol", pct(r.iv), "annualised");
    tile(t, "Delta", F.num(g.delta, 4), "per $1 of spot");
    tile(t, "Gamma", F.num(g.gamma, 5), "delta per $1");
    tile(t, "Theta", F.num(g.theta, 4), "per calendar day");
    tile(t, "Vega", F.num(g.vega, 4), "per 1 vol point");
    tile(t, "Rho", F.num(g.rho, 4), "per 1% rate");
    tile(t, "Vanna", F.num(g.vanna, 5), "vega per $1 of spot");
    tile(t, "Vomma", F.num(g.vomma, 5), "vega per vol point");
    tile(t, "Charm", F.num(g.charm, 5), "delta per day");
    tile(t, "P(finish ITM)", pct(r.prob_itm, 1), "risk-neutral");
    tile(t, "P(touch strike)", pct(r.prob_touch, 1), "at any point before expiry");
    tile(t, "Expected move", "±" + F.num(r.expected_move.move, 2),
      `${F.num(r.expected_move.low, 2)} to ${F.num(r.expected_move.high, 2)}`);
    tile(t, "Breakeven", F.num(r.breakeven, 2), "at expiry");
    tile(t, "Forward", F.num(r.forward, 3), "spot carried to expiry");
  }
  $("#cCalc").addEventListener("click", () => calc(false));
  $("#cSolve").addEventListener("click", () => calc(true));
  $("#cLoad").addEventListener("click", () => {
    const s = (state.overview && state.overview.symbols || []).find((x) => x.symbol === state.sym);
    if (!s) { toast("Pick a symbol first", "err"); return; }
    $("#cSpot").value = F.num(s.spot, 2);
    $("#cStrike").value = F.num(s.spot, 0);
    $("#cIv").value = nz(s.iv30) ? 30 : (s.iv30 * 100).toFixed(1);
    const mac = (state.overview.macro || {});
    if (!nz(mac.risk_free_3m)) $("#cRate").value = (mac.risk_free_3m * 100).toFixed(2);
    toast("Loaded " + s.symbol);
    calc(false);
  });

  async function loadStatus() {
    let s;
    try { s = await api("/api/status"); } catch (e) { return; }
    state.status = s;
    $("#logoutBtn").hidden = !s.auth_enabled;
    const db = s.db || {}, lr = s.last_run || {};
    $("#statusKv").innerHTML = [
      ["Market", esc(s.market_state)], ["Server time (ET)", esc(s.server_time_et)],
      ["Last run", esc((lr.finished || "never").replace("T", " ").slice(0, 19))],
      ["Symbols in last run", (lr.stats || {}).symbols || "--"],
      ["Run duration", (lr.stats || {}).elapsed ? (lr.stats.elapsed + "s") : "--"],
      ["__split", ""],
      ["Contract quotes stored", F.compact(db.contract_quote)],
      ["Expiry snapshots", F.compact(db.expiry_metrics)],
      ["Symbol snapshots", F.compact(db.symbol_metrics)],
      ["Price bars", F.compact(db.ohlc)],
      ["News articles", F.compact(db.news)],
      ["Ideas recorded", F.compact(db.idea)],
      ["Trades logged", F.compact(db.trade)],
      ["Database size", F.compact(db._bytes) + "B"],
      ["HTTP cache", `${(s.http_cache || {}).files || 0} files · ${F.compact((s.http_cache || {}).bytes)}B`],
      ["__split", ""],
      ["Password protection", s.auth_enabled
        ? `<span class="pos">on</span>`
        : `<span class="warn">off — run <span class="mono">set_password.py</span></span>`],
      ["Watchlist size", (s.config || {}).watchlist_size],
      ["Account / risk per trade",
        `$${F.compact((s.config || {}).account_size)} / ${pct((s.config || {}).risk_per_trade_pct, 0)}`],
      ["Scan window", ((s.config || {}).scan_dte || []).join("–") + " days"],
      ["Refreshing now", (s.refreshing || []).join(", ") || "nothing"],
    ].map(([k, v]) => k === "__split" ? `<div class="kv-split"></div>`
      : `<dt>${k}</dt><dd>${v == null ? "--" : v}</dd>`).join("");
  }

  async function runPipeline(fresh, symbols) {
    try {
      await api("/api/refresh", { method: "POST", body: JSON.stringify({ fresh: !!fresh, symbols: symbols || null }) });
      toast("Pipeline started — this page keeps serving the previous snapshot");
      $$("main .card").forEach((c) => c.classList.add("refreshing"));
      let tries = 0;
      const poll = setInterval(async () => {
        tries++;
        const s = await api("/api/status").catch(() => null);
        if (!s) return;
        if (!(s.refreshing || []).length || tries > 90) {
          clearInterval(poll);
          $$("main .card").forEach((c) => c.classList.remove("refreshing"));
          state.overview = state.ideas = state.flow = state.perf = state.symbol = state.chain = null;
          toast("Refresh complete");
          show(state.current);
          loadStatus();
        }
      }, 4000);
    } catch (e) { toast(e.message, "err"); }
  }
  $("#fullRefresh").addEventListener("click", () => runPipeline(false));
  $("#fullRefreshFresh").addEventListener("click", () => runPipeline(true));
  $("#refreshBtn").addEventListener("click", () => runPipeline(false));

  /* ------------------------------------------------------- command palette */
  let palette = null, paletteIdx = 0, paletteRows = [];
  function openPalette() {
    if (palette) return;
    palette = document.createElement("div");
    palette.className = "palette-backdrop";
    palette.innerHTML = `<div class="palette">
      <input type="text" placeholder="Jump to a symbol…" aria-label="Jump to a symbol">
      <div class="palette-list"></div>
      <div class="palette-hint"><span><kbd>↑</kbd><kbd>↓</kbd> navigate</span>
        <span><kbd>enter</kbd> open</span><span><kbd>esc</kbd> close</span></div></div>`;
    document.body.appendChild(palette);
    const input = $("input", palette);
    const list = $(".palette-list", palette);
    const all = (state.overview && state.overview.symbols) || [];
    const draw = (q) => {
      q = (q || "").trim().toUpperCase();
      paletteRows = all.filter((s) => !q || s.symbol.includes(q)).slice(0, 40);
      paletteIdx = 0;
      list.innerHTML = paletteRows.map((s, i) => `
        <div class="palette-item" data-i="${i}" aria-selected="${i === 0}">
          <span class="sym">${s.symbol}</span>
          <span class="dim">${esc(s.regime || "")}</span>
          <span class="meta">${F.num(s.spot, 2)} · IV ${pct(s.iv30, 0)}</span>
        </div>`).join("") ||
        `<div class="palette-item dim">No match</div>`;
    };
    draw("");
    input.addEventListener("input", () => draw(input.value));
    const move = (d) => {
      if (!paletteRows.length) return;
      paletteIdx = (paletteIdx + d + paletteRows.length) % paletteRows.length;
      $$(".palette-item", list).forEach((el, i) => el.setAttribute("aria-selected", String(i === paletteIdx)));
      const el = list.children[paletteIdx];
      if (el) el.scrollIntoView({ block: "nearest" });
    };
    const pick = () => {
      const s = paletteRows[paletteIdx];
      if (!s) return;
      closePalette();
      state.sym = s.symbol;
      show(state.current === "chain" ? "chain" : "symbol");
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
      else if (e.key === "Enter") { e.preventDefault(); pick(); }
      else if (e.key === "Escape") { e.preventDefault(); closePalette(); }
    });
    list.addEventListener("click", (e) => {
      const it = e.target.closest("[data-i]");
      if (it) { paletteIdx = +it.dataset.i; pick(); }
    });
    palette.addEventListener("click", (e) => { if (e.target === palette) closePalette(); });
    input.focus();
  }
  function closePalette() { if (palette) { palette.remove(); palette = null; } }
  $("#searchBtn").addEventListener("click", openPalette);

  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target.tagName || "")) ;
    if (e.key === "Escape") { closePalette(); return; }
    if (typing) return;
    if (e.key === "/" || ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k")) {
      e.preventDefault(); openPalette(); return;
    }
    const n = parseInt(e.key, 10);
    if (n >= 1 && n <= VIEWS.length) { show(VIEWS[n - 1]); }
  });

  /* ---------------------------------------------------------------- theme */
  // ?theme=light|dark overrides the stored preference, so a themed view can be
  // linked or screenshotted without touching local storage.
  const urlTheme = new URLSearchParams(location.search).get("theme");
  const savedTheme = (urlTheme === "light" || urlTheme === "dark")
    ? urlTheme : (localStorage.getItem("td-theme") || "dark");
  document.documentElement.setAttribute("data-theme", savedTheme);
  $("#themeBtn").addEventListener("click", () => {
    const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("td-theme", next);
    // Charts read colours from CSS custom properties, so re-render on swap.
    if (state.overview) guard("#view-dashboard", renderOverview);
    if (state.current === "symbol" && state.symbol) loadSymbol(state.sym);
    if (state.current === "flow" && state.flow) guard("#view-flow", renderFlow);
    if (state.current === "journal" && state.perf) guard("#view-journal", renderJournal);
    if (state.current === "trades" && state.ideas) guard("#view-trades", renderIdeas);
  });

  /* ----------------------------------------------------------------- boot */
  const hash = (location.hash || "#dashboard").slice(1).split("/");
  if (hash[1]) state.sym = decodeURIComponent(hash[1]).toUpperCase();
  loadStatus();
  loadOverview().then(() => show(hash[0] || "dashboard"));
})();
