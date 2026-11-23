"""Judge each task's runs with prompts/grade.md.

Per task RUNS_DIR/<n>/ (one <provider>.jsonl per provider), into GRADE_DIR/<n>/grade-<j>.json. The judge lists what
a complete answer requires from all runs' evidence, then per run: recall (how much of it the run's evidence
holds, 0 to 4) and errors (how far its answer departs from all the evidence), then ranks the answers by
helpfulness. Runs are shown in a shuffled order under labels A, B, C (one judge call per task in the plugin;
judges > 1 is a development option, each further judge seeing the runs rotated under new labels). Stored keyed by provider, with each run's context tokens (its search and fetch
results, about 4 characters per token). A task whose grade file exists is skipped, so a rerun resumes.
"""

import json
import random
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from string import ascii_uppercase

sys.path.insert(0, str(Path(__file__).parent))
from common import PROVIDERS, PROMPTS, UsageLimit, claude, obj, write_json  # noqa: E402
from judge_runs import load_run  # noqa: E402

# The judge reads every run's evidence in one call, so it must fit the judge model's context: 200k tokens unless
# the model is known to have more ("[1m]" models), with 40% left for the prompt, the answers and the output.
# Evidence is counted at 3 characters per token for Latin text (URLs and markup run denser than prose), and at 1
# per token for other scripts (Chinese, Japanese, Korean, Cyrillic, ... often run 1 to 1.5).
CHARS_PER_TOKEN = 3
OTHER_CHARS_PER_TOKEN = 1
HEADROOM = 0.6


def context_tokens(model):
    return 1_000_000 if model and "[1m]" in model else 200_000


def chars_per_token(text):
    """A conservative characters-per-token estimate for text: Latin at CHARS_PER_TOKEN, anything past Latin
    Extended (code point 0x24F) at OTHER_CHARS_PER_TOKEN."""
    if not text:
        return CHARS_PER_TOKEN
    other = sum(ord(c) > 0x24F for c in text)
    tokens = (len(text) - other) / CHARS_PER_TOKEN + other / OTHER_CHARS_PER_TOKEN
    return len(text) / tokens


def evidence_budget(model, text=None):
    """Characters of evidence one judge call can carry, across all runs; text (the evidence) adjusts the
    characters-per-token rate for non-Latin scripts."""
    cpt = CHARS_PER_TOKEN if text is None else chars_per_token(text)
    return int(context_tokens(model) * HEADROOM * cpt)


def shares(sizes, budget):
    """Per-run caps that fit the budget: an even split, with what small runs don't use passed to the others."""
    caps, left, todo = {}, budget, sorted(sizes, key=sizes.get)
    while todo:
        even = left // len(todo)
        e = todo.pop(0)
        caps[e] = min(sizes[e], even)
        left -= caps[e]
    return caps

STR = {"type": "string"}
COST = {"enum": ["none", "limited", "critical"]}
GRADE_SCHEMA = obj(["required", "answers", "ranking_reason", "ranks", "rationale"], {
    "required": obj(["direct_answer", "other"], {"direct_answer": {"type": "array", "minItems": 1, "items": STR},
                                                 "other": {"type": "array", "items": STR}}),
    "answers": {"type": "array", "items": obj(["label", "recall_reason", "recall", "error_reason", "error"], {
        "label": STR, "recall_reason": STR, "recall": {"type": "integer", "enum": [0, 1, 2, 3, 4]},
        "error_reason": STR, "error": COST})},
    "ranking_reason": STR,
    "ranks": {"type": "array", "items": obj(["label", "rank"], {
        "label": STR, "rank": {"type": "integer", "enum": list(range(1, len(PROVIDERS) + 1))}})},
    "rationale": STR,
})


def head(task, date):
    return f"# Task\n\n{task}\n\nRuns happened on {date}.\n"


def grade_one(n, task, runs, order, j, out_dir, date, model, task_id=None, capped=()):
    """One judge call for one task; writes GRADE_DIR/<n>/grade-<j>.json."""
    out = Path(out_dir, n, f"grade-{j}.json")
    if out.exists():
        return
    start = ascii_uppercase.index("AXP"[(j - 1) % 3])  # judge 1 sees A, B, C...; judge 2 X, Y, Z...
    labels = {ascii_uppercase[(start + i) % 26]: e for i, e in enumerate(order)}
    parts = [head(task, date), "# Evidence by run\n"]
    parts += [f"## Run {lab} evidence\n\n{runs[e][1] or '(no searches or fetches)'}\n" for lab, e in labels.items()]
    parts += ["# Answers\n"] + [f"## Run {lab} answer\n\n{runs[e][0]}\n" for lab, e in labels.items()]
    meta = {}
    res, cost = claude("\n".join(parts), (PROMPTS / "grade.md").read_text(), GRADE_SCHEMA, model, meta)

    def provider(label):
        return labels[label.split()[-1].strip(":.")]

    answers = {provider(a["label"]): {k: v for k, v in a.items() if k != "label"} for a in res["answers"]}
    assert set(answers) == set(PROVIDERS), f"labels {list(answers)}"
    rank = {provider(a["label"]): a["rank"] for a in res["ranks"]}
    assert set(rank) == set(PROVIDERS) and sorted(rank.values()) == list(range(1, len(PROVIDERS) + 1)), f"ranks {rank}"
    tokens = {e: len(runs[e][1]) // 4 for e in PROVIDERS}
    write_json(out, {"labels": labels, "required": res["required"], "answers": answers, "rank": rank,
                     "ranking_reason": res["ranking_reason"], "rationale": res["rationale"],
                     "context_tokens": tokens, "cost": cost, "model": meta.get("model"), "task_id": task_id,
                     "capped": sorted(capped)})


def judge(runs_dir, out_dir, date, ns, workers=3, judges=1, model=None, progress=None, attempts=2):
    """Grade tasks ns (all of whose runs are done). Returns the tasks that still have no grade."""
    rng = random.Random(2029)
    jobs = []
    for n in ns:
        d = Path(runs_dir, n)
        order = rng.sample(PROVIDERS, len(PROVIDERS))  # drawn for every task, so labels don't depend on resume
        if all(Path(out_dir, n, f"grade-{j}.json").exists() for j in range(1, judges + 1)):
            continue
        full = {e: load_run(d / f"{e}.jsonl") for e in PROVIDERS}
        caps = shares({e: len(full[e][1]) for e in PROVIDERS},
                      evidence_budget(model, "".join(full[e][1] for e in PROVIDERS)))
        runs = {e: load_run(d / f"{e}.jsonl", caps[e]) if caps[e] < len(full[e][1]) else full[e] for e in PROVIDERS}
        capped = {e for e in PROVIDERS if runs[e][2]}
        t = json.loads((d / "task.json").read_text())
        jobs += [(n, t["task"], {e: r[:2] for e, r in runs.items()}, order[j - 1:] + order[:j - 1], j, t.get("id"),
                  capped) for j in range(1, judges + 1)]
    lock, done = threading.Lock(), [0]

    def go(job):
        n, task, runs, order, j, task_id, capped = job
        for attempt in range(attempts):
            try:
                grade_one(n, task, runs, order, j, out_dir, date, model, task_id, capped)
                break
            except UsageLimit:
                raise
            except Exception as exc:
                print(f"judge failed on task {n} (attempt {attempt + 1}): {exc}", file=sys.stderr, flush=True)
        with lock:
            done[0] += 1
            if progress:
                progress(done[0], len(jobs))

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(go, jobs))
    return [n for n in ns if not all(Path(out_dir, n, f"grade-{j}.json").exists() for j in range(1, judges + 1))]
