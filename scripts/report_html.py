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
 --track:light-dark(#e8ebf0,#262b34);--on-bar:light-dark(#fff,#0f1115);
 --r:12px;--shadow:0 1px 2px rgb(16 24 40/.05),0 1px 3px rgb(16 24 40/.06)}
:root[data-theme=light]{color-scheme:light}:root[data-theme=dark]{color-scheme:dark;--shadow:none}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--shadow:none}}
*{box-sizing:border-box}
html{scroll-padding-top:4.5rem}
body{margin:0;background:var(--bg);color:var(--text);
 font:16px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased}
main{max-width:64rem;margin:0 auto;padding:56px 16px 96px}
h1,h2,h3{text-wrap:balance;line-height:1.2;margin:0}p,li{text-wrap:pretty;overflow-wrap:anywhere}
header{padding-bottom:28px}
.eyebrow{font-size:.8rem;font-weight:650;letter-spacing:.06em;text-transform:uppercase;color:var(--accent);margin:0 0 10px}
.hero{margin-top:8px}
h1{font-size:clamp(1.6rem,3.5vw,2.2rem);letter-spacing:-.025em;font-weight:750;max-width:30ch}
h2{font-size:1.5rem;letter-spacing:-.015em;font-weight:700;margin:72px 0 6px}
.dek{color:var(--muted);margin:0 0 18px;font-size:.95rem}
h3{font-size:1.2rem;font-weight:650;letter-spacing:-.01em}
a{color:var(--accent);text-underline-offset:2px}a:hover{text-decoration-thickness:2px}
code{font:.9em ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--inset);padding:1px 5px;border-radius:5px}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
.sub{color:var(--muted);margin:12px 0 0;font-size:.98rem;font-variant-numeric:tabular-nums}
.num{font-variant-numeric:tabular-nums}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow);overflow:clip}
.prose{max-width:72ch}
.verdict{padding:28px 32px;font-size:1.05rem;background:linear-gradient(180deg,var(--accent-soft),var(--surface) 75%);
 box-shadow:0 0 0 1px color-mix(in srgb,var(--accent) 18%,transparent),var(--shadow);border-color:transparent}
.verdict .lead{display:flex;align-items:center;font-size:2.2rem;font-weight:750;letter-spacing:-.02em;line-height:1.2;
 margin-bottom:10px}
.verdict .lead .dot{width:.42em;height:.42em;margin-right:.35em}
.verdict p{margin:0;max-width:64ch;color:var(--muted);font-size:.98rem}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(8.5rem,1fr));gap:12px;margin:18px 0 20px}
.kpi b small{display:block;font-size:.5em;font-weight:500;color:var(--muted);margin-top:2px}
@media (min-width:860px){.verdict.has-kpis{display:grid;grid-template-columns:1fr minmax(19rem,24rem);gap:6px 36px;align-items:start}.verdict.has-kpis .lead{grid-column:1}.verdict.has-kpis>p{grid-column:1}.verdict.has-kpis .kpis{grid-column:2;grid-row:1/span 3;grid-template-columns:1fr 1fr;margin:0}}
.kpi{background:color-mix(in srgb,var(--surface) 70%,transparent);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.kpi b{display:block;font-size:1.55rem;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.2}
.kpi span{font-size:.82rem;color:var(--muted)}.fallback{color:var(--muted);font-size:.9rem;margin:10px 0 0}
.dot{display:inline-block;width:.6em;height:.6em;border-radius:50%;background:var(--c);margin-right:.5em;
 vertical-align:.05em;flex:none}
.e0{--c:var(--e0)}.e1{--c:var(--e1)}.e2{--c:var(--e2)}.e3{--c:var(--e3)}.eb{--c:var(--eb)}
.tw{container-type:inline-size;padding:0 18px}.tw.task{container-name:task}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch;overscroll-behavior-x:contain}
table.eng{min-width:52rem}.eng th,.eng td{padding:14px 11px}.eng thead th{white-space:nowrap}
.eng tbody tr.pick>*{background:color-mix(in srgb,var(--accent-soft) 70%,var(--surface))}.eng tr.pick>th{box-shadow:inset 3px 0 0 var(--c)}
.scroll.can-scroll{background:linear-gradient(to left,var(--surface),transparent 28px) right/28px 100% no-repeat local,radial-gradient(farthest-side at 100% 50%,light-dark(rgb(0 0 0/.2),rgb(255 255 255/.16)),transparent) right/14px 100% no-repeat scroll}
.swipe{display:none;color:var(--muted);font-size:.85rem;margin:8px 2px 0}@media (max-width:600px){.swipe.on{display:block}}
.eng tr>:first-child{position:sticky;left:0;z-index:1;background:var(--surface);padding-left:20px;padding-right:16px}
.eng tr>:last-child{padding-right:20px}
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
.h2h{padding:4px 22px}
.vs{padding:16px 0;border-bottom:1px solid var(--line)}.vs:last-child{border-bottom:0}
.vs .top{display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin-bottom:8px}
.vs .side{display:flex;align-items:baseline;gap:8px;min-width:0}.vs .side.r{flex-direction:row-reverse;text-align:right}
.vs .dot{margin:0;align-self:center}@media (max-width:480px){.vs .pc{display:none}}
.vs .nm{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vs .ct{font-size:1.25rem;font-weight:750;font-variant-numeric:tabular-nums;line-height:1}
.vs .pc{color:var(--muted);font-size:.85rem;font-variant-numeric:tabular-nums}
.split{display:flex;height:14px;border-radius:7px;overflow:hidden;gap:3px}
.split .a{background:var(--a)}.split .b{background:var(--b)}
.note{color:var(--muted);font-size:.88rem;margin:12px 2px 0}
.why-g{padding:18px 22px 6px}.why-g+.why-g{margin-top:16px}
.why .why-head{display:flex;align-items:center;font-size:1.08rem;font-weight:700;margin:0;padding-bottom:10px}
.why li>:last-child{max-width:70ch}
.why ul{list-style:none;padding:0;margin:0}
.why li{display:grid;grid-template-columns:7.5rem 1fr;gap:4px 16px;align-items:baseline;padding:14px 0;margin:0;
 border-top:1px solid var(--line)}
.why .favor-head{font-size:.85rem;font-weight:600;color:var(--muted);margin:0;padding:18px 0 4px;border-top:1px solid var(--line)}
.why .favor-head+ul li:first-child{border-top:0}
.why details.also{display:inline}.why details.also summary{display:inline;cursor:pointer;color:var(--muted);font-size:.82rem;
 list-style:none;margin-left:6px;white-space:nowrap;border:1px solid var(--line-strong);border-radius:999px;padding:0 8px;font-weight:600}.why details.also summary::-webkit-details-marker{display:none}
.why details.also summary::before{content:none}.why details.also[open] summary{color:var(--text)}
.why details.also div{margin-top:6px;font-size:.88rem;color:var(--muted)}
@media (max-width:600px){.why-g{padding:14px 16px 4px}.why li{display:block}.why li .tag::after{content:" · ";color:var(--muted)}}
.dot.ring{background:none;box-shadow:inset 0 0 0 2px var(--c)}
.also{color:var(--muted)}
.tag{font-size:.88rem;font-weight:650;color:var(--text);white-space:nowrap}
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
select{height:34px;font:inherit;font-size:.92rem;border-radius:8px;border:1px solid var(--field);background:var(--surface);color:var(--text);padding:0 8px;max-width:min(40rem,100%);min-width:0;text-overflow:ellipsis}
.nav label.pick{position:relative;flex:0 1 auto;min-width:0;margin-left:0}.nav label.pick select{appearance:none;-webkit-appearance:none;width:100%;padding:0 34px 0 12px;font-weight:500;cursor:pointer}.nav label.pick::after{content:"";position:absolute;right:14px;top:50%;width:7px;height:7px;pointer-events:none;border-right:2px solid var(--muted);border-bottom:2px solid var(--muted);transform:translateY(-70%) rotate(45deg)}
#task{padding:24px 20px 20px}
#task h3:focus{outline:none}#task h3:focus-visible{outline:2px solid var(--accent)}
.tn{display:block;font-size:.85rem;color:var(--muted);font-weight:600;
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
.label{display:block;font-size:.85rem;color:var(--muted);font-weight:600;
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
.pv>summary{font-weight:600}.pv>summary .cnt{margin-left:auto;font-weight:500}.pv>summary::before{order:9;margin-left:12px}#task .reason{margin:18px 0 0}
.task table.task{width:auto;min-width:min(40rem,100%)}.task th,.task td{padding-inline:12px 28px}.pv .body{display:grid;gap:12px;min-width:0}
.pv pre{white-space:pre-wrap;overflow-wrap:anywhere;margin:0;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
 background:var(--soft,rgba(127,127,127,.08));border-radius:8px;padding:10px 12px;max-height:28em;overflow:auto}
.pv ol.calls{padding-left:22px;margin:0}.pv ol.calls>li{margin:0 0 16px}
.pv ol.hits{margin:8px 0;padding-left:28px}.pv ol.hits li{margin:3px 0;overflow-wrap:anywhere}
.pv .cited{color:var(--good,#2f9e44);font-weight:650;font-size:.82rem;margin-left:4px}
#task .pv details.calls{border-top:0;margin:0}#task .pv details.calls>summary{padding:6px 0;font-size:.92rem}
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
 .h2h{padding:4px 16px}.tw{padding:0 16px}#task{padding:20px 16px}.nav{padding:10px 12px}.nav label{margin-left:0}}
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
 .why li,.vs,tr,.verdict,.errs li,.diagram{break-inside:avoid}.ex a.tlink{background:none;padding:0}}
""" + STACK.format(name="task", width="40rem") + """
@container task (width < 40rem){.task tr{grid-template-columns:repeat(3,minmax(0,1fr));gap:10px 12px;padding:12px 0}
 .task td::before{font-size:.62rem}}
"""

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
const safeUrl = u => /^https?:\\/\\//i.test(u || '') ? u : '';
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
    (calls ? `<details class="calls"><summary>Tool calls (${p.calls.length})</summary><ol class="calls">${calls}</ol></details>` : '<p class="meta">No tool calls.</p>') + '</div>';
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
  $('tpick').value = t.n;
  $('tlive').textContent = `Task ${t.n}, ${cur + 1} of ${T.length}${t.title ? ': ' + t.title : ''}`;
  const moved = document.activeElement;
  $('prev').disabled = cur === 0; $('next').disabled = cur === T.length - 1;
  if (moved && moved.disabled) (moved === $('prev') ? $('next') : $('prev')).focus();
  const heads = ['Provider', H.rank, 'Recall', H.error, 'Context', 'Time'];
  const cls = ['', ' class="c"', ' class="m"', ' class="m"', '', ''];
  const td = (k, v) => `<td role="cell"${cls[k]} data-label="${esc(heads[k])}">${v}</td>`;
  const rows = t.rows.map(r => `<tr role="row"><th role="rowheader" scope="row">${dot(r.provider)}${esc(r.provider)}</th>${td(1, rank(r.rank, r.provider))}` +
    `${td(2, meter(r.recall, ec(r.provider)))}${td(3, chip(r.error, r.sev))}${td(4, esc(r.tokens))}${td(5, esc(r.time))}</tr>`).join('');
  const box = $('task');
  box.innerHTML = `
    <h3 tabindex="-1"><span class="tn">Task ${esc(t.n)} of ${T.length}</span>${t.title ? ' ' + esc(t.title) : ''}</h3>
    <div class="prompt"><div class="clamp task-text" id="ttext">${esc(t.text)}</div>
      <button class="more" id="tmore" type="button" aria-expanded="false" aria-controls="ttext" data-more="Show full prompt" hidden>Show full prompt</button></div>
    <div class="reason prose"><span class="label">Why this order</span> ${linkify(t.ranking_reason)}</div>
    <div class="tw task"><table class="task" role="table"><thead role="rowgroup"><tr role="row">${heads.map((h, k) => `<th role="columnheader" scope="col"${cls[k]}>${esc(h)}</th>`).join('')}</tr></thead>
      <tbody role="rowgroup">${rows}</tbody></table></div>
    ${t.cap_note ? `<p class="note">${esc(t.cap_note)}</p>` : ''}
    <details><summary>What a complete answer needed</summary>
      <div class="prose">${list(t.direct)}${t.other.length ? '<p>Also expected:</p>' + list(t.other) : ''}
      <p><b>Judge's comparison:</b> ${linkify(t.rationale)}</p></div>
    </details>
    <div class="label" style="margin-top:24px">Answers</div>
    ${t.providers.map((p, i) => { const r = (t.rows.find(x => x.provider === p.provider) || {}).rank || '';
      return `<details class="pv" data-i="${i}"${r.split(' ')[0] === '1' ? ' open' : ''}><summary>${dot(p.provider)}${esc(p.provider)}'s answer ` +
      `${rank(r.split(' ')[0], p.provider)}<span class="meta cnt">${p.calls.length} tool call${p.calls.length === 1 ? '' : 's'}</span></summary></details>`; }).join('')}`;
  const build = d => { if (d.open && !d.dataset.built) { d.insertAdjacentHTML('beforeend', pvBody(t.providers[+d.dataset.i])); d.dataset.built = 1; } };
  box.querySelectorAll('details.pv').forEach(d => { build(d); d.addEventListener('toggle', () => build(d)); });
  if (how) { box.classList.remove('swap'); void box.offsetWidth; box.classList.add('swap'); }
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
  if (i < 0) { $('tpick').value = T[cur].n; return false; }
  show(i, how);
  if (scroll) $('tasks-section').scrollIntoView({behavior: scroll === 'instant' || matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'});
  return true;
}
const viewerOnScreen = () => { const r = $('task').closest('.viewer').getBoundingClientRect(); return r.bottom > 0 && r.top < innerHeight; };
$('prev').onclick = () => show(cur - 1, 'replace');
$('next').onclick = () => show(cur + 1, 'replace');
const winner = t => (t.rows.find(r => r.rank === '1' || r.rank.startsWith('1 ')) || {}).provider;
$('tpick').innerHTML = T.map(t => `<option value="${esc(t.n)}">${esc(t.n)} · ${esc(t.title || 'Task ' + t.n)}${winner(t) ? ' — best: ' + esc(winner(t)) : ''}</option>`).join('');
$('tpick').onchange = e => go(e.target.value, 'replace');
document.querySelectorAll('a.tlink').forEach(a => a.onclick = e => {
  e.preventDefault();
  if (go(a.dataset.task, 'push', true)) $('task').querySelector('h3').focus({preventScroll: true});
});
document.addEventListener('keydown', e => {
  if (e.target.closest('input, textarea, select') || e.metaKey || e.ctrlKey || e.altKey || !viewerOnScreen()) return;
  if (e.key === 'ArrowLeft' && cur > 0) show(cur - 1, 'replace');
  if (e.key === 'ArrowRight' && cur < T.length - 1) show(cur + 1, 'replace');
});
const sc = document.querySelector('.scroll');
const cue = () => { const on = sc.scrollWidth > sc.clientWidth + 1; sc.classList.toggle('can-scroll', on); $('swipe').classList.toggle('on', on); };
cue(); addEventListener('resize', cue);
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
    s = re.sub(r"\(task (\d+)\)", lambda m: f"<span class='ex'>{tlink(m[1], 'task ' + m[1])}</span>", s)
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


SHORT = {"Monthly cost": "Cost / month", "Context tokens": "Context", "List fee per task": "Fee / task",
         "Errors (main / side)": "Errors main / side"}  # one-line headers; the legend has the full names


def providers_table(head, rows, idx, defs, pick=None):
    tip = {k.lower(): v for k, v in defs}
    bars = [i > 0 and all("max" in r["cells"][i - 1] for r in rows) for i in range(len(head))]
    m = [' class="m"' if b else "" for b in bars]
    ths = "".join(f'<th role="columnheader" scope="col"{m[i]}'
                  + (f' title="{e(tip[h.lower()])}"' if h.lower() in tip else "") + f">{e(SHORT.get(h, h))}</th>"
                  for i, h in enumerate(head))
    body = ""
    for r in rows:
        name, i = r["provider"], idx[r["provider"]]
        cls = f' class="pick e{i}"' if name == pick else ""
        body += (f'<tr role="row"{cls}>'
                 f'<th role="rowheader" scope="row">{dot(i)}{e(name)}</th>'
                 + "".join(f'<td role="cell"{m[k]} data-label="{e(h)}">{meter(c, f"e{i}")}</td>'
                           for k, (h, c) in enumerate(zip(head[1:], r["cells"]), 1))
                 + "</tr>")
    return (f'<div class="card"><div class="scroll" tabindex="0" role="region" aria-label="Providers table, scrolls '
            f'sideways"><table class="eng" role="table"><thead role="rowgroup">'
            f'<tr role="row">{ths}</tr></thead><tbody role="rowgroup">{body}</tbody></table></div></div>'
            '<p class="swipe" id="swipe">Swipe the table for errors, cost and time &rarr;</p>')


def legend_html(defs):
    return ("<details class='legend'><summary>What the columns mean</summary><div class='defs'>"
            + "".join(f"<div><b>{e(t)}:</b> {e(d)}</div>" for t, d in defs) + "</div></details>")


def head_to_head(above, idx):
    """One row per pair: "Exa [18 ████|██ 12] Parallel", each side's share of the tasks where it was ranked above."""
    out = []
    for a, b, x, n in above:
        ca, cb = (f"var(--e{idx[y]})" for y in (a, b))
        y = n - x
        pa, pb = (f"{round(100 * v / n)}%" if n else "" for v in (x, y))
        out.append(f'<div class="vs" role="img" aria-label="{e(a)} ranked above {e(b)} on {x:g} of {n} tasks; '
                   f'{e(b)} above {e(a)} on {y:g}"><div class="top" aria-hidden="true">'
                   f'<span class="side">{dot(idx[a])}<span class="nm">{e(a)}</span><span class="ct">{x:g}</span><span class="pc">{pa}</span></span>'
                   f'<span class="side r">{dot(idx[b])}<span class="nm">{e(b)}</span><span class="ct">{y:g}</span><span class="pc">{pb}</span></span></div>'
                   f'<span class="split" style="--a:{ca};--b:{cb}" aria-hidden="true">'
                   f'<span class="a" style="flex:{x:g}"></span><span class="b" style="flex:{y:g}"></span></span></div>')
    return f'<div class="card h2h">{"".join(out)}</div>'


KPI = ["Ranked best", "Recall", "Errors (main / side)", "Monthly cost"]  # the pick's cells shown as tiles


def verdict_html(text, pick, idx, head=(), row=None):
    tiles = ""
    if row:
        cells = dict(zip(head[1:], row["cells"]))
        val = lambda t: re.sub(r"^(.+?) \((.+)\)$", r"\1<small>\2</small>", e(t))  # noqa: E731
        tiles = "".join(f"<div class='kpi'><b>{val(cells[h]['text'])}</b><span>{e(h)}</span></div>" for h in KPI if h in cells)
        tiles = f"<div class='kpis'>{tiles}</div>" if tiles else ""
    m = re.match(r"(.+?\.)\s+(.+)", text, re.S)
    if not m or len(m[1]) > 60:
        return f"{tiles}<p>{e(text)}</p>"
    return f'<span class="lead">{dot(idx[pick])}{e(m[1])}</span>{tiles}<p>{e(m[2])}</p>'


def why_html(why, idx):
    """Per alternative provider: its head, one row per point (the Providers column it explains, then the point and its
    example; the other tasks fold under "+N tasks"), and the point in its favor under its own label."""
    def li(x):
        also = (f"<details class='also'><summary>+{len(x['also'])} task{'s' if len(x['also']) > 1 else ''}</summary>"
                f"<div>Also {', '.join(tlink(k) for k in x['also'])}</div></details>") if x["also"] else ""
        text = re.sub(r"^though you'd have ", "you'd have ", x["text"]) if x["favor"] else x["text"]
        return f"<li><span class='tag'>{e(x.get('tag', ''))}</span><span>{inline(text)}{also}</span></li>"
    out = []
    for g in why:
        against = [x for x in g["items"] if not x["favor"]]
        favor = [x for x in g["items"] if x["favor"]]
        out.append("<div class='card why-g'>"
                   + (f"<p class='why-head'>{dot(idx[g['provider']])}{inline(g['head'])}</p>" if g["head"] else "")
                   + (f"<ul>{''.join(map(li, against))}</ul>" if against else "")
                   + (f"<p class='favor-head'>In {e(g['provider'])}'s favor</p><ul>{''.join(map(li, favor))}</ul>"
                      if favor else "") + "</div>")
    return "".join(out)


def render(d):
    head, rows = d["table"]["head"], d["table"]["rows"]
    # MCP providers cycle through 4 colors; the baseline (built-in search) is always grey ("b")
    idx = {n: i % 4 for i, n in enumerate(d["providers"])} | {d["baseline"]: "b"}
    sections = "".join(f"<details><summary>{e(title)}</summary>{body}</details>" for title, body in d["sections"])
    fallback = f"<p class='fallback'>{e(d['fallback'])}</p>" if d["fallback"] else ""
    notes = "".join(f"<p class='note'>{e(x)}</p>" for x in d.get("notes", []))
    data = json.dumps({"tasks": d["tasks"], "colors": idx, "heads": d["heads"]}).replace("<", "\\u003c")  # inert in <script>
    pick_row = next((r for r in rows if r["provider"] == d["pick"]), None)
    why = (f'<section aria-labelledby="h-why"><h2 id="h-why">Why</h2>'
           f'<p class="dek">What you\'d have experienced with another provider, by Providers column.</p>'
           f'<div class="why">{why_html(d["why"], idx)}'
           '</div></section>' if d["why"] else "")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(d['title'])}</title><style>{CSS}</style></head>
<body><main>
<header><p class="eyebrow">my-search-eval report</p><h1>{e(d['title'])}</h1>
<p class="sub">{e(d['subtitle'])}</p></header>
<section aria-labelledby="h-verdict" class="hero"><h2 id="h-verdict" class="sr">Verdict</h2>
<div class="card verdict{' has-kpis' if pick_row else ''}">{verdict_html(d['verdict'], d['pick'], idx, head, pick_row)}{fallback}</div></section>
<section aria-labelledby="h-providers"><h2 id="h-providers">Providers</h2>
<p class="dek">How each provider did, per task.</p>{providers_table(head, rows, idx, d['legend'], d['pick'])}
{legend_html(d['legend'])}{notes}</section>
<section aria-labelledby="h-h2h"><h2 id="h-h2h">Head to head</h2>
<p class="dek">Tasks where each provider's answer was ranked above the other's.</p>{head_to_head(d['above'], idx)}</section>
{why}
<section aria-labelledby="tasks-section"><h2 id="tasks-section">Tasks</h2>
<p class="dek">Every task, with each provider's answer and the judge's reasoning.</p>
<div class="card viewer"><div class="nav"><span class="seg"><button id="prev" type="button" aria-label="Previous task">&#8249; Prev</button>
<button id="next" type="button" aria-label="Next task">Next &#8250;</button></span>
<label class="pick"><span class="sr">Task</span><select id="tpick"></select></label>
<span id="tlive" class="sr" aria-live="polite"></span>
</div>
<div id="task"></div></div></section>
<section aria-labelledby="h-more"><h2 id="h-more">More</h2><div class="sections">{sections}</div></section>
</main>
<script type="application/json" id="data">{data}</script>
<script>{JS}</script>
</body></html>
"""
