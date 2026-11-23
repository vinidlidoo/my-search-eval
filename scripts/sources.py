"""The results a tool call returned, as a list a provider can diff: {"url", "title", "published", "rank", "chars",
"cited_in_answer"}. One parser per known format (Parallel's JSON, Exa's text blocks, built-in WebSearch's link
list, a fetch's own URL), and a URL scan for anything else. Also each call's provider request id, when the
provider prints one in its result text (Claude Code doesn't pass MCP _meta on)."""

import json
import re

URL = re.compile(r"""https?://[^\s"'<>()\[\]{}`\\]+[^\s"'<>()\[\]{}`\\.,;:!?]""")
NONE = {"", "n/a", "na", "none", "null", "unknown"}


def _clean(v):
    if v is None:
        return None
    v = str(v).strip()
    return None if v.lower() in NONE else v


def _json(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def parallel(text):
    """Parallel web_search / web_fetch: {"search_id" or "extract_id", "results": [{url, title, publish_date,
    excerpts, full_content}]}."""
    d = _json(text)
    if not isinstance(d, dict) or not isinstance(d.get("results"), list):
        return None
    out = []
    for r in d["results"]:
        if isinstance(r, dict) and isinstance(r.get("url"), str) and r["url"]:
            ex = r.get("excerpts") if isinstance(r.get("excerpts"), list) else []
            full = r.get("full_content") if isinstance(r.get("full_content"), str) else ""
            body = "".join(x for x in ex if isinstance(x, str)) + full
            out.append({"url": r["url"], "title": _clean(r.get("title")), "published": _clean(r.get("publish_date")),
                        "chars": len(body)})
    return out


EXA_BLOCK = re.compile(r"(?m)^Title: ")


def exa(text):
    """Exa web_search_exa / web_fetch_exa: text blocks of "Title:", "URL:", "Published:", "Author:", then the
    page text or highlights."""
    if not re.search(r"(?m)^URL: \S", text or ""):
        return None
    starts = [m.start() for m in EXA_BLOCK.finditer(text)] or [0]
    out = []
    for a, b in zip(starts, starts[1:] + [len(text)]):
        block = text[a:b]
        url = re.search(r"(?m)^URL: (\S+)", block)
        if not url:
            continue
        title = re.search(r"(?m)^Title: (.*)$", block)
        pub = re.search(r"(?m)^Published(?: Date)?: (.*)$", block)
        out.append({"url": url[1], "title": _clean(title[1]) if title else None,
                    "published": _clean(pub[1]) if pub else None, "chars": len(block)})
    return out


def builtin_search(text):
    """Built-in WebSearch: 'Web search results for query: ...' then 'Links: [{"title", "url"}, ...]'. The page text
    isn't returned (the rest is a summary), so chars is None."""
    m = re.search(r"Links: (\[.*?\])\s*(?:\n|$)", text or "", re.S)
    links = _json(m[1]) if m else None
    if not isinstance(links, list):
        return None
    return [{"url": x["url"], "title": _clean(x.get("title")), "published": None, "chars": None}
            for x in links if isinstance(x, dict) and isinstance(x.get("url"), str) and x["url"]]


def fallback(text):
    seen, out = set(), []
    for u in URL.findall(text or ""):
        if u not in seen:
            seen.add(u)
            out.append({"url": u, "title": None, "published": None, "chars": None})
    return out


def _norm(u):
    if not isinstance(u, str):
        return ""
    u = re.sub(r"^https?://(www\.)?", "", u.strip().lower()).split("#")[0]
    return u.rstrip("/")


def results(tool, inp, text, answer):
    """The parsed result list for one call; [] for calls that aren't search or fetch (Read, errors)."""
    tool, inp, text = tool or "", inp if isinstance(inp, dict) else {}, text or ""
    if tool == "Read" or not text:
        return []
    if tool == "WebFetch":
        url = inp.get("url")
        rows = [{"url": url, "title": None, "published": None, "chars": len(text)}] if isinstance(url, str) and url else []
    elif tool == "WebSearch":
        rows = builtin_search(text)
    else:
        rows = parallel(text) if tool.startswith("mcp__parallel__") else exa(text) if tool.startswith("mcp__exa__") else None
        rows = rows if rows is not None else parallel(text) or exa(text)
    if rows is None:
        rows = fallback(text)
    cited = {_norm(u) for u in URL.findall(answer or "")}
    return [{"rank": i, "url": r["url"], "title": r["title"], "published": r["published"], "chars": r["chars"],
             "cited_in_answer": _norm(r["url"]) in cited} for i, r in enumerate(rows, 1)]


# Request ids printed in result text, per provider first, then generic spellings.
REQUEST_ID = {
    "parallel": re.compile(r'"(?:search_id|extract_id)"\s*:\s*"([\w-]+)"'),
    "exa": re.compile(r'(?i)\b(?:request[_ ]?id|requestId)\b["\']?\s*[:=]\s*["\']?([\w-]{6,})'),
}
GENERIC_ID = re.compile(r'(?i)\b(?:request[_ ]?id|requestId|search_id|extract_id)\b["\']?\s*[:=]\s*["\']?([\w-]{6,})')


def request_id(tool, text, tool_use_result=None):
    """The provider's id for this call, or None. Built-in WebSearch: the server tool's own id, from the log."""
    tool = tool or ""
    if tool == "WebSearch" and isinstance(tool_use_result, dict):
        for r in tool_use_result.get("results") or []:
            if isinstance(r, dict) and r.get("tool_use_id"):
                return r["tool_use_id"]
        return None
    m = tool.split("__")
    pat = REQUEST_ID.get(m[1]) if len(m) > 2 else None
    found = (pat.search(text or "") if pat else None) or (GENERIC_ID.search(text or "") if tool.startswith("mcp__") else None)
    return found[1] if found else None
