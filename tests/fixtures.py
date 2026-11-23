"""Synthetic fixtures: fake transcripts, and fake runs, grades and a harvest for the report. No real data."""

import json
import random
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / "scripts"
FAKEBIN = TESTS / "fakebin"
sys.path.insert(0, str(SCRIPTS))

from common import PROVIDERS  # noqa: E402

SUBJECTS = ["PostgreSQL 18 logical replication slots", "Rust async traits in stable", "SQLite WAL mode on NFS",
            "HTTP/3 support in nginx", "Python 3.14 free-threading status", "WebGPU support in Firefox",
            "Kubernetes sidecar containers GA", "TLS 1.3 0-RTT replay risks"]


def ts(day):
    return (datetime(2026, 9, 1, 12, tzinfo=timezone.utc) + timedelta(days=day)).isoformat().replace("+00:00", "Z")


CWDS = ["/tmp/acme-widgets", "/tmp/zephyr-notes"]  # invented project folders


def make_transcripts(projects, sessions=6, per_session=2, drop_every=5, extra=()):
    """projects/<proj>/<session>.jsonl, each user turn running one web search. Every drop_every-th search
    carries DROPME, which the fake builder drops. extra: more search queries, one per turn, in an extra session."""
    k = 0
    for s in range(sessions + (1 if extra else 0)):
        d = Path(projects) / f"-tmp-proj{s % 2}"
        d.mkdir(parents=True, exist_ok=True)
        sid = f"00000000-0000-0000-0000-{s:012d}"
        lines = []
        queries = list(extra) if s == sessions else [None] * per_session
        for i, eq in enumerate(queries):
            k += 1
            subj = SUBJECTS[k % len(SUBJECTS)]
            q = eq or f"{subj} (case {k})" + (" DROPME" if k % drop_every == 0 else "")
            day = s * 6 + i
            lines += [
                {"type": "user", "sessionId": sid, "timestamp": ts(day), "cwd": CWDS[s % 2],
                 "message": {"role": "user", "content": f"What is the current state of {subj}?"}},
                {"type": "assistant", "sessionId": sid, "timestamp": ts(day),
                 "message": {"content": [{"type": "text", "text": "I'll search."},
                                         {"type": "tool_use", "id": f"tu{k}", "name": "WebSearch",
                                          "input": {"query": q}}]}},
                {"type": "user", "sessionId": sid, "timestamp": ts(day),
                 "message": {"content": [{"type": "tool_result", "tool_use_id": f"tu{k}",
                                          "content": "https://example.com/doc"}]}},
                {"type": "assistant", "sessionId": sid, "timestamp": ts(day),
                 "message": {"content": [{"type": "text", "text": "Here is what I found."}]}},
            ]
        (d / f"{sid}.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")


def make_report_fixture(root, n=5, seed=3):
    """root/{tasks.jsonl, harvest.jsonl, runs/<n>/<provider>.jsonl + -mcp.json, grades/<n>/grade-1.json}. Run logs
    come from the fake claude, so they have the same shape as the live ones."""
    import os

    from common import PROVIDER_INFO

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    env = {**os.environ, "PATH": f"{FAKEBIN}:{os.environ['PATH']}"}
    with (root / "harvest.jsonl").open("w") as h:
        for i in range(40):
            h.write(json.dumps({"id": f"h{i}", "session": f"s{i % 9}", "timestamp": ts(i), "calls": [{"query": "q"}]})
                    + "\n")
    with (root / "tasks.jsonl").open("w") as tf:
        for k in range(1, n + 1):
            task = f"What is the current state of {SUBJECTS[k % len(SUBJECTS)]}?"
            tf.write(json.dumps({"n": k, "id": f"h{k}", "session": f"s{k % 3}", "timestamp": ts(k),
                                 "source": "user", "basis": "context", "task": task}) + "\n")
            d = root / "runs" / str(k)
            d.mkdir(parents=True)
            (d / "task.json").write_text(json.dumps({"n": k, "task": task}))
            for e in PROVIDERS:
                mcp = d / f"{e}-mcp.json"
                mcp.write_text(json.dumps({"mcpServers": PROVIDER_INFO[e]["mcp"]}))
                log = subprocess.run([str(FAKEBIN / "claude"), "-p", "--mcp-config", str(mcp), "--output-format",
                                      "stream-json"], input=task, env=env, capture_output=True, text=True).stdout
                (d / f"{e}.jsonl").write_text(log)
            order = rng.sample(PROVIDERS, len(PROVIDERS))
            labels = dict(zip("ABC", order))
            g = {"labels": labels,
                 "required": {"direct_answer": ["the current status"], "other": ["the version it shipped in"]},
                 "answers": {e: {"recall": 4 - i, "recall_reason": f"Run {lab}'s evidence has the release notes.",
                                 "error": ["none", "limited", "critical"][(i + k) % 3] if i else "none",
                                 "error_reason": f"Run {lab} states an old version." if i else "Nothing departs."}
                             for i, (lab, e) in enumerate(labels.items())},
                 "rank": {e: i + 1 for i, e in enumerate(order)},
                 "ranking_reason": "Run A answers directly from the release notes; Run C hedges.",
                 "rationale": "A's evidence has the official release notes [https://example.com/notes]; "
                              "C's answer quotes an old blog post.",
                 "context_tokens": {e: 1000 for e in PROVIDERS}, "cost": 0.1, "model": "fake-model-1"}
            gd = root / "grades" / str(k)
            gd.mkdir(parents=True)
            (gd / "grade-1.json").write_text(json.dumps(g))
    return root
