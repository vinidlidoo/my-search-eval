"""results.jsonl.gz: a run row (the tool surface each provider offered), then one row per task x provider, for
anyone who wants the raw evidence (schema: docs/results-schema.md). Unblinded: provider and tool names stay, and
judge labels become provider names. Local paths and the username are scrubbed (common.local_scrub).
"""

import gzip
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import PROVIDER_INFO, PROVIDERS, local_scrub, plugin_version, read_jsonl, write_json  # noqa: E402
from cost import cost_usd, provider_calls  # noqa: E402
from judge_runs import final, read_log, tool_calls  # noqa: E402
import runner  # noqa: E402

SCHEMA_VERSION = 2


def judge_of(grades, e, unlabel):
    """The first judge's grades for provider e, with run labels replaced by provider names, or None."""
    if not grades:
        return None
    g = grades[0]
    a = g["answers"].get(e)
    if a is None:
        return None
    u = lambda s: unlabel(s, g["labels"])  # noqa: E731
    return {"rank": g["rank"].get(e), "recall": a["recall"], "recall_reason": u(a["recall_reason"]),
            "error": {"none": "none", "limited": "side", "critical": "main"}[a["error"]],
            "error_reason": u(a["error_reason"]), "ranking_reason": u(g["ranking_reason"]),
            "comparison": u(g["rationale"]), "required": g.get("required"), "model": g.get("model"),
            "judges": len(grades)}


def run_id(run_dir):
    """The run's unique id (uuid4), from config.json; made and saved there if an older run lacks one."""
    f = Path(run_dir) / "config.json"
    try:
        cfg = json.loads(f.read_text())
    except (OSError, ValueError):
        return str(uuid.uuid4())
    if not cfg.get("run_id"):
        cfg["run_id"] = str(uuid.uuid4())
        write_json(f, cfg)
    return cfg["run_id"]


def surface(runs_dir):
    """Per provider, what Claude was offered: the MCP server (name, URL, connection status; Claude Code's log has
    no server version), the tool names from the first replay's init event, and the routing lines the plugin
    wrote into the instructions for that provider (the changed lines only). Tool descriptions and schemas aren't
    in the log, so they're not recorded."""
    runs_dir = Path(runs_dir)
    instr = runs_dir.parent / "instructions"
    logs = sorted(runs_dir.glob("*/*.jsonl"), key=lambda p: (len(p.parent.name), p.parent.name))
    out = {}
    for e in PROVIDERS:
        init = next((d for log in logs if log.stem == e for d in read_log(log) if d.get("subtype") == "init"), {})
        mcp = PROVIDER_INFO[e]["mcp"]
        try:
            routing = [ln for ln in (instr / f"{e}-changed.txt").read_text().splitlines() if ln.strip()]
        except OSError:
            routing = None
        out[e] = local_scrub({
            "mcp_servers": [{"name": n, "url": c.get("url"), "version": None,
                             "status": next((s.get("status") for s in init.get("mcp_servers") or []
                                             if isinstance(s, dict) and s.get("name") == n), None)}
                            for n, c in mcp.items()],
            "tools_offered": [t for t in init.get("tools") or [] if t.startswith("mcp__") or t in ("WebSearch", "WebFetch")],
            "tool_descriptions": None,
            "routing_lines": routing})
    return out


def header(run, runs_dir, tasks):
    runs_dir = Path(runs_dir)
    init = next((d for log in sorted(runs_dir.glob("*/*.jsonl")) for d in read_log(log) if d.get("subtype") == "init"), {})
    return {"row_type": "run", "schema_version": SCHEMA_VERSION, "run_id": run, "run_folder": runs_dir.parent.name,
            "plugin_version": plugin_version(), "claude_code_version": init.get("claude_code_version"),
            "model": init.get("model"), "tasks": len(tasks), "providers": surface(runs_dir)}


def rows(run, runs_dir, tasks, grades_dir, included, unlabel):
    """Yield the run row, then the task x provider rows. included: task numbers (str) that are in the report."""
    runs_dir = Path(runs_dir)
    yield header(run, runs_dir, tasks)
    reruns = {(str(d.get("task")), d.get("provider")) for d in read_jsonl(runs_dir / "connection-failures.jsonl")}
    version = plugin_version()
    for t in tasks:
        n = str(t["n"])
        grades = [json.loads(p.read_text()) for p in sorted(Path(grades_dir, n).glob("grade-*.json"))]
        grades = [g for g in grades if g.get("task_id", t.get("id")) == t.get("id")]
        for e in PROVIDERS:
            log = runs_dir / n / f"{e}.jsonl"
            if not log.exists():
                continue
            lines = read_log(log)
            init = next((d for d in lines if d.get("subtype") == "init"), {})
            res = final(lines)
            st = runner.status(log, e)
            mcp = bool(PROVIDER_INFO[e]["mcp"])
            calls = tool_calls(lines)
            yield local_scrub({
                "row_type": "result", "schema_version": SCHEMA_VERSION, "run_id": run, "run_folder": runs_dir.parent.name,
                "task_id": int(n), "record_id": t.get("id"), "task": t.get("task"), "provider": e, "provider_name": PROVIDER_INFO[e].get("long", PROVIDER_INFO[e]["name"]),
                "model": init.get("model"), "plugin_version": version,
                "claude_code_version": init.get("claude_code_version"),
                "included_in_report": n in included,
                "answer": res.get("result"), "answer_is_error": bool(res.get("is_error")) or not st["finished"],
                "duration_ms": res.get("duration_ms"), "num_turns": res.get("num_turns"),
                "tool_calls": calls,
                "provider_calls": provider_calls(lines), "list_fee_usd": round(cost_usd(lines), 6),
                "mcp_connected": runner.connected(st) if mcp else None, "mcp_offered": st["offered"] if mcp else None,
                "rerun_after_connection_failure": (n, e) in reruns,
                "rate_limited_calls": st["rate_limited"],
                "judge_capped": any(e in g.get("capped", []) for g in grades) or any(c["truncated"] for c in calls),
                "tools_offered": [x for x in init.get("tools") or [] if x.startswith("mcp__") or x in ("WebSearch", "WebFetch")],
                "judge": judge_of(grades, e, unlabel),
            })


def write(path, runs_dir, tasks, grades_dir, included, unlabel):
    """Write the rows gzipped; return how many task x provider rows."""
    run = run_id(Path(runs_dir).parent)
    n = 0
    tmp = Path(path).with_name(Path(path).name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        for r in rows(run, runs_dir, tasks, grades_dir, included, unlabel):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += r["row_type"] == "result"
    tmp.replace(path)
    return n


def read(path):
    """(the run row, the task x provider rows)."""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        rs = [json.loads(ln) for ln in f if ln.strip()]
    return rs[0], rs[1:]

