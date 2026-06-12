/* optimize-lab workbench — a VIEWER over engine output, not a second engine.
 *
 * Renders ResultsReport JSON + precomputed variant bundles produced by the
 * Python engine (scripts/precompute_web.py). Computes NO queue physics:
 * every number on screen is the engine's own, or it is absent and says so.
 * The only client-side arithmetic is presentation (increments between two
 * displayed numbers, sign comparisons, formatting at the declared display
 * precision of 2 decimals / 1 decimal for percentages).
 *
 * Data loads via <script> injection (works from GitHub Pages AND file://).
 * Zero dependencies, no build step.
 */
"use strict";

/* ------------------------------------------------------------------ const */
const LEVERS = [
  { id: "matching", name: "Focus matching & routing", warn: false,
    desc: c => `Focus each employee where they beat the team average; route by blended score (preset: ${esc(c.weights.preset || "custom")}); aging cap ${c.lever_config.matching.aging_cap_min} min.` },
  { id: "appointment_smoothing", name: "Appointment smoothing", warn: false,
    desc: c => `Shift walk-in surge demand into evenly spread appointment slots, up to ${pct0(c.lever_config.appointment_smoothing.target_appointment_share)} share.` },
  { id: "prep_in_queue", name: "Prep-in-queue", warn: true,
    caveat: "Assumption-flagged: duration reduction and incomplete-catch rates are estimates — validate against real visit anatomy per customer before quoting externally.",
    desc: c => `Pre-arrival intake shortens service ${pct0(c.lever_config.prep_in_queue.duration_reduction)} and catches ${pct0(c.lever_config.prep_in_queue.incomplete_reduction)} of would-be incompletes.` },
  { id: "deflection", name: "Deflection", warn: true,
    caveat: "Assumption-flagged: the digitally-resolved share is an estimate — validate per customer. Reported separately, never blended into served.",
    desc: c => `${pct0(c.lever_config.deflection.rate)} of visits resolved digitally before arrival — reported separately, never blended into served.` },
  { id: "running_late", name: "Running Late", warn: false,
    desc: c => `Converts ${pct0(c.lever_config.running_late.no_show_reduction)} of would-be appointment no-shows into served visits (VE.11.01).` },
  { id: "break_scheduling", name: "Break scheduling", warn: false,
    desc: () => "Grid-searched break stagger fitted to the day's demand shape." },
];
const CANON = LEVERS.map(l => l.id);
const COLOR = { gray: "#9ca3af", teal: "#0d9488", red: "#dc2626",
                slate: "#334155", amber: "#92400e" };
const MODEL_FLAGS = "v1.4 model assumptions (always active): promise range_k = 0.15 and early-side accuracy beta_early = 0.1 are ⚠ estimates until VE.12.01 ratings + forecast logs allow fitting.";

/* ------------------------------------------------------------------ state */
const S = { mode: null, key: null, data: null, report: null, toggles: new Set() };

/* ------------------------------------------------------------------ utils */
const $ = id => document.getElementById(id);
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const isNum = v => typeof v === "number" && isFinite(v);
const f2 = v => isNum(v) ? v.toFixed(2) : "—";
const f1 = v => isNum(v) ? v.toFixed(1) : "—";
const sgn1 = v => isNum(v) ? (v > 0 ? "+" : "") + v.toFixed(1) : "—";
const pct0 = v => isNum(v) ? Math.round(v * 100) + "%" : "—";
const pctOf = v => isNum(v) ? (v * 100).toFixed(1) + "%" : "—";
function setKey(toggles) {
  const on = CANON.filter(l => toggles.has(l));
  return on.length ? on.join("+") : "none";
}
function banner(html, cls) { return `<div class="banner ${cls}">${html}</div>`; }

/* ------------------------------------------------------------- data load */
function injectScript(src) {
  return new Promise((res, rej) => {
    const el = document.createElement("script");
    el.src = src;
    el.onload = res;
    el.onerror = () => rej(new Error("failed to load " + src));
    document.head.appendChild(el);
  });
}

async function selectPreset(key) {
  const entry = (window.OPTLAB_INDEX || []).find(e => e.key === key);
  if (!entry) { fatal(`Preset "${esc(key)}" not in data/index.js.`); return; }
  if (!(window.OPTLAB_DATA && window.OPTLAB_DATA[key])) {
    try { await injectScript(entry.bundle); }
    catch (e) { fatal(esc(e.message) + " — run scripts/precompute_web.py."); return; }
  }
  const bundle = window.OPTLAB_DATA[key];
  S.mode = "bundle"; S.key = key; S.data = bundle; S.report = bundle.report;
  S.toggles = new Set(bundle.manifest.levers_enabled);   // default: combined
  renderAll();
}

function loadReportObject(obj, sourceName) {
  if (obj && obj.manifest && obj.report && obj.variants) {  // a full bundle
    S.mode = "bundle"; S.key = obj.manifest.preset || sourceName;
    S.data = obj; S.report = obj.report;
    S.toggles = new Set(obj.manifest.levers_enabled);
    renderAll(); return;
  }
  for (const k of ["meta", "metrics", "waterfall"]) {
    if (!obj || !obj[k]) {
      fatal(`Loaded file is not a schema-valid ResultsReport: missing "${k}". Nothing rendered — the workbench never guesses.`);
      return;
    }
  }
  S.mode = "report"; S.key = sourceName; S.data = null; S.report = obj;
  S.toggles = new Set();
  renderAll();
}

function handleFile(file) {
  const reader = new FileReader();
  reader.onload = () => {
    try {
      let text = reader.result;
      if (/\.js$/i.test(file.name)) {
        const m = text.match(/=\s*(\{[\s\S]*\});?\s*$/);
        if (!m) throw new Error("no bundle object found in .js file");
        text = m[1];
      }
      loadReportObject(JSON.parse(text), file.name);
    } catch (e) {
      fatal("Could not parse " + esc(file.name) + ": " + esc(e.message));
    }
  };
  reader.readAsText(file);
}

function fatal(msg) {
  $("banner").innerHTML = banner("⛔ " + msg, "red");
  ["lever-panel", "scoreboard", "charts", "analysis", "focus", "export"]
    .forEach(id => { $(id).innerHTML = ""; });
}

/* -------------------------------------------------------------- integrity */
function integrityProblems() {
  if (S.mode !== "bundle") return [];
  const { manifest, report, variants } = S.data;
  const probs = [];
  if (!manifest || !report || !variants) {
    probs.push("bundle is missing manifest/report/variants");
    return probs;
  }
  if (!manifest.engine_version) probs.push("manifest has no engine_version");
  if (manifest.scenario_name !== report.meta.scenario_name)
    probs.push(`manifest scenario "${esc(manifest.scenario_name)}" ≠ report scenario "${esc(report.meta.scenario_name)}"`);
  if (manifest.random_seed !== report.meta.random_seed)
    probs.push("manifest/report random_seed mismatch");
  if (manifest.monte_carlo_days !== report.meta.monte_carlo_days)
    probs.push("manifest/report monte_carlo_days mismatch");
  if (!manifest.golden_sha256) probs.push("manifest has no golden_sha256");
  for (const v of manifest.variants || []) {
    if (!variants[v.key]) probs.push(`variant "${esc(v.key)}" listed in manifest but absent from bundle`);
  }
  return probs;
}

/* ----------------------------------------------------------- view helpers */
function currentVariant() {
  if (S.mode !== "bundle") return null;
  return S.data.variants[setKey(S.toggles)] || null;
}
function baselineVariant() {
  return S.mode === "bundle" ? S.data.variants["none"] : null;
}
// In report-only mode, synthesize Before/After rows from metricComparisons.
function reportRows() {
  const m = S.report.metrics;
  const pick = k => m[k] || {};
  return {
    before: {
      mean_wait_min: pick("mean_wait_min").baseline, p90_wait_min: pick("p90_wait_min").baseline,
      served_per_day: pick("served_per_day").baseline, resolved_digitally_per_day: 0,
      turned_away_per_day: pick("turned_away_per_day").baseline,
      abandoned_per_day: pick("abandoned_per_day").baseline,
      mean_csat: pick("mean_csat").baseline, csat_5pt: (m.csat_5pt || {}).baseline,
      incomplete_time_cost_min_per_day: null, makespan_min: pick("makespan_min").baseline,
    },
    after: {
      mean_wait_min: pick("mean_wait_min").optimized, p90_wait_min: pick("p90_wait_min").optimized,
      served_per_day: pick("served_per_day").optimized,
      resolved_digitally_per_day: m.resolved_digitally_per_day,
      turned_away_per_day: pick("turned_away_per_day").optimized,
      abandoned_per_day: pick("abandoned_per_day").optimized,
      mean_csat: pick("mean_csat").optimized, csat_5pt: (m.csat_5pt || {}).optimized,
      incomplete_time_cost_min_per_day: m.incomplete_time_cost_min_per_day,
      makespan_min: pick("makespan_min").optimized,
    },
    deltas: {
      mean_wait: pick("mean_wait_min"), p90_wait: pick("p90_wait_min"),
      served: pick("served_per_day"), turned_away: pick("turned_away_per_day"),
      abandoned: pick("abandoned_per_day"), mean_csat: pick("mean_csat"),
    },
    punctBefore: (m.appointment_punctuality || {}).baseline,
    punctAfter: (m.appointment_punctuality || {}).optimized,
  };
}

/* ------------------------------------------------------------- rendering */
function renderAll() {
  const probs = integrityProblems();
  const warnHtml = (S.report.guardrail_warnings || [])
    .map(w => banner("⚠ GUARDRAIL WARNING — " + esc(w.message), "amber")).join("");
  if (probs.length) {
    $("banner").innerHTML =
      banner("⛔ INTEGRITY CHECK FAILED — numbers withheld. " +
             probs.map(esc).join("; "), "red");
    ["lever-panel", "scoreboard", "charts", "analysis", "focus", "export"]
      .forEach(id => { $(id).innerHTML = ""; });
    renderMeta(true);
    renderFooter();
    return;
  }
  $("banner").innerHTML = warnHtml;
  renderMeta(false);
  renderLevers();
  const cur = S.mode === "bundle" ? currentVariant() : "report";
  if (S.mode === "bundle" && !cur) {
    renderNotPrecomputed();
  } else {
    renderScoreboard();
  }
  renderCharts(cur);
  renderAnalysis();
  renderFocus();
  renderExport();
  renderFooter();
}

function renderMeta(failed) {
  const meta = S.report.meta;
  let line = `<b>${esc(meta.scenario_name)}</b>` +
    (meta.customer ? ` · ${esc(meta.customer)}` : "") +
    ` · seed ${esc(meta.random_seed)} · ${esc(meta.monte_carlo_days)} MC days` +
    ` · engine <span class="mono">${esc(S.mode === "bundle" ? S.data.manifest.engine_version : meta.engine_version)}</span>`;
  if (S.mode === "bundle") {
    line += failed
      ? ` · <span style="color:var(--red)">integrity: FAILED</span>`
      : ` · <span class="ok">integrity: manifest↔report verified · golden sha ${esc(S.data.manifest.golden_sha256.slice(0, 12))}…</span>`;
  } else {
    line += ` · single-report mode (loaded file: ${esc(S.key)})`;
  }
  $("meta-line").innerHTML = line;
}

function renderLevers() {
  if (S.mode === "report") {
    const last = S.report.waterfall[S.report.waterfall.length - 1] || {};
    const active = last.levers_active || [];
    $("lever-panel").innerHTML =
      `<h3>Levers in this report</h3>` +
      active.map(l => {
        const def = LEVERS.find(x => x.id === l) || { name: l };
        return `<div class="lever on"><div class="dot"></div><div><div class="name">${esc(def.name)}</div></div></div>`;
      }).join("") +
      `<p class="muted" style="font-size:12.5px">Single-report mode: lever toggling needs a precomputed bundle from <span class="mono">scripts/precompute_web.py</span>.</p>`;
    return;
  }
  const man = S.data.manifest;
  const enabled = new Set(man.levers_enabled);
  const rows = LEVERS.filter(l => l.id !== "break_scheduling" || enabled.has(l.id))
    .map(l => {
      const on = S.toggles.has(l.id);
      const dis = !enabled.has(l.id);
      const warn = l.warn ? ` <span class="warn-badge" title="${esc(l.caveat)}">⚠</span>` : "";
      const note = dis ? `<div class="desc">disabled in this scenario's config</div>`
                       : `<div class="desc">${l.desc(man)}</div>`;
      return `<div class="lever ${on ? "on" : ""} ${dis ? "disabled" : ""}" data-lever="${l.id}">
        <div class="dot"></div><div><div class="name">${esc(l.name)}${warn}</div>${note}</div></div>`;
    }).join("");
  const w = man.weights;
  $("lever-panel").innerHTML = `<h3>Optimization levers</h3>${rows}
    <details class="advanced"><summary>Advanced — routing weights (read-only)</summary>
      <table><tr><td>preset</td><td class="mono">${esc(w.preset || "custom")}</td></tr>
      <tr><td>throughput</td><td class="mono">${esc(w.throughput)}</td></tr>
      <tr><td>wait</td><td class="mono">${esc(w.wait)}</td></tr>
      <tr><td>csat</td><td class="mono">${esc(w.csat)}</td></tr></table>
      <p class="muted">This bundle was run with the weights above. The workbench never pretends it can re-run weights — change them in the scenario file and re-run the engine.</p>
    </details>`;
  $("lever-panel").querySelectorAll(".lever:not(.disabled)").forEach(el => {
    el.addEventListener("click", () => {
      const id = el.getAttribute("data-lever");
      if (S.toggles.has(id)) S.toggles.delete(id); else S.toggles.add(id);
      renderAll();
    });
  });
}

function renderNotPrecomputed() {
  const key = setKey(S.toggles);
  const levers = CANON.filter(l => S.toggles.has(l));
  const cmd = `.venv/bin/python scripts/precompute_web.py --preset ${S.data.manifest.preset} --levers ${levers.join(",") || "none"}`;
  $("scoreboard").innerHTML = `<div class="not-precomputed">
    <h3>This lever combination is not precomputed</h3>
    <p class="muted">The workbench displays the engine's own numbers or nothing — it never interpolates or estimates. Generate this combination (~6 min, one extra paired 200-day run), then reload:</p>
    <code id="cli-cmd">${esc(cmd)}</code>
    <button class="copy" id="copy-cmd">Copy command</button>
    <p class="muted" style="margin-top:10px">Precomputed states: baseline, every enabled lever solo, the canonical cumulative prefixes, and the combined set — or run <span class="mono">--full-grid</span> overnight for all ${Math.pow(2, S.data.manifest.levers_enabled.length)} subsets.</p>
  </div>`;
  $("copy-cmd").addEventListener("click", () => copyText(cmd, $("copy-cmd")));
}

function scoreRow(label, b, a, dcmp, lowerBetter, opts = {}) {
  let dcell = "—";
  if (dcmp && isNum(dcmp.delta_pct)) {
    const good = lowerBetter ? dcmp.delta_pct <= 0 : dcmp.delta_pct >= 0;
    const ci = isNum(dcmp.ci95_low)
      ? `<span class="ci">95% CI ${f1(dcmp.ci95_low)}…${f1(dcmp.ci95_high)}%</span>` : "";
    dcell = `<span class="${good ? "good" : "bad"}">${sgn1(dcmp.delta_pct)}%</span>${ci}`;
  }
  return `<tr class="${opts.cls || ""}"><td>${label}</td><td>${opts.fmt ? opts.fmt(b) : f2(b)}</td><td>${opts.fmt ? opts.fmt(a) : f2(a)}</td><td>${dcell}</td></tr>`;
}

function renderScoreboard() {
  let before, after, deltas, pB, pA, label;
  if (S.mode === "bundle") {
    const base = baselineVariant(), cur = currentVariant();
    before = base.metrics; after = cur.metrics; deltas = cur.vs_baseline;
    pB = base.punctuality; pA = cur.punctuality;
    label = cur.key === "none" ? "Baseline selected — enable levers to compare" : esc(cur.label);
  } else {
    const r = reportRows();
    before = r.before; after = r.after; deltas = r.deltas;
    pB = r.punctBefore; pA = r.punctAfter;
    label = "as reported (baseline vs optimized)";
  }
  const punct = S.mode === "bundle" ? S.data.manifest.punctuality_inputs : null;
  const punctNote = punct
    ? `thresholds: late_ok ${punct.late_ok_min} min · late_acceptable ${punct.late_acceptable_min} min`
    : `thresholds unavailable in single-report mode (they live in the scenario config)`;
  $("scoreboard").innerHTML = `<div class="panel"><h2>Scoreboard</h2>
    <p class="muted">${label} · display precision: 2 dp (values), 1 dp (deltas)</p>
    <table class="score">
      <tr><th>metric</th><th>Before</th><th>After</th><th>Δ</th></tr>
      ${scoreRow("mean wait (min)", before.mean_wait_min, after.mean_wait_min, deltas.mean_wait, true)}
      ${scoreRow("p90 wait (min)", before.p90_wait_min, after.p90_wait_min, deltas.p90_wait, true)}
      ${scoreRow("in-office served / day", before.served_per_day, after.served_per_day, deltas.served, false)}
      ${scoreRow("resolved digitally / day — separate, never blended into served", before.resolved_digitally_per_day, after.resolved_digitally_per_day, null, false, { cls: "sub" })}
      ${scoreRow("turned away / day", before.turned_away_per_day, after.turned_away_per_day, deltas.turned_away, true)}
      ${scoreRow("abandoned / day", before.abandoned_per_day, after.abandoned_per_day, deltas.abandoned, true)}
      ${scoreRow("predicted CSAT (0–100)", before.mean_csat, after.mean_csat, deltas.mean_csat, false)}
      ${scoreRow("predicted CSAT (5-pt)", before.csat_5pt, after.csat_5pt, null, false, { cls: "sub" })}
      ${scoreRow("incomplete time cost (min/day)", before.incomplete_time_cost_min_per_day, after.incomplete_time_cost_min_per_day, null, true)}
      <tr class="sep"><td colspan="4"><b>Appointment punctuality</b> <small>(${punctNote})</small></td></tr>
      ${punctRows(pB, pA)}
    </table></div>`;
}

function punctRows(pB, pA) {
  if (!pB || !pA) return `<tr><td colspan="4" class="muted">no punctuality block in this report</td></tr>`;
  return [
    ["on-time (≤ late_ok)", pB.pct_on_time, pA.pct_on_time, pctOf],
    ["acceptable (≤ late_acceptable)", pB.pct_acceptable, pA.pct_acceptable, pctOf],
    ["p50 lateness (min)", pB.p50_lateness_min, pA.p50_lateness_min, f2],
    ["p90 lateness (min)", pB.p90_lateness_min, pA.p90_lateness_min, f2],
    ["max lateness (min)", pB.max_lateness_min, pA.max_lateness_min, f2],
  ].map(([l, b, a, fmt]) =>
    `<tr><td>${l}</td><td>${fmt(b)}</td><td>${fmt(a)}</td><td>—</td></tr>`).join("");
}

/* ----------------------------------------------------------------- charts */
function svgWrap(title, inner, w, h, id) {
  return `<div class="panel chart-card"><h3>${esc(title)}</h3>
    <button class="dl" data-svg="${id}">download SVG</button>
    <svg id="${id}" viewBox="0 0 ${w} ${h}" width="100%" xmlns="http://www.w3.org/2000/svg">${inner}</svg></div>`;
}
function bar(x, y, w, h, fill, extra = "") {
  return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${w.toFixed(1)}" height="${Math.max(h, 0.5).toFixed(1)}" fill="${fill}" ${extra}/>`;
}
function txt(x, y, s, anchor = "middle", size = 11, fill = "#1d1c18", extra = "") {
  return `<text x="${x.toFixed(1)}" y="${y.toFixed(1)}" text-anchor="${anchor}" font-size="${size}" fill="${fill}" ${extra}>${esc(s)}</text>`;
}

function waterfallSVG(steps, field, lowerBetter, opts) {
  const W = 620, H = 320, padL = 46, padB = 64, padT = 28;
  const vals = steps.map(s => s[field]);
  const extraTop = opts.resolved ? opts.resolved : 0;
  const maxV = Math.max(...vals) + extraTop;
  const scale = v => padT + (H - padB - padT) * (1 - v / (maxV * 1.08));
  const n = steps.length + 1;
  const bw = (W - padL - 20) / n * 0.62, gap = (W - padL - 20) / n;
  let g = `<line x1="${padL}" y1="${scale(0)}" x2="${W - 10}" y2="${scale(0)}" stroke="#aaa"/>`;
  const X = i => padL + gap * i + gap * 0.18;
  // baseline bar
  g += bar(X(0), scale(vals[0]), bw, scale(0) - scale(vals[0]), COLOR.gray);
  g += txt(X(0) + bw / 2, scale(vals[0]) - 5, f1(vals[0]));
  for (let i = 1; i < steps.length; i++) {
    const prev = vals[i - 1], cur = vals[i], d = cur - prev;
    const good = lowerBetter ? d <= 0 : d >= 0;
    const top = scale(Math.max(prev, cur));
    g += bar(X(i), top, bw, Math.abs(scale(prev) - scale(cur)), good ? COLOR.teal : COLOR.red);
    g += txt(X(i) + bw / 2, top - 5, sgn1(d), "middle", 11, good ? COLOR.teal : COLOR.red);
  }
  const comb = vals[vals.length - 1];
  g += bar(X(n - 1), scale(comb), bw, scale(0) - scale(comb), COLOR.slate);
  g += txt(X(n - 1) + bw / 2, scale(comb) - 5, f1(comb));
  if (opts.resolved && opts.resolved > 0.05) {
    g += `<defs><pattern id="hatch" width="6" height="6" patternTransform="rotate(45)" patternUnits="userSpaceOnUse"><line x1="0" y1="0" x2="0" y2="6" stroke="${COLOR.teal}" stroke-width="1.6"/></pattern></defs>`;
    g += bar(X(n - 1), scale(comb + opts.resolved), bw, scale(comb) - scale(comb + opts.resolved),
             "url(#hatch)", `stroke="${COLOR.teal}"`);
    g += txt(X(n - 1) + bw / 2, scale(comb + opts.resolved) - 5,
             `+${f1(opts.resolved)} resolved digitally (not served)`, "middle", 9.5, COLOR.teal);
  }
  steps.concat([{ label: "COMBINED" }]).forEach((s, i) => {
    const lbl = (s.label || "").replace("Baseline (FIFO, as run today)", "Baseline");
    g += `<g transform="translate(${X(i) + bw / 2},${H - padB + 12}) rotate(-18)">` +
         txt(0, 0, lbl.length > 22 ? lbl.slice(0, 21) + "…" : lbl, "end", 9.5) + `</g>`;
  });
  g += txt(14, padT - 8, opts.ylabel, "start", 10, "#6b6a64");
  return g;
}

function groupedSVG(pairs, opts) {
  // pairs: [{label, b, a, lowerBetter}]
  const W = 620, H = 280, padB = 36, padT = 30;
  const maxV = Math.max(...pairs.flatMap(p => [p.b, p.a]));
  const scale = v => padT + (H - padB - padT) * (1 - v / (maxV * 1.12));
  const gap = (W - 60) / pairs.length;
  let g = `<line x1="40" y1="${scale(0)}" x2="${W - 10}" y2="${scale(0)}" stroke="#aaa"/>`;
  pairs.forEach((p, i) => {
    const x0 = 50 + gap * i + gap * 0.16, bw = gap * 0.27;
    const good = p.lowerBetter ? p.a <= p.b : p.a >= p.b;
    g += bar(x0, scale(p.b), bw, scale(0) - scale(p.b), COLOR.gray);
    g += txt(x0 + bw / 2, scale(p.b) - 5, f1(p.b));
    g += bar(x0 + bw + 6, scale(p.a), bw, scale(0) - scale(p.a), good ? COLOR.teal : COLOR.red);
    g += txt(x0 + bw + 6 + bw / 2, scale(p.a) - 5, f1(p.a));
    g += txt(x0 + bw + 3, H - padB + 16, p.label, "middle", 10.5);
  });
  if (opts && opts.lines) {
    for (const ln of opts.lines) {
      g += `<line x1="40" y1="${scale(ln.v)}" x2="${W - 10}" y2="${scale(ln.v)}" stroke="${ln.color}" stroke-dasharray="${ln.dash}" stroke-width="1.2"/>`;
      g += txt(W - 12, scale(ln.v) - 3, ln.label, "end", 9.5, ln.color);
    }
  }
  return g;
}

function renderCharts(cur) {
  const wf = S.report.waterfall;
  const resolved = S.report.metrics.resolved_digitally_per_day;
  let html = `<div class="charts-grid">`;
  html += svgWrap("Mean wait by lever (canonical progression, paired runs)",
    waterfallSVG(wf, "mean_wait_min", true, { ylabel: "mean wait (min)" }), 620, 320, "svg-wait");
  html += svgWrap("In-office served/day by lever (canonical progression)",
    waterfallSVG(wf, "served_per_day", false, { ylabel: "served / day", resolved }), 620, 320, "svg-served");

  let b, a, pB, pA, tag;
  if (S.mode === "bundle" && cur && cur !== "report") {
    b = baselineVariant().metrics; a = cur.metrics;
    pB = baselineVariant().punctuality; pA = cur.punctuality;
    tag = "current selection";
  } else if (S.mode === "bundle") {           // not precomputed: skip state charts
    b = null;
  } else {
    const r = reportRows(); b = r.before; a = r.after; pB = r.punctBefore; pA = r.punctAfter;
    tag = "as reported";
  }
  if (b) {
    html += svgWrap(`Baseline (gray) vs ${tag}`,
      groupedSVG([
        { label: "p90 wait (min)", b: b.p90_wait_min, a: a.p90_wait_min, lowerBetter: true },
        { label: "abandoned/day", b: b.abandoned_per_day, a: a.abandoned_per_day, lowerBetter: true },
        { label: "CSAT (0–100)", b: b.mean_csat, a: a.mean_csat, lowerBetter: false },
      ], null), 620, 280, "svg-grouped");
    const punct = S.mode === "bundle" ? S.data.manifest.punctuality_inputs : null;
    const lines = punct ? [
      { v: punct.late_ok_min, color: COLOR.slate, dash: "5 3", label: `late_ok = ${punct.late_ok_min}` },
      { v: punct.late_acceptable_min, color: COLOR.red, dash: "7 3", label: `late_acceptable = ${punct.late_acceptable_min}` },
    ] : null;
    html += svgWrap(`Appointment lateness — baseline (gray) vs ${tag}` +
                    (punct ? "" : " (thresholds unavailable in single-report mode)"),
      pB && pA ? groupedSVG([
        { label: "p50 lateness", b: pB.p50_lateness_min, a: pA.p50_lateness_min, lowerBetter: true },
        { label: "p90 lateness", b: pB.p90_lateness_min, a: pA.p90_lateness_min, lowerBetter: true },
        { label: "max lateness", b: pB.max_lateness_min, a: pA.max_lateness_min, lowerBetter: true },
      ], { lines }) : txt(310, 120, "no punctuality block in this report"),
      620, 280, "svg-punct");
  }
  html += `</div>`;
  $("charts").innerHTML = html;
  $("charts").querySelectorAll("button.dl").forEach(btn => {
    btn.addEventListener("click", () => downloadSVG(btn.getAttribute("data-svg")));
  });
}

/* --------------------------------------------------- analysis + findings */
function renderAnalysis() {
  const r = S.report;
  const solo = (r.per_lever_impact || []).map(p =>
    `<tr><td>${esc(p.lever)}</td><td>${sgn1(p.served_per_day_delta_pct)}%</td>
     <td>${sgn1(p.mean_wait_delta_pct)}%</td><td>${sgn1(p.p90_wait_delta_pct)}%</td>
     <td>${sgn1(p.mean_csat_delta_pts)}</td></tr>`).join("");
  const wf = r.waterfall;
  const cum = wf.map((s, i) => {
    const prev = i > 0 ? wf[i - 1] : null;
    const inc = (f) => prev ? ` <small class="muted">(${sgn1(s[f] - prev[f])})</small>` : "";
    return `<tr><td>${esc(s.label)}</td>
      <td>${f1(s.mean_wait_min)}${inc("mean_wait_min")}</td>
      <td>${f1(s.served_per_day)}${inc("served_per_day")}</td>
      <td>${f1(s.mean_csat)}${inc("mean_csat")}</td></tr>`;
  }).join("");
  const flips = signFlips(wf, r.per_lever_impact || []);
  const attr = (r.attribution || []).map(x =>
    `${esc(x.lever)} ${f1(x.share_of_combined_gain)}%`).join(" · ");
  $("analysis").innerHTML = `<div class="analysis-grid">
    <div class="panel"><h3>Solo impact <small class="muted">(each lever alone vs baseline — order-independent)</small></h3>
      <table class="plain"><tr><th>lever</th><th>served Δ%</th><th>wait Δ%</th><th>p90 Δ%</th><th>CSAT Δpts</th></tr>${solo}</table>
      <p class="muted" style="margin-top:8px">Attribution of combined gain: ${attr || "—"}</p></div>
    <div class="panel"><h3>Cumulative progression <small class="muted">(one lever added per row, canonical order — order-dependent)</small></h3>
      <table class="plain"><tr><th>step</th><th>wait</th><th>served</th><th>CSAT</th></tr>${cum}</table>
      <p class="muted" style="margin-top:8px">These two tables answer different questions and are deliberately not reconciled.</p>
      ${flips.map(f => `<div class="flip-note">⇄ <b>${esc(f.lever)}</b> on ${esc(f.metric)}: cumulative increment ${sgn1(f.inc)} vs solo impact ${sgn1(f.solo)}% — the lever interacts with the levers applied before it (capacity freed or demand reshaped upstream changes what it has left to do).</div>`).join("")}
    </div></div>`;
}

function signFlips(wf, solo) {
  const out = [];
  const soloBy = {};
  solo.forEach(p => { soloBy[p.lever] = p; });
  for (let i = 1; i < wf.length; i++) {
    const added = (wf[i].levers_active || []).filter(l => !(wf[i - 1].levers_active || []).includes(l));
    if (added.length !== 1 || !soloBy[added[0]]) continue;
    const p = soloBy[added[0]];
    const checks = [
      ["served/day", wf[i].served_per_day - wf[i - 1].served_per_day, p.served_per_day_delta_pct],
      ["mean wait", wf[i].mean_wait_min - wf[i - 1].mean_wait_min, p.mean_wait_delta_pct],
    ];
    for (const [metric, inc, soloPct] of checks) {
      if (Math.abs(inc) > 0.15 && Math.abs(soloPct) > 0.1 && Math.sign(inc) !== Math.sign(soloPct)) {
        out.push({ lever: added[0], metric, inc, solo: soloPct });
      }
    }
  }
  return out;
}

function renderFocus() {
  const rec = S.report.focus_recommendation || [];
  if (!rec.length) { $("focus").innerHTML = ""; return; }
  $("focus").innerHTML = `<div class="panel"><h3>Focus recommendation (per employee)</h3>
    <table class="plain"><tr><th>employee</th><th>qualified</th><th>focus</th><th style="text-align:left">rationale</th></tr>
    ${rec.map(e => `<tr><td>${esc(e.employee_id)}</td>
      <td>${esc((e.qualified_services || []).join(", "))}</td>
      <td><b>${esc((e.focus_services || []).join(", "))}</b></td>
      <td style="text-align:left">${esc(e.rationale || "")}</td></tr>`).join("")}
    </table></div>`;
}

function renderFooter() {
  const flags = S.report.assumption_flags || [];
  const items = flags.map(f =>
    `<b>⚠ ${esc(f.lever)}</b> = ${esc(f.assumed_value)} — ${esc(f.caveat)}`);
  $("flags-footer").innerHTML =
    (items.length ? items.join("<br>") + "<br>" :
      "<b>⚠</b> No lever assumption flags active in this report.<br>") +
    `<span class="muted">${esc(MODEL_FLAGS)}</span>`;
}

/* ----------------------------------------------------------------- export */
function renderExport() {
  $("export").innerHTML = `<h3>Export</h3>
    <button class="export-btn" id="exp-csv">Scoreboard + tables → CSV</button>
    <button class="export-btn" id="exp-copy">Copy summary (plain text)</button>
    <span class="muted" style="font-size:12.5px"> · each chart has its own “download SVG” button</span>`;
  $("exp-csv").addEventListener("click", exportCSV);
  $("exp-copy").addEventListener("click", () => copyText(buildSummary(), $("exp-copy")));
}

function csvEsc(v) {
  const s = String(v ?? "");
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function exportCSV() {
  const rows = [["section", "metric", "before", "after", "delta_pct", "ci95_low", "ci95_high"]];
  const m = S.report.metrics;
  for (const [k, label] of [["mean_wait_min", "mean wait (min)"], ["p90_wait_min", "p90 wait (min)"],
    ["served_per_day", "served/day"], ["turned_away_per_day", "turned away/day"],
    ["abandoned_per_day", "abandoned/day"], ["mean_csat", "CSAT (0-100)"], ["makespan_min", "makespan (min)"]]) {
    const c = m[k] || {};
    rows.push(["scoreboard", label, c.baseline, c.optimized, c.delta_pct, c.ci95_low, c.ci95_high]);
  }
  rows.push(["scoreboard", "resolved digitally/day", 0, m.resolved_digitally_per_day, "", "", ""]);
  rows.push(["scoreboard", "CSAT (5pt)", (m.csat_5pt || {}).baseline, (m.csat_5pt || {}).optimized, "", "", ""]);
  const p = m.appointment_punctuality || {};
  for (const k of ["pct_on_time", "pct_acceptable", "p50_lateness_min", "p90_lateness_min", "max_lateness_min"]) {
    rows.push(["punctuality", k, (p.baseline || {})[k], (p.optimized || {})[k], "", "", ""]);
  }
  for (const s of S.report.per_lever_impact || []) {
    rows.push(["solo_impact", s.lever, "", "", s.mean_wait_delta_pct, "", ""]);
  }
  for (const s of S.report.waterfall || []) {
    rows.push(["progression", s.label, "", "", "", "", ""],
              ["progression_values", s.label, s.mean_wait_min, s.served_per_day, s.mean_csat, "", ""]);
  }
  for (const a of S.report.attribution || []) {
    rows.push(["attribution", a.lever, "", "", a.share_of_combined_gain, "", ""]);
  }
  const csv = rows.map(r => r.map(csvEsc).join(",")).join("\n");
  download(new Blob([csv], { type: "text/csv" }),
    `${(S.report.meta.scenario_name || "report").replace(/\W+/g, "-").toLowerCase()}-workbench.csv`);
}

function buildSummary() {
  const meta = S.report.meta, m = S.report.metrics;
  const ln = [];
  ln.push(`${meta.scenario_name} — optimize-lab results (seed ${meta.random_seed}, ${meta.monte_carlo_days} MC days, engine ${S.mode === "bundle" ? S.data.manifest.engine_version : meta.engine_version})`);
  const row = (label, c, unit = "") =>
    ln.push(`  ${label}: ${f2(c.baseline)}${unit} → ${f2(c.optimized)}${unit} (${sgn1(c.delta_pct)}%)`);
  row("mean wait", m.mean_wait_min, " min");
  row("p90 wait", m.p90_wait_min, " min");
  row("in-office served/day", m.served_per_day);
  ln.push(`  resolved digitally/day: ${f2(m.resolved_digitally_per_day)} (separate from served)`);
  row("abandoned/day", m.abandoned_per_day);
  row("CSAT (0-100)", m.mean_csat);
  const p = m.appointment_punctuality;
  if (p) ln.push(`  punctuality: on-time ${pctOf(p.baseline.pct_on_time)} → ${pctOf(p.optimized.pct_on_time)}; p90 lateness ${f2(p.baseline.p90_lateness_min)} → ${f2(p.optimized.p90_lateness_min)} min`);
  for (const w of S.report.guardrail_warnings || []) ln.push(`  GUARDRAIL WARNING: ${w.message}`);
  for (const f of S.report.assumption_flags || []) ln.push(`  ⚠ ${f.lever} = ${f.assumed_value}: ${f.caveat}`);
  return ln.join("\n");
}

function copyText(text, btn) {
  const done = () => { if (btn) { const t = btn.textContent; btn.textContent = "Copied ✓"; setTimeout(() => { btn.textContent = t; }, 1400); } };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text, done));
  } else fallbackCopy(text, done);
}
function fallbackCopy(text, done) {
  const ta = document.createElement("textarea");
  ta.value = text; document.body.appendChild(ta); ta.select();
  try { document.execCommand("copy"); } catch (e) { /* state visibly */ alert("Copy failed — select and copy manually:\n\n" + text); }
  document.body.removeChild(ta); done();
}
function download(blob, name) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = name;
  document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 200);
}
function downloadSVG(id) {
  const el = document.getElementById(id);
  if (!el) return;
  const src = `<?xml version="1.0" encoding="UTF-8"?>\n` + el.outerHTML;
  download(new Blob([src], { type: "image/svg+xml" }), id + ".svg");
}

/* ------------------------------------------------------------------- boot */
function boot() {
  const sel = $("preset-select");
  const idx = window.OPTLAB_INDEX || [];
  if (!idx.length) {
    fatal("No precomputed data found (data/index.js empty or missing). Run scripts/precompute_web.py, or use “Load report…” with a CLI-produced ResultsReport.");
  }
  sel.innerHTML = idx.map(e => `<option value="${esc(e.key)}">${esc(e.name)}</option>`).join("") +
    `<option value="__loaded" hidden>loaded file</option>`;
  sel.addEventListener("change", () => selectPreset(sel.value));
  $("file-input").addEventListener("change", ev => {
    if (ev.target.files && ev.target.files[0]) {
      handleFile(ev.target.files[0]);
      sel.value = "__loaded";
      ev.target.value = "";
    }
  });
  if (idx.length) selectPreset(idx[0].key);
}
document.addEventListener("DOMContentLoaded", boot);
