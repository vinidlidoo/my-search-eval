"""Offline tests: no network, no model. A fake `claude` (tests/fakebin) stands in for every LLM call.

Run: python3 -I -m unittest discover -s tests
"""

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fixtures import FAKEBIN, SCRIPTS, make_report_fixture, make_transcripts

sys.path.insert(0, str(SCRIPTS))
import build  # noqa: E402
import common  # noqa: E402
import grade  # noqa: E402
import harvest  # noqa: E402
import orchestrate  # noqa: E402
import report  # noqa: E402
import runner  # noqa: E402

STRIPPED = ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_NEWCLOUD",
            "ANTHROPIC_PROFILE"]
RESUME = "/my-search-eval:run --resume"


def quiet(f, *a):
    """Run f, returning what it printed."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        f(*a)
    return buf.getvalue()


class FakeClaude(unittest.TestCase):
    """Puts the fake claude first on PATH, with credentials set in the parent environment and dev mode on."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mse-test-"))
        self.env = dict(os.environ)
        self.log = self.tmp / "claude-calls.jsonl"
        os.environ["PATH"] = f"{FAKEBIN}:{os.environ['PATH']}"
        os.environ["FAKE_CLAUDE_LOG"] = str(self.log)
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.tmp / "config")
        os.environ["MY_SEARCH_EVAL_DEV"] = "1"
        (self.tmp / "counters").mkdir()
        os.environ["FAKE_COUNTER_DIR"] = str(self.tmp / "counters")
        for k in ("ANTHROPIC_MODEL", "FAKE_USAGE_LIMIT", "FAKE_MCP_FAIL", "FAKE_MCP_FAIL_ONE", "FAKE_RATE_LIMIT",
                  "FAKE_FAIL", "FAKE_BIG", "FAKE_AUTH_JSON", "MY_SEARCH_EVAL_HOME"):
            os.environ.pop(k, None)
        for k in STRIPPED:
            os.environ[k] = "set-in-parent"
        self.home = self.tmp / "home"
        self.projects = self.tmp / "projects"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)

    def calls(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []

    def start(self, *extra, sessions=6, per_session=4, **kw):
        if not self.projects.exists():
            make_transcripts(self.projects, sessions=sessions, per_session=per_session, **kw)
        return quiet(orchestrate.main, ["start", "--quick", "--home", str(self.home), "--projects", str(self.projects),
                                        "--no-open", "--foreground", *extra])

    def resume(self):
        return quiet(orchestrate.main, ["start", "--resume", "--home", str(self.home), "--foreground"])

    def run_dir(self):
        return (self.home / "runs" / "latest").resolve()

    def progress(self):
        return json.loads((self.run_dir() / "progress.json").read_text())


class BillingSafety(FakeClaude):
    def test_child_env_strips_credentials(self):
        env = common.child_env()
        for k in STRIPPED:
            self.assertNotIn(k, env)
        self.assertIn("PATH", env)

    def test_children_never_see_credentials(self):
        common.claude("hi", "sys", {"type": "object", "properties": {"pick": {"enum": ["a", "b"]}}})
        runner.run("exa", "a task", "instructions", self.tmp / "run")
        common.preflight_auth()
        calls = self.calls()
        self.assertEqual(len(calls), 3)
        for c in calls:
            self.assertEqual(c["env"], [], c)

    def test_preflight_refuses_api_key_and_cloud(self):
        for auth in [{"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty", "apiKeySource": "X"},
                     {"loggedIn": True, "authMethod": "api_key_helper", "apiProvider": "firstParty",
                      "apiKeySource": "apiKeyHelper"},
                     {"loggedIn": True, "authMethod": "third_party", "apiProvider": "bedrock"},
                     {"loggedIn": False}]:
            os.environ["FAKE_AUTH_JSON"] = json.dumps(auth)
            with self.assertRaises(common.AuthError):
                common.preflight_auth()
        os.environ.pop("FAKE_AUTH_JSON")
        self.assertEqual(common.preflight_auth(), "max")

    def test_preflight_timeout_is_a_message(self):
        real = common.subprocess.run

        def slow(*a, **k):
            raise subprocess.TimeoutExpired("claude", 60)
        common.subprocess.run = slow
        try:
            with self.assertRaises(common.AuthError) as cm:
                common.preflight_auth()
            self.assertIn("didn't answer", str(cm.exception))
        finally:
            common.subprocess.run = real

    def test_work_checks_the_login(self):
        self.start()
        os.environ["FAKE_AUTH_JSON"] = json.dumps({"loggedIn": True, "authMethod": "api_key",
                                                   "apiProvider": "firstParty", "apiKeySource": "X"})
        (self.run_dir() / "progress.json").write_text(json.dumps({"status": "failed"}))
        orchestrate.work(self.run_dir(), self.home)
        self.assertIn("Refusing to run", self.progress()["error"])


class Report(FakeClaude):
    def render(self, root, *extra):
        return report.main([str(root / "tasks.jsonl"), str(root / "grades"), str(root / "runs"), str(root / "report.md"),
                            "--harvest", str(root / "harvest.jsonl"), "--cache-dir", str(self.tmp / "cache"), *extra])

    def test_report_from_fixture(self):
        root = make_report_fixture(self.tmp / "fx", n=5)
        quiet(self.render, root)
        term = (root / "terminal.txt").read_text()
        md = (root / "report.md").read_text()
        page = (root / "report.html").read_text()
        self.assertIn("Verdict", term)
        self.assertIn("Head to head", term)
        self.assertIn("## Methodology", md)
        self.assertIn("### Task 5", md)
        self.assertTrue(page.lstrip().lower().startswith("<!doctype html"))
        self.assertIn("Synthetic subject", page)
        self.assertNotIn("session's model", md + page)
        self.assertTrue((root / "method.svg").exists())
        n = len(self.calls())  # cached: a second render makes no new calls
        quiet(self.render, root)
        self.assertEqual(len(self.calls()), n)

    def test_report_two_tasks(self):
        """Too few tasks for a pattern: the report still renders, and its Why holds at most main errors (one task
        is enough for one)."""
        root = make_report_fixture(self.tmp / "fx2", n=2)
        quiet(self.render, root)
        term = (root / "terminal.txt").read_text()
        self.assertIn("Verdict", term)
        if "\nWhy\n" in term:
            self.assertEqual(set(re.findall(r"\[([^\]]+)\]", term.split("\nWhy\n", 1)[1])), {"Main errors"})
        else:
            self.assertIn("No Why section: too few tasks for patterns", term)

    def test_why_groups_follow_the_providers_columns(self):
        """Biggest leads first, an even column left out, the point in the other provider's favor last, 4 at most."""
        def e(rank, recall, err=""):
            return {"rank": rank, "recall": recall, "main": float(err == "main"), "side": float(err == "side"),
                    "sev": {"": 0, "side": 1, "main": 2}[err]}
        P, R, B = report.PROVIDERS[0], report.PROVIDERS[1], report.PROVIDERS[2]
        T = {}
        for k in range(1, 6):  # P has more recall: lead 5
            T[str(k)] = {"e": {P: e(1, 4), R: e(2, 3), B: e(3, 1)}}
        for k in range(6, 9):  # side errors: 3 each way, even
            T[str(k)] = {"e": {P: e(1, 3), R: e(2, 3, "side"), B: e(3, 1)}}
        for k in range(9, 12):
            T[str(k)] = {"e": {P: e(2, 3, "side"), R: e(1, 3), B: e(3, 1)}}
        T["12"] = {"e": {P: e(1, 3), R: e(2, 3, "main"), B: e(3, 1)}}  # one main error: enough on its own
        for k in range(13, 16):  # R has more recall on 3: the point in its favor
            T[str(k)] = {"e": {P: e(2, 2), R: e(1, 4), B: e(3, 1)}}
        g = report.groups(T, {}, P, R)
        vs_r = [x for x in g.values() if x["pair"] == (P, R)]
        self.assertEqual([(x["tag"], x["favor"]) for x in vs_r],
                         [("Recall", False), ("Main errors", False), ("Recall", True)])
        # after the check: the largest leads that survived, the favor point kept, WHY_CAP at most
        g = {"a": {"lead": 9}, "b": {"lead": 1}, "c": {"lead": 5}, "d": {"lead": 3}, "f": {"lead": 0}}
        why = [{"gid": k, "pair": (P, R), "favor": k == "f"} for k in ("a", "b", "c", "d", "f")]
        self.assertEqual([b["gid"] for b in report.cap(why, g)], ["a", "c", "d", "f"])
        self.assertEqual([b["gid"] for b in report.cap([x for x in why if x["gid"] != "c"], g)], ["a", "b", "d", "f"])

    def test_failed_verdict_and_why_calls_fall_back(self):
        root = make_report_fixture(self.tmp / "fx3", n=5)
        os.environ["FAKE_FAIL"] = "pick"
        stats = self.tmp / "stats.json"
        stats.write_text(json.dumps({"lost": [{"n": "6", "reason": "the judge call failed"}],
                                     "connect": {"exa": [5, 6]}, "stop_note": None}))
        quiet(self.render, root, "--stats", str(stats))
        term = (root / "terminal.txt").read_text()
        self.assertIn("Automatic verdict", term)
        self.assertIn("1 task was left out: the judge call failed (task 6)", term)
        self.assertIn("connected on 5 of 6 tasks", term)
        os.environ["FAKE_FAIL"] = "titles"
        quiet(self.render, root)
        self.assertIn("The Why section is omitted", (root / "terminal.txt").read_text())

    def test_half_written_failure_line_and_capped_grade(self):
        root = make_report_fixture(self.tmp / "fx4", n=5)
        (root / "runs" / "connection-failures.jsonl").write_text('{"task": "1", "provider": "exa"}\n{"task": "2", "eng')
        g = root / "grades" / "3" / "grade-1.json"
        d = json.loads(g.read_text())
        d["capped"] = ["exa"]
        g.write_text(json.dumps(d))
        quiet(self.render, root)
        md = (root / "report.md").read_text()
        self.assertIn("Cut here: task 3 (Exa)", md)
        self.assertIn("The judge saw part of Exa's evidence (it exceeded the judge's budget).",
                      md.split("### Task 3", 1)[1].split("### Task 4", 1)[0])
        self.assertIn("task 1 (Exa)", md)  # the whole line counts as a rerun

    def test_export_schema_and_round_trip(self):
        import uuid

        import export
        root = make_report_fixture(self.tmp / "fx6", n=3)
        (root / "config.json").write_text(json.dumps({"want": 3}))
        inst = root / "instructions"
        inst.mkdir()
        (inst / "exa-changed.txt").write_text("- Search with `mcp__exa__web_search_exa`.\n")
        g = root / "grades" / "3" / "grade-1.json"
        g.unlink()  # task 3 is left out of the report, but its runs are still exported
        quiet(self.render, root)
        head, rows = export.read(root / "results.jsonl.gz")
        self.assertEqual(len(rows), 3 * len(common.PROVIDERS))
        doc = (SCRIPTS.parent / "docs" / "results-schema.md").read_text()
        sections = re.split(r"^#{2,3} ", doc, flags=re.M)

        def fields(title):
            body = next(x for x in sections if x.startswith(title))
            return set(re.findall(r"^\| `(\w+)` \|", body, flags=re.M))
        self.assertEqual(set(head), fields("Run row"))
        for e, p in head["providers"].items():
            self.assertEqual(set(p), fields("providers"))
            for m in p["mcp_servers"]:
                self.assertEqual(set(m), fields("mcp_servers"))
        for r in rows:
            self.assertEqual(set(r), fields("Result row"))
            for c in r["tool_calls"]:
                self.assertEqual(set(c), fields("tool_calls"))
                for x in c["results"]:
                    self.assertEqual(set(x), fields("results"))
            if r["judge"]:
                self.assertEqual(set(r["judge"]), fields("judge"))
        # run row: a uuid kept in config.json, separate from the folder name, and each provider's tool surface
        self.assertEqual(head["run_id"], json.loads((root / "config.json").read_text())["run_id"])
        uuid.UUID(head["run_id"])
        self.assertEqual(head["run_folder"], root.name)
        self.assertEqual(head["providers"]["exa"]["tools_offered"], ["WebSearch", "WebFetch", "mcp__exa__web_search_exa"])
        self.assertEqual(head["providers"]["exa"]["routing_lines"], ["- Search with `mcp__exa__web_search_exa`."])
        self.assertEqual(head["providers"]["exa"]["mcp_servers"][0]["status"], "connected")
        self.assertIsNone(head["providers"]["parallel"]["routing_lines"])
        self.assertEqual(head["providers"]["baseline"]["mcp_servers"], [])
        quiet(self.render, root)  # a re-render keeps the run id
        self.assertEqual(export.read(root / "results.jsonl.gz")[0]["run_id"], head["run_id"])

        by = {(r["task_id"], r["provider"]): r for r in rows}
        r = by[(1, "exa")]
        grade1 = json.loads((root / "grades" / "1" / "grade-1.json").read_text())
        self.assertEqual(r["judge"]["recall"], grade1["answers"]["exa"]["recall"])
        self.assertEqual(r["judge"]["rank"], grade1["rank"]["exa"])
        self.assertNotRegex(r["judge"]["recall_reason"] + r["judge"]["comparison"], r"\bRun [ABC]\b")
        self.assertTrue(r["answer"].startswith("Answer from exa: synthetic."))
        self.assertTrue(r["included_in_report"] and not by[(3, "exa")]["included_in_report"])
        self.assertIsNone(by[(3, "exa")]["judge"])
        c = r["tool_calls"][0]
        self.assertEqual((c["id"], c["tool"], c["duration_ms"], c["truncated"], c["started_at"]),
                         ("t1", "mcp__exa__web_search_exa", 1500, False, "2026-11-20T10:00:01.000Z"))
        self.assertEqual(c["result_chars"], len(c["result"]))
        self.assertIsNone(c["provider_request_id"])  # Exa prints none
        self.assertEqual([(x["rank"], x["url"], x["published"], x["cited_in_answer"]) for x in c["results"]],
                         [(1, c["results"][0]["url"], "2026-01-02", False), (2, "https://example.org/b", None, True)])
        p = by[(1, "parallel")]["tool_calls"][0]
        self.assertEqual(p["provider_request_id"], "search_fake123")
        self.assertEqual([x["title"] for x in p["results"]][1], "Second page")
        b = by[(1, "baseline")]["tool_calls"][0]
        self.assertEqual([x["url"] for x in b["results"]][1], "https://example.org/b")
        self.assertIsNone(b["results"][0]["chars"])
        self.assertEqual(r["model"], by[(1, "baseline")]["model"])
        self.assertEqual(r["plugin_version"], json.loads((SCRIPTS.parent / ".claude-plugin" / "plugin.json").read_text())["version"])
        self.assertEqual(by[(1, "baseline")]["provider_name"], "built-in search (Anthropic)")
        self.assertIsNone(by[(1, "baseline")]["mcp_connected"])
        # the HTML viewer gets the same result list, compact
        data = json.loads(re.search(r'<script type="application/json" id="data">(.*?)</script>',
                                    (root / "report.html").read_text(), re.S)[1])
        exa_view = next(x for x in data["tasks"][0]["providers"] if x["provider"] == "Exa")
        self.assertEqual(exa_view["calls"][0]["results"][1], [2, "https://example.org/b", "Second page", None, True])
        self.assertNotIn("recall", data["tasks"][0])  # recall and error notes live in the provider sections only

    def test_export_and_html_caps(self):
        import export
        from judge_runs import MAX_RESULT
        os.environ["FAKE_BIG"] = str(MAX_RESULT + 500)
        root = make_report_fixture(self.tmp / "fx7", n=2)
        quiet(self.render, root)
        c = export.read(root / "results.jsonl.gz")[1][0]["tool_calls"][0]
        self.assertEqual(len(c["result"]), MAX_RESULT)
        self.assertGreater(c["result_chars"], MAX_RESULT)
        self.assertTrue(c["truncated"])
        data = json.loads(re.search(r'<script type="application/json" id="data">(.*?)</script>',
                                    (root / "report.html").read_text(), re.S)[1])
        call = data["tasks"][0]["providers"][0]["calls"][0]
        self.assertEqual(len(call["preview"]), report.PREVIEW)
        self.assertTrue(call["cut"])

    def check_scrub(self, user):
        """A crafted log: a Read of a saved output under the home folder, a WebFetch whose prompt names local
        paths, and an answer and result that name the user, in paths and in prose."""
        import gzip

        import export
        root = make_report_fixture(self.tmp / f"fx9-{user}", n=2)
        os.environ["HOME"] = f"/Users/{user}"
        home = str(Path.home())
        saved = f"{home}/.claude/projects/-Users-{user}-proj/9f0c/tool-results/abc.txt"
        log = root / "runs" / "1" / "exa.jsonl"
        lines = [json.loads(x) for x in log.read_text().splitlines()]
        extra = [
            {"type": "assistant", "timestamp": "2026-11-20T10:00:03Z", "message": {"content": [
                {"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": saved}},
                {"type": "tool_use", "id": "f1", "name": "WebFetch", "input": {
                    "url": "https://example.com/doc",
                    "prompt": f"Compare {home}/notes/plan.md, /home/{user}/a, ~{user}/b and -Users-{user}-proj"}}]}},
            {"type": "user", "timestamp": "2026-11-20T10:00:04Z", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "r1", "content": f"saved at {saved}"},
                {"type": "tool_result", "tool_use_id": "f1", "content": f"Page text mentioning /Users/{user}/x"}]}}]
        for d in lines:
            if d["type"] == "result":
                d["result"] = f"The {user}-up for {user}, see {home}/notes/plan.md"
        log.write_text("\n".join(json.dumps(d) for d in lines[:-1] + extra + lines[-1:]) + "\n")
        quiet(self.render, root)
        dump = gzip.open(root / "results.jsonl.gz", "rt").read()
        page = (root / "report.html").read_text()
        in_path = rf"(?:[/\\](?:Users|home)[/\\]|~|-(?:Users|home)-){re.escape(user)}(?![\w.])"
        for text in (dump, page, (root / "report.md").read_text()):
            self.assertNotIn(home, text)
            self.assertNotRegex(text, in_path)
        row = next(r for r in export.read(root / "results.jsonl.gz")[1] if r["task_id"] == 1 and r["provider"] == "exa")
        calls = {c["id"]: c for c in row["tool_calls"]}
        self.assertEqual(calls["r1"]["input"]["file_path"], "<run>/tool-results/abc.txt")
        self.assertEqual(calls["f1"]["input"]["prompt"],
                         "Compare ~/notes/plan.md, /home/<user>/a, ~<user>/b and -Users-<user>-proj")
        self.assertEqual(row["answer"], f"The {user}-up for {user}, see ~/notes/plan.md")  # prose is left alone
        self.assertIn("<run>/tool-results/abc.txt", calls["r1"]["result"])
        self.assertIn("mentioning ~/x", calls["f1"]["result"])
        self.assertEqual(calls["r1"]["results"], [])
        return page

    def test_local_paths_and_username_are_scrubbed(self):
        self.check_scrub("zq9tester")

    def test_ordinary_word_username_stays_in_prose(self):
        page = self.check_scrub("runner")  # "runner-up" is in the report's own text
        self.assertIn("runner-up", page)
        self.assertNotIn("<user>-up", page)

    def test_report_md_escapes_html(self):
        root = make_report_fixture(self.tmp / "fx10", n=3)
        evil = '<img src=x onerror=alert(1)>'
        tasks = [json.loads(x) for x in (root / "tasks.jsonl").read_text().splitlines()]
        tasks[0]["task"] += " " + evil
        (root / "tasks.jsonl").write_text("".join(json.dumps(t) + "\n" for t in tasks))
        g = root / "grades" / "1" / "grade-1.json"
        d = json.loads(g.read_text())
        d["rationale"] += " " + evil
        g.write_text(json.dumps(d))
        quiet(self.render, root)
        md = (root / "report.md").read_text()
        self.assertNotIn("<img", md)
        self.assertIn("&lt;img src=x", md)
        self.assertIn("<details><summary>", md)

    def test_html_escapes_answers_and_results(self):
        root = make_report_fixture(self.tmp / "fx8", n=2)
        evil = '</script><script>alert(1)</script><img src=x onerror=alert(2)>'
        log = root / "runs" / "1" / "exa.jsonl"
        lines = [json.loads(x) for x in log.read_text().splitlines()]
        for d in lines:
            if d["type"] == "result":
                d["result"] = evil
            if d["type"] == "user":
                d["message"]["content"][0]["content"] = evil
            if d["type"] == "assistant":
                d["message"]["content"][0]["input"] = {"query": evil}
        log.write_text("\n".join(json.dumps(d) for d in lines) + "\n")
        quiet(self.render, root)
        page = (root / "report.html").read_text()
        self.assertEqual(page.count("<script"), 2)  # the data block and the viewer, nothing injected
        self.assertNotIn("</script><script>alert", page)
        # the viewer escapes the answer, input and result when it builds a provider section
        js = re.search(r"const esc = (.*?);\n", page)[1]
        node = __import__("shutil").which("node")  # the JS checks need node; skipped without it
        if node:
            out = subprocess.run([node, "-e", f"const esc = {js}; process.stdout.write(esc({json.dumps(evil)}))"],
                                 capture_output=True, text=True)
            self.assertNotIn("<", out.stdout)
            self.assertTrue(out.stdout)
        self.assertIn("pvBody(t.providers", page)
        if node:  # the viewer script parses
            viewer = self.tmp / "viewer.js"
            viewer.write_text(re.findall(r"<script>(.*?)</script>", page, re.S)[0])
            check = subprocess.run([node, "--check", str(viewer)], capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stderr)
        for part in ("${esc(p.answer)}", "${esc(c.input)}", "${esc(c.preview)}", "${esc(c.tool)}", "${esc(c.id)}"):
            self.assertIn(part, page)

    def test_sample_coverage_and_unpriced_tools(self):
        root = make_report_fixture(self.tmp / "fx11", n=5)
        (root / "config.json").write_text(json.dumps({"seed": 7, "sent": 9, "dropped": 2, "private": 1, "want": 6,
                                                       "kept": 5}))
        log = root / "runs" / "1" / "parallel.jsonl"
        lines = [json.loads(x) for x in log.read_text().splitlines()]
        extra = [{"type": "assistant", "message": {"content": [
                     {"type": "tool_use", "id": "v1", "name": "mcp__parallel__search_vertical", "input": {}}]}},
                 {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "v1", "content": "[]"}]}}]
        log.write_text("\n".join(json.dumps(d) for d in lines[:-1] + extra + lines[-1:]) + "\n")
        quiet(self.render, root)
        md = (root / "report.md").read_text()
        term = " ".join((root / "terminal.txt").read_text().split())
        self.assertIn("Sample: drawn at random (seed 7), at most 3 per session; the task builder read 9 records and "
                      "dropped 2 (expired premise, or no fair public question), and the privacy check dropped 1", md)
        self.assertIn("This report covers 5 of the 6 tasks planned; the task builder kept 5", term)
        self.assertIn("Parallel: 1 call to unpriced tools not included in its cost: search_vertical (1).", term)

    def test_replay_date_is_local(self):
        import time
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        try:
            lines = [{"type": "system", "subtype": "init", "timestamp": "2026-11-24T03:30:00.000Z"},
                     {"type": "result", "subtype": "success", "result": "a", "duration_ms": 1000}]
            self.assertEqual(report.run_stats(lines, "exa")[2], "2026-11-23")  # evening of Nov 23 in California
        finally:
            os.environ.pop("TZ")
            time.tzset()

    def test_every_why_group_renders_alike(self):
        root = make_report_fixture(self.tmp / "fx12", n=10)
        quiet(self.render, root)
        md = (root / "report.md").read_text().split("## Why", 1)[1].split("\n## ", 1)[0]
        term = (root / "terminal.txt").read_text().split("\nWhy\n", 1)[1].split("\nFull report", 1)[0]
        heads = re.findall(r"(?m)^With (.+) instead, you'd have:$", md)
        self.assertGreaterEqual(len(heads), 2)  # the runner-up and the remaining provider
        self.assertNotRegex(md + term, r"(?m)^ *With .+ instead, you'd have [^:]")  # no group as a bare sentence
        for h in heads:
            self.assertRegex(md, rf"(?m)^With {re.escape(h)} instead, you'd have:\n\n- ")
            self.assertRegex(term, rf"(?m)^  With {re.escape(h)} instead, you'd have:\n  - ")
        data = json.loads(re.search(r'<script type="application/json" id="data">(.*?)</script>',
                                    (root / "report.html").read_text(), re.S)[1])
        self.assertTrue(data["tasks"])
        page = (root / "report.html").read_text()
        html_heads = re.findall(r"<p class='why-head'><span class=\"dot[^\"]*\" aria-hidden=\"true\"></span>With (.+?) instead, you&#x27;d have:</p><ul>", page)
        self.assertEqual(html_heads, heads)  # every group: a heading, then its list

    def test_no_graded_task_raises(self):
        root = make_report_fixture(self.tmp / "fx5", n=2)
        for g in (root / "grades").rglob("grade-1.json"):
            g.unlink()
        with self.assertRaises(RuntimeError):
            self.render(root)


class Pipeline(FakeClaude):
    def test_end_to_end_freeze_and_resume(self):
        cfg = Path(os.environ["CLAUDE_CONFIG_DIR"])
        cfg.mkdir()
        (cfg / "shared.md").write_text("## Web\n\nSearch with Exa `web_search_exa`.\n")
        (cfg / "CLAUDE.md").write_text("# Mine\n\n@shared.md\n")
        out = self.start()
        self.assertIn("Usage: runs on your Claude plan, with no API charges.", out)
        self.assertNotIn("$", out.split("Plan:", 1)[1].split("\n\n", 1)[0])
        run = self.run_dir()
        for f in ["report.md", "report.html", "terminal.txt", "tasks.jsonl", "harvest.jsonl", "tasks.frozen"]:
            self.assertTrue((run / f).exists(), f)
        tasks = orchestrate.load_tasks(run)
        self.assertEqual(len(tasks), 10)
        self.assertFalse(any("DROPME" in t["task"] for t in tasks))
        per = {}
        for t in tasks:
            per[t["session"]] = per.get(t["session"], 0) + 1
        self.assertLessEqual(max(per.values()), 3)
        self.assertIn("Web", (run / "instructions" / "exa.md").read_text())  # the import was inlined
        p = self.progress()
        self.assertEqual(p["status"], "done")
        self.assertEqual(set(p["timings"]), set(orchestrate.PHASES))
        self.assertNotIn("phase_start", p)
        self.assertEqual(orchestrate.took({"build": 120, "replay": 500, "report": 10}),
                         " in 10 min (build 2, replay 8, report <1)")
        self.assertRegex(quiet(orchestrate.main, ["status", "--home", str(self.home)]), r"^Done in \S+ min \(instructions ")
        self.assertTrue((self.home / "cache").is_dir())  # the cache lives in the outputs home
        # replays allow Read, but only on Claude Code's saved tool outputs
        replay = next(c["args"] for c in self.calls() if "stream-json" in c["args"])
        self.assertIn("WebSearch,WebFetch,Read", replay)
        rule = next(a for a in replay if a.startswith("Read("))
        projects = str(Path(os.environ["CLAUDE_CONFIG_DIR"]).resolve() / "projects")
        self.assertRegex(rule, r"^Read\(/" + re.escape(projects) + r"/-[^/]*-run-[^/]+/\*\*\)$")
        self.assertIn("--append-system-prompt", replay)
        # a finished run: resume says so and makes no model calls but the login check
        n = len(self.calls())
        self.assertIn("already done", self.resume())
        self.assertEqual([c["argv"] for c in self.calls()[n:]], [["auth", "status"]])
        # once replay started, the task list is frozen: a resume never rebuilds or renumbers
        before = (run / "tasks.jsonl").read_text()
        (run / "build.jsonl").unlink()
        (run / "progress.json").write_text(json.dumps({"status": "failed"}))
        n = len(self.calls())
        self.resume()
        self.assertEqual((run / "tasks.jsonl").read_text(), before)
        self.assertFalse(any("Record" in json.dumps(c["args"]) for c in self.calls()[n:]))
        self.assertEqual(self.progress()["status"], "done")

    def test_no_instructions(self):
        cfg = Path(os.environ["CLAUDE_CONFIG_DIR"])
        cfg.mkdir()
        (cfg / "CLAUDE.md").write_text("Search with Exa.\n")
        self.start("--no-instructions")
        replay = next(c["args"] for c in self.calls() if "stream-json" in c["args"])
        self.assertNotIn("--append-system-prompt", replay)
        self.assertIn("run with --no-instructions, so your CLAUDE.md was left out", (self.run_dir() / "report.md").read_text())

    def test_task_dir_belongs_to_one_task(self):
        run = self.tmp / "r"
        d = run / "runs" / "1"
        d.mkdir(parents=True)
        (d / "task.json").write_text(json.dumps({"n": 1, "id": "old"}))
        (d / "exa.jsonl").write_text("{}")
        (run / "grades" / "1").mkdir(parents=True)
        (run / "grades" / "1" / "grade-1.json").write_text("{}")
        orchestrate.prepare_task_dir(run, {"n": 1, "id": "new", "task": "t"})
        self.assertFalse((d / "exa.jsonl").exists())
        self.assertFalse((run / "grades" / "1").exists())

    def test_usage_limit_pauses_then_resumes(self):
        os.environ["FAKE_USAGE_LIMIT"] = "stream"
        self.start()
        p = self.progress()
        self.assertEqual(p["status"], "paused")
        self.assertIn(RESUME, p["error"])
        self.assertEqual(list(self.run_dir().glob("grades/*/*.json")), [])  # error text is never judged
        log = next((self.run_dir() / "runs").rglob("exa.jsonl"))
        self.assertFalse(runner.status(log, "exa")["finished"])
        self.assertNotIn("phase_start", p)  # paused time isn't counted
        replay = p["timings"]["replay"]
        os.environ.pop("FAKE_USAGE_LIMIT")
        self.resume()
        p = self.progress()
        self.assertEqual(p["status"], "done")
        self.assertGreaterEqual(p["timings"]["replay"], replay)  # summed across the resume

    def test_usage_limit_in_judge_pauses(self):
        self.start()
        os.environ["FAKE_USAGE_LIMIT"] = "json"
        for g in self.run_dir().glob("grades/*/*.json"):
            g.unlink()
        (self.run_dir() / "progress.json").write_text(json.dumps({"status": "failed"}))
        self.resume()
        self.assertEqual(self.progress()["status"], "paused")
        self.assertIn(RESUME, self.progress()["error"])

    def test_provider_that_never_connects_stops_the_run(self):
        os.environ["FAKE_MCP_FAIL"] = "parallel"
        self.start()
        p = self.progress()
        self.assertEqual(p["status"], "failed")
        self.assertIn("Parallel's MCP server", p["error"])
        self.assertIn(RESUME, p["error"])

    def test_provider_that_fails_on_one_task_loses_that_task(self):
        os.environ["FAKE_MCP_FAIL"] = "parallel"
        os.environ["FAKE_MCP_FAIL_ONE"] = "1"
        self.start()
        self.assertEqual(self.progress()["status"], "done")
        term = (self.run_dir() / "terminal.txt").read_text()
        self.assertIn("Parallel's MCP server didn't connect", term)
        self.assertIn("Parallel's MCP server connected on 9 of 10 tasks", term)

    def test_rate_limit_stops_replay_and_reports_what_finished(self):
        os.environ["FAKE_RATE_LIMIT"] = "exa:5"
        self.start()
        self.assertEqual(self.progress()["status"], "done")
        run = self.run_dir()
        term, md = (run / "terminal.txt").read_text(), (run / "report.md").read_text()
        stats = json.loads((run / "stats.json").read_text())
        k = 10 - len(stats["lost"])
        note = (f"Exa's keyless rate limit was reached after {k} tasks; this report covers those {k}. The run "
                "stopped there instead of waiting for the limit to reset.")
        self.assertIn(note, " ".join(term.split()))  # the terminal wraps lines
        limits = md.split("## Limits", 1)[1]
        self.assertIn(note, limits)
        self.assertIn("Exa hit its rate limit", term)  # the task that hit it is left out
        self.assertEqual(len(report.load(run / "tasks.jsonl", run / "grades", str(run / "runs"))[0]), k)
        # task by task: the first replays are all three providers on task 1, then task 2
        replays = [c["prompt"] for c in self.calls() if "stream-json" in c["args"]]
        self.assertEqual(len(set(replays[:3])), 1)
        self.assertEqual(len(set(replays[3:6])), 1)
        self.assertNotEqual(replays[0], replays[3])

    def test_rate_limit_before_any_task_finishes(self):
        os.environ["FAKE_RATE_LIMIT"] = "exa:0"
        self.start()
        p = self.progress()
        self.assertEqual(p["status"], "failed")
        self.assertIn("keyless rate limit was reached before any task finished", p["error"])
        self.assertIn("or use --quick", p["error"])
        self.assertNotIn("API key", p["error"])

    def test_plan_has_no_dollars_or_cap_warning(self):
        full = orchestrate.plan(30, 100, False, "max", False)
        self.assertNotIn("$", full)
        self.assertNotIn("Warning", full)
        self.assertIn("consider `--quick`", orchestrate.plan(30, 50, False, "pro", False))

    def test_judge_evidence_fits_the_context(self):
        self.assertEqual(grade.shares({"a": 10, "b": 500, "c": 300}, 400), {"a": 10, "c": 195, "b": 195})
        os.environ["FAKE_BIG"] = "90000"  # 3 runs x 90k characters > a 120k budget
        real, grade.CHARS_PER_TOKEN = grade.CHARS_PER_TOKEN, 1
        try:
            self.start()
        finally:
            grade.CHARS_PER_TOKEN = real
        g = json.loads(next((self.run_dir() / "grades").rglob("grade-1.json")).read_text())
        self.assertTrue(g["capped"])
        self.assertIn("Cut here", (self.run_dir() / "report.md").read_text())

    def test_privacy_check_drops_tasks(self):
        self.start(extra=["pricing question from me@example.org", "deploy notes for acme-widgets",
                          "read /Users/someone/notes.md"])
        tasks = " ".join(t["task"] for t in orchestrate.load_tasks(self.run_dir()))
        for bad in ("example.org", "acme-widgets", "/Users/"):
            self.assertNotIn(bad, tasks)
        names = build.private_names([{"cwd": "/tmp/acme-widgets"}, {"cwd": "/home/x/Projects"}])
        self.assertIn("acme-widgets", names)
        self.assertNotIn("Projects", names)
        self.assertIsNone(build.privacy_issue("What changed in the /v1/messages endpoint?", names))

    def test_too_few_tasks_refuses_without_a_run_folder(self):
        make_transcripts(self.projects, sessions=2, per_session=2)
        with self.assertRaises(SystemExit) as cm:
            quiet(orchestrate.main, ["start", "--home", str(self.home), "--projects", str(self.projects), "--tasks",
                                     "12", "--foreground", "--no-open"])
        self.assertIn("cleanupPeriodDays", str(cm.exception))
        self.assertEqual(list((self.home / "runs").iterdir()), [])
        self.assertEqual([p.name for p in self.home.iterdir() if p.suffix == ".jsonl"], [])

    def test_background_worker_status_and_lock(self):
        make_transcripts(self.projects, sessions=3, per_session=4)
        quiet(orchestrate.main, ["start", "--tasks", "2", "--home", str(self.home), "--projects", str(self.projects),
                                 "--no-open"])
        out = ""
        for _ in range(40):
            out = quiet(orchestrate.main, ["status", "--home", str(self.home), "--wait", "30"])
            if out.startswith(("Done", "Failed", "Stopped", "Paused")) or "stopped" in out:
                break
        self.assertTrue(out.startswith("Done"), out)
        self.assertIn("Verdict", out)
        # one worker at a time: while the lock is held, start and resume point at the active run
        held = orchestrate.acquire(self.home)
        try:
            self.assertIn("already in progress",
                          quiet(orchestrate.main, ["start", "--resume", "--home", str(self.home)]))
            orchestrate.work(self.run_dir(), self.home)
            self.assertIn("Another run is already active", self.progress()["error"])
        finally:
            held.close()

    def test_failure_diagnostics_hold_no_private_content(self):
        import grade
        os.environ["HOME"] = "/Users/zq9tester"
        home, user = str(Path.home()), Path.home().name
        secret_query = "zq9 secret research question"
        real = grade.judge

        def boom(runs_dir, *a, **kw):  # fail in judging with private text in the message
            raise ValueError(f"{secret_query} at {home}/notes https://private.example.net/x {user}")
        grade.judge = boom
        try:
            self.start()
        finally:
            grade.judge = real
        run = self.run_dir()
        p = self.progress()
        self.assertEqual((p["status"], p["phase"], p["exc_type"]), ("failed", "judge", "ValueError"))
        diag = (run / "diagnostics.md").read_text()
        # plant more private content in the run folder: a task text, a query, a URL, the home path, the username
        tasks = common.read_jsonl(run / "tasks.jsonl")
        log = next((run / "runs").glob("*/exa.jsonl"))
        with log.open("a") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "z", "name":
                    "WebFetch", "input": {"url": "https://private.example.net/y", "prompt": f"{home} {user}"}}]}}) + "\n")
        out = quiet(orchestrate.main, ["diagnose", "--home", str(self.home)])
        diag2 = (run / "diagnostics.md").read_text()
        logs = [json.loads(x) for x in log.read_text().splitlines() if x.startswith("{")]
        queries = [b["input"].get("query") for d in logs if d.get("type") == "assistant"
                   for b in d["message"]["content"] if b.get("input", {}).get("query")]
        self.assertTrue(tasks and queries)
        planted = [*(t["task"] for t in tasks), *queries, secret_query, "https://private.example.net", "example.com",
                   home, "/tmp/acme-widgets", str(self.tmp)]
        self.assertEqual(out.split("\nWritten to ")[0].rstrip(), diag2.rstrip())  # the command prints the file, then where it is
        for text in (diag, diag2):
            for x in planted:
                self.assertNotIn(x, text)
            self.assertNotRegex(text, rf"(?<![A-Za-z0-9]){re.escape(user)}(?![A-Za-z0-9])")
        self.assertIn("- phase: judge", diag)
        self.assertIn("- exception: ValueError", diag)
        self.assertIn("scripts/orchestrate.py:", diag)  # stack frames: file, line, function
        self.assertIn("in work", diag)
        self.assertRegex(diag, r"\| exa \| \d+ \| \d+ \|")
        self.assertIn(f"- plugin: {common.plugin_version()}", diag)
        self.assertIn("- quick: True", diag)
        status = quiet(orchestrate.main, ["status", "--home", str(self.home)])
        self.assertIn("Diagnostics:", status)

    def test_stopped_worker_and_fake_failure_write_diagnostics(self):
        os.environ["MY_SEARCH_EVAL_FAIL"] = "replay"
        self.start()
        diag = (self.run_dir() / "diagnostics.md").read_text()
        self.assertIn("- reason: test failure (MY_SEARCH_EVAL_FAIL)", diag)
        self.assertIn("- exception: RuntimeError", diag)
        prog = self.run_dir() / "progress.json"
        state = json.loads(prog.read_text())
        state.update(status="running", phase="judge", updated="2020-01-01T00:00:00")  # a worker that died
        prog.write_text(json.dumps(state))
        out = quiet(orchestrate.main, ["status", "--home", str(self.home)])
        self.assertIn("stopped unexpectedly", out)
        self.assertIn("Diagnostics:", out)
        diag = (self.run_dir() / "diagnostics.md").read_text()
        self.assertIn("- status: stopped unexpectedly", diag)
        self.assertIn("- exception: WorkerStopped", diag)
        self.assertIn("- replayed (any provider): 0", diag)  # task folders exist, but no replay ran

    def test_issue_url(self):
        import urllib.parse
        os.environ["MY_SEARCH_EVAL_FAIL"] = "build"
        self.start()
        diag = self.run_dir() / "diagnostics.md"
        diag.write_text(diag.read_text() + "\n" + "\n".join(f"- padding line {i} with words" for i in range(800)))
        out = quiet(orchestrate.main, ["issue-url", "--home", str(self.home)])
        version = common.plugin_version()
        self.assertIn("Repository: vinidlidoo/my-search-eval", out)
        self.assertIn(f"Title: build: RuntimeError (v{version})", out)
        self.assertIn(f"Body file: {diag}", out)
        url = re.search(r"(https://github\.com/\S+)", out)[1]
        self.assertLessEqual(len(url), 8000)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(q["title"], [f"build: RuntimeError (v{version})"])
        self.assertTrue(q["body"][0].startswith("# my-search-eval diagnostics"))
        self.assertIn("please attach the full diagnostics.md", q["body"][0])
        self.assertTrue(url.startswith("https://github.com/vinidlidoo/my-search-eval/issues/new?"))
        outside = self.tmp / "elsewhere"  # issue-url reads only run folders under the outputs home
        outside.mkdir()
        (outside / "config.json").write_text("{}")
        (outside / "diagnostics.md").write_text("x")
        with self.assertRaises(SystemExit):
            quiet(orchestrate.main, ["issue-url", str(outside), "--home", str(self.home)])
        skill = (SCRIPTS.parent / "skills" / "run" / "SKILL.md").read_text()
        allowed = next(ln for ln in skill.splitlines() if ln.startswith("allowed-tools:"))
        self.assertNotIn("gh ", allowed)  # filing is never pre-approved

    def test_purge(self):
        self.start()
        out = quiet(orchestrate.main, ["purge", "--home", str(self.home)])
        self.assertIn("Removed the LLM cache", out)
        self.assertFalse((self.home / "cache").exists())
        self.assertEqual(sorted(p.name for p in self.run_dir().iterdir()),
                         ["config.json", "method.svg", "progress.json", "report.html", "report.md", "results.jsonl.gz",
                          "terminal.txt"])
        self.assertTrue(quiet(orchestrate.main, ["status", "--home", str(self.home)]).startswith("Done"))  # still a run

    def make_runs(self, ages):
        """Run folders named for runs started `age` days ago, with a report, raw logs and a status."""
        from datetime import datetime, timedelta
        out = {}
        for key, age, status in ages:
            d = self.home / "runs" / (datetime.now() - timedelta(days=age)).strftime("%Y%m%d-%H%M%S")
            (d / "runs" / "1").mkdir(parents=True)
            for f in ["config.json", "report.md", "report.html", "terminal.txt", "results.jsonl.gz", "harvest.jsonl"]:
                (d / f).write_text("x")
            (d / "progress.json").write_text(json.dumps({"status": status}))
            out[key] = d
        return out

    def test_prune_retention(self):
        r = self.make_runs([("old", 90, "done"), ("running", 80, "running"), ("paused", 75, "paused"),
                            ("old2", 70, "done"), ("mid", 30, "done"), ("r1", 3, "done"), ("r2", 2, "done"),
                            ("cur", 0, "running")])
        outside = self.tmp / "outside"  # a symlinked run folder: never followed, never deleted
        (outside / "runs").mkdir(parents=True)
        (outside / "config.json").write_text("x")
        os.utime(outside / "config.json", (0, 0))
        (self.home / "runs" / "linked").symlink_to(outside)
        (self.home / "runs" / "latest").symlink_to(r["cur"].name)
        cache = self.home / "cache"
        cache.mkdir()
        for name, age in (("stale.json", 61), ("fresh.json", 59)):
            (cache / name).write_text("{}")
            t = __import__("time").time() - age * 86400
            os.utime(cache / name, (t, t))
        (self.home / "ledger.json").write_text("{}")  # left by older versions

        held = orchestrate.acquire(self.home)  # a worker is active: its "running" run is never touched
        try:
            out = orchestrate.prune(self.home, keep_logs=False, current=r["cur"])
        finally:
            held.close()
        # a paused run that isn't the latest can't be resumed, so it ages out like any other
        self.assertEqual(out["runs"], [r["old"].name, r["paused"].name, r["old2"].name])
        self.assertEqual(out["logs"], [r["mid"].name])
        self.assertEqual(out["cache"], 1)
        for k in ("old", "paused", "old2"):
            self.assertFalse(r[k].exists())
        self.assertTrue((r["running"] / "harvest.jsonl").exists())  # a worker holds the lock: never touched
        # with no worker, "running" is left over from a crash, and that run ages out
        self.assertEqual(orchestrate.prune(self.home, keep_logs=False, current=r["cur"])["runs"], [r["running"].name])
        self.assertEqual(sorted(p.name for p in r["mid"].iterdir()),  # pruned logs keep what dates the run
                         ["config.json", "progress.json", "report.html", "report.md", "results.jsonl.gz", "terminal.txt"])
        for k in ("r1", "r2", "cur"):  # the last KEEP_RUNS runs keep their raw logs
            self.assertTrue((r[k] / "harvest.jsonl").exists())
        self.assertTrue((outside / "runs").exists() and (self.home / "runs" / "linked").is_symlink())
        self.assertEqual(sorted(p.name for p in cache.iterdir()), ["fresh.json"])
        self.assertFalse((self.home / "ledger.json").exists())
        line = orchestrate.prune_line(out)
        self.assertIn(f"older than {common.RETENTION_DAYS} days", line)
        self.assertIn("deleted 1 cache entry", line)

    def test_prune_keeps_the_latest_paused_run(self):
        r = self.make_runs([("superseded", 100, "paused"), ("paused", 90, "paused"), ("newest", 65, "done"),
                            ("cur", 0, "running")])
        (self.home / "runs" / "latest").symlink_to(r["paused"].name)  # the run --resume would continue
        out = orchestrate.prune(self.home, keep_logs=False, current=r["cur"])
        self.assertEqual(out["runs"], [r["superseded"].name])
        self.assertTrue((r["paused"] / "harvest.jsonl").exists())

    def test_prune_deletes_an_old_purged_run(self):
        r = self.make_runs([("old", 90, "done"), ("last", 10, "done"), ("cur", 0, "running")])
        quiet(orchestrate.main, ["purge", "--home", str(self.home)])
        self.assertTrue((r["old"] / "config.json").exists())  # purge keeps what dates the run
        (r["old"] / "config.json").unlink()  # and a folder purged by an older version, with only its reports
        (r["old"] / "progress.json").unlink()
        out = orchestrate.prune(self.home, keep_logs=False, current=r["cur"])
        self.assertEqual(out["runs"], [r["old"].name])  # found by its timestamp name

    def test_prune_always_keeps_the_latest_run(self):
        r = self.make_runs([("older", 200, "done"), ("last", 100, "done"), ("cur", 0, "running")])
        stray = self.home / "runs" / "zz-not-a-date"  # sorts last by name, but started 300 days ago
        stray.mkdir()
        (stray / "config.json").write_text("{}")
        t = __import__("time").time() - 300 * 86400
        os.utime(stray / "config.json", (t, t))
        out = orchestrate.prune(self.home, keep_logs=True, current=r["cur"])
        self.assertEqual(sorted(out["runs"]), sorted([r["older"].name, stray.name]))  # --keep-logs doesn't exempt these
        self.assertTrue((r["last"] / "harvest.jsonl").exists())  # the most recent other run stays, even when old

    def test_prune_orders_runs_by_start_time(self):
        r = self.make_runs([("a", 30, "done"), ("b", 20, "done"), ("c", 10, "done"), ("d", 5, "done"), ("cur", 0, "running")])
        stray = self.home / "runs" / "zz-recent-name"  # a name that sorts last, from a run 25 days ago
        (stray / "runs").mkdir(parents=True)
        (stray / "config.json").write_text("{}")
        t = __import__("time").time() - 25 * 86400
        os.utime(stray / "config.json", (t, t))
        out = orchestrate.prune(self.home, keep_logs=False, current=r["cur"])
        self.assertEqual(sorted(out["logs"]), sorted([r["a"].name, stray.name, r["b"].name]))  # only c, d, cur keep logs
        self.assertTrue((r["c"] / "harvest.jsonl").exists())

    def test_outputs_are_owner_only(self):
        self.home.mkdir(parents=True)
        os.chmod(self.home, 0o755)
        old = self.home / "runs" / "20200101-000000"
        old.mkdir(parents=True)
        (old / "report.html").write_text("x")
        os.chmod(old / "report.html", 0o644)
        mask = os.umask(0o022)
        try:
            self.start()
        finally:
            os.umask(mask)
        loose = [str(p) for p in [self.home, *self.home.rglob("*")]
                 if not p.is_symlink() and p.stat().st_mode & 0o077]
        self.assertEqual(loose, [])
        import export
        cfg = json.loads((self.run_dir() / "config.json").read_text())  # the run id and tool surface, saved by the run
        head = export.read(self.run_dir() / "results.jsonl.gz")[0]
        self.assertEqual(head["run_id"], cfg["run_id"])
        self.assertEqual(head["providers"], cfg["tool_surface"])
        self.assertIn("mcp__exa__web_search_exa", cfg["tool_surface"]["exa"]["tools_offered"])

    def test_cache_hit_refreshes_last_use(self):
        c = common.Cache(self.tmp / "c")
        e = c.entry(["x"], None, lambda: ("out", "m"))
        f = next((self.tmp / "c").iterdir())
        os.utime(f, (0, 0))
        self.assertEqual(c.entry(["x"], None, lambda: self.fail("cache miss")), e)
        self.assertGreater(f.stat().st_mtime, 86400)


class Robustness(FakeClaude):
    def test_harvest_skips_odd_lines_and_limits_briefs(self):
        cfg = Path(os.environ["CLAUDE_CONFIG_DIR"])
        (cfg / "briefs").mkdir(parents=True)
        inside = cfg / "briefs" / "x-brief.md"
        inside.write_text("Inside brief text.")
        outside = self.tmp / "y-brief.md"
        outside.write_text("Outside brief text.")
        d = self.projects / "-tmp-p" / "s1" / "subagents"
        d.mkdir(parents=True)
        lines = ['[1, 2]', '"text"']
        for i, brief in enumerate((inside, outside)):
            (d / f"agent-{i}.jsonl").write_text("\n".join(lines + [
                json.dumps({"type": "user", "sessionId": f"s{i}", "message": {"content": f"Read {brief} and research."}}),
                json.dumps({"type": "assistant", "sessionId": f"s{i}", "message": {"content": [
                    "a bare string block",
                    {"type": "tool_use", "id": f"t{i}", "name": "WebSearch", "input": {"query": "q"}}]}})]) + "\n")
        main = self.projects / "-tmp-p" / "s2.jsonl"  # a subagent turn kept inline in a main transcript
        main.write_text("\n".join(json.dumps(x) for x in [
            {"type": "user", "sessionId": "s2", "message": {"content": "The real question."}},
            {"type": "user", "sessionId": "s2", "isSidechain": True, "message": {"content": "A subagent's prompt."}},
            {"type": "assistant", "sessionId": "s2", "message": {"content": [
                {"type": "tool_use", "id": "t2", "name": "WebSearch", "input": {"query": "q"}}]}}]) + "\n")
        out = self.tmp / "h.jsonl"
        harvest.main(out, self.projects)
        recs = {r["id"]: r for r in common.read_jsonl(out)}
        self.assertEqual(recs["t2"]["user_messages"], ["The real question."])
        self.assertIn("Inside brief text.", recs["t0"]["delegation"])
        self.assertTrue(recs["t1"]["unrecoverable"])
        self.assertNotIn("Outside brief text.", recs["t1"]["delegation"])

    def test_result_parsers_and_request_ids(self):
        import sources
        ans = "See https://www.a.example.com/x/ and https://c.example.com/z#part."
        par = json.dumps({"extract_id": "extract_9z", "results": [
            {"url": "https://a.example.com/x", "title": "A", "publish_date": "2026-03-01", "excerpts": ["abc", "de"]},
            {"url": "https://b.example.com/y", "title": None, "full_content": "12345"}]})
        r = sources.results("mcp__parallel__web_fetch", {}, par, ans)
        self.assertEqual([(x["rank"], x["url"], x["title"], x["published"], x["chars"], x["cited_in_answer"]) for x in r],
                         [(1, "https://a.example.com/x", "A", "2026-03-01", 5, True), (2, "https://b.example.com/y", None, None, 5, False)])
        self.assertEqual(sources.request_id("mcp__parallel__web_fetch", par), "extract_9z")
        exa = ("Title: One\nURL: https://c.example.com/z\nPublished: N/A\nAuthor: N/A\nHighlights:\nbody\n\n"
               "Title: Two\nURL: https://d.example.com\nPublished: 2025-12-31T00:00:00.000Z\nText: t")
        r = sources.results("mcp__exa__web_search_exa", {}, exa, ans)
        self.assertEqual([(x["url"], x["title"], x["published"], x["cited_in_answer"]) for x in r],
                         [("https://c.example.com/z", "One", None, True), ("https://d.example.com", "Two", "2025-12-31T00:00:00.000Z", False)])
        self.assertIsNone(sources.request_id("mcp__exa__web_search_exa", exa))
        self.assertEqual(sources.request_id("mcp__exa__web_search_exa", exa + "\nrequestId: abc123def"), "abc123def")
        web = 'Web search results for query: "q"\n\nLinks: [{"title":"T","url":"https://e.example.com/1"}]\n\nSummary https://f.example.com'
        self.assertEqual([x["url"] for x in sources.results("WebSearch", {"query": "q"}, web, "")], ["https://e.example.com/1"])
        self.assertEqual(sources.request_id("WebSearch", web, {"results": [{"tool_use_id": "srvtoolu_1"}]}), "srvtoolu_1")
        f = sources.results("WebFetch", {"url": "https://a.example.com/x", "prompt": "p"}, "summary", ans)
        self.assertEqual((f[0]["url"], f[0]["chars"], f[0]["cited_in_answer"]), ("https://a.example.com/x", 7, True))
        other = sources.results("mcp__other__search", {}, "see (https://g.example.com/a), https://g.example.com/a and https://h.example.com.", "")
        self.assertEqual([x["url"] for x in other], ["https://g.example.com/a", "https://h.example.com"])
        self.assertEqual(sources.results("Read", {"file_path": "x"}, "https://i.example.com", ""), [])

    def test_viewer_result_list_escapes_and_drops_unsafe_links(self):
        node = __import__("shutil").which("node")
        if not node:
            self.skipTest("needs node")
        import report_html
        js = report_html.JS
        parts = [re.search(p, js, re.S)[0] for p in (r"const esc = .*?;\n", r"const safeUrl = .*?;\n", r"const hit = .*?\n};\n")]
        rows = [[1, "javascript:alert(1)", "<b>x</b>", None, False], [2, "https://a.example.com/?q=\"><img>", None, "2026", True]]
        out = subprocess.run([node, "-e", "".join(parts) + f"process.stdout.write({json.dumps(rows)}.map(hit).join('\\n'))"],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        first, second = out.stdout.split("\n")
        self.assertNotIn("href", first)  # a non-http URL is shown, never linked
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", first)
        self.assertIn('href="https://a.example.com/?q=&quot;&gt;&lt;img&gt;"', second)
        self.assertIn("✓ cited", second)

    def test_rate_limit_counts_only_provider_refusals(self):
        from judge_runs import refusal
        self.assertTrue(refusal("Error: 429 Too Many Requests (rate limit exceeded for the free tier)", True))
        self.assertTrue(refusal('{"error": {"code": 429, "message": "Rate limit exceeded"}}', True))
        page = "Title: API rate limits\nURL: https://example.com/docs/rate-limits\nHighlights: quota exceeded returns 429"
        self.assertFalse(refusal(page, False))  # a short page about rate limits
        self.assertFalse(refusal("Failed to crawl https://www.example.org/r/x: HTTP 429", True))  # the site's own 429
        self.assertFalse(refusal("Error fetching https://example.net/429/rate-limit: timeout", True))  # only a URL
        log = self.tmp / "l.jsonl"
        log.write_text("\n".join(json.dumps(x) for x in [
            {"type": "system", "subtype": "init", "tools": ["mcp__exa__web_search_exa"],
             "mcp_servers": [{"name": "exa", "status": "connected"}]},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "a", "name": "mcp__exa__web_search_exa", "input": {}},
                {"type": "tool_use", "id": "b", "name": "mcp__exa__web_fetch_exa", "input": {}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "a", "content": page},
                {"type": "tool_result", "tool_use_id": "b", "is_error": True,
                 "content": "Failed to crawl https://www.example.org/r/x: HTTP 429"}]}},
            {"type": "result", "subtype": "success", "result": "ok"}]) + "\n")
        self.assertEqual(runner.status(log, "exa")["rate_limited"], 0)
        self.assertTrue(runner.done(log, "exa"))

    def test_parsers_and_costs_survive_odd_inputs(self):
        import cost
        import sources
        self.assertEqual(sources.results("mcp__parallel__web_search", {}, json.dumps({"results": [
            {"url": 123}, {"url": {"x": 1}}, {"url": "https://example.com/a", "excerpts": "not a list"}]}), "")[0]["url"],
            "https://example.com/a")
        self.assertEqual(sources.results("WebSearch", {}, 'Links: [{"title": "t", "url": 5}]', ""), [])
        self.assertEqual(sources.results("WebFetch", {"url": ["https://example.com"]}, "text", ""), [])
        self.assertGreater(cost.call_cost("mcp__parallel__web_search", {"numResults": "abc"}), 0)
        self.assertGreater(cost.call_cost("mcp__exa__web_fetch_exa", {"urls": "https://example.com"}), 0)

    def test_evidence_budget_shrinks_for_non_latin_text(self):
        self.assertEqual(grade.evidence_budget(None, "plain English text " * 100), grade.evidence_budget(None))
        self.assertLess(grade.evidence_budget(None, "漢字かな交じり文" * 100), grade.evidence_budget(None) / 2)

    def test_resume_keeps_the_instructions_it_started_with(self):
        import instructions
        run = self.tmp / "run"
        d = run / "instructions"
        d.mkdir(parents=True)
        for e in common.PROVIDERS:
            (d / f"{e}.md").write_text(f"rewritten for {e}")
        (d / "warnings.json").write_text('["Parallel: instructions still name web_search_exa"]')
        cfg = Path(os.environ["CLAUDE_CONFIG_DIR"])
        cfg.mkdir()
        (cfg / "CLAUDE.md").write_text("")  # emptied since the run started
        self.assertEqual(instructions.write_all(run, None, self.tmp / "cache"),
                         (True, ["Parallel: instructions still name web_search_exa"]))

    def test_diagnostics_labels(self):
        import diagnostics
        self.assertEqual(diagnostics.known("Exa's MCP server didn't connect on any of the first tasks; it may be down."),
                         "an MCP server failed to connect")
        self.assertIsNone(diagnostics.known("authentication of the author failed"))
        self.assertEqual(diagnostics.failure(common.AuthError("anything"))["exc_label"], "login check refused")

    def test_lock_waits_for_a_brief_probe(self):
        import threading
        held = orchestrate.acquire(self.home)
        threading.Timer(0.6, held.close).start()
        lock = orchestrate.acquire_patiently(self.home, seconds=3)
        self.assertIsNotNone(lock)
        lock.close()

    def test_runner_status(self):
        log = self.tmp / "l.jsonl"
        log.write_text("\n".join(json.dumps(x) for x in [
            {"type": "system", "subtype": "init", "tools": ["WebSearch", "mcp__exa__web_search_exa"],
             "mcp_servers": [{"name": "exa", "status": "connected"}]},
            {"type": "result", "subtype": "success", "is_error": True, "result": "API Error: 500"}]) + "\n{half")
        st = runner.status(log, "exa")
        self.assertFalse(st["finished"])
        self.assertFalse(st["usage_limit"])
        log.write_text(json.dumps({"type": "system", "subtype": "init", "tools": ["WebSearch"],
                                   "mcp_servers": [{"name": "exa", "status": "connected"}]}) + "\n"
                       + json.dumps({"type": "result", "subtype": "success", "result": "ok"}) + "\n")
        self.assertFalse(runner.done(log, "exa"))  # connected but never offered

    def test_read_rule_names_only_the_replays_own_folder(self):
        cwd = Path(tempfile.mkdtemp(prefix="run-"))
        try:
            rule = runner.read_rule(cwd)
            self.assertEqual(rule, f"Read(/{common.project_dir(cwd)}/**)")
            self.assertTrue(common.project_dir(cwd).name.endswith(cwd.name.replace("_", "-")))
            self.assertNotIn("/projects/**", rule)
        finally:
            cwd.rmdir()

    def test_runner_cleans_its_project_folder(self):
        cwd = Path(tempfile.mkdtemp(prefix="run-"))
        proj = common.project_dir(cwd)
        (proj / "memory").mkdir(parents=True)
        runner.cleanup(cwd)
        self.assertFalse(proj.exists())
        self.assertFalse(cwd.exists())

    def test_dev_flags_are_hidden(self):
        os.environ.pop("MY_SEARCH_EVAL_DEV")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            orchestrate.main(["start", "--home", str(self.home)])
        os.environ["MY_SEARCH_EVAL_HOME"] = str(self.tmp / "elsewhere")
        self.assertEqual(common.default_home(), Path(os.environ["CLAUDE_CONFIG_DIR"]) / "my-search-eval")

    def test_windows_is_refused(self):
        real = sys.platform
        sys.platform = "win32"
        try:
            with self.assertRaises(SystemExit) as cm:
                orchestrate.main(["status"])
            self.assertIn("not native Windows", str(cm.exception))
        finally:
            sys.platform = real


class Lint(unittest.TestCase):
    def test_ruff(self):
        root = SCRIPTS.parent
        try:
            r = subprocess.run(["ruff", "check", str(root)], capture_output=True, text=True)
        except FileNotFoundError:
            self.skipTest("ruff not installed")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
