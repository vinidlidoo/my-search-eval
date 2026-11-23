"""diagnostics.md: what went wrong in a run, safe to paste into a public issue.

Built only from a whitelist of known fields: versions, the phase, the exception type, stack frames (file, line,
function; no locals, no message unless it matches one of our own fixed messages), progress counts, per-provider
counts, flags and the retention summary. Never task text, queries, URLs, answers, results, judge notes,
instructions, paths or the username. local_scrub runs over the result as a second guard.
"""

import json
import platform
import re
import sys
import traceback
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import PROVIDERS, ROOT, local_scrub, plugin_version, read_jsonl, write_text  # noqa: E402

# Our own failure messages, by the start that identifies them; only the label goes into diagnostics.md.
KNOWN = [
    (r"^Only \d+ usable research tasks", "too few usable tasks after the task builder"),
    (r"^No task finished on every provider", "no task finished on every provider"),
    (r"^Every judge call failed", "every judge call failed"),
    (r"MCP server (?:failed to|didn't) connect", "an MCP server failed to connect"),
    (r"keyless rate limit was reached before any task", "rate limit before any task finished"),
    (r"^Another run is already active", "another run was active"),
    (r"usage limit", "Claude usage limit"),
    (r"(?i)fake failure", "test failure (MY_SEARCH_EVAL_FAIL)"),
]
PROGRESS_FIELDS = ["status", "phase", "step", "steps", "done", "total", "updated"]
INT = re.compile(r"^\d+$")


def known(message):
    """The fixed label of one of our own messages, or None."""
    for pat, label in KNOWN:
        if re.search(pat, message or ""):
            return label
    return None


def frames(tb):
    """[(file, line, function)]: files inside the plugin relative to it, any other file by its name only."""
    out = []
    for f in traceback.extract_tb(tb):
        p = Path(f.filename)
        try:
            name = str(p.resolve().relative_to(ROOT))
        except (OSError, ValueError):
            name = p.name
        out.append([name, f.lineno, f.name])
    return out


def failure(exc):
    """What progress.json keeps about an exception, for diagnostics.md."""
    label = "login check refused" if type(exc).__name__ == "AuthError" else known(str(exc))
    return {"exc_type": type(exc).__name__, "exc_label": label, "trace": frames(exc.__traceback__)}


def _claude_version(runs):
    for log in sorted(runs.glob("*/*.jsonl")):
        for d in read_jsonl(log):
            if d.get("subtype") == "init" and d.get("claude_code_version"):
                v = str(d["claude_code_version"])
                return v if re.fullmatch(r"[\w.+-]{1,40}", v) else None
    return None


def _providers(runs):
    import runner
    out = {}
    for e in PROVIDERS:
        c = {"runs": 0, "finished": 0, "failed": 0, "connected": 0, "offered": 0, "rate_limited_calls": 0,
             "usage_limit": 0}
        for log in runs.glob(f"*/{e}.jsonl"):
            st = runner.status(log, e)
            c["runs"] += 1
            c["finished"] += bool(st["finished"])
            c["failed"] += not st["finished"]
            c["connected"] += runner.connected(st)
            c["offered"] += bool(st["offered"])
            c["rate_limited_calls"] += int(st["rate_limited"] or 0)
            c["usage_limit"] += bool(st["usage_limit"])
        c["connection_retries"] = sum(1 for d in read_jsonl(runs / "connection-failures.jsonl")
                                      if d.get("provider") == e)
        out[e] = c
    return out


def _count(path):
    try:
        return sum(1 for ln in Path(path).read_text(errors="replace").splitlines() if ln.strip())
    except OSError:
        return None


def build(run_dir, stopped_unexpectedly=False):
    """The diagnostics.md text for a run folder."""
    run_dir = Path(run_dir)
    runs = run_dir / "runs"
    try:
        p = json.loads((run_dir / "progress.json").read_text())
    except (OSError, ValueError):
        p = {}
    try:
        cfg = json.loads((run_dir / "config.json").read_text())
    except (OSError, ValueError):
        cfg = {}
    status = "stopped unexpectedly" if stopped_unexpectedly else p.get("status")
    if stopped_unexpectedly:  # the worker died without recording an exception; ignore any older one
        p = {**p, "exc_type": "WorkerStopped", "exc_label": "the background worker stopped without an error", "trace": []}
    exc_type = p.get("exc_type")
    lines = ["# my-search-eval diagnostics", "",
             "Built from a fixed list of fields: no task text, queries, URLs, answers, results, judge notes, "
             "instructions, paths or username.", "",
             "## Versions", "",
             f"- plugin: {plugin_version()}",
             f"- Claude Code: {_claude_version(runs) or 'unknown'}",
             f"- Python: {platform.python_version()}",
             f"- OS: {platform.system()} {platform.release()} ({platform.machine()})", "",
             "## Failure", "",
             f"- status: {status}",
             f"- phase: {p.get('phase')} (step {p.get('step')} of {p.get('steps')})",
             f"- exception: {exc_type or 'none recorded'}",
             f"- reason: {p.get('exc_label') or 'not one of the plugin’s own messages'}", ""]
    trace = p.get("trace") or []
    if trace:
        lines += ["Stack (file, line, function; most recent call last):", "", "```"]
        lines += [f"{f}:{n} in {fn}" for f, n, fn in trace
                  if isinstance(f, str) and isinstance(n, int) and isinstance(fn, str)]
        lines += ["```", ""]
    lines += ["## Progress", "", *(f"- {k}: {p.get(k)}" for k in PROGRESS_FIELDS if k in p), ""]
    tasks = {"harvested": _count(run_dir / "harvest.jsonl"), "pool": cfg.get("pool"), "sent to builder": cfg.get("sent"),
             "built": _count(run_dir / "tasks.jsonl"), "dropped by builder": cfg.get("dropped"),
             "dropped by privacy check": cfg.get("private"), "wanted": cfg.get("want"),
             "replayed (any provider)": len([d for d in runs.iterdir() if d.is_dir() and any(d.glob("*.jsonl"))])
             if runs.is_dir() else 0,
             "judged": len(list((run_dir / "grades").glob("*/grade-*.json")))}
    lines += ["## Tasks", "", *(f"- {k}: {v}" for k, v in tasks.items() if v is not None), ""]
    lines += ["## Providers", "", "| provider | runs | finished | failed | connected | offered | rate-limited calls | "
              "usage limit | connection retries |", "|---|--:|--:|--:|--:|--:|--:|--:|--:|"]
    for e, c in _providers(runs).items():
        lines.append(f"| {e} | {c['runs']} | {c['finished']} | {c['failed']} | {c['connected']} | {c['offered']} | "
                     f"{c['rate_limited_calls']} | {c['usage_limit']} | {c['connection_retries']} |")
    model = cfg.get("model")
    model = "default" if not model else model if re.fullmatch(r"[\w.\[\]:-]{1,60}", model) else "custom"
    lines += ["", "## Flags", "",
              f"- model: {model}",
              f"- quick: {bool(cfg.get('quick'))}",
              f"- no-instructions: {cfg.get('instructions') is False}",
              f"- keep-logs: {bool(cfg.get('keep_logs'))}", ""]
    pr = cfg.get("pruned") or {}
    lines += ["## Retention at start", "",
              f"- run folders deleted: {pr.get('runs', 0)}", f"- raw logs pruned: {pr.get('logs', 0)}",
              f"- cache entries deleted: {pr.get('cache', 0)}", ""]
    return local_scrub("\n".join(lines))


def write(run_dir, stopped_unexpectedly=False):
    path = Path(run_dir) / "diagnostics.md"
    write_text(path, build(run_dir, stopped_unexpectedly))
    return path


def repo():
    """owner/name from plugin.json's repository URL."""
    try:
        url = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())["repository"]
    except (OSError, ValueError, KeyError):
        return None
    m = re.search(r"github\.com[/:]([\w.-]+/[\w.-]+?)(?:\.git)?/?$", str(url))
    return m[1] if m else None


MAX_URL = 8000


def issue(run_dir):
    """(title, body path, fallback URL) for filing diagnostics.md as an issue."""
    run_dir = Path(run_dir)
    body_path = run_dir / "diagnostics.md"
    body = body_path.read_text()
    phase = re.search(r"(?m)^- phase: (\S+)", body)
    exc = re.search(r"(?m)^- exception: (\S+)", body)
    title = f"{phase[1] if phase else 'run'}: {exc[1] if exc else 'failure'} (v{plugin_version()})"
    base = f"https://github.com/{repo()}/issues/new?"
    note = "\n\n(trimmed to fit a link; please attach the full diagnostics.md)"
    text = body
    while True:
        url = base + urllib.parse.urlencode({"title": title, "body": text}, quote_via=urllib.parse.quote)
        if len(url) <= MAX_URL or not text:
            return title, body_path, url
        keep = text[:-len(note)] if text.endswith(note) else text
        text = keep[: int(len(keep) * 0.9)].rsplit("\n", 1)[0] + note


if __name__ == "__main__":
    print(build(sys.argv[1]))
