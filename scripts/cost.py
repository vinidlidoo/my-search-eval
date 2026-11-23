"""Dollar cost of each run's search-provider calls, at each provider's public API list price (common.PROVIDER_INFO).

Import: from cost import cost_usd; cost_usd(judge_runs.read_log(path_to_provider_jsonl)) -> float

Assumptions:
- List (pay-as-you-go) price; free tiers, signup credits and plan discounts ignored.
- The runs used both MCPs keyless (free, rate-limited); this is what the same calls would
  cost on the paid API, which the MCPs bill at "the same rates as the underlying API".
- Built-in WebSearch/WebFetch count as $0 (included in the Claude Code subscription).
- A call whose tool_result is an error is not billed.
"""

import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import PROVIDER_INFO, PRICES_CHECKED  # noqa: E402
from judge_runs import tool_pairs  # noqa: E402

CHECKED = PRICES_CHECKED
PRICES = {tool: p for v in PROVIDER_INFO.values() for tool, p in v.get("prices", {}).items()}
FREE = {e: v["free"] for e, v in PROVIDER_INFO.items() if v.get("free")}
DAYS_PER_MONTH = 30.44


def monthly_usd(provider, tasks_per_month, mean_cost_per_task, calls_per_task, peak_tasks_per_day=None):
    """A month of use at list price, against the keyed free credit and the keyless daily cap.

    Keyless use is free while under the cap, so "binds" says whether a keyless user hits it:
    on an average day, or (if peak_tasks_per_day is given) on the busiest day.
    """
    f = FREE[provider]
    list_cost = tasks_per_month * mean_cost_per_task
    per_day = tasks_per_month / DAYS_PER_MONTH * calls_per_task
    peak = peak_tasks_per_day * calls_per_task if peak_tasks_per_day else None
    cap = f["keyless_per_day"]
    return {
        "provider": provider,
        "list_usd": list_cost,
        "free_monthly_usd": f["monthly"],
        "after_free_usd": max(0.0, list_cost - f["monthly"]),
        "calls_per_day": per_day,
        "peak_calls_per_day": peak,
        "keyless_cap_per_day": cap,
        "keyless_binds_avg": None if cap is None else per_day > cap,
        "keyless_binds_peak": None if cap is None or peak is None else peak > cap,
    }


def provider_calls(lines):
    """Successful billable provider calls (MCP tools only, not built-in search) in a parsed run log."""
    return sum(res is not None and not res.get("is_error") and call["name"].startswith("mcp__")
               for call, res in tool_pairs(lines))


def task_volume(ts):
    """Research tasks per month and on the busiest day, from the task log's timestamps."""
    ts = sorted(datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone() for t in ts)  # local days, for the busiest day
    days = (ts[-1] - ts[0]).total_seconds() / 86400
    return len(ts) / days * DAYS_PER_MONTH, max(Counter(t.date() for t in ts).values())


def call_cost(name, inp):
    """List price of one call; 0 for a tool with no known price (see unpriced) or inputs that don't parse."""
    p = PRICES.get(name)
    if not p:
        return 0.0
    try:
        return _cost(p, inp if isinstance(inp, dict) else {})
    except (TypeError, ValueError):
        return p.get("base", 0.0) if "per_url" not in p else p["per_url"]


def _cost(p, inp):
    if "per_url" in p:
        urls = inp.get("urls") or [inp.get("url")]
        urls = [urls] if isinstance(urls, str) else urls if isinstance(urls, list) else []
        return p["per_url"] * len([u for u in urls if isinstance(u, str) and u])
    extra = max(0, int(inp.get("numResults") or 10) - 10)  # Parallel MCP fast mode returns 10
    return p["base"] + p.get("extra", 0) * extra


def unpriced(lines):
    """Names of the successful calls to MCP tools with no known price (e.g. Parallel's vertical search), one per
    call: they're not in cost_usd, and the report says so instead of counting them as $0."""
    return [call["name"] for call, res in tool_pairs(lines) if res is not None and not res.get("is_error")
            and str(call.get("name", "")).startswith("mcp__") and call.get("name") not in PRICES]


def cost_usd(lines):
    """Sum of list-price $ for the successful provider calls in a parsed run log."""
    return sum((call_cost(call["name"], call.get("input") or {})
                for call, res in tool_pairs(lines) if res is not None and not res.get("is_error")), 0.0)
