"""Report: terminal summary plus report.md (same summary, per-task data, methodology).

Usage: python3 -I report.py TASKS_JSONL GRADE_DIR RUNS_DIR[,RUNS_DIR...] OUT_MD
       [--harvest HARVEST_JSONL] [--model M] [--judge-model M] [--cache-dir DIR] [--stats STATS_JSON]
Writes OUT_MD, report.html, terminal.txt, method.svg and results.jsonl.gz next to it. --harvest (harvest.py output) gives the
volume and the sample line; config.json and stats.json next to TASKS_JSONL (the run folder) give the sample, the
planned task count and what was lost. Run directly, it's a developer tool; files are still written owner-only.
Reads GRADE_DIR/<n>/grade-*.json (grade.py) and RUNS_DIR/<n>/<provider>.jsonl (time, MCP use, date).
Prints the terminal summary and writes OUT_MD. Outcomes come from the judge's ranking of the answers (one judge
call per task). The verdict is one LLM judgment over every metric (verdict.md), checked so it cites only numbers
in its input.
Why: code splits each rank difference by the judge's recall and error grades; one writing call (report.md) names
the cause for fixed groups of tasks, plus task titles; a check call (verify-why.md) trims each bullet's tasks.
Writing calls are cached in --cache-dir (default: llm-cache next to OUT_MD), keyed by their full input (grades,
notes, prompts, model), so re-rendering the same grades gives the same Why.
"""

import json
import os
import re
import statistics as st
import sys
import textwrap
from collections import Counter
from datetime import date, datetime
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import (ASSETS, BASELINE, PROVIDER_INFO, PROVIDERS, PROMPTS, SETUP_CHECKED, Cache, UsageLimit, ask, cached, local_scrub,  # noqa: E402
                    read_jsonl, write_text)
from grade import evidence_budget  # noqa: E402
from cost import CHECKED, FREE, cost_usd, provider_calls, monthly_usd, task_volume, unpriced  # noqa: E402
from judge_runs import (MAX_EVIDENCE, MAX_RESULT, final, read_log, run_evidence, text_of, tool_calls,  # noqa: E402
                        tool_pairs)

NAME = {e: v["name"] for e, v in PROVIDER_INFO.items()}
LONG = {e: v.get("long", v["name"]) for e, v in PROVIDER_INFO.items()}  # in prose; tables use NAME
MCP_PREFIX = {e: f"mcp__{e}__" for e in PROVIDERS if e != BASELINE}
WIDTH = 96
INTS = {"type": "array", "items": {"type": "integer"}}
ERR = {"none": "none", "limited": "side", "critical": "main"}  # a grade's error, as the report names it


def cell(error):
    """"side point", "main point" or "none"."""
    return ERR[error] + (" point" if error != "none" else "")


def cap_note(names):
    """The per-task note naming the providers whose evidence the judge saw only part of."""
    if not names:
        return ""
    who = " and ".join(f"{n}'s" for n in names)
    return f"The judge saw part of {who} evidence (it exceeded the judge's budget)."


def obj(props):
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


SEV = {"none": 0, "limited": 1, "critical": 2}
WHY_SCHEMA = obj({
    "bullets": {"type": "array", "maxItems": 5, "items": obj({"group": {"type": "string"}, "text": {"type": "string"},
                                                               "tasks": INTS})},
    "titles": {"type": "array", "items": obj({"task": {"type": "integer"}, "title": {"type": "string"}})}})
MIN_GROUP = 3  # tasks a Why statement needs, in its set and after the check
CHECK_SCHEMA = obj({"bullets": {"type": "array", "items": obj({"bullet": {"type": "integer"}, "holds": INTS})}})


def evidence_stats(lines):
    """(characters of search and fetch results, uncapped; whether the judge's evidence caps cut this run)."""
    sizes = [len(text_of(res.get("content"))) for _, res in tool_pairs(lines) if res is not None]
    return sum(sizes), any(n > MAX_RESULT for n in sizes) or run_evidence(lines)[2]


def run_stats(lines, provider):
    """(seconds, whether the run called its provider's MCP, date) from a parsed run log. A run with a rate-limit
    refusal never reaches the report: it's rerun, or its task is left out (orchestrate.replay)."""
    mcp = [res for call, res in tool_pairs(lines) if call["name"].startswith(MCP_PREFIX.get(provider, "\0"))]
    day = next((_iso(t).date().isoformat() for x in lines if (t := x.get("timestamp"))), None)  # local, like the run folder
    return final(lines).get("duration_ms", 0) / 1000, bool(mcp), day


VERB = r"(?:\w+(?:s|ed|ly)|both|and|or|nor|but|each|never|would|also|had|has|got|found|cites|adds|says|leans)\b"


def unlabel(text, labels):
    """Replace a judge's run labels (A, B, C) with provider names; "A" stays when it reads as an article."""
    names = {k: NAME[v] for k, v in labels.items()}
    ks = "".join(names)
    text = re.sub(rf"\bRuns ([{ks}]) and ([{ks}])\b", lambda m: f"{names[m[1]]} and {names[m[2]]}", text)
    text = re.sub(rf"\bRun ([{ks}])\b", lambda m: names[m[1]], text)

    def bare(m):
        label, after, before = m[1], text[m.end():], text[:m.start()]
        if label != "A":
            return names[label]
        if re.match(r"\s+[A-Z]", after) or re.search(r"\b(?:Series|Plan|Option|Type|Grade|Class|Part)\s+$", before):
            return m[0]  # "A Team", "Series A"
        if not re.search(r"(?:^|[.!?:(]\s*)$", before):  # mid-sentence "A" is never an article
            return names[label]
        return names[label] if after.startswith("'s") or re.match(rf"\s+{VERB}|[,;:.)]", after) else m[0]

    text = re.sub(rf"(?<![/\w.-])([{ks}])(?![\w/-])", bare, text)
    return re.sub(r"(^|[.!?]\s+|\(\s*)built-in", r"\1Built-in", text)


def wrap(text, indent="  ", first=None):
    return textwrap.wrap(text, WIDTH, initial_indent=first if first is not None else indent,
                         subsequent_indent=indent, break_on_hyphens=False)


def fmt_k(tokens):
    """Tokens are estimated at 4 characters each, so shown with a tilde."""
    return f"~{tokens / 1000:.0f}k"


def load(tasks_path, grade_dir, runs_dirs):
    """(tasks, run dates, grade files skipped because their ranks don't match the configured providers). Each run log
    is parsed once; its init line is kept for run_config."""
    tasks = {str(json.loads(ln)["n"]): json.loads(ln) for ln in Path(tasks_path).read_text().splitlines() if ln.strip()}
    runs = {d.name: d for r in runs_dirs.split(",") for d in Path(r).iterdir() if d.is_dir()}
    T, dates, skipped = {}, set(), 0
    for n in tasks:
        gs = [json.loads(p.read_text()) for p in sorted(Path(grade_dir, n).glob("grade-*.json"))]
        tid = tasks[n].get("id")
        bad = [g for g in gs if set(g.get("rank", {})) != set(PROVIDERS) or (tid and g.get("task_id", tid) != tid)]
        if bad:
            skipped += len(bad)
            gs = [g for g in gs if g not in bad]
        if not gs or n not in runs or not all((runs[n] / f"{e}.jsonl").exists() for e in PROVIDERS):
            continue
        per, init = {}, {}
        for e in PROVIDERS:
            a = [g["answers"][e] for g in gs]
            lines = read_log(runs[n] / f"{e}.jsonl")
            init[e] = next((d for d in lines if d.get("subtype") == "init"), {})
            secs, used, day = run_stats(lines, e)
            chars, capped = evidence_stats(lines)
            capped = capped or any(e in g.get("capped", []) for g in gs)
            dates.add(day)
            per[e] = {"rank": st.mean(g["rank"][e] for g in gs), "ranks": [g["rank"][e] for g in gs],
                      "first": st.mean(g["rank"][e] == 1 for g in gs),
                      "recall": st.mean(x["recall"] for x in a),
                      "side": st.mean(x["error"] == "limited" for x in a),
                      "main": st.mean(x["error"] == "critical" for x in a),
                      "errors": [x["error"] for x in a], "sev": st.mean(SEV[x["error"]] for x in a),
                      "tokens": chars // 4, "capped": capped, "secs": secs, "used": used, "unpriced": unpriced(lines),
                      "usd": cost_usd(lines) if cost_usd else None,
                      "calls": provider_calls(lines) if provider_calls else 0}
        T[n] = {"task": tasks[n], "grades": gs, "e": per, "dir": runs[n], "init": init,
                "judge_model": next((g.get("model") for g in gs if g.get("model")), None)}
    return T, sorted(d for d in dates if d), skipped


def read_harvest(harvest):
    """(records, their sorted timestamps) from the harvest log (every research task found in the transcripts)."""
    if not harvest or not Path(harvest).exists():
        return [], []
    rs = [json.loads(ln) for ln in Path(harvest).read_text().splitlines() if ln.strip()]
    return rs, sorted(t for r in rs if (t := r.get("timestamp")))


def harvest_stats(rs, ts):
    """{"tasks", "searches", "first", "last"}, or None without timestamps."""
    return {"tasks": len(rs), "searches": sum(len(r.get("calls") or []) for r in rs),
            "first": _iso(ts[0]), "last": _iso(ts[-1])} if ts else None


def span(a, b, year=True):
    """A date span: "Sep 14 to Nov 18, 2026" (years shown when they differ); without the year, "Nov 19",
    "Nov 19-20" or "Nov 19 to Dec 2"."""
    if year:
        if a.year != b.year:
            return f"{a:%b} {a.day}, {a.year} to {b:%b} {b.day}, {b.year}"
        return f"{a:%b} {a.day} to {b:%b} {b.day}, {b.year}"
    if a == b:
        return f"{a:%b} {a.day}"
    return f"{a:%b} {a.day}-{b.day}" if (a.year, a.month) == (b.year, b.month) else f"{a:%b} {a.day} to {b:%b} {b.day}"


def redact(server):
    """An MCP server's address without secrets: scheme and host for http; the command's basename for stdio. Keyed
    when userinfo, a key-like query, headers, env, or a long token-like path segment carries a credential."""
    from urllib.parse import urlsplit
    url = server.get("url")
    if url:
        u = urlsplit(url)
        keyed = bool(u.username or u.password or server.get("headers")
                     or re.search(r"key|token|secret|auth", u.query, re.I)
                     or any(re.fullmatch(r"[A-Za-z0-9_-]{20,}", seg) for seg in u.path.split("/")))
        return f"{server.get('type', 'http')} MCP server at {u.scheme}://{u.hostname}", keyed
    cmd = Path(str(server.get("command", "?"))).name
    keyed = bool(server.get("env")) or any(re.search(r"key|token", str(a), re.I) for a in server.get("args", []))
    return f"stdio MCP server ({cmd})", keyed



def run_config(T):
    """Per provider: connection (redacted), whether it used an API key, and the search tools the run had; plus the
    models and Claude Code versions that ran the tasks, counted over all tasks."""
    out, models, versions = {}, Counter(), Counter()
    for k, t in T.items():
        for e in PROVIDERS:
            init = t["init"][e]
            if e == PROVIDERS[0]:
                models[init.get("model") or "unknown"] += 1
                versions[init.get("claude_code_version") or "unknown"] += 1
            if e in out:
                continue
            cfg = t["dir"] / f"{e}-mcp.json"
            servers = json.loads(cfg.read_text()).get("mcpServers", {}) if cfg.exists() else {}
            conns = [redact(v) for v in servers.values()]
            known = cfg.exists() or e == BASELINE
            out[e] = {"connection": "; ".join(c + (", with an API key" if key else ", keyless") for c, key in conns)
                      or ("no MCP server" if known else "unknown (no MCP config saved with the runs)"),
                      "keyless": (bool(conns) and not any(key for _, key in conns)) if known else None,
                      "tools": [x for x in init.get("tools", []) if x.startswith(("Web", "mcp__"))]}
    return out, models, versions


def counted(c):
    """"2.1.328 (10 tasks) and 2.1.330 (20 tasks)", or the single value."""
    if len(c) == 1:
        return next(iter(c))
    return " and ".join(f"{v} ({n} tasks)" for v, n in sorted(c.items()))


def reruns(runs_dirs, T):
    """Runs rerun after an MCP connection failure: (task, provider) pairs from connection-failures.jsonl."""
    out = set()
    for r in runs_dirs.split(","):
        for d in read_jsonl(Path(r) / "connection-failures.jsonl"):
            if str(d.get("task")) in T and d.get("provider") in PROVIDERS:
                out.add((str(d["task"]), d.get("provider")))
    return sorted(out, key=lambda x: int(x[0]))


def compute(T):
    """Per-provider means, and per pair (tasks ranked above, below, tied)."""
    agg = {e: {k: st.mean(t["e"][e][k] for t in T.values()) for k in ("recall", "side", "main", "tokens", "secs")}
           for e in PROVIDERS}
    for e in PROVIDERS:
        agg[e]["best"] = sum(t["e"][e]["first"] for t in T.values())
        usd = [t["e"][e]["usd"] for t in T.values()]
        agg[e]["usd"] = st.mean(usd) if None not in usd else None
        agg[e]["calls"] = st.mean(t["e"][e]["calls"] for t in T.values())
        agg[e]["unpriced"] = [x for t in T.values() for x in t["e"][e]["unpriced"]]
    pairs = {}
    for a in PROVIDERS:
        for b in PROVIDERS:
            if a != b:
                d = [t["e"][b]["rank"] - t["e"][a]["rank"] for t in T.values()]
                pairs[(a, b)] = (sum(x > 0 for x in d), sum(x < 0 for x in d), sum(x == 0 for x in d))
    order = sorted(PROVIDERS, key=lambda e: (-agg[e]["best"], PROVIDERS.index(e)))
    return agg, pairs, order


def usd(v):
    return "$0" if v == 0 else f"${v:.3f}"


def volume(ts):
    """{"per_month", "peak"}: research tasks per month and on the busiest day, from the harvest log's sorted
    timestamps; None if unknown or under two weeks of history."""
    if not task_volume or len(ts) < 2 or (_iso(ts[-1]) - _iso(ts[0])).days < 14:
        return None
    per_month, peak = task_volume(ts)
    return {"per_month": per_month, "peak": peak}


def _iso(t):
    """A transcript timestamp in the person's local time."""
    return datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone()


def bill(agg, vol):
    """Per provider: what the person would pay a month at their volume, after free tiers (cost.monthly_usd)."""
    for e in PROVIDERS:
        if e == BASELINE:
            agg[e]["bill"] = {"after_free_usd": 0.0, "list_usd": 0.0, "included": True}
        elif vol and agg[e]["usd"] is not None and e in (FREE or {}):
            agg[e]["bill"] = monthly_usd(e, vol["per_month"], agg[e]["usd"], agg[e]["calls"], vol["peak"])
        else:
            agg[e]["bill"] = None


def pay(x):
    b = x["bill"]
    if b is None:
        return None
    if b.get("included"):
        return "$0"
    return "$0 (free tier)" if b["after_free_usd"] < 0.5 else f"${b['after_free_usd']:.0f}/mo"


def unpriced_notes(agg):
    """A line per provider whose runs called tools with no known price: their cost isn't in the figures."""
    out = []
    for e in PROVIDERS:
        names = Counter(agg[e].get("unpriced") or [])
        if names:
            n = sum(names.values())
            tools = ", ".join(f"{t.split('__')[-1]} ({c})" for t, c in sorted(names.items()))
            out.append(f"{LONG[e]}: {n} call{'s' if n > 1 else ''} to unpriced tools not included in its cost: {tools}.")
    return out


def keyless_limits(T, agg, vol, keyless):
    """For the verdict input: each keyless provider's daily cap against the busiest day."""
    parts = []
    for e in keyless:
        b = agg[e]["bill"]
        cap = b and b.get("keyless_cap_per_day")
        if cap and vol:
            parts.append(f"{LONG[e]}'s keyless tier may allow about {cap} calls a day (unconfirmed: from {LONG[e]}'s "
                         f"changelog, not its current docs); your busiest day would have needed about "
                         f"{b['peak_calls_per_day']:.0f}")
        elif b and vol:
            parts.append(f"{LONG[e]} publishes no keyless daily cap")
    if not parts:
        return ""
    return ("Keyless limits: these providers ran on their free keyless MCPs, which are rate-limited: "
            + "; ".join(parts) + ". An API key with the free monthly credit lifts these limits.")


def count(x):
    return f"{x:g}"


VERDICT_SCHEMA = obj({"pick": {"enum": PROVIDERS}, "runner_up": {"enum": PROVIDERS}, "verdict": {"type": "string"}})
NUM = re.compile(r"\d+(?:[.,]\d+)*")


PER_TASK = "Per task (judge 1's grades; numbers here are per-task details, not totals):"


def coverage(n, cfg):
    """How many tasks the report covers against the plan, or None without the run's config."""
    want, kept = cfg.get("want"), cfg.get("kept")
    if not want:
        return None
    text = f"This report covers {n} of the {want} tasks planned"
    if kept is not None and kept < want:
        text += f"; the task builder kept {kept} (the others had an expired premise or no fair public question)"
    if n < (kept if kept is not None else want):
        text += f"; {(kept if kept is not None else want) - n} more were left out during the run"
    return text + "."


def run_notes(stats, n=None, cfg=None):
    """Lines about what the runs lost: the plan against the tasks covered, MCP connections, tasks left out (and why),
    an early stop."""
    cov = coverage(n, cfg or {}) if n is not None else None
    out = [cov] if cov else []
    for e, (ok, tried) in (stats.get("connect") or {}).items():
        if e in NAME and ok < tried:
            out.append(f"{LONG[e]}'s MCP server connected on {ok} of {tried} tasks; tasks where it didn't were left out.")
    lost = stats.get("lost") or []
    if lost:
        by = {}
        for x in lost:
            by.setdefault(x["reason"], []).append(str(x["n"]))
        out.append(f"{len(lost)} task{'s were' if len(lost) > 1 else ' was'} left out: "
                   + "; ".join(f"{r} (task{'s' if len(ks) > 1 else ''} {', '.join(ks)})" for r, ks in by.items()) + ".")
    if stats.get("stop_note"):
        out.append(stats["stop_note"])
    return out


def verdict_input(T, agg, pairs, order, vol, keyless=(), stats=None):
    """Every number the verdict may cite, as plain text."""
    n = len(T)
    out = [f"Tasks: {n}, from the person's own Claude Code transcripts."]
    out += run_notes(stats or {})
    out += unpriced_notes(agg)
    if vol:
        out.append(f"Volume: about {vol['per_month']:.0f} research tasks a month; busiest day {vol['peak']} tasks.")
    out += ["", "Per provider (per task unless noted):"]
    for e in order:
        x, b = agg[e], agg[e]["bill"]
        cost = ("included in Claude Code, $0" if e == BASELINE else
                f"list fee ${x['usd']:.3f} per task" if x["usd"] is not None else "list fee unknown")
        if b and e != BASELINE:
            cost += (f"; at the person's volume ${b['list_usd']:.2f} a month at list price, free credit "
                     f"${b['free_monthly_usd']:.0f} a month, so ${b['after_free_usd']:.2f} a month after the free tier")
        out.append(f"- {LONG[e]}: ranked best on {count(x['best'])} of {n}; recall {x['recall']:.1f} of 4; wrong on the "
                   f"main point {100 * x['main']:.0f}%, on a side point only {100 * x['side']:.0f}%; {cost}; "
                   f"{fmt_k(x['tokens'])} context tokens; {x['secs']:.0f} s.")
    lim = keyless_limits(T, agg, vol, keyless)
    if lim:
        out += ["", lim]
    out += ["", "Head to head (tasks where the first provider's answer was ranked above / below the second's):"]
    out += [f"- {LONG[a]} vs {LONG[b]}: above {count(pairs[(a, b)][0])}, below {count(pairs[(a, b)][1])}"
            + (f", tied {count(pairs[(a, b)][2])}" if pairs[(a, b)][2] else "") for a, b in combinations(order, 2)]
    out += ["", "How the judge's grades line up with its ranking (more recall; fewer errors at equal recall; "
            "otherwise the judge's call):", *(f"- {ln}" for ln in computed(T, order, n))]
    out += ["", PER_TASK]
    for k, t in T.items():
        g = t["grades"][0]
        out += ["", f"## Task {k}", " ".join(t["task"]["task"].split()),
                "Ranks: " + ", ".join(f"{LONG[e]} {g['rank'][e]}" for e in sorted(order, key=lambda e: g["rank"][e])),
                *(f"{LONG[e]}: recall {g['answers'][e]['recall']} of 4 ({unlabel(g['answers'][e]['recall_reason'], g['labels'])}) "
                  f"Error: {ERR[g['answers'][e]['error']]} ({unlabel(g['answers'][e]['error_reason'], g['labels'])})"
                  for e in order),
                f"Ranking reason: {unlabel(g['ranking_reason'], g['labels'])}",
                f"Comparison: {unlabel(g['rationale'], g['labels'])}"]
    return "\n".join(out)


def numbers(text):
    """(token, value) for each number; a dotted version like 2.1.330 counts as its parts."""
    out = []
    for x in NUM.findall(text):
        y = x.replace(",", "")
        out += [(x, float(v)) for v in y.split(".")] if y.count(".") > 1 else [(x, float(y))]
    return out


def unsupported(text, source):
    """Numbers in text that aren't in source, allowing whole-number rounding of a source number."""
    have = {v for _, v in numbers(source)}
    return sorted({x for x, v in numbers(text)
                   if v not in have and not (v == int(v) and any(abs(u - v) < 0.5 for u in have))})


def decide(cache, T, agg, pairs, order, vol, model, keyless=(), stats=None):
    """(pick, runner_up, verdict text, check): one LLM judgment over every metric, checked; one retry, then a
    plain fallback (also when the call itself fails). check records each rejected attempt for the diagnostics."""
    system, prompt = (PROMPTS / "verdict.md").read_text(), verdict_input(T, agg, pairs, order, vol, keyless, stats)
    bad, rejected = [], []
    for attempt in range(2):
        p = prompt if attempt == 0 else (prompt + f"\n\nYour previous verdict cited numbers not in the data above "
                                          f"({', '.join(bad)}). Cite only the summary numbers given above the per-task notes.")
        # every attempt is cached, so a re-render replays the same path; only the accepted one counts as used
        try:
            entry = cache.entry([system, p], model, lambda: ask(p, system, VERDICT_SCHEMA, model))
        except UsageLimit:
            raise
        except Exception as exc:
            print(f"verdict call failed: {exc}", file=sys.stderr, flush=True)
            rejected.append({"text": "(the verdict call failed)", "unsupported": [type(exc).__name__]})
            break
        out = entry["out"]
        bad = unsupported(out["verdict"], prompt.split(PER_TASK)[0])  # cited numbers must be summary metrics
        if out["pick"] != out["runner_up"] and not bad:
            cache.use(entry)
            check = {"status": "retry" if rejected else "ok", "rejected": rejected}
            return out["pick"], out["runner_up"], " ".join(out["verdict"].split()), check
        bad = bad or ["pick equals runner-up"]
        rejected.append({"text": " ".join(out["verdict"].split()), "unsupported": bad})
    w, r = order[0], order[1]
    best = count(agg[w]["best"])
    tied = [e for e in order if agg[e]["best"] == agg[w]["best"]]
    text = (f"Use {LONG[w]}. It gave the best answer on {best} of {len(T)} tasks, more than any other provider."
            if len(tied) == 1 else
            f"Use {LONG[w]}. It is tied with {' and '.join(LONG[e] for e in tied[1:])} for most best answers "
            f"({best} of {len(T)} tasks each).")
    return w, r, text, {"status": "fallback", "rejected": rejected}


def ranking(t, order):
    return ", ".join(f"{NAME[e]} {t['e'][e]['rank']:g}" for e in sorted(order, key=lambda e: t["e"][e]["rank"]))


def task_facts(t, order):
    """The lines the writer and the checker see for one task: ranking, then recall and errors."""
    return [f"Ranking: {ranking(t, order)}",
            "Recall: " + ", ".join(f"{NAME[e]} {t['e'][e]['recall']:g}/4" for e in order),
            "Errors: " + ", ".join(f"{NAME[e]} {' / '.join(ERR[v] for v in t['e'][e]['errors'])}" for e in order)]


def judge_notes(t):
    out = []
    for j, g in enumerate(t["grades"], 1):
        out += [f"Judge {j} ranking reason: {unlabel(g['ranking_reason'], g['labels'])}",
                f"Judge {j} comparison: {unlabel(g['rationale'], g['labels'])}"]
    return out


EXAMPLE = re.compile(r"\((\d+)\)")
REWRITE_SCHEMA = obj({"bullets": {"type": "array", "items": obj({"bullet": {"type": "integer"}, "text": {"type": "string"},
                                                                  "tasks": INTS})}})


def split(T, a, b):
    """Tasks where a's answer ranked above b's, split by the judge's own grades: a had more recall; equal
    recall and b had the worse error; the rest is the judge's call."""
    out = {"recall": [], "errors": [], "call": []}
    for k, t in T.items():
        x, y = t["e"][a], t["e"][b]
        if x["rank"] < y["rank"]:
            out["recall" if x["recall"] > y["recall"] else
                "errors" if x["recall"] == y["recall"] and x["sev"] < y["sev"] else "call"].append(k)
    return out


def computed(T, order, n, link=str):
    """The Why lines that come from code alone: each pair, both ways, split by the judge's grades."""
    lines = []
    for a, b in combinations(order, 2):
        for x, y in ((a, b), (b, a)):
            sp = split(T, x, y)
            total = sum(map(len, sp.values()))
            if not total:
                continue
            parts = [f"more recall on {len(sp['recall'])}" if sp["recall"] else "",
                     f"fewer errors on {len(sp['errors'])} ({', '.join(map(link, sp['errors']))})" if sp["errors"] else "",
                     f"judge's call on {len(sp['call'])}" if sp["call"] else ""]
            line = f"{NAME[x]} above {NAME[y]} on {total}: {', '.join(p for p in parts if p)}."
            lines.append(line[0].upper() + line[1:])
    return lines


def groups(T, agg, w, r):
    """Fixed task sets the writer explains, framed as what the person would have experienced without the pick w.
    Against the runner-up r: material missed (w ranked above with more recall), wrong information (w ranked above
    and r had the worse error), other reasons (w ranked above, judge's call), and, for fairness, what r gave that w
    didn't (r ranked above). Against the remaining provider: what you'd have missed or been told wrong."""
    rest = [r] + [e for e in PROVIDERS if e not in (w, r)]
    above = lambda a, b: [k for k, t in T.items() if t["e"][a]["rank"] < t["e"][b]["rank"]]  # noqa: E731
    sp = split(T, w, r)
    W, R = NAME[w], NAME[r]
    out = {
        "missed": (f"the {len(sp['recall'])} tasks where {W} was ranked above {R} with more recall", sp["recall"],
                   f"With {R} instead, you'd have: missed … (material {W}'s evidence held and {R}'s didn't)", (w, r)),
        "wrong": None,
        "other": (f"the {len(sp['call'])} tasks where {W} was ranked above {R} on the judge's call", sp["call"],
                  f"With {R} instead, you'd have: got … (something concrete the judge preferred in {W}'s answer, "
                  "neither material nor errors)",
                  (w, r)),
        "gave": (f"the {len(above(r, w))} tasks where {R} was ranked above {W}", above(r, w),
                 f"With {R} instead, though, you'd have: gained … (what {R} gave that {W} didn't)", (w, r)),
    }
    wrong = [k for k in above(w, r) if T[k]["e"][r]["sev"] > T[k]["e"][w]["sev"]]
    out["wrong"] = (f"the {len(wrong)} tasks where {W} was ranked above {R} and {R}'s answer had the worse error", wrong,
                    f"With {R} instead, you'd have: been told … (wrong information in {R}'s answer)", (w, r))
    for o in rest[1:]:
        ks = above(w, o)
        out[f"vs-{o}"] = (f"the {len(ks)} tasks where {W} was ranked above {NAME[o]}", ks,
                          f"With {NAME[o]} instead, you'd have: … (what you'd have missed or been told wrong)", (w, o))
    return {g: v for g, v in out.items() if len(v[1]) >= MIN_GROUP}


def provider_notes(t, providers):
    """Judge 1's recall and error reasons for the providers a group compares."""
    g = t["grades"][0]
    return [f"{NAME[e]} recall ({g['answers'][e]['recall']}/4): {unlabel(g['answers'][e]['recall_reason'], g['labels'])} "
            f"Error ({ERR[g['answers'][e]['error']]}): {unlabel(g['answers'][e]['error_reason'], g['labels'])}"
            for e in providers]


MOSTLY = re.compile(r"\b(?:mostly|usually|most often|mainly)\s+")


def check_why(cache, bullets, T, order, groups_, model):
    """Check, then repair once.
    1. One check call: keep each bullet's listed tasks (examples cited in its text count as listed) where the claim holds.
    2. A bullet citing an example outside its kept tasks goes to one rewrite call with those tasks; one under 3
       kept tasks goes with its whole group.
    3. One check call over the rewrites; drop any that still fail. "Mostly" and the like are cut unless the
       claim holds on more than half the group."""
    bullets = [{"group": b["group"], "text": b["text"].strip(),
                "tasks": sorted(({str(k) for k in b["tasks"]} | set(EXAMPLE.findall(b["text"])))
                                & set(groups_[b["group"]][1]), key=int), "pair": groups_[b["group"]][3]}
               for b in bullets if b["group"] in groups_]
    check_sys = (PROMPTS / "verify-why.md").read_text()

    def ctx_for(items):
        ctx = []
        for i, (text, ks) in items.items():
            ctx += [f"# Bullet {i}", text, ""]
            for k in ks:
                ctx += [f"## Task {k}", *task_facts(T[k], order), *judge_notes(T[k]),
                        *provider_notes(T[k], bullets[i]["pair"]), ""]
        return "\n".join(ctx)

    def check(items):
        if not items:  # no bullets (e.g. too few tasks for any group): nothing to check
            return {}
        prompt = ctx_for(items)
        res = cached(cache, [check_sys, prompt], model, lambda: ask(prompt, check_sys, CHECK_SCHEMA, model))["bullets"]
        holds = {r["bullet"]: {str(k) for k in r["holds"]} for r in res}
        return {i: [k for k in ks if k in holds.get(i, set())] for i, (_, ks) in items.items()}

    def ok(text, ks):
        return len(ks) >= MIN_GROUP and set(EXAMPLE.findall(text)) <= set(ks)

    kept = check({i: (b["text"], b["tasks"]) for i, b in enumerate(bullets)})
    redo = {i: kept[i] if len(kept[i]) >= MIN_GROUP else groups_[b["group"]][1]
            for i, b in enumerate(bullets) if not ok(b["text"], kept[i])}
    if redo:
        system = ((PROMPTS / "report.md").read_text() + "\n\n## Now\n\nRewrite each bullet below. Keep its subject, and word "
                  "the claim at the level the judge's notes support on at least 3 of the tasks shown with it, citing "
                  "examples only from those tasks. Return each bullet's number, new text and the tasks where it holds.")
        prompt = ctx_for({i: (bullets[i]["text"], ks) for i, ks in redo.items()})
        res = cached(cache, [system, prompt], model, lambda: ask(prompt, system, REWRITE_SCHEMA, model))["bullets"]
        new = {r["bullet"]: (r["text"].strip(), sorted({str(k) for k in r["tasks"]} & set(redo[r["bullet"]]), key=int))
               for r in res if r["bullet"] in redo}
        rechecked = check(new) if new else {}
        for i in redo:
            if i in new and ok(new[i][0], rechecked[i]):
                bullets[i]["text"], kept[i] = new[i][0], rechecked[i]
            else:
                del kept[i]
    out = []
    for i, ks in sorted(kept.items()):
        label, pop = groups_[bullets[i]["group"]][:2]
        text = bullets[i]["text"]
        if 2 * len(ks) <= len(pop):
            text = MOSTLY.sub("", text)
        text = re.sub(r"^(?:you'd have|you would have)\s+", "", text, flags=re.I).rstrip(".")
        if not text.startswith(tuple(NAME.values())):
            text = text[0].lower() + text[1:]
        out.append({"text": text, "tasks": ks, "group": label, "gid": bullets[i]["group"], "pair": bullets[i]["pair"]})
    return out


def md_refs(text, b):
    """"… (6)" -> "… ([task 6](#task-6); also tasks [1](#task-1), …)"."""
    ex = EXAMPLE.findall(text)
    also = [k for k in b["tasks"] if k not in ex]
    tail = ("; also " + ("tasks " if len(also) > 1 else "task ") + ", ".join(f"[{k}](#task-{k})" for k in also)) if also else ""
    out = EXAMPLE.sub(lambda m: f"([task {m[1]}](#task-{m[1]}){tail})", text, count=1)
    return out if ex else out + (f" (tasks {', '.join(f'[{k}](#task-{k})' for k in also)})" if also else "")


def why_coverage(why):
    """Methodology lines: each Why statement and how many of its group's tasks it holds on."""
    short = lambda t: re.split(r",? (?:such as|like|e\.g\.) ", t)[0]  # noqa: E731
    return [f"- \"{short(task_ref(b['text']))}\": {len(b['tasks'])} of {b['group']}." for _, _, b, _ in why_layout(why) if b]


def task_ref(text):
    """"(6)" -> "(task 6)"."""
    return EXAMPLE.sub(r"(task \1)", text)


def why_layout(why):
    """[(kind, text, bullet, alternative provider's name)]: a "With R instead, you'd have:" header for the runner-up R,
    its items, and the "though you'd have gained" line (kind "though", the one point in R's favor); then the same
    header and item for each remaining provider, so every group renders alike."""
    by = {b["gid"]: b for b in why}
    out = []
    items = [by[g] for g in ("missed", "wrong", "other") if g in by]
    if items:
        alt = NAME[items[0]["pair"][1]]
        out.append(("head", f"With {alt} instead, you'd have:", None, alt))
        out += [("item", b["text"], b, NAME[b["pair"][1]]) for b in items]
        if "gave" in by:
            out.append(("though", f"though you'd have {by['gave']['text']}.", by["gave"], alt))
    elif "gave" in by:
        alt = NAME[by["gave"]["pair"][1]]
        out += [("head", f"With {alt} instead, you'd have:", None, alt), ("item", by["gave"]["text"], by["gave"], alt)]
    for g, b in by.items():
        if g.startswith("vs-"):
            alt = NAME[g[3:]]
            out += [("head", f"With {alt} instead, you'd have:", None, alt), ("item", b["text"], b, alt)]
    return out


def why_groups(why):
    """The Why for the HTML: one group per alternative provider, {"provider", "head", "items": [{"text", "also",
    "favor"}]}; "also" lists the bullet's tasks not cited in its text."""
    out = []
    for kind, text, b, alt in why_layout(why):
        if kind == "head":
            out.append({"provider": alt, "head": None, "items": []})
        if kind == "head":
            out[-1]["head"] = text
        else:
            out[-1]["items"].append({"text": task_ref(text), "also": [k for k in b["tasks"] if k not in EXAMPLE.findall(text)],
                                     "favor": kind == "though"})
    return out


def legend(has_bill, has_usd):
    """[(term, definition)] for the providers table; cost entries only when cost data exists."""
    parts = [("Ranked best", "tasks where the judge ranked this provider's answer first."),
             ("Recall", "how much of what a complete answer needs (pooled from all providers' results) this provider's "
                        "results held, 0 to 4, averaged."),
             ("Wrong (main / side)", "share of answers wrong on the main point / only on a side point.")]
    if has_bill:
        parts.append(("Monthly cost", "what you'd pay a month at your volume (research tasks per month in your "
                                      "transcripts) after the provider's free tier."))
    if has_usd:
        parts.append(("List fee per task", f"the provider's API list price for the run's search and fetch calls "
                                           f"({LONG[BASELINE]} is included in Claude Code)."))
    parts += [("Context tokens", "search and fetch results added to Claude's context per task, estimated at 4 "
                                 "characters per token."), ("Time", "the whole run, from task to final answer.")]
    return parts


def legend_text(defs):
    return " ".join(f"{term}: {d}" for term, d in defs)


FALLBACK_NOTE = "(Automatic verdict: the model's verdict couldn't be used; see Diagnostics.)"


TITLE = "Which search provider makes Claude more useful for you"


def subtitle(n, sessions, hs, dates):
    of = (f"{n} of your {hs['tasks']:,} research tasks, {span(hs['first'], hs['last'])}" if hs
          else f"{n} of your research tasks")
    replayed = span(date.fromisoformat(dates[0]), date.fromisoformat(dates[-1]), year=False) if dates else "on unknown dates"
    return f"{of} ({sessions} sessions), replayed {replayed}"


def summary(T, dates, agg, pairs, order, why, verdict, pick, sample="", check=None, extra_notes=()):
    """The summary as (terminal lines, markdown lines, the same content as fields for the HTML)."""
    n, title = len(T), TITLE
    skipped = {e: [k for k, t in T.items() if not t["e"][e]["used"]] for e in MCP_PREFIX}
    notes = [f"{NAME[e]} never called its MCP on {len(ks)} task(s) ({', '.join(ks)}); those runs used built-in search."
             for e, ks in skipped.items() if ks] + list(extra_notes)
    has_usd = all(agg[e]["usd"] is not None for e in PROVIDERS)
    has_bill = all(agg[e]["bill"] for e in PROVIDERS)
    fee = ("list fee per task", lambda e: usd(agg[e]["usd"]))
    cols = [("ranked best", lambda e: f"{count(agg[e]['best'])}/{n}"),
            ("recall", lambda e: f"{agg[e]['recall']:.1f}/4"),
            ("wrong (main / side)", lambda e: f"{100 * agg[e]['main']:.0f}% / {100 * agg[e]['side']:.0f}%"),
            *([("monthly cost", lambda e: pay(agg[e]))] if has_bill else [fee] if has_usd else []),
            ("context tokens", lambda e: fmt_k(agg[e]["tokens"])),
            ("time", lambda e: f"{agg[e]['secs']:.0f} s")]
    mcols = cols + ([fee] if has_bill else [])
    pairs_shown = [(a, b) for a, b in combinations(order, 2)]
    link = lambda k: f"[{k}](#task-{k})"  # noqa: E731

    fallback = [FALLBACK_NOTE] if check and check["status"] == "fallback" else []
    term = [title, *wrap(sample, ""), "", "Verdict", *wrap(verdict), *(ln for f in fallback for ln in wrap(f)), ""]
    w = max(len(NAME[e]) for e in PROVIDERS) + 1
    widths = [max(len(h), *(len(f(e)) for e in PROVIDERS)) + (2 if i else 0) for i, (h, f) in enumerate(cols)]
    term.append(f"  {'per task':{w}}" + "".join(f"{h:>{wd}}" for (h, _), wd in zip(cols, widths)))
    for e in order:
        term.append(f"  {NAME[e]:{w}}" + "".join(f"{f(e):>{wd}}" for (_, f), wd in zip(cols, widths)))
    term += [*(ln for t in notes for ln in wrap(t)), "", "Head to head"]
    above = [(NAME[a], NAME[b], pairs[(a, b)][0], n) for a, b in pairs_shown]
    rows = [(f"{a} above {b}", f"{count(x)}/{n}") for a, b, x, n in above]
    w2 = max(len(p) for p, _ in rows) + 2
    term += [f"  {p:{w2}}{v:>6}" for p, v in rows]
    if why:
        term += ["", "Why"]
        for kind, text, b, _ in why_layout(why):
            text = task_ref(text)
            term += wrap(text, "    ", "  - ") if kind == "item" else wrap(text)

    md = [f"# {title}", "", sample, "", "## Verdict", "", verdict, "", *([fallback[0], ""] if fallback else []), "## Providers", "",
          "| Provider | " + " | ".join(h.capitalize() for h, _ in mcols) + " |", "|---|" + "--:|" * len(mcols)]
    md += [f"| {NAME[e]} | " + " | ".join(f(e) for _, f in mcols) + " |" for e in order]
    defs = legend(has_bill, has_usd)
    md += ["", legend_text(defs), "", *notes, *([""] if notes else []),
           "## Head to head", "", "Tasks where the first provider's answer was ranked above the second's.", "",
           "| Pair | Tasks |", "|---|--:|", *(f"| {p} | {v} |" for p, v in rows)]
    if why:
        md += ["", "## Why", ""]
        for kind, text, b, _ in why_layout(why):
            md += ((([""] if md[-1] else []) + [text, ""]) if kind == "head" else [f"- {md_refs(text, b)}"]
                   if kind == "item" else ["", md_refs(text, b)])
    md += ["", "## How the grades explain the ranking", "",
           "Each task where one answer was ranked above another, put in one bucket by the judge's own grades: more "
           "recall, fewer errors at equal recall, or the judge's call.", "",
           *(f"- {line}" for line in computed(T, order, n, link))]
    # the bars beside fractions show the value as displayed
    bar = {"ranked best": lambda e: (agg[e]["best"], n), "recall": lambda e: (round(agg[e]["recall"], 1), 4)}
    parts = {"title": title, "subtitle": sample, "verdict": verdict, "pick": NAME[pick],
             "fallback": fallback[0] if fallback else "", "legend": defs, "baseline": NAME[BASELINE], "notes": notes,
             "table": {"head": ["Provider"] + [h.capitalize() for h, _ in mcols],
                       "rows": [{"provider": NAME[e], "cells": [{"text": f(e)} | (dict(zip(("value", "max"), bar[h](e)))
                                                                            if h in bar else {}) for h, f in mcols]}
                                for e in order]},
             "above": above, "why": why_groups(why) if why else []}
    return term, md, parts


def md_safe(text):
    """report.md with every "<" escaped, so quoted task text, answers and notes can't become HTML in a Markdown
    previewer; only the report's own <details> and <summary> tags stay."""
    text = text.replace("<", "&lt;")
    return re.sub(r"&lt;(/?(?:details|summary))>", r"<\1>", text)


def task_md(d, heads):
    """One task's block, from task_data: ranking, recall and errors, then judge 1's notes."""
    two = heads["two"]
    out = [f"### Task {d['n']}", "", *(f"> {ln}" if ln.strip() else ">" for ln in d["text"].split("\n")), "",
           *([d["cap_note"], ""] if d["cap_note"] else []),
           f"| Provider | {heads['rank']} | Recall | {heads['error']} | Context tokens | Time |", "|---|--:|--:|---|--:|--:|"]
    for r in d["rows"]:
        out.append(f"| {r['provider']}{' (never called its MCP)' if r['unused_mcp'] else ''} | {r['rank']} | "
                   f"{r['recall']['text']} | {r['error']} | {r['tokens']} | {r['time']} |")
    j1 = " 1" if two else ""
    out += ["",
            f"**Judge{j1}'s ranking reason:** {d['ranking_reason']}", "",
            f"**Judge{j1}'s comparison:** {d['rationale']}", ""]
    errs = [f"- {x['provider']} ({f'judge {j}, ' if (j := x['judge']) else ''}{x['severity']} error): {x['reason']}"
            for x in d["errors"]]
    if errs:
        out += ["**Errors:**", "", *errs, ""]
    who = "judge 1's " if two else ""
    out += [f"<details><summary>What a complete answer needed, and {who}recall per provider</summary>", "",
            "Direct answer:", "", *(f"- {x}" for x in d["direct"]), ""]
    if d["other"]:
        out += ["Other points:", "", *(f"- {x}" for x in d["other"]), ""]
    out += ["Judge 1's recall:" if two else "Recall:", "", *(f"- {x}" for x in d["recall"]), "", "</details>", ""]
    return out


def per_task_md(T, order, judges, titles, tasks, heads):
    def label(k, task):
        if titles.get(k):
            return titles[k]
        s = " ".join(task.split())
        return s if len(s) <= 70 else s[:70].rsplit(" ", 1)[0] + " …"

    note = ("one judge per task" if judges == 1 else
            f"ranks from {judges} judges, shown judge 1 / judge 2, and notes from judge 1" if judges == 2 else
            f"ranks from {judges} judges, and notes from judge 1")
    out = ["## Per-task data", "",
           f"Each provider's rank per task (1 = most helpful answer, in bold). Click a task number for its block; {note}.", "",
           "| Task | Subject | " + " | ".join(NAME[e] for e in order) + " |", "|--:|---|" + "--:|" * len(order)]
    for k, t in T.items():
        cells = []
        for e in order:
            r = " / ".join(map(str, t["e"][e]["ranks"]))
            cells.append(f"**{r}**" if t["e"][e]["rank"] == min(t["e"][o]["rank"] for o in order) else r)
        out.append(f"| [{k}](#task-{k}) | {label(k, t['task']['task'])} | " + " | ".join(cells) + " |")
    out.append("")
    for d in tasks:
        out += task_md(d, heads)
    return out


def providers_text():
    mcps = [LONG[e] for e in MCP_PREFIX]
    return (" and ".join(mcps) if len(mcps) < 3 else ", ".join(mcps[:-1]) + " and " + mcps[-1]), LONG[BASELINE]


def methodology_md(n, judge_model, judges, has_cost=True, coverage=(), no_instructions=None):
    """Method, independent of which providers ran: names come from the configuration. no_instructions: why the runs
    had no instructions, or None if they had them."""
    from build import PER_SESSION
    from runner import TIMEOUT
    mcps, base = providers_text()
    cost = (f"- Cost: each provider's list API price for the run's calls to its priced search and fetch tools, per "
            f"task; {base} is $0. Calls to tools with no known price (for example a provider's vertical search) aren't "
            "counted; the report says how many there were. Monthly cost multiplies the price by your research tasks "
            "per month (tasks found in your transcripts over the months they span), then subtracts the provider's free "
            "monthly credit.\n") if has_cost else ""
    mine = (" and your instructions (your user CLAUDE.md, with its web-tool lines routed to each provider)"
            if not no_instructions else f" ({no_instructions})")
    return f"""## Methodology

![How the eval works: transcripts, sampled tasks, one run per provider, judge, verdict and report](method.svg)

**Setup.** Each provider runs keyless, configured as its docs install it in Claude Code (checked {SETUP_CHECKED}): its keyless HTTP MCP server, with no API key. The Configuration section shows the exact setup.

**What ran**

1. Tasks: {n} research questions recovered from your Claude Code transcripts. A research task is one request or one subagent where Claude searched the web with built-in WebSearch or Exa, Parallel, Firecrawl or webclaw search; other search tools aren't recognized. Tasks were drawn at random, at most {PER_SESSION} per session, and each was rewritten to stand alone (follow-ups folded in); the task builder drops tasks whose premise has expired or that have no fair public question, and a code check drops any that still hold private details. The run needs at least 10 tasks before it starts; tasks lost later are left out, and the summary says which and why.
2. Runs: each task replayed once per provider, with the same model{mine}. Replays are fresh headless sessions: no project CLAUDE.md, skills, plugins, hooks or other MCP servers; web search and fetch only, plus reading back their own saved results; {TIMEOUT // 60} minutes at most. Each provider under test ({mcps}) ran as an MCP server alongside {base}; the baseline had {base} only. A replay whose MCP server didn't connect, or that didn't finish, is rerun once; if it fails again, the task is left out for every provider. If a provider's keyless rate limit refuses calls, the run stops and reports the tasks finished, instead of waiting for the limit to reset.
3. Judging: one judge call per task, by {judge_model}, the same model that ran the tasks. The judge reads the task, each run's evidence (everything its searches and fetches returned, up to the caps in Limits) and each run's answer, in a shuffled order under labels A, B and C. Provider and tool names, and request ids, are removed from the evidence and the answers. A task whose judge call fails twice is left out.

**What the judge does, in one call per task**

1. Lists what a complete answer requires, from all runs' evidence together, before grading the answers: the direct answer to each question, then the points a person asking would expect.
2. Recall (0 to 4): how much of that list each run's evidence contains. Evidence missing the direct answer can't score above 2.
3. Errors: whether each answer departs from what all the evidence shows, on the main point (main error), only on a side point (side error), or not at all. Saying "I couldn't find it" is never an error.
4. Ranking: orders the answers from most to least helpful to the person, in light of the evidence, using each rank once and giving its reasons first.

The ranking decides every per-task outcome in this report. Recall and errors are reported because they explain the ranking; they don't decide it.

**How providers are compared**

- Ranked best: tasks where a provider's answer was ranked first.
- Head to head: tasks where one provider's answer was ranked above another's.
- Verdict: one LLM call reads every number in this report's summary (ranked best, recall, errors, ranked above and how the grades explain it, monthly cost after free tiers, keyless limits, context tokens, time) and each task with the judge's grades and notes. It picks the provider it judges would make Claude most useful to you, {base} included, weighing the metrics itself with no fixed thresholds, and names the runner-up. Code checks that every number the verdict cites is among the summary numbers in its input; otherwise it retries once. If it still fails, or the call fails, the report names the provider ranked best most often instead.
- Why: code picks fixed sets of tasks that back the verdict: where the recommended provider was ranked above the runner-up with more recall, where the runner-up's answer had the worse error, where it was the judge's call, where the runner-up was ranked above, and where the recommended provider was ranked above the remaining one. An LLM names what made the difference in each set, from the judge's notes, with an example. A check call keeps only the tasks where each statement holds; a failing bullet is rewritten once and rechecked, or dropped. Each set needs at least {MIN_GROUP} tasks, and a statement is kept only if the check confirms it on at least {MIN_GROUP}; with fewer, the report has no Why section and says why.
- How the grades explain the ranking: each task where one answer was ranked above another is put in one bucket by the judge's own grades. More recall: the higher-ranked run's evidence held more. Fewer errors: equal recall, and the lower-ranked answer had the worse error. Judge's call: everything else.
{cost}- Context tokens: the size of the search and fetch results a run put into Claude's context, estimated at 4 characters per token. Time: the headless session's wall time, from task to final answer.
""" + ("\n**How far each Why statement holds** (tasks where the check kept it, of the set it was written for)\n\n"
       + "\n".join(coverage) + "\n" if coverage else "")


def config_md(T, run_cfg, judge_model, writer_model, dates, hs, sample_note, rr):
    cfg, models, versions = run_cfg
    when = dates[0] if len(dates) == 1 else f"{dates[0]} to {dates[-1]}" if dates else "unknown"
    out = ["## Configuration", ""]
    for e in PROVIDERS:
        tools = ", ".join(f"`{x}`" for x in cfg[e]["tools"]) or "none"
        out.append(f"- {LONG[e]}: {cfg[e]['connection']}. Search tools: {tools}.")
    out += [f"- Tasks ran on: {counted(models)}, Claude Code {counted(versions)}.",
            f"- Judge: {judge_model}. Verdict and Why: {writer_model}.",
            f"- Run dates: {when}."
            + (f" {len(rr)} {'run was' if len(rr) == 1 else 'runs were'} rerun after the provider's MCP server failed to connect: "
               + ", ".join(f"task {k} ({NAME.get(e, e)})" for k, e in rr) + "." if rr else ""),
            f"- Tasks: {len(T)}" + (f", from {hs['tasks']:,} research tasks found in your transcripts "
                                     f"({span(hs['first'], hs['last'])})" if hs else "") + ".",
            f"- Sample: {sample_note}"
            + (f". Your transcripts held {hs['tasks']:,} research tasks ({hs['searches']:,} searches)." if hs else "."),
            *([f"- Prices: the plugin's price table{f' (checked {CHECKED})' if CHECKED else ''}, list prices with sources."]
              if cost_usd else []), ""]
    return out


def capped_limit(T, model=None):
    capped = [(k, e) for k, t in T.items() for e in PROVIDERS if t["e"][e]["capped"]]
    budget = evidence_budget(model)
    text = (f"- The judge reads at most {MAX_RESULT // 1000}k characters of each search or fetch result"
            + (f" and {MAX_EVIDENCE // 1000}k per run" if MAX_EVIDENCE < budget else "")
            + f", and all runs' evidence for a task must fit about {budget // 1000}k characters (its context window, "
            "with room for the rest; less for text in scripts like Chinese or Japanese, which use more tokens per "
            "character). Each run gets an even share, and what a smaller run doesn't use goes to the others. A run "
            "over its share is judged on its earliest results.")
    if capped:
        by = {}
        for k, e in capped:
            by.setdefault(k, []).append(NAME[e])
        text += " Cut here: " + "; ".join(f"task {k} ({' and '.join(v)})" for k, v in by.items()) + "."
    return text


def limits_md(T, judge_model, keyless, model=None, stop_note=None):
    n = len(T)
    mcps, base = providers_text()
    forced = "- Near-equal answers are still forced into an order, so some \"above\" results are coin flips."
    return ["## Limits", "",
            f"- One Claude model judges, by design: {judge_model}, the same model that ran the tasks. Claude is the "
            "provider's real customer: it reads the results and writes the answer, so it ranks the answers in light of "
            "the evidence. Recall and errors explain its ranking but don't decide it.",
            forced,
            f"- No error bars, and the verdict is a judgment, not a significance test: with {n} tasks, a few tasks can "
            "swing a close call.",
            "- Recall is relative: \"complete\" means what the providers found together.",
            capped_limit(T, model),
            *([f"- {stop_note}"] if stop_note else []),
            "- One run per provider per task. The same task run again can search differently and land on a better or worse "
            "answer, so a single task's result partly reflects luck. Across many tasks this mostly evens out.",
            f"- A provider's results mean that provider plus {base}: the MCP runs kept {base} available.",
            "- Each provider's results have their own format (JSON, text blocks, or summaries written by "
            f"{base}'s search tool), so the judge can tell the runs apart by format, though it isn't told which "
            "provider is which.",
            *([f"- {' and '.join(LONG[e] for e in keyless)} ran on {'their' if len(keyless) > 1 else 'its'} free keyless "
               f"tier, which is rate-limited; an API key lifts the limits"
               + (" and comes with free monthly credit." if all(e in (FREE or {}) for e in keyless) else ".")]
              if keyless else []),
            "- Your tasks only. The verdict reflects the questions in these sessions and the web on the run dates; a "
            "different mix of questions could change it.", ""]


def diagnostics_md(check, extra=()):
    out = ["## Diagnostics", ""]
    status = {"ok": "passed the number check on the first try.",
              "retry": "failed the number check once and passed on the retry.",
              "fallback": "was not usable (failed number check or failed call); the report shows an automatic verdict "
                          "instead."}[check["status"]]
    out.append(f"- Verdict: {status}")
    for r in check["rejected"]:
        out.append(f"  - Rejected: \"{r['text']}\" Unsupported: {', '.join(r['unsupported'])}.")
    out += [f"- {x}" for x in extra]
    return out + [""]


def task_heads(judges):
    """The per-task table's rank and error headers, the same for every task."""
    two = judges > 1
    return {"two": two, "rank": "Rank (judge 1 / 2)" if two else "Rank", "error": "Errors (judge 1 / 2)" if two else "Error",
            "export": "results.jsonl.gz, next to this report"}


def task_data(k, t, order, judges, title):
    """One task, for the markdown block (task_md) and the HTML viewer, scrubbed of local paths and the username."""
    return local_scrub(_task_data(k, t, order, judges, title))


def _task_data(k, t, order, judges, title):
    g, two = t["grades"][0], judges > 1
    by_rank = sorted(order, key=lambda e: t["e"][e]["rank"])
    errs = [{"provider": NAME[e], "judge": j if two else None, "severity": ERR[gj["answers"][e]["error"]],
             "reason": unlabel(gj["answers"][e]["error_reason"], gj["labels"])}
            for e in by_rank for j, gj in enumerate(t["grades"], 1)
            if gj["answers"][e]["error"] != "none" and (j == 1 or gj["answers"][e]["error"] != g["answers"][e]["error"])]

    def row(e):
        x = t["e"][e]
        return {"provider": NAME[e], "unused_mcp": e in MCP_PREFIX and not x["used"], "rank": " / ".join(map(str, x["ranks"])),
                "recall": {"text": f"{x['recall']:g}/4", "value": x["recall"], "max": 4},
                "error": " / ".join(map(cell, x["errors"])),
                "sev": "main" if "critical" in x["errors"] else "side" if "limited" in x["errors"] else "none",
                "tokens": fmt_k(x["tokens"]), "time": f"{x['secs']:.0f} s"}
    return {"n": k, "title": title or "", "text": t["task"]["task"].strip(), "rows": [row(e) for e in by_rank],
            "cap_note": cap_note([NAME[e] for e in by_rank if t["e"][e]["capped"]]),
            "ranking_reason": unlabel(g["ranking_reason"], g["labels"]), "rationale": unlabel(g["rationale"], g["labels"]),
            "errors": errs,
            "recall": [f"{NAME[e]} ({g['answers'][e]['recall']}/4): {unlabel(g['answers'][e]['recall_reason'], g['labels'])}"
                       for e in by_rank],
            "direct": g["required"]["direct_answer"], "other": g["required"]["other"],
            "providers": [provider_evidence(t, e, g) for e in by_rank]}


PREVIEW = 2000  # characters of each tool result shown in report.html; results.jsonl.gz has the full text


def provider_evidence(t, e, g):
    """One provider's answer and tool calls for the HTML viewer, with the judge's notes on it."""
    lines = read_log(t["dir"] / f"{e}.jsonl")
    a = g["answers"][e]
    calls = [{"tool": c["tool"], "id": c["id"], "input": json.dumps(c["input"], ensure_ascii=False),
              "chars": c["result_chars"], "ms": c["duration_ms"], "flags": c["flags"], "rid": c["provider_request_id"],
              "results": [[r["rank"], r["url"], r["title"], r["published"], r["cited_in_answer"]] for r in c["results"]],
              "preview": (c["result"] or "")[:PREVIEW], "shown": min(PREVIEW, c["result_chars"] or 0),
              "cut": (c["result_chars"] or 0) > PREVIEW}
             for c in tool_calls(lines)]
    return {"provider": NAME[e], "answer": local_scrub(final(lines).get("result") or "(no answer)"), "calls": calls,
            "recall": f"{a['recall']}/4. {unlabel(a['recall_reason'], g['labels'])}",
            "error": f"{cell(a['error'])}. {unlabel(a['error_reason'], g['labels'])}"}


def main(argv):
    import argparse
    import shutil

    from report_html import md_block, render
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tasks")
    ap.add_argument("grades")
    ap.add_argument("runs", help="run dirs, comma-separated")
    ap.add_argument("out_md", help="report.md path; report.html, method.svg and results.jsonl.gz go next to it")
    ap.add_argument("--harvest", help="harvest log: every research task found, with timestamps (volume, sample line)")
    ap.add_argument("--model", help="model for the verdict and Why calls (default: your default model)")
    ap.add_argument("--judge-model", help="model that judged the tasks, if the grade files don't record it")
    ap.add_argument("--cache-dir", help="LLM cache folder (default: llm-cache next to OUT_MD)")
    ap.add_argument("--no-instructions", action="store_true", help="the runs had no user instructions")
    ap.add_argument("--stats", help="JSON: tasks left out, MCP connections, an early stop (from the orchestrator)")
    a = ap.parse_args(argv)
    model = a.model

    T, dates, skipped = load(a.tasks, a.grades, a.runs)
    if not T:
        raise RuntimeError("no graded task has a finished run on every provider, so there is nothing to report")
    recorded = Counter(t["judge_model"] for t in T.values() if t["judge_model"])
    judge_model = counted(recorded) if recorded else (a.judge_model or "your default model (not recorded)")
    run_cfg = run_config(T)
    cfg = run_cfg[0]
    keyless = [e for e in MCP_PREFIX if cfg[e]["keyless"] is not False]
    unknown = [NAME[e] for e in keyless if cfg[e]["keyless"] is None]
    if unknown:
        print(f"Warning: no MCP config saved with the runs for {', '.join(unknown)}; assuming keyless.", file=sys.stderr)
    rr = reruns(a.runs, T)
    agg, pairs, order = compute(T)
    harvest, stamps = read_harvest(a.harvest)
    vol, hs = volume(stamps), harvest_stats(harvest, stamps)
    bill(agg, vol)

    out_md = Path(a.out_md).resolve()
    out_md.parent.mkdir(parents=True, exist_ok=True)
    cache = Cache(a.cache_dir or out_md.parent / "llm-cache")
    run_dir = Path(a.tasks).resolve().parent  # the run folder: config.json and stats.json, when the plugin ran it
    stats_path = Path(a.stats) if a.stats else run_dir / "stats.json"
    stats = json.loads(stats_path.read_text()) if stats_path.exists() else {}
    try:
        rcfg = json.loads((run_dir / "config.json").read_text())
    except (OSError, ValueError):
        rcfg = {}
    w, r, verdict, check = decide(cache, T, agg, pairs, order, vol, model, keyless, stats)
    if not a.no_instructions:
        no_instr = None
    elif rcfg.get("instructions") is False:
        no_instr = "run with --no-instructions, so your CLAUDE.md was left out"
    else:
        no_instr = "you have no user-level CLAUDE.md, so no instructions were added"

    # Why: one writing call over fixed task groups, then the check (and at most one repair).
    why, titles, why_note = [], {}, None
    try:
        why, titles, why_note = write_why(cache, T, agg, order, w, r, model)
    except UsageLimit:
        raise
    except Exception as exc:
        print(f"Why call failed: {exc}", file=sys.stderr, flush=True)
        why_note = "The Why section is omitted: its writing call failed."

    sample = subtitle(len(T), len({t["task"].get("session") for t in T.values()}), hs, dates)
    if skipped:
        print(f"Warning: skipped {skipped} grade file(s) whose ranks or task don't match this run.",
              file=sys.stderr)
    notes = run_notes(stats, len(T), rcfg) + unpriced_notes(agg) + ([why_note] if why_note else [])
    term, md, parts = summary(T, dates, agg, pairs, order, why, verdict, w, sample, check, notes)
    judges = max(len(t["grades"]) for t in T.values())
    heads, tasks = task_heads(judges), [task_data(k, t, order, judges, titles.get(k)) for k, t in T.items()]
    writer_model = counted(cache.models) if cache.models else (model or "your default model")
    method = methodology_md(len(T), judge_model, judges, cost_usd is not None, why_coverage(why),
                            no_instr).splitlines()
    config = config_md(T, run_cfg, judge_model, writer_model, dates, hs, sample_sentence(rcfg), rr)
    limits = limits_md(T, judge_model, keyless, model, stats.get("stop_note"))
    diag = diagnostics_md(check, notes + [f"Instructions: {w}" for w in stats.get("instruction_warnings") or []])
    md += ["", *per_task_md(T, order, judges, titles, tasks, heads), *method, "", *config, *limits, *diag]
    write_text(out_md, md_safe("\n".join(md)))
    shutil.copyfile(ASSETS / "method.svg", out_md.parent / "method.svg")

    svg = re.sub(r"^<\?xml[^>]*>", "", (ASSETS / "method.svg").read_text())
    parts["tasks"] = [{k: v for k, v in d.items() if k not in ("errors", "recall")} for d in tasks]  # in provider sections
    parts["heads"] = heads
    # MCP providers in config order: each keeps its color in the HTML; the baseline is grey
    parts["providers"] = [NAME[e] for e in PROVIDERS if e != BASELINE]
    parts["sections"] = [("How the grades explain the ranking",
                          md_block([f"- {ln}" for ln in computed(T, order, len(T), lambda k: f"[{k}](#task-{k})")])),
                         ("Methodology", md_block(method, svg)), ("Configuration", md_block(config)),
                         ("Limits", md_block(limits)), ("Diagnostics", md_block(diag))]
    out_html = out_md.with_suffix(".html")
    write_text(out_html, render(parts))
    import export
    out_gz = out_md.parent / "results.jsonl.gz"
    export.write(out_gz, a.runs.split(",")[0], read_jsonl(a.tasks), a.grades, set(T), unlabel)
    text = "\n".join(term + ["", f"Full report, with every task and the method: {out_html}",
                              f"Every answer and tool call, kept on your machine: {out_gz}"])
    write_text(out_md.parent / "terminal.txt", text + "\n")
    print(text)
    return out_html


def write_why(cache, T, agg, order, w, r, model):
    """(Why bullets, task titles, a note when there are none): one writing call over fixed task groups, then the check
    and at most one repair."""
    groups_ = groups(T, agg, w, r)
    ctx = [f"# Recommended: {NAME[w]}; runner-up: {NAME[r]}", "",
           "# How the grades explain the ranking", *computed(T, order, len(T)), ""]
    for g, (label, ks, frame, pair) in groups_.items():
        ctx += [f"# Group {g}: {label}", f"Bullet frame: {frame}", ""]
        for k in ks:
            ctx += [f"## Task {k}: {T[k]['task']['task']}", *task_facts(T[k], order), *judge_notes(T[k]),
                    *provider_notes(T[k], pair), ""]
    ctx += ["# All tasks (for titles)", *(f"- Task {k}: {' '.join(t['task']['task'].split())[:300]}" for k, t in T.items())]
    prompt, system = "\n".join(ctx), (PROMPTS / "report.md").read_text()
    out = cached(cache, [system, prompt], model, lambda: ask(prompt, system, WHY_SCHEMA, model))
    why = check_why(cache, out["bullets"], T, order, groups_, model)
    titles = {str(x["task"]): x["title"].strip() for x in out["titles"]}
    note = (None if why else f"No Why section: too few tasks for patterns (a statement needs at least {MIN_GROUP} "
            "tasks that share it)." if not groups_ else "No Why section: the check kept no statement on enough tasks.")
    return why, titles, note


def sample_sentence(cfg):
    """How the tasks were drawn, from the run's config.json."""
    if not cfg.get("seed") and cfg.get("seed") != 0:
        return "drawn at random"
    from build import PER_SESSION
    out = f"drawn at random (seed {cfg['seed']}), at most {PER_SESSION} per session"
    if cfg.get("sent") is not None:
        out += (f"; the task builder read {cfg['sent']} records and dropped {cfg.get('dropped', 0)} (expired premise, "
                "or no fair public question)")
    if cfg.get("private"):
        out += f", and the privacy check dropped {cfg['private']}"
    return out


if __name__ == "__main__":
    os.umask(0o077)  # as the plugin does: reports hold task text
    main(sys.argv[1:])
