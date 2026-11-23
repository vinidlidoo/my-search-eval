"""Shared pieces: the providers (in one place), paths, the child environment and headless claude calls.

Every `claude -p` child runs with credentials and provider switches removed from its environment, so it can
only use the subscription login (see preflight_auth).
"""

import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "prompts"
ASSETS = ROOT / "assets"

# The providers, in one place. Keys: provider id (an MCP provider's tools are mcp__<id>__*).
# - name: display name ("long": the name in prose, where it fits); scrub: a regex for the provider's name in results, removed so the judge stays blind;
#   baseline: Claude Code's own search, no MCP.
# - mcp: the server config, as the provider's docs install it in Claude Code (checked SETUP_CHECKED), keyless.
# - search / fetch: the provider's tool names, for rewriting the user's instructions.
# - prices: $ per call at the provider's public API list price ("extra": $ per result above 10; "per_url": $ per
#   URL fetched). Built-in search is included in Claude Code, so $0.
# - free: free allowances. "monthly": a keyed account's recurring credit in $; "keyless_per_day": the keyless
#   MCP's daily call cap (None: no published number).
PRICES_CHECKED = "2026-11-21"
PROVIDER_INFO = {
    "exa": {
        "name": "Exa",
        "scrub": r"\bExa(?:\.ai)?\b|exa\.ai",
        "mcp": {"exa": {"type": "http", "url": "https://mcp.exa.ai/mcp"}},
        "search": ["mcp__exa__web_search_exa"],
        "fetch": ["mcp__exa__web_fetch_exa"],
        "prices": {
            # web_search_exa: /search type "auto" with highlights; $7/1k requests (<=10 results), $1/1k extra.
            "mcp__exa__web_search_exa": {"base": 0.007, "extra": 0.001,
                                         "source": "https://exa.ai/pricing; https://exa.ai/docs/changelog"},
            # web_fetch_exa: /contents for text, $1/1k pages.
            "mcp__exa__web_fetch_exa": {"per_url": 0.001, "source": "https://exa.ai/pricing (Contents)"},
        },
        "free": {"monthly": 10.0, "keyless_per_day": 150,
                 "source": "https://exa.ai/docs/admin/rate-limits ($10 every month); keyless 150 calls/day from "
                           "https://exa.ai/docs/changelog (2026-02-02)"},
    },
    "parallel": {
        "name": "Parallel",
        "scrub": r"parallel\.ai|\bParallel(?= (?:search|fetch|web|MCP|tool))",
        "mcp": {"parallel": {"type": "http", "url": "https://mcp.parallel.ai/mcp"}},
        "search": ["mcp__parallel__web_search"],
        "fetch": ["mcp__parallel__web_fetch"],
        "prices": {
            # web_search: Search API fast mode, $1/1k requests, 10 results included.
            "mcp__parallel__web_search": {"base": 0.001, "extra": 0.001,
                                          "source": "https://docs.parallel.ai/getting-started/pricing"},
            # web_fetch: Extract API, $1/1k URLs.
            "mcp__parallel__web_fetch": {"per_url": 0.001,
                                         "source": "https://docs.parallel.ai/getting-started/pricing (Extract)"},
        },
        "free": {"monthly": 5.0, "keyless_per_day": None,
                 "source": "https://parallel.ai/pricing ($5 free monthly with a card on file); keyless MCP: "
                           "'variable limits', https://docs.parallel.ai/integrations/mcp/quickstart"},
    },
    "baseline": {
        "name": "built-in search",
        "long": "built-in search (Anthropic)",
        "baseline": True,
        "mcp": {},
        "search": ["WebSearch"],
        "fetch": ["WebFetch"],
        "prices": {"WebSearch": {"base": 0.0, "source": "built-in, $0"},
                   "WebFetch": {"base": 0.0, "source": "built-in, $0"}},
    },
}
PROVIDERS = list(PROVIDER_INFO)
BASELINE = next(e for e, v in PROVIDER_INFO.items() if v.get("baseline"))

# Never bill API credits or a cloud provider: children see none of these.
STRIP_ENV = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL",
             "ANTHROPIC_PROFILE"}
STRIP_PREFIXES = ("CLAUDE_CODE_USE_",)  # CLAUDE_CODE_USE_BEDROCK, _VERTEX, _FOUNDRY and any later one


def child_env(env=None):
    """The environment for a `claude -p` child: the parent's, minus credentials and provider switches."""
    env = os.environ if env is None else env
    return {k: v for k, v in env.items() if k not in STRIP_ENV and not k.startswith(STRIP_PREFIXES)}


def config_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def default_model():
    """The model the person's own sessions use: ANTHROPIC_MODEL, else the user settings' "model", else None
    (the account default). Children skip user settings, so this is passed to them explicitly."""
    if os.environ.get("ANTHROPIC_MODEL"):
        return None  # inherited by every child as is
    try:
        return json.loads((config_dir() / "settings.json").read_text()).get("model") or None
    except (OSError, ValueError):
        return None


class AuthError(RuntimeError):
    pass


class UsageLimit(RuntimeError):
    """The Claude subscription's usage limit (or an API rate limit) stopped a call."""


# Text of a Claude Code error result that means the subscription's usage limit or an API rate limit.
USAGE_LIMIT = re.compile(r"usage limit|hit your limit|limit reached|limit will reset|rate.?limit|\b429\b", re.I)


def is_usage_limit(text):
    return bool(USAGE_LIMIT.search(text or ""))


def preflight_auth():
    """Refuse unless the children would run on a subscription login. Checked with the children's own
    environment, from an empty folder, so an apiKeyHelper in user or managed settings still shows."""
    with tempfile.TemporaryDirectory(prefix="auth-") as cwd:
        try:
            r = subprocess.run(["claude", "auth", "status", "--json"], cwd=cwd, env=child_env(),
                               capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            raise AuthError("`claude` is not on PATH. Install Claude Code's CLI, or add it to the PATH that "
                            "python3 sees, then try again.") from None
        except subprocess.TimeoutExpired:
            raise AuthError("`claude auth status` didn't answer in 60 s. Check that `claude` works in a "
                            "terminal, then try again.") from None
    try:
        s = json.loads(r.stdout)
    except ValueError:
        raise AuthError(f"Could not read `claude auth status --json`: {r.stdout[:200]} {r.stderr[:200]}") from None
    method, provider = s.get("authMethod"), s.get("apiProvider")
    if not s.get("loggedIn"):
        raise AuthError("Not logged in for the background runs. Run `claude auth login` once in a terminal with "
                        "a Claude subscription (Pro, Max, Team or Enterprise). The runs don't use CLAUDE_CODE_OAUTH_TOKEN "
                        "or API keys from the environment.")
    if method != "claude.ai" or provider != "firstParty" or s.get("apiKeySource"):
        how = {"api_key": "an API key", "api_key_helper": "an apiKeyHelper", "oauth_token": "an OAuth token",
               "third_party": f"a cloud provider ({provider})"}.get(method, f"{method} via {provider}")
        where = {"api_key_helper": " It is set as \"apiKeyHelper\" in ~/.claude/settings.json or managed settings.",
                 "third_party": " Unset CLAUDE_CODE_USE_* or remove it from the \"env\" of your settings."}.get(method, "")
        raise AuthError(
            f"Refusing to run: Claude Code would bill {how}, not your subscription.{where} This eval makes many "
            "model calls and runs only on a Claude subscription login: run `claude auth login`, then try again.")
    return s.get("subscriptionType") or "subscription"


def claude(prompt, system, schema, model=None, meta=None, timeout=1800):
    """Headless claude -p call with a JSON schema and no tools. model None uses the default model. Returns
    (structured output, API-equivalent $). If meta is a dict, meta["model"] gets the model that answered.
    Raises UsageLimit when the subscription's limit stopped the call."""
    cmd = ["claude", "-p", *(["--model", model] if model else []), "--system-prompt", system, "--tools", "",
           "--strict-mcp-config", "--setting-sources", "project,local", "--no-session-persistence",
           "--output-format", "json", "--json-schema", json.dumps(schema)]
    with tempfile.TemporaryDirectory(prefix="judge-") as cwd:
        r = subprocess.run(cmd, input=prompt, cwd=cwd, env=child_env(), capture_output=True, text=True,
                           timeout=timeout)
    try:
        out = json.loads(r.stdout)
    except ValueError:
        if is_usage_limit(r.stdout + r.stderr):
            raise UsageLimit((r.stdout + r.stderr).strip()[:300]) from None
        raise RuntimeError(f"claude -p failed: {r.stdout[:300]} {r.stderr[:300]}") from None
    if not isinstance(out, dict):
        raise RuntimeError(f"claude -p failed: {r.stdout[:300]}")
    if out.get("is_error") and is_usage_limit(str(out.get("result"))):
        raise UsageLimit(str(out.get("result"))[:300])
    if out.get("structured_output") is None:
        raise RuntimeError(f"no structured output: {r.stdout[:300]} {r.stderr[:300]}")
    if meta is not None:
        usage = out.get("modelUsage") or {}
        meta["model"] = max(usage, key=lambda m: (usage[m] or {}).get("outputTokens", 0)) if usage else model
    return out["structured_output"], out.get("total_cost_usd") or 0


def obj(required, props):
    return {"type": "object", "additionalProperties": False, "required": required, "properties": props}


def read_jsonl(path):
    """The JSON objects in a JSONL file; lines that don't parse, or aren't objects, are skipped."""
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def write_text(path, text):
    """Write atomically, so a reader or a resume never sees half a file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text)
    tmp.replace(p)


def write_json(path, data):
    write_text(path, json.dumps(data, indent=1))


# ---------- local identity scrub (for everything a report or export shows) ----------

SAVED_OUTPUT = re.compile(r"""(?<![^\s"'`<>(\[])[^\s"'`<>(\[]+/tool-results/([^\s"'`<>/\\)\]]+)""")  # starts at a token edge


def local_scrub(value, home=None, user=None):
    """value (str, or lists and dicts of them) with this machine's identity taken out: Claude Code's saved-output
    paths become <run>/tool-results/<file>, the home folder ~, and the username <user> where it is part of a path
    (/Users/<name>, /home/<name>, ~<name>, an encoded -Users-<name>- folder), never in prose."""
    home = str(home or Path.home())
    user = user if user is not None else Path(home).name
    if isinstance(value, dict):
        return {k: local_scrub(v, home, user) for k, v in value.items()}
    if isinstance(value, list):
        return [local_scrub(v, home, user) for v in value]
    if not isinstance(value, str):
        return value
    s = SAVED_OUTPUT.sub(r"<run>/tool-results/\1", value) if "/tool-results/" in value else value
    if len(home) > 1:
        s = re.sub(re.escape(home) + r"(?![\w.-])", "~", s)
    if user:  # only in path contexts, so a username that is an ordinary word ("runner") stays in prose
        u, end = re.escape(user), r"(?![\w.-])"
        s = re.sub(r"([/\\](?:Users|home)[/\\])" + u + end, r"\1<user>", s)
        s = re.sub(r"~" + u + end, "~<user>", s)
        s = re.sub(r"(-(?:Users|home)-)" + u + r"(?=-|\b)", r"\1<user>", s)  # Claude Code's encoded project folders
    return s


def secure(home):
    """Owner-only access to the outputs folder: 0700 folders, 0600 files, including ones an older version wrote.
    Symlinks are left alone."""
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    for root, dirs, files in os.walk(home, followlinks=False):
        r = Path(root)
        if not r.is_symlink():
            os.chmod(r, 0o700)
        for f in files:
            p = r / f
            if not p.is_symlink():
                os.chmod(p, 0o600)


SETUP_CHECKED = "2026-11-18"  # when each provider's MCP setup was last checked against its docs
RETENTION_DAYS = 60  # run folders and cache entries older than this are deleted (the latest run is always kept)


def plugin_version():
    try:
        return json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text()).get("version")
    except (OSError, ValueError):
        return None


def dev():
    """MY_SEARCH_EVAL_DEV=1 unlocks the test-only options (other folders, a custom task count)."""
    return os.environ.get("MY_SEARCH_EVAL_DEV") == "1"


def default_home():
    """~/.claude/my-search-eval; in dev mode, MY_SEARCH_EVAL_HOME overrides it."""
    if dev() and os.environ.get("MY_SEARCH_EVAL_HOME"):
        return Path(os.environ["MY_SEARCH_EVAL_HOME"]).expanduser()
    return config_dir() / "my-search-eval"


def project_dir(cwd):
    """The folder Claude Code keeps for a working directory under ~/.claude/projects."""
    return config_dir().resolve() / "projects" / re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd))


def key_of(parts, model):
    """Cache key over the full input; with no explicit model, the default model (ANTHROPIC_MODEL) counts."""
    return hashlib.sha256("\0".join(map(str, [*parts, model or os.environ.get("ANTHROPIC_MODEL", "")])).encode()
                          ).hexdigest()


class Cache:
    """LLM writing calls keyed by their full input, one file per entry, so the same input gives the same output
    across re-renders and runs. models counts the models of the entries a caller used."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.models = Counter()

    def entry(self, parts, model, call):
        """The {"out", "model"} entry for this input; call() -> (out, model that answered) on a miss."""
        f = self.path / f"{key_of(parts, model)}.json"
        try:
            entry = json.loads(f.read_text())
            os.utime(f)  # last used: entries unused for RETENTION_DAYS are deleted
            return entry
        except (OSError, ValueError):
            pass
        out, m = call()
        write_json(f, {"out": out, "model": m})
        return {"out": out, "model": m}

    def use(self, entry):
        self.models[entry.get("model") or "your default model"] += 1
        return entry["out"]


def cached(cache, parts, model, call):
    """call() -> (out, model that answered); cached and counted as used."""
    return cache.use(cache.entry(parts, model, call))


def ask(prompt, system, schema, model):
    """claude(), returning (structured output, the model that actually answered)."""
    meta = {}
    out, _ = claude(prompt, system, schema, model, meta)
    return out, meta.get("model") or model
