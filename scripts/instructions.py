"""The person's instructions, rewritten per provider (prompts/instructions.md).

Reads the user-level CLAUDE.md with its `@` imports inlined, then makes one cached `claude -p` call per provider
that points the web-tool lines at that provider. Replays skip user settings, so this text, appended to the
system prompt, is how the person's instructions reach them without the original web-tool lines.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import PROVIDER_INFO, PROVIDERS, PROMPTS, Cache, ask, config_dir, obj, write_text  # noqa: E402

IMPORT = re.compile(r"(?<![\w`])@((?:~|\.{1,2})?/?[^\s`'\"()]+)")
MAX_DEPTH = 5
REWRITE_SCHEMA = obj(["instructions", "changed"], {"instructions": {"type": "string"},
                                                   "changed": {"type": "array", "items": {"type": "string"}}})


def resolve(token, base):
    p = Path(token).expanduser()
    p = p if p.is_absolute() else base / p
    return p if p.is_file() else None


def expand(path, depth=0, seen=None):
    """A file's text with `@path` imports inlined after the line that names them (up to MAX_DEPTH hops; not
    inside code fences or code spans)."""
    seen = set() if seen is None else seen
    path = Path(path).resolve()
    if path in seen:
        return ""
    seen.add(path)
    out, fence = [], False
    for line in path.read_text().splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
        found = []
        if not fence and depth < MAX_DEPTH:
            for m in IMPORT.finditer(re.sub(r"`[^`]*`", "", line)):
                p = resolve(m[1], path.parent)
                if p:
                    found.append((m[0], p))
        if found and line.strip() in {t for t, _ in found}:
            line = ""  # a line that is only an import becomes the imported text
        out.append(line)
        for _, p in found:
            out.append(expand(p, depth + 1, seen))
    return "\n".join(out).strip() + "\n"


def user_instructions():
    """The user-level CLAUDE.md, imports inlined, or "" if there is none."""
    f = config_dir() / "CLAUDE.md"
    return expand(f) if f.is_file() else ""


def tool_names(e):
    return PROVIDER_INFO[e]["search"] + PROVIDER_INFO[e]["fetch"]


def leftovers(text, provider):
    """Other providers' tool names still in a provider's instructions (tool names, not plain words)."""
    own = " ".join(tool_names(provider))
    others = {f"mcp__{e}__" for e in PROVIDERS if e != provider and PROVIDER_INFO[e]["mcp"]}
    # short names (web_search_exa), unless they are part of this provider's own names
    others |= {t.split("__")[-1] for e in PROVIDERS if e != provider for t in tool_names(e)
               if t.startswith("mcp__") and t.split("__")[-1] not in own}
    return sorted(t for t in others if t in text)


def rewrite(text, provider, model, cache):
    """(instructions for this provider, lines changed, warning or None)."""
    info = PROVIDER_INFO[provider]
    tools = ("Provider under test: built-in search only (no MCP). Tools: WebSearch, WebFetch." if info.get("baseline")
             else f"Provider under test: {info['name']}. Search tools: {', '.join(info['search'])}. "
                  f"Fetch tools: {', '.join(info['fetch'])}. Built-in WebSearch and WebFetch stay available.")
    system = (PROMPTS / "instructions.md").read_text()
    prompt = f"{tools}\n\n# The person's instructions\n\n{text}"
    out = cache.use(cache.entry([system, prompt], model, lambda: ask(prompt, system, REWRITE_SCHEMA, model)))
    left = leftovers(out["instructions"], provider)
    if left:
        retry = prompt + f"\n\nYour previous rewrite still named tools this replay doesn't have: {', '.join(left)}."
        out = cache.use(cache.entry([system, retry], model, lambda: ask(retry, system, REWRITE_SCHEMA, model)))
        left = leftovers(out["instructions"], provider)
    warn = f"{info['name']}: instructions still name {', '.join(left)}" if left else None
    return out["instructions"], out["changed"], warn


def write_all(run_dir, model, cache_dir):
    """Write run_dir/instructions/<provider>.md for every provider; return (has instructions, warnings)."""
    d = Path(run_dir) / "instructions"
    d.mkdir(parents=True, exist_ok=True)
    saved = d / "warnings.json"
    if all((d / f"{e}.md").exists() for e in PROVIDERS):  # a resumed run keeps the instructions it started with
        try:
            return True, json.loads(saved.read_text())
        except (OSError, ValueError):
            return True, []
    text = user_instructions()
    if not text.strip():
        return False, []
    cache, warns = Cache(cache_dir), []
    for e in PROVIDERS:
        f = d / f"{e}.md"
        if f.exists():
            continue
        new, changed, warn = rewrite(text, e, model, cache)
        write_text(d / f"{e}-changed.txt", "\n".join(changed) + "\n")
        write_text(f, new)  # last, so its existence means this provider is done
        if warn:
            warns.append(warn)
    write_text(saved, json.dumps(warns))
    return True, warns
