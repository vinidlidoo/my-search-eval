"""Sample harvested records and build self-contained tasks from them (prompts/task-builder.md).

Sampling: the pool is every recoverable record, shuffled with a fixed seed, at most PER_SESSION per session.
Building: records go to the builder in batches, a few batches at a time, oversampling because the builder drops
some (expired premise, no public question). It stops once `want` tasks are kept or the pool runs out.
Each batch's result is appended to build.jsonl, so a rerun only builds what's missing.
A code check then drops any built task that still holds an email address, a local path, the local username or
the name of one of the person's project folders (privacy_issue).
"""

import getpass
import json
import random
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from common import PROMPTS, UsageLimit, claude, obj, read_jsonl, write_text  # noqa: E402

PER_SESSION = 3
BATCH = 6
WORKERS = 3
OVERSAMPLE = 1.5

STR = {"type": "string"}
BUILD_SCHEMA = obj(["items"], {"items": {"type": "array", "items": obj(["record", "task", "drop", "basis"], {
    "record": {"type": "integer"}, "task": STR, "drop": STR, "basis": {"enum": ["context", "query"]}})}})


EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
LOCAL_PATH = re.compile(r"~/|(?<![\w.:/-])/(?:Users|home|root|private|tmp|var/folders|mnt|Volumes)/|\b[A-Za-z]:\\")


# Folder names too common to identify anyone.
GENERIC_DIRS = {"projects", "project", "documents", "desktop", "downloads", "developer", "github", "repos", "code",
                "work", "workspace", "source", "src", "tmp", "temp", "scratch", "sandbox"}


def private_names(records):
    """Words that identify this person locally: their username and their project folders' names."""
    names = {getpass.getuser(), Path.home().name}
    home = str(Path.home())
    names |= {Path(r["cwd"]).name for r in records if r.get("cwd") and r["cwd"] != home}
    names = {n for n in names if n.lower() not in GENERIC_DIRS}
    return {n for n in names if n and len(n) >= 3 and (len(n) >= 5 or n in (getpass.getuser(), Path.home().name))}


def privacy_issue(text, names):
    """Why a built task can't go to the search providers, or None."""
    if EMAIL.search(text):
        return "an email address"
    if LOCAL_PATH.search(text):
        return "a local path"
    for n in names:
        if re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", text, re.I):
            return "a local username or project name"
    return None


def sample(records, seed):
    """Recoverable records in a seeded random order, at most PER_SESSION per session."""
    pool = [r for r in records if not r.get("unrecoverable")]
    random.Random(seed).shuffle(pool)
    per, out = {}, []
    for r in pool:
        s = r.get("session")
        if per.get(s, 0) < PER_SESSION:
            per[s] = per.get(s, 0) + 1
            out.append(r)
    return out


def render(i, r):
    """One record as the builder reads it: date, origin, context before the searches, the searches."""
    out = [f"# Record {i}", f"Date: {(r.get('timestamp') or 'unknown')[:10]}", f"Origin: {r.get('source')}"]
    if r.get("delegation"):
        out += ["Delegation:", r["delegation"]]
    for m in r.get("user_messages") or []:
        out += ["User message:", m]
    if r.get("agent_text_before"):
        out += ["Agent text before the searches:", r["agent_text_before"]]
    out += ["Searches:", *(f"- {c.get('query')}" for c in r.get("calls") or [])]
    return "\n".join(out)


def build_batch(batch, model):
    """{record id: {"task", "drop", "basis"}} for one batch."""
    system = (PROMPTS / "task-builder.md").read_text()
    prompt = ("Write a task or a drop for each record below, judging each one on its own. Return one item per "
              "record, by its number; leave `task` empty for a drop and `drop` empty for a task.\n\n"
              + "\n\n".join(render(i, r) for i, r in enumerate(batch, 1)))
    out, _ = claude(prompt, system, BUILD_SCHEMA, model)
    res = {}
    for it in out["items"]:
        if 1 <= it["record"] <= len(batch):
            res[batch[it["record"] - 1]["id"]] = {k: it[k].strip() if isinstance(it[k], str) else it[k]
                                                  for k in ("task", "drop", "basis")}
    return res


def kept(order, decided, names=frozenset()):
    return [r for r in order if (d := decided.get(r["id"])) and d["task"] and not d["drop"]
            and not privacy_issue(d["task"], names)]


def build(harvest_path, run_dir, want, seed, model=None, progress=None):
    """Write run_dir/tasks.jsonl with up to `want` tasks; return (tasks, pool size, records sent, tasks dropped by
    the privacy check, records dropped by the builder)."""
    run_dir = Path(run_dir)
    log = run_dir / "build.jsonl"
    records = read_jsonl(harvest_path)
    names = private_names(records)
    order = sample(records, seed)
    decided = {}
    for row in read_jsonl(log):
        decided.update(row["results"])
    failed = set()
    while len(kept(order, decided, names)) < want:
        todo = [r for r in order if r["id"] not in decided and r["id"] not in failed]
        if not todo:
            break
        need = want - len(kept(order, decided, names))
        take = todo[:max(BATCH, int(need * OVERSAMPLE + 0.999))]
        batches = [take[i:i + BATCH] for i in range(0, len(take), BATCH)]

        def go(batch):
            try:
                return batch, build_batch(batch, model)
            except UsageLimit:
                raise
            except Exception as exc:  # one retry on the next round, then the records are skipped
                print(f"builder batch failed: {exc}", file=sys.stderr, flush=True)
                return batch, None

        with ThreadPoolExecutor(WORKERS) as ex:
            for batch, res in ex.map(go, batches):
                if res is None:
                    failed |= {r["id"] for r in batch if r.get("_tried")}
                    for r in batch:
                        r["_tried"] = True
                    continue
                for r in batch:  # a record the builder skipped counts as dropped
                    res.setdefault(r["id"], {"task": "", "drop": "not returned by the builder", "basis": "context"})
                decided.update(res)
                with log.open("a") as f:
                    f.write(json.dumps({"results": res}) + "\n")
        if progress:
            progress(min(len(kept(order, decided, names)), want), want)
    private = sum(1 for r in order if (d := decided.get(r["id"])) and d["task"] and not d["drop"]
                  and privacy_issue(d["task"], names))
    if private:
        print(f"privacy check: dropped {private} built task(s) holding an email, a local path, the username or a "
              "project name", file=sys.stderr, flush=True)
    tasks = []
    for n, r in enumerate(kept(order, decided, names)[:want], 1):
        d = decided[r["id"]]
        tasks.append({"n": n, "id": r["id"], "session": r.get("session"), "timestamp": r.get("timestamp"),
                      "source": r.get("source"), "basis": d["basis"], "task": d["task"]})
    write_text(run_dir / "tasks.jsonl", "".join(json.dumps(t) + "\n" for t in tasks))
    dropped = sum(1 for r in order if (d := decided.get(r["id"])) and (not d["task"] or d["drop"]))
    return tasks, len(order), sum(1 for r in order if r["id"] in decided), private, dropped
