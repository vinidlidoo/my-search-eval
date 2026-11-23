"""Replay one task on one provider as a fresh headless Claude Code session.

Writes OUT_DIR/<provider>.jsonl (stream-json log) and OUT_DIR/<provider>-mcp.json (the MCP config it ran with).

Isolation:
- Only the provider's MCP server is loaded (--strict-mcp-config); built-in WebSearch/WebFetch stay on.
- User-level settings are skipped (--setting-sources project,local) and the person's instructions, rewritten
  for this provider, are appended instead, so the original web-tool lines never reach the run.
- No shell or file-editing tools. Read is allowed only inside this replay's own project folder, where Claude
  Code saves its long tool results, so a long result is read in full as in a real session, not cut to its
  preview. Other sessions' saved outputs stay out of reach.
- The run starts in an empty temp folder with a neutral name and is not saved as a session; the project
  folder Claude Code creates for that temp folder is removed afterwards.
- Credentials and provider switches are removed from the environment (common.child_env), so runs use the
  subscription login.
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import PROVIDER_INFO, child_env, is_usage_limit, project_dir, read_jsonl  # noqa: E402
from judge_runs import refusal  # noqa: E402  (an error result refusing for a rate limit)

TIMEOUT = 900


def run(provider, task_text, instructions, out_dir, model=None):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    servers = PROVIDER_INFO[provider]["mcp"]
    mcp = out / f"{provider}-mcp.json"
    mcp.write_text(json.dumps({"mcpServers": servers}))
    cwd = tempfile.mkdtemp(prefix="run-")
    allowed = ["WebSearch", "WebFetch", read_rule(cwd)] + [f"mcp__{name}" for name in servers]
    cmd = ["claude", "-p", *(["--model", model] if model else []),
           "--setting-sources", "project,local",
           *(["--append-system-prompt", instructions] if instructions else []),
           "--strict-mcp-config", "--mcp-config", str(mcp.resolve()),
           "--tools", "WebSearch,WebFetch,Read", "--allowedTools", *allowed,
           "--no-session-persistence", "--output-format", "stream-json", "--verbose"]
    log = out / f"{provider}.jsonl"
    try:
        with log.open("w") as f:
            try:
                subprocess.run(cmd, input=task_text.strip(), text=True, cwd=cwd, env=child_env(), stdout=f,
                               stderr=subprocess.STDOUT, timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                f.write(json.dumps({"type": "harness", "error": f"timed out after {TIMEOUT} s"}) + "\n")
    finally:
        cleanup(cwd)


def read_rule(cwd):
    """Read access to this replay's own project folder only ("//abs/path" is an absolute path in a rule)."""
    return f"Read(/{project_dir(cwd)}/**)"


def cleanup(cwd):
    """Remove the temp folder and the project folder Claude Code made for it, unless it holds a transcript."""
    proj = project_dir(cwd)
    shutil.rmtree(cwd, ignore_errors=True)
    if proj.is_dir() and "-run-" in proj.name and not any(proj.rglob("*.jsonl")):
        shutil.rmtree(proj, ignore_errors=True)


def text_of(content):
    if isinstance(content, list):
        return "\n".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    return content if isinstance(content, str) else json.dumps(content)


def status(log, provider=None):
    """What a run log shows: {"finished": a result that isn't an error, "error": the error text, "usage_limit",
    "servers": MCP statuses, "offered": every MCP server's tools were offered, "rate_limited": the provider's own
    MCP calls refused for a rate limit}."""
    st = {"finished": False, "error": None, "usage_limit": False, "servers": None, "offered": True,
          "rate_limited": 0}
    prefix = f"mcp__{provider}__" if provider and PROVIDER_INFO[provider]["mcp"] else None
    calls = set()
    for d in read_jsonl(log):
        if d.get("subtype") == "init":
            st["servers"] = [s.get("status") for s in d.get("mcp_servers", [])]
            tools = d.get("tools") or []
            st["offered"] = all(any(t.startswith(f"mcp__{s}__") for t in tools)
                                for s in (PROVIDER_INFO[provider]["mcp"] if provider else {}))
        elif d.get("type") == "assistant" and prefix:
            for b in (d.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and str(b.get("name", "")).startswith(prefix):
                    calls.add(b.get("id"))
        elif d.get("type") == "user" and calls:
            content = (d.get("message") or {}).get("content")
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in calls:
                    if refusal(text_of(b.get("content")), b.get("is_error")):
                        st["rate_limited"] += 1
        elif d.get("type") == "result":
            err = bool(d.get("is_error")) or d.get("subtype") not in (None, "success")
            st["finished"] = not err
            st["error"] = str(d.get("result") or d.get("subtype")) if err else None
            st["usage_limit"] = err and is_usage_limit(st["error"])
        elif d.get("type") == "harness":
            st["error"] = d.get("error")
    return st


def connected(st):
    return all(s == "connected" for s in st["servers"] or []) and st["offered"]


def done(log, provider):
    """A finished run whose provider connected, was offered, and wasn't rate-limited."""
    st = status(log, provider)
    return st["finished"] and connected(st) and not st["rate_limited"]
