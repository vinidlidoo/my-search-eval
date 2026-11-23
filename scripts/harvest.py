"""Harvest web search tasks and their context from Claude Code transcripts.

Usage: python3 -I harvest.py OUT.jsonl [PROJECTS_DIR]
Writes one record per task. A task is every web search a subagent (or workflow agent) ran for one
delegation, or every web search the main agent ran in one user turn.
Output holds private context: it stays in the run folder on this machine.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import config_dir  # noqa: E402

PROJECTS = config_dir() / "projects"

# General web search tools only. Docs-only and vertical search tools are excluded.
WEB_SEARCH_TOOLS = {
    "WebSearch",
    "mcp__exa__web_search_exa",
    "mcp__exa__web_search_advanced_exa",
    "mcp__parallel__web_search",
    "mcp__firecrawl__firecrawl_search",
    "mcp__webclaw__search",
}

# Text Claude Code injects as a "user" message without isMeta set.
INJECTED_PREFIXES = (
    "<",  # task notifications, command output, system reminders, wake and relay tags
    "This session is being continued",
    "Another Claude session",
    "[Request interrupted",
    "[Image",
    "[Cross-session",
)

WORKFLOW_RELAY = "[Workflow harness — user request]"

# Delegations that only point to a brief file: inline the brief, or mark the task unrecoverable.
BRIEF_PATH = re.compile(r"(/[^\s`'\")]*brief[^\s`'\")/]*\.(?:md|txt))", re.IGNORECASE)

# Fetch tools: what the agent opened after searching shows where the answer lived.
FETCH_TOOLS = {
    "WebFetch",
    "mcp__webclaw__scrape",
    "mcp__exa__web_fetch_exa",
    "mcp__parallel__web_fetch",
    "mcp__firecrawl__firecrawl_scrape",
}
URL = re.compile(r"https?://[^\s\"'<>)\]}`]+")

USER_MSGS_KEPT = 3
CTX_CHARS = 1500


def text_of(content, thinking=False):
    """Plain text of a message content (str or list of blocks), skipping tool results."""
    if isinstance(content, str):
        return content
    parts = []
    for b in content if isinstance(content, list) else []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            parts.append(b.get("text", ""))
        elif thinking and b.get("type") == "thinking" and b.get("thinking"):
            parts.append("[thinking] " + b["thinking"])
    return "\n".join(parts)


def is_real_user(d, text):
    return bool(text) and not d.get("isMeta") and not d.get("isCompactSummary") and not text.startswith(INJECTED_PREFIXES)


def query_of(args):
    args = args or {}
    q = args.get("query") or args.get("objective") or ""
    if isinstance(q, list):
        q = " | ".join(map(str, q))
    return str(q)


def harvest_file(path, projects=PROJECTS):
    is_subagent = "subagents" in path.parts
    project = path.parts[len(Path(projects).parts)]
    user_msgs = []  # recent real user messages, newest last
    delegation = None  # first user message of a subagent
    agent_text = []  # agent text since the last real user message
    task = None
    cwd = None  # the session's working directory, for brief files
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(d, dict) or (d.get("isSidechain") and not is_subagent):
                continue  # not a transcript entry, or a subagent turn kept inline in the main transcript
            cwd = cwd or d.get("cwd")
            msg = d.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if d.get("type") == "user":
                if task and isinstance(content, list):
                    for blk in content:
                        if isinstance(blk, dict) and blk.get("type") == "tool_result" and blk.get("tool_use_id") in task["_search_ids"]:
                            raw = json.dumps(blk.get("content"))
                            for u in URL.findall(raw):
                                u = u.rstrip(".,;\\")
                                if u not in task["returned_urls"]:
                                    task["returned_urls"].append(u)
                t = text_of(content).strip()
                if is_subagent and t and not d.get("isMeta"):
                    if delegation is None:
                        delegation = t
                    elif delegation.startswith(WORKFLOW_RELAY) and "\n[computed task]\n" not in delegation:
                        # Workflow agents get the user's request first, then the real task.
                        delegation += "\n[computed task]\n" + t
                elif not is_subagent and is_real_user(d, t):
                    if task:
                        yield task
                        task = None
                    user_msgs = (user_msgs + [t])[-USER_MSGS_KEPT:]
                    agent_text = []
            elif d.get("type") == "assistant" and isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use" and b.get("name") in WEB_SEARCH_TOOLS:
                        if task is None:
                            task = {
                                "id": b.get("id"),
                                "source": ("workflow" if "workflows" in path.parts else "subagent") if is_subagent else "user",
                                "session": d.get("sessionId"),
                                "project": project,
                                "cwd": cwd,
                                "timestamp": d.get("timestamp"),
                                "delegation": clip_delegation(delegation),
                                "user_messages": [m[-CTX_CHARS:] for m in user_msgs] if not is_subagent else [],
                                "agent_text_before": "\n".join(agent_text)[-CTX_CHARS:],
                                "calls": [],
                                "returned_urls": [],
                                "fetched_urls": [],
                                "agent_text_after": "",
                                "_search_ids": set(),
                            }
                        task["calls"].append({"tool": b["name"], "query": query_of(b.get("input")), "args": b.get("input")})
                        task["_search_ids"].add(b.get("id"))
                    elif task and b.get("type") == "tool_use" and b.get("name") in FETCH_TOOLS:
                        inp = b.get("input") or {}
                        for u in [inp.get("url")] + list(inp.get("urls") or []):
                            if u and u not in task["fetched_urls"]:
                                task["fetched_urls"].append(u)
                t = text_of(content, thinking=True).strip()
                if t:
                    agent_text.append(t)
                    if task:
                        task["agent_text_after"] = (task["agent_text_after"] + "\n" + text_of(content).strip())[-CTX_CHARS:]
    if task:
        yield task


def clip_delegation(d):
    """Keep a delegation within budget; for workflow agents, keep the real (computed) task whole."""
    if not d:
        return None
    if "\n[computed task]\n" in d:
        relay, computed = d.split("\n[computed task]\n", 1)
        computed = computed.split("The computed task text follows:", 1)[-1].strip()  # drop harness boilerplate
        m = re.search(r"\bTASK\b", computed)
        if m and m.start() > CTX_CHARS:  # long context first: keep its head and the task section whole
            computed = computed[:CTX_CHARS] + "\n[…]\n" + computed[m.start():m.start() + CTX_CHARS * 3]
        else:
            computed = computed[:CTX_CHARS * 6]
        return relay[:CTX_CHARS // 2] + "\n[computed task]\n" + computed
    return d[:CTX_CHARS * 2]


def allowed(path, cwd):
    """Brief files are read only from ~/.claude or the session's own project folder."""
    p = Path(path).resolve()
    home = Path.home().resolve()
    proj = Path(cwd).resolve() if cwd else None
    # a session started in the home folder (or above it) doesn't make the whole home folder readable
    roots = [config_dir().resolve()] + ([proj] if proj and proj != home and proj not in home.parents else [])
    return any(p == r or r in p.parents for r in roots)


def resolve_brief(task):
    """Inline a delegation's brief file if it still exists and sits under ~/.claude or the session's project
    folder; flag the task otherwise."""
    m = BRIEF_PATH.search(task["delegation"] or "")
    task["unrecoverable"] = False
    if m:
        brief = Path(m.group(1))
        if brief.is_file() and allowed(brief, task.get("cwd")):
            task["delegation"] += "\n[brief]\n" + brief.read_text(errors="replace")[: CTX_CHARS * 2]
        else:
            task["unrecoverable"] = True
    return task


def main(out, projects=PROJECTS):
    """Write every task to out; return {"tasks", "searches", "unrecoverable"}."""
    seen = set()
    n = calls = unrec = 0
    projects = Path(projects)
    with open(out, "w") as w:
        for path in sorted(projects.rglob("*.jsonl")):
            for task in harvest_file(path, projects):
                # Resumed and forked sessions repeat calls: dedupe by the first tool_use id.
                if task["id"] in seen:
                    continue
                seen.add(task["id"])
                del task["_search_ids"]
                resolve_brief(task)
                w.write(json.dumps(task) + "\n")
                n += 1
                calls += len(task["calls"])
                unrec += task["unrecoverable"]
    return {"tasks": n, "searches": calls, "unrecoverable": unrec}


if __name__ == "__main__":
    print(main(*sys.argv[1:]))
