"""report.html: the same content as report.md, self-contained (no network), light and dark.

Summary first; the per-task viewer shows one task at a time (Prev / Next / task number, arrow keys);
methodology, configuration, limits and diagnostics are collapsed. render(data) takes the dict report.py builds.
"""

import html
import json
import re

DIAGRAM_STEPS = ["Your Claude Code transcripts", "Research tasks, sampled and rewritten to stand alone",
                 "One run per provider, same model, your instructions routed to each provider", "Judge, per task: recall, errors, ranking",
                 "Verdict and report"]

# Tables become stacked per-provider cards when their container is narrower than this.
STACK = """
@container {name} (width < {width}){{
 .{name} thead{{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}}
 .{name} tbody{{display:block}}
 .{name} tr{{display:grid;grid-template-columns:repeat(auto-fill,minmax(9.5rem,1fr));gap:12px 16px;padding:14px 0;
  border-bottom:1px solid var(--line)}}
 .{name} tr:last-child{{border-bottom:0}}
 .{name} th,.{name} td{{display:block;padding:0;border:0;text-align:left;white-space:normal}}
 .{name} tbody th{{grid-column:1/-1;font-size:1.02rem}}
 .{name} td.m,.{name} td.c{{padding:0;text-align:left;width:auto}}
 .{name} td::before{{content:attr(data-label);display:block;font-size:.68rem;letter-spacing:.05em;
  text-transform:uppercase;color:var(--muted);font-weight:650;margin-bottom:3px;white-space:nowrap}}
 .{name} .meter{{display:grid;grid-template-columns:1fr;gap:5px;justify-items:start;width:100%}}
 .{name} .meter i{{max-width:7rem}}}}
"""

CSS = """
:root{color-scheme:light dark;
 --bg:light-dark(#f7f8fa,#0f1115);--surface:light-dark(#fff,#171a20);--inset:light-dark(#f2f4f7,#11141a);
 --text:light-dark(#1b1f27,#e6e8ec);--muted:light-dark(#5b6270,#a1a8b5);
 --line:light-dark(#e3e6eb,#2a2f38);--line-strong:light-dark(#cfd4dc,#3a404b);--field:light-dark(#8a93a3,#6b7383);
 --accent:light-dark(#1f5fd1,#7aa7ff);--accent-soft:light-dark(#eaf1fd,#1a2540);
 --bad:light-dark(#b42318,#ff8a80);--bad-soft:light-dark(#fdecea,#3a1d1d);
 --warn:light-dark(#8a5a00,#f2c46d);--warn-soft:light-dark(#fdf3dc,#3a2f17);
 --e0:light-dark(#1f5fd1,#7aa7ff);--e1:light-dark(#c46a00,#f0a347);--e2:light-dark(#0f7b6c,#4fc1ae);--e3:light-dark(#7c3aed,#b39bff);--eb:light-dark(#6b7280,#9aa3b2);
 --track:light-dark(#e8ebf0,#262b34);
 --r:12px;--shadow:0 1px 2px rgb(16 24 40/.05),0 1px 3px rgb(16 24 40/.06)}
:root[data-theme=light]{color-scheme:light}:root[data-theme=dark]{color-scheme:dark;--shadow:none}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--shadow:none}}
*{box-sizing:border-box}
html{scroll-padding-top:4.5rem}
body{margin:0;background:var(--bg);color:var(--text);
 font:16px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased}
main{max-width:64rem;margin:0 auto;padding:56px 16px 96px}
h1,h2,h3{text-wrap:balance;line-height:1.2;margin:0}p,li{text-wrap:pretty;overflow-wrap:anywhere}
header{padding-bottom:32px;border-bottom:1px solid var(--line)}
h1{font-size:clamp(1.8rem,4.5vw,2.6rem);letter-spacing:-.025em;font-weight:750;max-width:26ch}
h2{font-size:1.3rem;letter-spacing:-.01em;font-weight:650;margin:56px 0 16px}
h3{font-size:1.2rem;font-weight:650;letter-spacing:-.01em}
a{color:var(--accent);text-underline-offset:2px}a:hover{text-decoration-thickness:2px}
code{font:.9em ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--inset);padding:1px 5px;border-radius:5px}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
.sub{color:var(--muted);margin:12px 0 0;font-size:.98rem;font-variant-numeric:tabular-nums}
.num{font-variant-numeric:tabular-nums}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow)}
.prose{max-width:72ch}
.verdict{padding:28px 32px;font-size:1.05rem;background:linear-gradient(180deg,var(--accent-soft),var(--surface) 75%);
 box-shadow:0 0 0 1px color-mix(in srgb,var(--accent) 18%,transparent),var(--shadow);border-color:transparent}
.verdict .lead{display:flex;align-items:center;font-size:1.9rem;font-weight:750;letter-spacing:-.02em;line-height:1.2;
 margin-bottom:10px}
.verdict .lead .dot{width:.42em;height:.42em;margin-right:.35em}
.verdict p{margin:0;max-width:72ch}.fallback{color:var(--muted);font-size:.9rem;margin:10px 0 0}
.dot{display:inline-block;width:.6em;height:.6em;border-radius:50%;background:var(--c);margin-right:.5em;
 vertical-align:.05em;flex:none}
.e0{--c:var(--e0)}.e1{--c:var(--e1)}.e2{--c:var(--e2)}.e3{--c:var(--e3)}.eb{--c:var(--eb)}
.tw{container-type:inline-size;padding:0 18px}.tw.eng{container-name:eng}.tw.task{container-name:task}
table{border-collapse:collapse;width:100%;font-size:.94rem}
th,td{padding:12px 8px;text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
thead th{font-size:.72rem;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:650;
 border-bottom:1px solid var(--line);white-space:normal;vertical-align:bottom}
thead th[title]{cursor:help}
tbody th,tbody td{border-bottom:1px solid var(--line)}tbody tr:last-child>*{border-bottom:0}
tr>:first-child{text-align:left;padding-left:0}tr>:last-child{padding-right:0}tbody th{font-weight:600}
.meter{display:inline-grid;grid-template-columns:3.1rem 4rem;gap:6px;align-items:center;justify-items:start}
.meter>span{line-height:1;order:-1}
th.m,td.m{text-align:left;padding-left:18px}th.c,td.c{text-align:center;width:4.5rem}
.meter i{width:100%;height:8px;border-radius:4px;
 background:linear-gradient(90deg,var(--c,var(--accent)) calc(var(--v)*100%),var(--track) 0)}
.legend{margin:10px 2px 0;border:0}
.legend summary{font-size:.88rem;color:var(--muted);font-weight:600;padding:6px 0}
.defs{display:grid;grid-template-columns:repeat(auto-fit,minmax(14rem,1fr));gap:12px 24px;margin:8px 0 4px;
 font-size:.82rem;color:var(--muted);line-height:1.5}
.defs b{display:block;color:var(--text);font-weight:600}
.h2h{display:flex;flex-wrap:wrap;gap:12px 36px;padding:16px 20px}
.pair{display:flex;align-items:center;gap:8px;white-space:nowrap}.pair .bar{width:84px;flex:0 1 84px;min-width:40px}
.pair .dot{margin:0}.pair b{font-weight:650}
.bar{position:relative;height:10px;border-radius:5px;
 background:linear-gradient(90deg,var(--a) calc(var(--v)*100%),var(--surface) 0 calc(var(--v)*100% + 2px),var(--b) 0)}
.bar::after{content:"";position:absolute;left:calc(50% - 1px);top:0;bottom:0;width:2px;background:var(--surface)}
.note{color:var(--muted);font-size:.88rem;margin:12px 2px 0}
.why .why-head{font-weight:650;margin:0 0 4px}
.why ul{list-style:none;padding:0;margin:0}
.why li{display:flex;gap:12px;align-items:baseline;padding:14px 0;margin:0;border-top:1px solid var(--line)}
.why ul:last-child li:last-child{border-bottom:1px solid var(--line)}
.why li>.dot{transform:translateY(-.05em)}.why ul+ul{margin-top:28px}
.dot.ring{background:none;box-shadow:inset 0 0 0 2px var(--c)}
.also{color:var(--muted)}
.tag{display:inline-block;font-size:.66rem;letter-spacing:.05em;text-transform:uppercase;font-weight:650;color:var(--muted);
 border:1px solid var(--line);border-radius:999px;padding:1px 7px;margin-right:4px;vertical-align:.12em;white-space:nowrap}
a.tlink{text-decoration-color:color-mix(in srgb,var(--accent) 40%,transparent);font-variant-numeric:tabular-nums}
.ex{white-space:nowrap}.ex a.tlink{display:inline-block;padding:0 2px;margin:0;border-radius:4px;background:var(--accent-soft);text-decoration:none;
 font-size:.9em;font-weight:600;line-height:1.6}
.ex a.tlink:hover{text-decoration:underline}
.viewer{overflow:clip}
.nav{position:sticky;top:0;z-index:2;display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:12px 20px;
 background:color-mix(in srgb,var(--surface) 92%,transparent);backdrop-filter:blur(8px);border-bottom:1px solid var(--line);
 border-radius:var(--r) var(--r) 0 0}
.seg{display:inline-flex}.seg button:first-child{border-radius:8px 0 0 8px}
.seg button+button{border-radius:0 8px 8px 0;margin-left:-1px}
.nav label{display:flex;align-items:center;gap:8px;margin-left:8px}
.hint{margin-left:auto;color:var(--muted);font-size:.82rem}
button,input[type=number]{height:34px;font:inherit;font-size:.92rem;border-radius:8px;border:1px solid var(--field);
 background:var(--surface);color:var(--text)}
button{font-weight:600;padding:0 14px;cursor:pointer}
button:hover:not(:disabled){background:var(--inset)}
button:disabled{color:var(--muted);background:var(--inset);cursor:default;border-color:var(--line-strong)}
input[type=number]{width:4.4rem;padding:0 8px;font-variant-numeric:tabular-nums}
#tpos{color:var(--muted);font-size:.92rem}
#task{padding:24px 20px 20px}
#task h3:focus{outline:none}#task h3:focus-visible{outline:2px solid var(--accent)}
.tn{display:block;font-size:.75rem;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);font-weight:650;
 margin-bottom:6px}
.prompt{margin:16px 0 0;background:var(--inset);border-radius:10px;padding:14px 16px}
.clamp{overflow:hidden;max-height:calc(var(--lines)*1lh);
 -webkit-mask-image:linear-gradient(#000 40%,transparent);mask-image:linear-gradient(#000 40%,transparent)}
.clamp.fits,.clamp[data-open]{max-height:none;-webkit-mask-image:none;mask-image:none}
.task-text{--lines:4;white-space:pre-wrap;overflow-wrap:anywhere;font-size:.95rem;line-height:1.6}
.more{height:auto;margin-top:6px;padding:2px 0;font-size:.85rem;border:0;background:none;color:var(--accent)}
.more:hover:not(:disabled){background:none;text-decoration:underline}
.task.tw{padding:0;margin:20px 0 0;border-block:1px solid var(--line)}
.rk{display:inline-grid;place-items:center;min-width:1.75em;height:1.75em;padding:0 6px;border-radius:999px;
 border:1px solid var(--line-strong);font-weight:650;font-size:.85rem}
.rk.first{background:var(--c);border-color:var(--c);color:var(--surface)}
.rk.first.e1{background:light-dark(#a85a00,var(--e1));border-color:light-dark(#a85a00,var(--e1))}
.chip{display:inline-block;padding:1px 9px;border-radius:999px;font-size:.82rem;font-weight:600;white-space:nowrap}
.chip.none{color:var(--muted);font-weight:500;padding:0}
.chip.side{color:var(--warn);background:var(--warn-soft)}.chip.main{color:var(--bad);background:var(--bad-soft)}
.label{display:block;font-size:.75rem;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:650;
 margin-bottom:4px}
.reason{margin:20px 0 0}
.errs{margin:20px 0 0;padding:0 0 0 14px;border-left:2px solid var(--bad)}
.errs ul{list-style:none;margin:0;padding:0}.errs li{margin:8px 0}
.err{--lines:4}.err .who{font-weight:600}
details{border-top:1px solid var(--line)}
summary{cursor:pointer;font-weight:600;list-style:none;padding:14px 0;display:flex;align-items:center;gap:10px}
summary::-webkit-details-marker{display:none}
summary::before{content:"";width:7px;height:7px;border-right:2px solid var(--muted);border-bottom:2px solid var(--muted);
 transform:rotate(-45deg);transition:transform .2s;flex:none}
details[open]>summary::before{transform:rotate(45deg)}
details[open]{padding-bottom:12px}
#task details{margin-top:20px}
#task details.pv{margin-top:0}#task details.pv:last-child{border-bottom:1px solid var(--line)}
.pv>summary{font-weight:600}.pv .body{display:grid;gap:12px;min-width:0}
.pv pre{white-space:pre-wrap;overflow-wrap:anywhere;margin:0;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
 background:var(--soft,rgba(127,127,127,.08));border-radius:8px;padding:10px 12px;max-height:28em;overflow:auto}
.pv ol.calls{padding-left:22px;margin:0}.pv ol.calls>li{margin:0 0 16px}
.pv ol.hits{margin:8px 0;padding-left:28px}.pv ol.hits li{margin:3px 0;overflow-wrap:anywhere}
.pv .cited{color:var(--good,#2f9e44);font-weight:650;font-size:.82rem;margin-left:4px}
#task .pv details.raw{margin-top:4px;border-top:0}#task .pv details.raw>summary{padding:4px 0;font-weight:500;font-size:.85rem;color:var(--muted)}
.pv .meta{color:var(--muted);font-size:.85rem}.pv .flag{color:var(--bad);font-weight:650;font-size:.8rem;
 text-transform:uppercase;letter-spacing:.04em;margin-left:6px}
.sections details{padding:0 4px}.sections details:last-child{border-bottom:1px solid var(--line)}
.sections details[open]{padding-bottom:20px}
.sections p,.sections ul,.sections ol{max-width:72ch}
ul,ol{padding-left:22px}li{margin:5px 0}
.diagram{background:#fff;border:1px solid var(--line);border-radius:10px;padding:12px;margin:12px 0}
.diagram svg{display:block;max-width:100%;height:auto}
.steps{list-style:none;padding:0;counter-reset:s}
.steps li{counter-increment:s;display:flex;gap:12px;align-items:baseline;padding:6px 0}
.steps li::before{content:counter(s);flex:none;display:inline-grid;place-items:center;width:1.6em;height:1.6em;
 border-radius:50%;background:var(--accent-soft);color:var(--accent);font-weight:700;font-size:.8rem}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap}
@media (min-width:701px){.steps{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}}
@media (max-width:700px){.diagram{display:none}.hint{display:none}}
@media (max-width:600px){
 main{padding:32px 16px 64px}h2{margin-top:44px}.verdict{padding:20px}.verdict .lead{font-size:1.5rem}
 .h2h{padding:14px 16px}.tw{padding:0 16px}#task{padding:20px 16px}.nav{padding:10px 12px}.nav label{margin-left:0}}
@media (pointer:coarse){.also a.tlink{display:inline-block;min-height:24px;padding:2px 3px}}
@media (prefers-reduced-motion:no-preference){
 #task.swap{animation:fade .18s ease-out}
 @keyframes fade{from{opacity:.35;transform:translateY(3px)}}
 details::details-content{interpolate-size:allow-keywords;overflow:clip;block-size:0;
  transition:block-size .22s ease,content-visibility .22s allow-discrete}
 details[open]::details-content{block-size:auto}}
@media (prefers-reduced-motion:reduce){summary::before{transition:none}}
@media print{
 :root,:root[data-theme]{color-scheme:light}body{background:#fff;font-size:11pt}main{max-width:none;padding:0}
 .card,.verdict{box-shadow:none}.verdict{border-color:var(--line)}.nav,.more{display:none}#task,#task.swap{animation:none}
 .clamp{max-height:none;-webkit-mask-image:none;mask-image:none}
 h2,h3,.why-head,summary{break-after:avoid}
 .why li,.pair,tr,.verdict,.errs li,.diagram{break-inside:avoid}.ex a.tlink{background:none;padding:0}}
@container eng (width < 50rem){
 table.eng{font-size:.84rem}.eng thead th{font-size:.64rem}.eng th,.eng td{padding:10px 5px}
 .eng th.m,.eng td.m{padding-left:10px}.eng .meter{grid-template-columns:2.8rem 2.6rem;gap:5px}}
.eng-t{display:none;width:100%;table-layout:fixed;font-size:.84rem}
.eng-t th,.eng-t td{padding:9px 3px;text-align:right;white-space:normal;overflow-wrap:anywhere;vertical-align:top}
.eng-t tbody th{width:32%;text-align:left;font-size:.66rem;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);font-weight:650}
.eng-t thead th{text-transform:none;letter-spacing:0;font-size:.8rem;color:var(--text);font-weight:650;line-height:1.3}
.eng-t thead td{width:32%}.eng-t .dot{margin-right:.25em}
@container eng (width < 39.5rem){table.eng{display:none}.eng-t{display:table}}
""" + STACK.format(name="task", width="40rem")

JS = """
const D = document.getElementById('data');
const {tasks: T, colors: E, heads: H} = JSON.parse(D.textContent);
D.remove();
const keys = T.map(t => String(t.n));
let cur = 0;
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const linkify = s => esc(s).replace(/https?:\\/\\/[^\\s)\\]]+[^\\s)\\].,;:]/g, u => `<a href="${u}" target="_blank" rel="noopener">${u}</a>`);
const list = xs => xs.length ? '<ul>' + xs.map(x => `<li>${linkify(x)}</li>`).join('') + '</ul>' : '';
const ec = name => 'e' + (E[name] ?? 0);
const dot = name => name in E ? `<span class="dot ${ec(name)}" aria-hidden="true"></span>` : '';
// {text, value, max} -> a zero-based bar beside the text (non-zero values get a visible sliver)
const meter = (r, cls) => {
  if (!r.max) return esc(r.text);
  const v = Math.min(1, r.value / r.max);
  return `<span class="meter ${cls}" style="--v:${v > 0 ? Math.max(v, .03) : 0}"><i aria-hidden="true"></i><span>${esc(r.text)}</span></span>`;
};
const rank = (s, name) => /^\\d+$/.test(s) ? `<span class="rk${s === '1' ? ' first ' + ec(name) : ''}">${esc(s)}</span>` : esc(s);
const FLAG = {error: 'error', rate_limit: 'rate limit', empty: 'empty result', no_result: 'no result'};
const num = n => n == null ? '?' : Number(n).toLocaleString('en-US');
// One provider's answer, judge notes and tool calls; built when its section is first opened.
const safeUrl = u => /^https?:\/\//i.test(u || '') ? u : '';
// [rank, url, title, published, cited] -> one line of the result list
const hit = ([rank, url, title, pub, cited]) => {
  const u = safeUrl(url), label = esc(title || url || '(no URL)');
  return `<li value="${esc(rank)}">${u ? `<a href="${esc(u)}" target="_blank" rel="noopener">${label}</a>` : label}` +
    `${title && url ? ` <span class="meta">${esc(url)}</span>` : ''}${pub ? ` <span class="meta">· ${esc(pub)}</span>` : ''}` +
    `${cited ? ' <span class="cited" title="cited in the answer">✓ cited</span>' : ''}</li>`;
};
const pvBody = p => {
  const calls = p.calls.map(c => `<li><div><code>${esc(c.tool)}</code>` +
    c.flags.map(f => `<span class="flag">${esc(FLAG[f] || f)}</span>`).join('') +
    ` <span class="meta">${esc(c.id)} · ${num(c.chars)} chars${c.ms != null ? ' · ' + (c.ms / 1000).toFixed(1) + ' s' : ''}` +
    `${c.rid ? ' · request ' + esc(c.rid) : ''}</span></div>` +
    `<pre>${esc(c.input)}</pre>` +
    (c.results.length ? `<ol class="hits">${c.results.map(hit).join('')}</ol>` : '') +
    (c.preview ? `<details class="raw"><summary>Raw result${c.cut ? ` (first ${num(c.shown)} of ${num(c.chars)} characters; the full text is in ${esc(H.export)})` : ''}</summary>` +
      `<pre>${esc(c.preview)}${c.cut ? '\\n…' : ''}</pre></details>` : '') + '</li>').join('');
  return `<div class="body prose"><p><b>Recall:</b> ${linkify(p.recall)}</p><p><b>Errors:</b> ${linkify(p.error)}</p>` +
    `<div><span class="label">Answer</span><pre>${esc(p.answer)}</pre></div>` +
    `<div><span class="label">Tool calls (${p.calls.length})</span>${calls ? '<ol class="calls">' + calls + '</ol>' : '<p class="meta">None.</p>'}</div></div>`;
};
const chip = (s, sev) => `<span class="chip ${sev}">${esc(s)}</span>`;
let fitting = 0;
const fitSoon = () => { if (!fitting) fitting = requestAnimationFrame(() => { fitting = 0; fitClamps(); }); };
function fitClamps() {
  document.querySelectorAll('#task .clamp').forEach(el => {
    const btn = document.querySelector(`[aria-controls="${el.id}"]`);
    const open = el.hasAttribute('data-open');
    el.removeAttribute('data-open'); el.classList.remove('fits');
    const clipped = el.scrollHeight - el.clientHeight > 1.5 * parseFloat(getComputedStyle(el).lineHeight);
    el.toggleAttribute('data-open', open);
    el.classList.toggle('fits', !clipped);
    btn.hidden = !clipped;
  });
}
function show(i, how) {
  const refocus = $('task').contains(document.activeElement);
  cur = Math.max(0, Math.min(T.length - 1, i)); const t = T[cur];
  $('tnum').value = t.n;
  $('tpos').textContent = `${cur + 1} of ${T.length}`;
  $('tlive').textContent = `Task ${t.n}, ${cur + 1} of ${T.length}${t.title ? ': ' + t.title : ''}`;
  const moved = document.activeElement;
  $('prev').disabled = cur === 0; $('next').disabled = cur === T.length - 1;
  if (moved && moved.disabled) (moved === $('prev') ? $('next') : $('prev')).focus();
  const heads = ['Provider', H.rank, 'Recall', H.error, 'Context tokens', 'Time'];
  const cls = ['', ' class="c"', ' class="m"', '', '', ''];
  const td = (k, v) => `<td role="cell"${cls[k]} data-label="${esc(heads[k])}">${v}</td>`;
  const rows = t.rows.map(r => `<tr role="row"><th role="rowheader" scope="row">${dot(r.provider)}${esc(r.provider)}</th>${td(1, rank(r.rank, r.provider))}` +
    `${td(2, meter(r.recall, ec(r.provider)))}${td(3, chip(r.error, r.sev))}${td(4, esc(r.tokens))}${td(5, esc(r.time))}</tr>`).join('');
  const box = $('task');
  box.innerHTML = `
    <h3 tabindex="-1"><span class="tn">Task ${esc(t.n)}</span>${t.title ? ' ' + esc(t.title) : ''}</h3>
    <div class="prompt"><div class="clamp task-text" id="ttext">${esc(t.text)}</div>
      <button class="more" id="tmore" type="button" aria-expanded="false" aria-controls="ttext" data-more="Show full prompt" hidden>Show full prompt</button></div>
    <div class="tw task"><table class="task" role="table"><thead role="rowgroup"><tr role="row">${heads.map((h, k) => `<th role="columnheader" scope="col"${cls[k]}>${esc(h)}</th>`).join('')}</tr></thead>
      <tbody role="rowgroup">${rows}</tbody></table></div>
    ${t.cap_note ? `<p class="note">${esc(t.cap_note)}</p>` : ''}
    <div class="reason prose"><span class="label">Why this order</span> ${linkify(t.ranking_reason)}</div>
    <details><summary>What a complete answer needed</summary>
      <div class="prose">${list(t.direct)}${t.other.length ? '<p>Also expected:</p>' + list(t.other) : ''}
      <p><b>Judge's comparison:</b> ${linkify(t.rationale)}</p></div>
    </details>
    <div class="label" style="margin-top:20px">Each provider's answer and tool calls</div>
    ${t.providers.map((p, i) => `<details class="pv" data-i="${i}"><summary>${dot(p.provider)}${esc(p.provider)}: ` +
      `${p.calls.length} tool call${p.calls.length === 1 ? '' : 's'}</summary></details>`).join('')}`;
  box.querySelectorAll('details.pv').forEach(d => d.addEventListener('toggle', () => {
    if (d.open && !d.dataset.built) { d.insertAdjacentHTML('beforeend', pvBody(t.providers[+d.dataset.i])); d.dataset.built = 1; }
  }));
  box.classList.remove('swap'); void box.offsetWidth; box.classList.add('swap');
  box.querySelectorAll('button.more').forEach(b => b.onclick = () => {
    const el = $(b.getAttribute('aria-controls')), open = !el.hasAttribute('data-open');
    el.toggleAttribute('data-open', open);
    b.setAttribute('aria-expanded', open);
    b.textContent = open ? 'Show less' : b.dataset.more;
  });
  fitClamps();
  if (refocus) box.querySelector('h3').focus({preventScroll: true});
  const hash = '#task-' + t.n;
  if (how === 'push') history.pushState(null, '', hash);
  else if (how === 'replace') history.replaceState(null, '', hash);
}
function go(n, how, scroll) {
  const i = keys.indexOf(String(n));
  if (i < 0) { $('tnum').value = T[cur].n; return false; }
  show(i, how);
  if (scroll) $('tasks-section').scrollIntoView({behavior: scroll === 'instant' || matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'});
  return true;
}
const viewerOnScreen = () => { const r = $('task').closest('.viewer').getBoundingClientRect(); return r.bottom > 0 && r.top < innerHeight; };
$('prev').onclick = () => show(cur - 1, 'replace');
$('next').onclick = () => show(cur + 1, 'replace');
$('tnum').min = Math.min(...keys.map(Number)); $('tnum').max = Math.max(...keys.map(Number));
$('tnum').onchange = e => go(e.target.value, 'replace');
document.querySelectorAll('a.tlink').forEach(a => a.onclick = e => {
  e.preventDefault();
  if (go(a.dataset.task, 'push', true)) $('task').querySelector('h3').focus({preventScroll: true});
});
document.addEventListener('keydown', e => {
  if (e.target.closest('input, textarea, select') || e.metaKey || e.ctrlKey || e.altKey || !viewerOnScreen()) return;
  if (e.key === 'ArrowLeft' && cur > 0) show(cur - 1, 'replace');
  if (e.key === 'ArrowRight' && cur < T.length - 1) show(cur + 1, 'replace');
});
addEventListener('resize', fitSoon);
addEventListener('beforeprint', () => document.querySelectorAll('details:not(.pv)').forEach(d => d.open = true));
const fromHash = scroll => { const m = location.hash.match(/^#task-(\\d+)$/); return !!m && go(m[1], null, scroll); };
window.addEventListener('popstate', () => { if (!fromHash(false) && !location.hash) show(0, null); });
if (!fromHash('instant')) show(0, location.hash.startsWith('#task-') ? 'replace' : null);
"""


def e(s):
    return html.escape(str(s), quote=True)


def tlink(k, text=None):
    return f'<a class="tlink" href="#task-{k}" data-task="{k}">{text or k}</a>'


def inline(s):
    """Escape, then a small Markdown subset: task links, links, bold, code."""
    s = e(s)
    s = re.sub(r"\(task (\d+)\)", lambda m: f"<span class='ex'>({tlink(m[1], 'task ' + m[1])})</span>", s)
    s = re.sub(r"\[(\d+)\]\(#task-\d+\)", lambda m: tlink(m[1]), s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", s)


def md_block(lines, svg=""):
    """Render the methodology-style Markdown: paragraphs, bullets, numbered lists, the diagram."""
    out, lst = [], None
    for ln in lines:
        m = re.match(r"^\s*(?:- |\d+\. )(.*)", ln)
        kind = ("ol" if re.match(r"^\s*\d+\. ", ln) else "ul") if m else None
        if lst and kind != lst:
            out.append(f"</{lst}>")
            lst = None
        if m:
            if not lst:
                out.append(f"<{kind}>")
                lst = kind
            out.append(f"<li>{inline(m[1])}</li>")
        elif ln.startswith("## "):
            continue  # the <details> summary carries the section title
        elif ln.startswith("!["):
            if svg:  # the list is the text equivalent: visible on phones, visually hidden next to the diagram
                out.append(f'<div class="diagram" aria-hidden="true">{svg}</div>'
                           '<ol class="steps" aria-label="How the eval works">'
                           + "".join(f"<li>{e(x)}</li>" for x in DIAGRAM_STEPS) + "</ol>")
        elif ln.strip():
            out.append(f"<p>{inline(ln)}</p>")
    if lst:
        out.append(f"</{lst}>")
    return "\n".join(out)


def meter(c, cls=""):
    """A {"text", "value", "max"} cell as a zero-based bar beside the text (non-zero values get a visible sliver);
    a cell without a max is plain text."""
    if not c.get("max"):
        return e(c["text"])
    v = min(1, c["value"] / c["max"])
    return (f'<span class="meter {cls}" style="--v:{max(v, .03) if v else 0:.3f}">'
            f'<i aria-hidden="true"></i><span>{e(c["text"])}</span></span>')


def dot(i, ring=False):
    return f'<span class="dot{" ring" if ring else ""} e{i}" aria-hidden="true"></span>'


def providers_table(head, rows, idx, defs):
    tip = {k.lower(): v for k, v in defs}
    bars = [i > 0 and all("max" in r["cells"][i - 1] for r in rows) for i in range(len(head))]
    m = [' class="m"' if b else "" for b in bars]
    ths = "".join(f'<th role="columnheader" scope="col"{m[i]}'
                  + (f' title="{e(tip[h.lower()])}"' if h.lower() in tip else "") + f">{e(h)}</th>"
                  for i, h in enumerate(head))
    body = ""
    for r in rows:
        name, i = r["provider"], idx[r["provider"]]
        body += (f'<tr role="row"><th role="rowheader" scope="row">{dot(i)}{e(name)}</th>'
                 + "".join(f'<td role="cell"{m[k]} data-label="{e(h)}">{meter(c, f"e{i}")}</td>'
                           for k, (h, c) in enumerate(zip(head[1:], r["cells"]), 1))
                 + "</tr>")
    flip = ("<table class='eng-t'><thead><tr><td></td>"
            + "".join(f"<th scope='col'>{dot(idx[r['provider']])}{e(r['provider'])}</th>" for r in rows) + "</tr></thead><tbody>"
            + "".join(f"<tr><th scope='row'>{e(h)}</th>" + "".join(f"<td class='num'>{e(r['cells'][k]['text'])}</td>" for r in rows)
                      + "</tr>" for k, h in enumerate(head[1:]))
            + "</tbody></table>")
    return (f'<div class="card"><div class="tw eng"><table class="eng" role="table"><thead role="rowgroup">'
            f'<tr role="row">{ths}</tr></thead><tbody role="rowgroup">{body}</tbody></table>{flip}</div></div>')


def legend_html(defs):
    return ("<details class='legend'><summary>What the columns mean</summary><div class='defs'>"
            + "".join(f"<div><b>{e(t)}:</b> {e(d)}</div>" for t, d in defs) + "</div></details>")


def head_to_head(above, idx):
    """One line per pair: "● Exa 18 ━━━━ 12 Parallel ●", the bar split at the share of tasks each was ranked above."""
    out = []
    for a, b, x, n in above:
        ca, cb = (f"var(--e{idx[y]})" for y in (a, b))
        out.append(f'<div class="pair" aria-label="{e(a)} ranked above {e(b)} on {x:g} of {n} tasks">'
                   f'{dot(idx[a])}<span>{e(a)}</span><b class="num">{x:g}</b>'
                   f'<span class="bar" style="--v:{x / n if n else 0:.3f};--a:{ca};--b:{cb}" aria-hidden="true"></span>'
                   f'<b class="num">{n - x:g}</b><span>{e(b)}</span>{dot(idx[b])}</div>')
    return f'<div class="card h2h">{"".join(out)}</div>'


def verdict_html(text, pick, idx):
    m = re.match(r"(.+?\.)\s+(.+)", text, re.S)
    if not m or len(m[1]) > 60:
        return f"<p>{e(text)}</p>"
    return f'<span class="lead">{dot(idx[pick])}{e(m[1])}</span> <p>{e(m[2])}</p>'


def why_html(why, idx):
    """One hairline list per alternative provider; a filled dot marks a point against it, a ring a point in its favor."""
    out = []
    for g in why:
        items = []
        for x in g["items"]:
            also = (" <span class='also'>(also " + ("tasks " if len(x["also"]) > 1 else "task ")
                    + ", ".join(tlink(k) for k in x["also"]) + ")</span>") if x["also"] else ""
            tag = f"<span class='tag'>{e(x['tag'])}</span> " if x.get("tag") else ""
            items.append(f"<li>{dot(idx[g['provider']], x['favor'])}<span>{tag}{inline(x['text'])}{also}</span></li>")
        out.append((f"<p class='why-head'>{inline(g['head'])}</p>" if g["head"] else "")
                   + (f"<ul>{''.join(items)}</ul>" if items else ""))
    return "".join(out)


def render(d):
    head, rows = d["table"]["head"], d["table"]["rows"]
    # MCP providers cycle through 4 colors; the baseline (built-in search) is always grey ("b")
    idx = {n: i % 4 for i, n in enumerate(d["providers"])} | {d["baseline"]: "b"}
    sections = "".join(f"<details><summary>{e(title)}</summary>{body}</details>" for title, body in d["sections"])
    fallback = f"<p class='fallback'>{e(d['fallback'])}</p>" if d["fallback"] else ""
    notes = "".join(f"<p class='note'>{e(x)}</p>" for x in d.get("notes", []))
    data = json.dumps({"tasks": d["tasks"], "colors": idx, "heads": d["heads"]}).replace("<", "\\u003c")  # inert in <script>
    why = (f'<section aria-labelledby="h-why"><h2 id="h-why">Why</h2><div class="why">{why_html(d["why"], idx)}'
           '</div></section>' if d["why"] else "")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(d['title'])}</title><style>{CSS}</style></head>
<body><main>
<header><h1>{e(d['title'])}</h1>
<p class="sub">{e(d['subtitle'])}</p></header>
<section aria-labelledby="h-verdict"><h2 id="h-verdict">Verdict</h2>
<div class="card verdict">{verdict_html(d['verdict'], d['pick'], idx)}{fallback}</div></section>
<section aria-labelledby="h-providers"><h2 id="h-providers">Providers</h2>{providers_table(head, rows, idx, d['legend'])}
{legend_html(d['legend'])}{notes}</section>
<section aria-labelledby="h-h2h"><h2 id="h-h2h">Head to head</h2>{head_to_head(d['above'], idx)}
<p class="note">Tasks where the first provider's answer was ranked above the second's.</p></section>
{why}
<section aria-labelledby="tasks-section"><h2 id="tasks-section">Tasks</h2>
<div class="card viewer"><div class="nav"><span class="seg"><button id="prev" type="button" aria-label="Previous task">&#8249; Prev</button>
<button id="next" type="button" aria-label="Next task">Next &#8250;</button></span>
<label>Task <input id="tnum" type="number" inputmode="numeric"></label><span id="tpos" aria-hidden="true"></span>
<span id="tlive" class="sr" aria-live="polite"></span>
<span class="hint">Arrow keys also move between tasks.</span></div>
<div id="task"></div></div></section>
<section aria-labelledby="h-more"><h2 id="h-more">More</h2><div class="sections">{sections}</div></section>
</main>
<script type="application/json" id="data">{data}</script>
<script>{JS}</script>
</body></html>
"""
