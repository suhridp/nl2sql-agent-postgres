"""Self-contained HTML dashboard for a run: optimizer study, guardrail matrix, agent benchmark and a
question explorer. No external scripts; open the file anywhere."""
from __future__ import annotations

import json
from pathlib import Path


def _trim_trace(t):
    return {k: t.get(k) for k in ("key", "model", "system", "kind", "qid", "trial", "difficulty", "question", "status",
                                  "gold_sql", "sql", "reply_text", "correct", "result_check", "llm_calls",
                                  "latency_s", "harmful_sql_ran")} | {
        "attempts": [{k: a[k] for k in ("sql", "outcome", "code", "message")} for a in t["attempts"]][:6],
        "cols": t["cols"][:12], "rows": [r[:12] for r in t["rows"][:8]], "optimizer": t.get("optimizer") or {}}


def render(S, manifest, opt, guard, traces, standalone=True) -> str:
    oq = None
    if opt:
        oq = {"summary": {k: v for k, v in opt["summary"].items()}, "rounds": opt["rounds"],
              "confirm_rounds": opt["confirm_rounds"],
              "queries": [{"id": q["id"], "expect": q["expect"], "sql": q["sql"], "policies": q["policies"],
                           "original_ms": q["result"]["original_ms"], "notes": q["result"]["notes"],
                           "candidates": [{k: c[k] for k in ("rule", "sql", "verified", "reject_reason", "cost",
                                                             "median_ms", "speedup", "accepted")}
                                          for c in q["result"]["candidates"]]} for q in opt["queries"]]}
    data = {"S": S, "M": manifest, "O": oq, "G": guard and {k: guard[k] for k in ("attacks", "summary", "db_unchanged",
                                                                                   "timeout_ms")},
            "T": [_trim_trace(t) for t in traces]}
    blob = json.dumps(data, separators=(",", ":"), default=str).replace("</", "<\\/")
    page = TEMPLATE.replace("__DATA__", blob)
    if standalone:
        page = ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" '
                'content="width=device-width, initial-scale=1, viewport-fit=cover">\n' + page + "\n</html>\n")
    return page


def write_dashboard(run_dir: Path, S, manifest, opt, guard, traces, standalone=True):
    (Path(run_dir) / "dashboard.html").write_text(render(S, manifest, opt, guard, traces, standalone))


TEMPLATE = r"""<title>SQL Agent Eval</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Sans+Condensed:wght@600&display=swap">
<style>
/* Layout: an instrument readout. Real measurements first, then the agent benchmark, then the raw log. */
:root{
  --bg:#f5f6f8; --panel:#ffffff; --ink:#161b26; --ink-2:#4b5466; --ink-3:#7a8394; --rule:#dfe3ea; --rule-2:#eef0f4;
  --accent:#2a78d6; --accent-soft:#e3eefb; --s2:#eb6834; --s3:#1baf7a; --bad:#d03b3b; --bad-soft:#fbe7e6; --good:#0c7a3c; --good-soft:#e2f3e8;
  --warn:#9a6400; --warn-soft:#fbf0d9; --muted-dot:#a3abba;
  --h0:#f3f6fa; --h1:#cde2fb; --h2:#86b6ef; --h3:#3987e5; --h4:#1c5cab; --h5:#0d366b;
  --display:"IBM Plex Sans Condensed","Arial Narrow",system-ui,sans-serif;
  --body:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#12151b; --panel:#1a1e26; --ink:#e9ecf2; --ink-2:#b4bccb; --ink-3:#8590a3; --rule:#2c323d; --rule-2:#232832;
  --accent:#5598e7; --accent-soft:#1d2a3d; --s2:#d95926; --s3:#199e70; --bad:#ef6b6b; --bad-soft:#3a1f22; --good:#4cc27a; --good-soft:#1b3326;
  --warn:#e3b04b; --warn-soft:#3a2f17; --muted-dot:#5d6676;
  --h0:#1e232c; --h1:#184f95; --h2:#256abf; --h3:#3987e5; --h4:#86b6ef; --h5:#cde2fb; color-scheme:dark}}
:root[data-theme="dark"]{
  --bg:#12151b; --panel:#1a1e26; --ink:#e9ecf2; --ink-2:#b4bccb; --ink-3:#8590a3; --rule:#2c323d; --rule-2:#232832;
  --accent:#5598e7; --accent-soft:#1d2a3d; --s2:#d95926; --s3:#199e70; --bad:#ef6b6b; --bad-soft:#3a1f22; --good:#4cc27a; --good-soft:#1b3326;
  --warn:#e3b04b; --warn-soft:#3a2f17; --muted-dot:#5d6676;
  --h0:#1e232c; --h1:#184f95; --h2:#256abf; --h3:#3987e5; --h4:#86b6ef; --h5:#cde2fb; color-scheme:dark}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--ink);font:14px/1.5 var(--body);margin:0}
.wrap{max-width:1180px;margin:0 auto;padding-block:28px 64px;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));display:flex;flex-direction:column;gap:30px}
h1,h2,h3{font-family:var(--display);font-weight:600;letter-spacing:.01em;text-wrap:balance;margin:0}
h1{font-size:30px;line-height:1.1} h2{font-size:20px} h3{font:600 14px var(--body)}
.eyebrow{font:500 11px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--ink-3)}
.muted{color:var(--ink-2)} .small{font-size:12px} .mono{font-family:var(--mono);font-variant-numeric:tabular-nums}
header{display:flex;flex-direction:column;gap:10px}
.meta{display:flex;flex-wrap:wrap;gap:6px 18px;font:12px var(--mono);color:var(--ink-2)}
.banner{border:1px solid var(--warn);background:var(--warn-soft);padding:10px 14px;border-radius:6px;max-width:82ch}
.banner b{color:var(--warn)}
section{display:flex;flex-direction:column;gap:14px}
.sec-head{display:flex;flex-direction:column;gap:4px;max-width:78ch}
.real{display:inline-flex;align-items:center;gap:6px;font:500 11px var(--mono);color:var(--good);background:var(--good-soft);padding:3px 8px;border-radius:4px;width:fit-content}
.stand{color:var(--warn);background:var(--warn-soft)}
.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
@media (max-width:860px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}}
.kpi{background:var(--panel);border:1px solid var(--rule);border-radius:8px;padding:12px 14px;display:flex;flex-direction:column;gap:2px;min-width:0}
.kpi b{font:600 24px var(--display);font-variant-numeric:tabular-nums}
.kpi span{font-size:12px;color:var(--ink-2)}
.grid2{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:20px;align-items:start}
@media (max-width:860px){.grid2{grid-template-columns:minmax(0,1fr)}}
.panel{background:var(--panel);border:1px solid var(--rule);border-radius:8px;padding:14px;min-width:0}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:12.5px}
th{font:500 11px var(--mono);letter-spacing:.04em;text-transform:uppercase;color:var(--ink-3);text-align:left;padding:6px 8px;border-bottom:1px solid var(--rule);white-space:nowrap}
td{padding:6px 8px;border-bottom:1px solid var(--rule-2);vertical-align:top;font-variant-numeric:tabular-nums}
td.num{font-family:var(--mono);white-space:nowrap}
tr.hl td{background:var(--accent-soft)}
.ci{color:var(--ink-3);font-size:11px}
.chip{display:inline-flex;align-items:center;gap:4px;font:500 11px/1 var(--mono);padding:4px 7px;border-radius:4px;white-space:nowrap}
.c-bad{background:var(--bad-soft);color:var(--bad)} .c-good{background:var(--good-soft);color:var(--good)}
.c-mid{background:var(--warn-soft);color:var(--warn)} .c-neu{background:var(--rule-2);color:var(--ink-2)}
svg text{fill:var(--ink-2);font:11px var(--mono)}
.tip{position:fixed;pointer-events:none;background:var(--ink);color:var(--bg);font:12px/1.4 var(--mono);padding:8px 10px;border-radius:6px;max-width:320px;z-index:20;box-shadow:0 4px 18px rgba(0,0,0,.18)}
.matrix td.cell{text-align:center;font:500 11px var(--mono);border:2px solid var(--panel);border-radius:4px;white-space:nowrap}
.legend{display:flex;gap:14px;flex-wrap:wrap;font:11px var(--mono);color:var(--ink-2);align-items:center}
.sw{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px;vertical-align:-1px}
.tabs{display:flex;flex-wrap:wrap;gap:6px}
.tab{font:500 12px var(--mono);border:1px solid var(--rule);background:var(--panel);color:var(--ink-2);padding:6px 10px;border-radius:999px;cursor:pointer}
.tab[aria-selected="true"]{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.tab:focus-visible,button:focus-visible,select:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.explorer{display:grid;grid-template-columns:minmax(0,330px) minmax(0,1fr);gap:16px;align-items:start}
@media (max-width:860px){.explorer{grid-template-columns:minmax(0,1fr)}}
.filters{display:flex;flex-wrap:wrap;gap:8px}
.filters label{display:flex;flex-direction:column;gap:3px;font:500 10.5px var(--mono);letter-spacing:.05em;text-transform:uppercase;color:var(--ink-3)}
select{font:12px var(--mono);background:var(--panel);color:var(--ink);border:1px solid var(--rule);border-radius:5px;padding:5px 6px;max-width:170px}
.tlist{display:flex;flex-direction:column;max-height:620px;overflow:auto;border:1px solid var(--rule);border-radius:8px;background:var(--panel)}
.trow{display:grid;grid-template-columns:1fr auto;gap:2px 8px;padding:9px 12px;border:0;border-bottom:1px solid var(--rule-2);border-left:3px solid transparent;cursor:pointer;text-align:left;background:none;color:inherit;font:inherit}
.trow:hover{background:var(--rule-2)} .trow.sel{background:var(--accent-soft);border-left-color:var(--accent)}
.trow .t{font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.trow .s{grid-column:1/-1;font:11.5px var(--mono);color:var(--ink-3)}
.detail{display:flex;flex-direction:column;gap:10px;min-width:0}
pre.sql{margin:0;padding:10px 12px;font:12px/1.55 var(--mono);white-space:pre-wrap;word-break:break-word;background:var(--panel);border:1px solid var(--rule);border-radius:6px;color:var(--ink)}
.att{display:grid;grid-template-columns:28px minmax(0,1fr);gap:8px}
.att .n{font:500 11px var(--mono);color:var(--ink-3);padding-top:9px}
.empty{padding:28px;text-align:center;color:var(--ink-3)}
footer{color:var(--ink-3);font-size:12px;max-width:82ch}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
<div class="wrap">
<header>
  <span class="eyebrow">Natural-language-to-SQL agent · PostgreSQL</span>
  <h1>SQL Agent Eval</h1>
  <div class="meta" id="meta"></div>
  <div id="banner"></div>
</header>

<section id="sec-opt">
  <div class="sec-head"><span class="eyebrow">01 · Query optimization</span><h2>Which rewrites actually make PostgreSQL faster</h2>
    <span class="real">● real measurement, no model involved</span>
    <p class="muted small" id="opt-intro"></p></div>
  <div class="kpis" id="opt-kpis"></div>
  <div class="grid2">
    <div class="panel"><h3>Every verified rewrite</h3><p class="muted small">Median paired speedup over the original with a 95% bootstrap interval. Filled dots passed the acceptance gate; grey ones were rejected as not reliably faster.</p><svg id="rewrites" width="100%" role="img" aria-label="Speedup of each rewrite"></svg></div>
    <div class="panel"><h3>Does the planner's cost estimate predict real time?</h3><p class="muted small">Each dot is a rewrite: estimated cost relative to the original against measured time relative to the original. Shaded quadrants are where the estimate was right; red dots moved the opposite way to their estimate.</p><svg id="costtime" viewBox="0 0 420 340" width="100%" role="img" aria-label="Planner cost ratio against time ratio"></svg></div>
  </div>
  <div class="panel scroll"><h3>Selection policies, re-timed in a separate confirmation run</h3><table id="policies"></table></div>
</section>

<section id="sec-guard">
  <div class="sec-head"><span class="eyebrow">02 · Guardrails</span><h2>What each safety layer stops on its own</h2>
    <span class="real">● real measurement, no model involved</span>
    <p class="muted small" id="guard-intro"></p></div>
  <div class="panel scroll"><table class="matrix" id="matrix"></table>
    <div class="legend" style="margin-top:10px"><span><i class="sw" style="background:var(--good)"></i>blocked (reason shown)</span><span><i class="sw" style="background:var(--bad)"></i>passed this layer / ran</span></div></div>
</section>

<section id="sec-agent">
  <div class="sec-head"><span class="eyebrow">03 · Agent benchmark</span><h2>Accuracy and behaviour by pipeline stage</h2>
    <span class="real" id="agent-badge"></span>
    <p class="muted small">Execution accuracy compares the agent's result rows with the gold query's. Systems add one stage at a time: full schema with documentation, then validation and repair, then a second look at empty results, then optimization.</p></div>
  <div class="tabs" id="tabs" role="tablist" aria-label="Model"></div>
  <div class="grid2">
    <div class="panel"><h3>Execution accuracy (95% CI)</h3><svg id="acc" viewBox="0 0 440 260" width="100%" role="img" aria-label="Accuracy by system"></svg></div>
    <div class="panel scroll"><h3>By difficulty</h3><table class="matrix" id="diff"></table></div>
  </div>
  <div class="panel scroll"><table id="agenttable"></table></div>
</section>

<section>
  <div class="sec-head"><span class="eyebrow">04 · Questions</span><h2>Question explorer</h2><p class="muted small" id="qnote"></p></div>
  <div class="filters" id="filters"></div>
  <div class="explorer"><div class="tlist" id="tlist" role="listbox" aria-label="Questions"></div><div class="detail" id="detail"></div></div>
</section>
<footer id="foot"></footer>
</div>
<div class="tip" id="tip" hidden></div>
<script id="data" type="application/json">__DATA__</script>
<script>
(function(){
const {S, M, O, G, T} = JSON.parse(document.getElementById('data').textContent);
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const pct = x => x == null ? 'n/a' : Math.round(100*x) + '%';
const fx = x => x == null ? 'n/a' : (x >= 10 ? x.toFixed(0) : x.toFixed(2)) + '×';
const tip = $('#tip');
const showTip = (ev, h) => { tip.innerHTML = h; tip.hidden = false; tip.style.left = Math.min(ev.clientX + 14, innerWidth - 330) + 'px'; tip.style.top = (ev.clientY + 14) + 'px'; };
const hideTip = () => tip.hidden = true;
const bindTips = root => root.querySelectorAll('[data-tip]').forEach(el => { el.onmousemove = ev => showTip(ev, el.dataset.tip); el.onmouseleave = hideTip; });

$('#meta').innerHTML = [`run <b>${esc(M.name)}</b>`, `PostgreSQL ${esc((M.postgres||'').split(' ')[0])}`, `driver ${esc(M.driver)}`, `code ${esc(M.git)}`, esc(M.finished)].map(x => `<span>${x}</span>`).join('');
if (M.scripted_only) $('#banner').innerHTML = `<div class="banner"><b>Agent results use scripted stand-ins.</b> Sections 01 and 02 are real PostgreSQL measurements and need no model. Section 03 shows that the agent pipeline, repair loop and grading work; it says nothing about how any model performs.</div>`;
$('#agent-badge').className = M.scripted_only ? 'real stand' : 'real';
$('#agent-badge').textContent = M.scripted_only ? '● scripted stand-ins: harness validation only' : '● real model runs';

// ---------- 01 optimizer
if (!O) { $('#sec-opt').hidden = true; } else {
  const s = O.summary, ga = s.gate_all, ct = s.cost_top1, cvt = s.cost_vs_time;
  $('#opt-intro').textContent = `${O.queries.length} correct but naively written queries. Each rewrite must pass the validator and return identical rows on both the main data and a small edge-case database (NULLs, empty groups, boundary dates). Timings interleave the original and its rewrites over ${O.rounds} rounds after a warm-up; a rewrite is accepted only if the whole 95% interval of its speedup is above 1.05×.`;
  $('#opt-kpis').innerHTML = [
    [fx(ga.geomean_speedup), `geometric-mean speedup across all ${O.queries.length} queries with the gate (confirmed)`],
    [`${ga.n_rewritten} / ${O.queries.length}`, `queries rewritten; ${ga.n_regressions} confirmed regressions`],
    [`${ct.n_slower_median}`, `rewrites that would have run slower if the planner's cost estimate were trusted`],
    [cvt.spearman == null ? 'n/a' : cvt.spearman.toFixed(2), `rank correlation of estimated cost and measured time`]
  ].map(([b, t]) => `<div class="kpi"><b>${b}</b><span>${esc(t)}</span></div>`).join('');
  // rewrites dot plot (log scale)
  const rows = []; O.queries.forEach(q => q.candidates.forEach(c => { if (c.speedup && c.speedup.median) rows.push({q, c}); }));
  const W = 520, L = 150, R = 16, T = 10, rowH = 18, H = T + rows.length * rowH + 34;
  const lo = Math.log10(Math.min(0.1, ...rows.map(r => r.c.speedup.lo))), hi = Math.log10(Math.max(10, ...rows.map(r => r.c.speedup.hi)));
  const X = v => L + (Math.log10(v) - lo) / (hi - lo) * (W - L - R);
  let g = '';
  for (let e = Math.ceil(lo); e <= Math.floor(hi); e++) { const v = Math.pow(10, e); g += `<line x1="${X(v)}" x2="${X(v)}" y1="${T}" y2="${H-28}" stroke="var(--rule)"/><text x="${X(v)}" y="${H-14}" text-anchor="middle">${v >= 1 ? v + '×' : v}</text>`; }
  g += `<line x1="${X(1)}" x2="${X(1)}" y1="${T}" y2="${H-28}" stroke="var(--ink-3)" stroke-dasharray="3 3"/>`;
  let last = null;
  rows.forEach((r, i) => {
    const y = T + i * rowH + rowH / 2, sp = r.c.speedup, col = r.c.accepted ? 'var(--accent)' : 'var(--muted-dot)';
    const lab = r.q.id !== last ? `${r.q.id} ${r.c.rule}` : `   ${r.c.rule}`; last = r.q.id;
    g += `<text x="${L-8}" y="${y+4}" text-anchor="end" style="fill:${r.c.accepted ? 'var(--ink)' : 'var(--ink-3)'}">${esc(lab.length > 26 ? lab.slice(0,25)+'…' : lab)}</text>`;
    g += `<line x1="${X(sp.lo)}" x2="${X(sp.hi)}" y1="${y}" y2="${y}" stroke="${col}" stroke-width="2" stroke-linecap="round" opacity=".55"/>`;
    g += `<circle cx="${X(sp.median)}" cy="${y}" r="5" fill="${col}" stroke="var(--panel)" stroke-width="1.5" data-tip="${esc(r.q.id)} · ${esc(r.c.rule)}<br>speedup ${fx(sp.median)} [${fx(sp.lo)}, ${fx(sp.hi)}]<br>${r.c.median_ms?.toFixed(1)} ms vs ${r.q.original_ms?.toFixed(1)} ms<br>${r.c.accepted ? 'accepted' : 'rejected by the gate'}"/>`;
  });
  const sv = $('#rewrites'); sv.setAttribute('viewBox', `0 0 ${W} ${H}`); sv.innerHTML = g; bindTips(sv);
  // cost vs time scatter
  const P = cvt.pairs, CW = 420, CH = 340, cl = 48, cr = 12, ctp = 12, cb = 40;
  const ext = v => [Math.log10(Math.min(...v, 0.5)), Math.log10(Math.max(...v, 2))];
  const [xa, xb] = ext(P.map(p => p.cost_ratio)), [ya, yb] = ext(P.map(p => p.time_ratio));
  const CX = v => cl + (Math.log10(v) - xa) / (xb - xa) * (CW - cl - cr), CY = v => ctp + (1 - (Math.log10(v) - ya) / (yb - ya)) * (CH - ctp - cb);
  let h = `<rect x="${CX(1)}" y="${ctp}" width="${CW-cr-CX(1)}" height="${CY(1)-ctp}" fill="var(--rule-2)"/><rect x="${cl}" y="${CY(1)}" width="${CX(1)-cl}" height="${CH-cb-CY(1)}" fill="var(--rule-2)"/>`;
  [0.01,0.1,1,10,100].forEach(v => { if (Math.log10(v) >= xa && Math.log10(v) <= xb) h += `<text x="${CX(v)}" y="${CH-cb+16}" text-anchor="middle">${v}</text>`; if (Math.log10(v) >= ya && Math.log10(v) <= yb) h += `<text x="${cl-6}" y="${CY(v)+4}" text-anchor="end">${v}</text>`; });
  h += `<line x1="${CX(1)}" x2="${CX(1)}" y1="${ctp}" y2="${CH-cb}" stroke="var(--ink-3)" stroke-dasharray="3 3"/><line x1="${cl}" x2="${CW-cr}" y1="${CY(1)}" y2="${CY(1)}" stroke="var(--ink-3)" stroke-dasharray="3 3"/>`;
  h += `<text x="${(cl+CW-cr)/2}" y="${CH-6}" text-anchor="middle">estimated cost, rewrite ÷ original</text><text transform="translate(12 ${(ctp+CH-cb)/2}) rotate(-90)" text-anchor="middle">measured time ratio</text>`;
  P.forEach(p => { const wrong = (p.cost_ratio < .95 && p.time_ratio > 1.05) || (p.cost_ratio > 1.05 && p.time_ratio < .95);
    h += `<circle cx="${CX(p.cost_ratio)}" cy="${CY(p.time_ratio)}" r="5.5" fill="${wrong ? 'var(--bad)' : 'var(--accent)'}" stroke="var(--panel)" stroke-width="1.5" data-tip="${esc(p.id)} · ${esc(p.rule)}<br>cost ×${p.cost_ratio.toFixed(2)}, time ×${p.time_ratio.toFixed(2)}${wrong ? '<br><b>estimate pointed the wrong way</b>' : ''}"/>`;
    if (wrong) h += `<text x="${CX(p.cost_ratio)+8}" y="${CY(p.time_ratio)+4}" style="fill:var(--ink)">${esc(p.id)}</text>`; });
  $('#costtime').innerHTML = h; bindTips($('#costtime'));
  const names = {original:'never rewrite', sqlglot_only:'SQLGlot rewrite if ≥10% faster (phase 1)', cost_top1:'trust the planner: lowest estimated cost', gate_topk1:'time only the cheapest candidate, then gate', gate_all:'time every verified candidate, then gate'};
  $('#policies').innerHTML = `<thead><tr><th>policy</th><th>rewritten</th><th>geomean speedup</th><th>slower than original</th><th>confirmed regressions</th><th>timed runs</th></tr></thead><tbody>` +
    Object.keys(names).map(k => { const v = s[k]; return `<tr class="${k==='gate_all'?'hl':''}"><td><b class="mono">${k}</b><div class="ci">${esc(names[k])}</div></td><td class="num">${v.n_rewritten}</td><td class="num">${fx(v.geomean_speedup)}</td><td>${v.n_slower_median ? `<span class="chip c-bad">${v.n_slower_median}</span>` : '<span class="chip c-good">0</span>'}</td><td>${v.n_regressions ? `<span class="chip c-bad">${v.n_regressions}</span>` : '<span class="chip c-good">0</span>'}</td><td class="num">${v.timing_runs}</td></tr>`; }).join('') + '</tbody>';
}

// ---------- 02 guardrails
if (!G) { $('#sec-guard').hidden = true; } else {
  const layers = [['validator','validator'],['cost_check','cost check'],['role_only','role only'],['execution_path','execution path'],['full_stack','full stack']];
  $('#guard-intro').textContent = `${G.summary.n} attack statements, each sent to every layer alone and then to the full stack, as the SELECT-only analyst role. "Role only" switches read-only mode off to show what privileges alone stop; "execution path" is the read-only transaction, SELECT-only cursor and ${G.timeout_ms/1000}s timeout without the validator. Table fingerprints before and after: ${G.db_unchanged ? 'unchanged' : 'CHANGED'}.`;
  let t = `<thead><tr><th>attack</th>${layers.map(l => `<th>${l[1]}<div class="ci">${G.summary[l[0]]}/${G.summary.n}</div></th>`).join('')}</tr></thead><tbody>`;
  G.attacks.forEach(a => { t += `<tr><td><span class="mono small">${esc(a.id)}</span> <span class="muted small">${esc(a.category)}</span><div class="ci mono" style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(a.sql)}">${esc(a.sql)}</div></td>` +
    layers.map(([k]) => { const r = a[k]; return `<td class="cell" style="background:var(${r.blocked ? '--good-soft' : '--bad-soft'});color:var(${r.blocked ? '--good' : '--bad'})" data-tip="${esc(k)}: ${esc(r.outcome)}${r.detail ? '<br>' + esc(r.detail) : ''}">${r.blocked ? esc(r.outcome.replace(/^validator:|^cost_check:/, '').replace(/_/g,' ').slice(0,18)) : (k === 'validator' || k === 'cost_check' ? 'passed' : 'ran')}</td>`; }).join('') + '</tr>'; });
  $('#matrix').innerHTML = t + '</tbody>'; bindTips($('#matrix'));
}

// ---------- 03 agent
let model = S.models[0];
function renderAgent(){
  $('#tabs').innerHTML = S.models.map(m => `<button class="tab" role="tab" aria-selected="${m===model}" data-m="${esc(m)}">${esc(m)}</button>`).join('');
  $('#tabs').querySelectorAll('.tab').forEach(b => b.onclick = () => { model = b.dataset.m; renderAgent(); renderList(); });
  const R = S.rows.filter(r => r.model === model), sys = S.systems;
  const W = 440, H = 260, l = 40, r = 12, tp = 12, b = 40, X = i => l + (i + .5) * (W - l - r) / sys.length, Y = v => tp + (1 - v) * (H - tp - b);
  let g = '';
  [0, .25, .5, .75, 1].forEach(v => g += `<line x1="${l}" x2="${W-r}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--rule)"/><text x="${l-6}" y="${Y(v)+4}" text-anchor="end">${v*100}%</text>`);
  R.forEach(row => { const i = sys.indexOf(row.system), c = row.ex.cluster || row.ex.wilson;
    g += `<text x="${X(i)}" y="${H-b+16}" text-anchor="middle">${esc(row.system.replace('_',' '))}</text>`;
    g += `<line x1="${X(i)}" x2="${X(i)}" y1="${Y(c[0])}" y2="${Y(c[1])}" stroke="var(--accent)" stroke-width="2" stroke-linecap="round" opacity=".5"/>`;
    g += `<circle cx="${X(i)}" cy="${Y(row.ex.rate)}" r="6" fill="var(--accent)" stroke="var(--panel)" stroke-width="2" data-tip="${esc(row.system)}<br>${row.ex.k}/${row.ex.n} correct (${pct(row.ex.rate)})<br>95% CI ${pct(c[0])}–${pct(c[1])}"/>`;
    g += `<text x="${X(i)}" y="${Y(row.ex.rate)-11}" text-anchor="middle" style="fill:var(--ink)">${pct(row.ex.rate)}</text>`; });
  $('#acc').innerHTML = g; bindTips($('#acc'));
  const D = ['easy','medium','hard','extra'], heat = v => { const i = Math.min(5, Math.round(v * 5)); return [`var(--h${i})`, i >= 3 ? '#fff' : 'var(--ink)']; };
  $('#diff').innerHTML = `<thead><tr><th>system</th>${D.map(d => `<th>${d}</th>`).join('')}</tr></thead><tbody>` + R.map(row => `<tr><td class="mono small">${esc(row.system)}</td>${D.map(d => { const x = row.by_difficulty[d]; if (!x) return '<td></td>'; const [bg, fg] = heat(x.rate); return `<td class="cell" style="background:${bg};color:${fg}">${pct(x.rate)}<span style="opacity:.7"> /${x.n}</span></td>`; }).join('')}</tr>`).join('') + '</tbody>';
  $('#agenttable').innerHTML = `<thead><tr><th>system</th><th>first try failed</th><th>recovered</th><th>silently wrong</th><th>asked when ambiguous</th><th>abstained when unanswerable</th><th>refused attacks</th><th>harmful SQL ran</th><th>LLM calls</th><th>p50 latency</th></tr></thead><tbody>` +
    R.map(row => `<tr><td class="mono small"><b>${esc(row.system)}</b></td><td class="num">${row.n_first_failed}/${row.n}</td><td class="num">${row.recovered}/${row.n_first_failed}</td><td class="num">${row.silent_wrong}</td><td class="num">${pct(row.ambiguous_clarified.rate)}</td><td class="num">${pct(row.unanswerable_abstained.rate)}</td><td class="num">${pct(row.adversarial_refused.rate)}</td><td>${row.harmful_sql_ran ? `<span class="chip c-bad">${row.harmful_sql_ran}</span>` : '<span class="chip c-good">0</span>'}</td><td class="num">${row.llm_calls.toFixed(1)}</td><td class="num">${row.latency_p50.toFixed(2)}s</td></tr>`).join('') + '</tbody>';
}
renderAgent();

// ---------- 04 explorer
const F = {system: '', kind: '', outcome: ''}; let sel = null;
const OUT = t => t.correct ? 'correct' : (t.kind === 'adversarial' && t.harmful_sql_ran ? 'harmful' : 'wrong');
function renderFilters(){
  const opts = {system: S.systems, kind: ['answerable','ambiguous','unanswerable','adversarial'], outcome: ['correct','wrong','harmful']};
  $('#filters').innerHTML = Object.entries(opts).map(([k, vs]) => `<label for="f-${k}">${k}<select id="f-${k}" data-k="${k}"><option value="">all</option>${vs.map(v => `<option value="${esc(v)}" ${F[k]===v?'selected':''}>${esc(v)}</option>`).join('')}</select></label>`).join('');
  $('#filters').querySelectorAll('select').forEach(s => s.onchange = () => { F[s.dataset.k] = s.value; renderList(); });
}
function chip(t){ const o = OUT(t); return `<span class="chip ${o==='correct'?'c-good':o==='harmful'?'c-bad':'c-mid'}">${o==='correct' ? (t.kind==='answerable'?'correct':t.status) : o==='harmful' ? 'harmful SQL ran' : (t.status==='answered'?'wrong answer':t.status)}</span>`; }
function renderList(){
  const L = T.filter(t => t.model === model && (!F.system || t.system === F.system) && (!F.kind || t.kind === F.kind) && (!F.outcome || OUT(t) === F.outcome));
  L.sort((a, b) => (a.correct - b.correct) || a.qid.localeCompare(b.qid));
  $('#qnote').textContent = `${L.length} trials match. Wrong answers are listed first.`;
  $('#tlist').innerHTML = L.length ? L.slice(0, 500).map(t => `<button class="trow ${t.key===sel?'sel':''}" data-k="${esc(t.key)}" role="option"><span class="t">${esc(t.question)}</span>${chip(t)}<span class="s">${esc(t.qid)} · ${esc(t.system)} · ${esc(t.kind)}</span></button>`).join('') : '<div class="empty">No trials match these filters.</div>';
  $('#tlist').querySelectorAll('.trow').forEach(b => b.onclick = () => { sel = b.dataset.k; $('#tlist').querySelectorAll('.trow').forEach(x => x.classList.toggle('sel', x === b)); renderDetail(); });
  if (!L.some(t => t.key === sel)) { sel = L[0]?.key; $('#tlist .trow')?.classList.add('sel'); }
  renderDetail();
}
function renderDetail(){
  const t = T.find(x => x.key === sel), el = $('#detail');
  if (!t) { el.innerHTML = '<div class="empty">Select a question.</div>'; return; }
  let h = `<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center">${chip(t)}<span class="mono small muted">${esc(t.qid)} · ${esc(t.system)} · ${esc(t.difficulty||t.kind)} · ${t.llm_calls} model call${t.llm_calls===1?'':'s'}</span></div>`;
  h += `<div style="font-size:15px;border-left:3px solid var(--ink);padding:2px 0 2px 12px">${esc(t.question)}</div>`;
  if (t.reply_text) h += `<div class="small"><span class="eyebrow">Agent replied</span><br>${esc(t.reply_text)}</div>`;
  t.attempts.forEach((a, i) => { const ok = a.outcome === 'ok';
    h += `<div class="att"><div class="n">${String(i+1).padStart(2,'0')}</div><div style="display:flex;flex-direction:column;gap:6px;min-width:0"><div><span class="chip ${ok?'c-good':'c-mid'}">${ok ? (a.code === 'after_result_check' ? 'ran after result check' : 'ran') : 'stopped by ' + esc(a.outcome)}</span> <span class="mono small muted">${esc(a.code && a.code!=='after_result_check' ? a.code : '')}</span></div><pre class="sql">${esc(a.sql || '(no SQL)')}</pre>${a.message ? `<div class="small muted">${esc(a.message)}</div>` : ''}</div></div>`; });
  if (t.optimizer && t.optimizer.chosen_rule) h += `<div class="small"><span class="chip c-good">optimizer: ${esc(t.optimizer.chosen_rule)} ${fx(t.optimizer.speedup)}</span></div>`;
  if (t.gold_sql) h += `<div><span class="eyebrow">Gold query</span><pre class="sql" style="margin-top:4px">${esc(t.gold_sql)}</pre></div>`;
  if (t.rows && t.rows.length) h += `<div class="scroll"><span class="eyebrow">Result (first rows)</span><table style="margin-top:4px"><thead><tr>${t.cols.map(c => `<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${t.rows.map(r => `<tr>${r.map(v => `<td class="num">${esc(v)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
  el.innerHTML = h;
}
renderFilters(); renderList();
$('#foot').textContent = 'All numbers come from files in this run directory: optimizer.json, guardrails.json, traces.jsonl and summary.json. Timings are warm-cache, server-side, on one machine.';
})();
</script>
"""
