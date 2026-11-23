"""Log walker: a run log's tool calls, evidence and final answer, scrubbed of provider and tool names.

Import: from judge_runs import load_run; load_run(path_to_provider_jsonl) -> (answer, evidence)
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import PROVIDER_INFO, local_scrub, read_jsonl  # noqa: E402

MAX_RESULT = 100000  # chars kept per tool result (well above any result seen in testing)
MAX_EVIDENCE = 400000  # chars kept per run; a cap that bites penalizes the provider that returned more
# Provider and tool names that would unblind a run: every tool name, the paths of saved tool outputs (their file
# names carry the tool), plus each provider's own pattern (PROVIDER_INFO).
SCRUB = re.compile(
    r"/\S*/tool-results/\S+|mcp__\w+|\bweb_(?:search|fetch)\w*\b|\bWeb(?:Search|Fetch)\b|"
    + "".join(f"{v['scrub']}|" for v in PROVIDER_INFO.values() if v.get("scrub"))
    + r"\b(?:search_id|extract_id|request_id|requestId|searchId|extractId)\b[\"':= ]+[\w-]+",
)


def scrub(text):
    return SCRUB.sub("[tool]", text)


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    return json.dumps(content)


def read_log(log):
    """A run log's JSON objects, parsed once; lines that don't parse are skipped."""
    return read_jsonl(log)


def tool_pairs(lines):
    """Yield (tool_use block, its tool_result block) once per call, in result order; calls never answered come
    last, with None as the result."""
    calls = {}
    for d in lines:
        msg = d.get("message") or {}
        if d.get("type") == "assistant":
            for b in msg.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    calls[b["id"]] = b
        elif d.get("type") == "user" and isinstance(msg.get("content"), list):
            for b in msg["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in calls:
                    yield calls.pop(b["tool_use_id"]), b
    for c in calls.values():
        yield c, None


# A provider refusing a call for its rate limit or quota. Matched only on error results, with URLs removed, and not
# when the error is about a page the provider tried to fetch (a site's own 429).
RATE_LIMIT = re.compile(r"rate.?limit|too many requests|\b429\b|quota exceeded|daily limit|limit exceeded", re.I)
SITE_FAILURE = re.compile(r"(?i)\b(?:crawl\w*|scrap\w*|failed to (?:fetch|load|retrieve|get|access)|"
                          r"could not (?:fetch|load|retrieve|access)|(?:fetching|loading) (?:the )?(?:url|page|site))\b")
URL_TEXT = re.compile(r"\b(?:https?|ftp)://\S+|\bwww\.\S+", re.I)


def refusal(text, is_error):
    """A tool result that is the provider refusing for a rate limit: an error, not page text or a URL."""
    if not is_error:
        return False
    t = URL_TEXT.sub(" ", text or "")
    return bool(RATE_LIMIT.search(t)) and not SITE_FAILURE.search(t)
EMPTY = re.compile(r"^\s*(?:\[\s*\]|\{\s*\}|null|none|no (?:search )?results?(?: found)?\.?)?\s*$", re.I)


def _ms(a, b):
    """Milliseconds between two ISO timestamps, or None."""
    from datetime import datetime
    try:
        f = lambda t: datetime.fromisoformat(t.replace("Z", "+00:00"))  # noqa: E731
        return max(0, int((f(b) - f(a)).total_seconds() * 1000))
    except (AttributeError, TypeError, ValueError):
        return None


def _utc(t):
    """An ISO timestamp from the log, as UTC with a Z, or None."""
    from datetime import datetime, timezone
    try:
        d = datetime.fromisoformat(t.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def tool_calls(lines, cap=MAX_RESULT):
    """Every tool call in order, not blinded but scrubbed of local paths and the username (common.local_scrub):
    {"id", "tool", "input", "started_at", "duration_ms", "provider_request_id", "result" (cut at cap),
    "result_chars", "truncated", "is_error", "flags" (error, rate_limit, empty, no_result), "results" (the parsed
    result list, sources.results)}. Times come from the log's timestamps, when present."""
    import sources
    started, ended, raw, out = {}, {}, {}, []
    for d in lines:
        msg = d.get("message") or {}
        content = msg.get("content")
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                started[b.get("id")] = d.get("timestamp")
            elif b.get("type") == "tool_result":
                ended[b.get("tool_use_id")] = d.get("timestamp")
                raw[b.get("tool_use_id")] = d.get("tool_use_result")
    answer = final(lines).get("result") or ""
    order = {cid: i for i, cid in enumerate(started)}
    for call, res in sorted(tool_pairs(lines), key=lambda p: order.get(p[0].get("id"), 1e9)):
        cid, tool, inp = call.get("id"), call.get("name"), call.get("input") or {}
        text = text_of(res.get("content")) if res is not None else None
        flags = []
        if res is None:
            flags.append("no_result")
        else:
            if res.get("is_error"):
                flags.append("error")
            if refusal(text, res.get("is_error")):
                flags.append("rate_limit")
            if EMPTY.match(text):
                flags.append("empty")
        found = [] if res is None or res.get("is_error") else sources.results(tool, inp, text, answer)
        out.append(local_scrub({
            "id": cid, "tool": tool, "input": inp, "started_at": _utc(started.get(cid)),
            "duration_ms": _ms(started.get(cid), ended.get(cid)),
            "provider_request_id": None if text is None else sources.request_id(tool, text, raw.get(cid)),
            "result": None if text is None else text[:cap], "result_chars": None if text is None else len(text),
            "truncated": text is not None and len(text) > cap, "is_error": bool(res and res.get("is_error")),
            "flags": flags, "results": found}))
    return out


def final(lines):
    """The log's last result line, or {}."""
    return next((d for d in reversed(lines) if d.get("type") == "result"), {})


def run_evidence(lines, cap=MAX_EVIDENCE):
    """(final answer, ordered evidence: each call's inputs and what it returned, whether a cap cut it). Read calls
    are the run reading a long result Claude Code saved to a file."""
    evidence = []
    for call, res in tool_pairs(lines):
        if res is None:
            continue
        name, inp = call.get("name", ""), call.get("input") or {}
        kind = "read saved result" if name == "Read" else "fetch" if "fetch" in name.lower() else "search"
        body = text_of(res.get("content"))[:MAX_RESULT]
        # Only what was asked, not each provider's parameter names, which would unblind it.
        asked = [v for k in ("query", "objective", "search_queries", "url", "urls") if (v := inp.get(k))]
        asked = "; ".join(x if isinstance(x, str) else ", ".join(map(str, x)) for x in asked)
        if name == "Read":
            asked = f"lines {inp.get('offset', 1)} on" if inp.get("offset") else "from the start"
        evidence.append(f"### {kind}: {asked}\n{body}")
    ev, total, cut = [], 0, False
    for e in evidence:
        if total + len(e) > cap:
            ev.append("[evidence truncated]")
            cut = True
            break
        ev.append(e)
        total += len(e)
    return scrub(final(lines).get("result") or "(no answer)"), scrub("\n\n".join(ev) or "(no evidence)"), cut


def load_run(log, cap=MAX_EVIDENCE):
    """(final answer, ordered evidence: each call's inputs and what it returned, whether the cap cut it)."""
    return run_evidence(read_log(log), cap)
