"""my-search-eval: which search provider makes Claude more useful for you, judged on your own past research.

Usage:
  python3 -I orchestrate.py start [--quick] [--model M] [--keep-logs] [--no-instructions] [--resume]
  python3 -I orchestrate.py status [--wait SECONDS]
  python3 -I orchestrate.py purge
  python3 -I orchestrate.py diagnose
  python3 -I orchestrate.py issue-url [RUN]

start: checks the login, harvests your transcripts, prints the plan, then runs the eval in a detached
background worker. Phases: instructions, build, replay, judge, report. Every run, grade and builder batch is a
file, so `start --resume` continues the latest run where it stopped.
status: one progress line; with --wait, blocks until the phase changes or the run ends (at most SECONDS).
Once done, it prints the terminal summary.
purge: deletes the cache and every run's raw logs; reports and each run's config stay.
diagnose: writes diagnostics.md for the latest run (fixed fields only, nothing private) and prints it.
issue-url: prints the issue title, the diagnostics.md path and a pre-filled GitHub issue link (read-only).
`start --resume` reuses the run's saved options; other flags are ignored.

Outputs: ~/.claude/my-search-eval/runs/<timestamp>/, with runs/latest pointing at the newest, and the LLM cache
in ~/.claude/my-search-eval/cache/. Retention, at each start: run folders older than RETENTION_DAYS are deleted
(the latest run is always kept), raw logs of all but the last KEEP_RUNS runs unless --keep-logs, and cache
entries unused for RETENTION_DAYS. One worker runs at a time (a lock on ~/.claude/my-search-eval/worker.lock).
Test-only options (--home, --projects, --cache-dir, --tasks, --no-open, --foreground) need MY_SEARCH_EVAL_DEV=1.
"""

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import build  # noqa: E402
import diagnostics  # noqa: E402
import export  # noqa: E402
import grade  # noqa: E402
import harvest  # noqa: E402
import instructions  # noqa: E402
import report  # noqa: E402
import runner  # noqa: E402
from common import (PROVIDER_INFO, PROVIDERS, RETENTION_DAYS, AuthError, secure, UsageLimit, default_home, default_model, dev,  # noqa: E402
                    preflight_auth, read_jsonl, write_json, write_text)

FULL, QUICK, MIN_TASKS = 30, 10, 10
WORKERS = 3
KEEP_RUNS = 3
KEEP_FILES = {"report.html", "report.md", "terminal.txt", "method.svg", "results.jsonl.gz", "diagnostics.md",
              "config.json", "progress.json"}  # what log pruning and --purge keep: the reports, and what dates the run
RATE_STOP = 2  # refusals from one provider's rate limit, across the run, that stop replaying
PHASES = ["instructions", "build", "replay", "judge", "report"]
# Wall time on Opus, fit to two runs (10 tasks: 14 min; 30 tasks: 25 min): fixed minutes + minutes per task.
MIN_FIXED, MIN_PER_TASK = 8.0, 0.57
RUN = "/my-search-eval:run"
RESUME = "/my-search-eval:run --resume"
TRANSCRIPTS_HINT = ("Claude Code deletes transcripts after 30 days by default; raise \"cleanupPeriodDays\" in "
                    "~/.claude/settings.json to keep more.")


class Paused(Exception):
    """The run stops and can be resumed later (the Claude usage limit)."""


class Failed(Exception):
    """The run can't go on; the message says what to do next."""


def now():
    return datetime.now().isoformat(timespec="seconds")


# ---------- the one-worker lock ----------

def lock_path(home):
    return Path(home) / "worker.lock"


def acquire(home):
    """Take the HOME-level worker lock; return its open file (keep it open), or None if another worker has it."""
    import fcntl
    Path(home).mkdir(parents=True, exist_ok=True)
    f = lock_path(home).open("a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def acquire_patiently(home, seconds=5):
    """acquire(), retried for a few seconds: a `status` probe (worker_active) may hold the lock for a moment."""
    deadline = time.time() + seconds
    while True:
        lock = acquire(home)
        if lock is not None or time.time() >= deadline:
            return lock
        time.sleep(0.25)


def worker_active(home):
    f = acquire(home)
    if f is None:
        return True
    f.close()  # closing releases it
    return False


# ---------- progress ----------

class Progress:
    def __init__(self, run_dir):
        self.path = Path(run_dir) / "progress.json"
        try:
            self.state = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.state = {}
        self.lock = threading.Lock()

    def set(self, **kw):
        with self.lock:
            s = self.state
            ending = kw.get("status") in ("done", "paused", "failed")
            if (ending or "phase" in kw) and s.get("phase_start"):  # bank the phase's working time; pauses don't count
                t = s.setdefault("timings", {})
                t[s["phase"]] = round(t.get(s["phase"], 0) + time.time() - s.pop("phase_start"))
            if kw.get("phase") in PHASES and not ending:
                kw["phase_start"] = time.time()
            s.update({**kw, "updated": now()})
            write_json(self.path, self.state)

    def phase(self, name, total=0, message=""):
        self.set(status="running", phase=name, step=PHASES.index(name) + 1, steps=len(PHASES), done=0,
                 total=total, message=message, error=None, exc_type=None, exc_label=None, trace=None)
        if dev() and os.environ.get("MY_SEARCH_EVAL_FAIL") == name:  # tests: fail on entering this phase
            raise RuntimeError(f"fake failure in {name}")

    def count(self, done, total):
        self.set(done=done, total=total)


def took(timings):
    """' in 14 min (build 2, replay 8, ...)': working time per phase, summed across resumes."""
    if not timings:
        return ""
    m = lambda sec: f"{sec / 60:.0f}" if sec >= 30 else "<1"  # noqa: E731
    parts = ", ".join(f"{k} {m(timings[k])}" for k in PHASES if k in timings)
    return f" in {m(sum(timings.values()))} min ({parts})"


def line(p):
    """One progress line from progress.json's state."""
    if not p:
        return "No progress yet."
    if p.get("status") == "done":
        return f"Done{took(p.get('timings'))}. Report: {p.get('report')}"
    if p.get("status") in ("failed", "paused"):
        return p.get("error") or "Stopped."
    n = f" {p['done']}/{p['total']}" if p.get("total") else ""
    msg = f" ({p['message']})" if p.get("message") else ""
    return f"[{p.get('step')}/{p.get('steps')}] {p.get('phase')}{n}{msg}"



# ---------- plan ----------

def plan(want, pool, has_instr, sub, quick):
    n = min(want, pool)
    batches = -(-int(n * build.OVERSAMPLE + 0.999) // build.BATCH)
    runs, judges = n * len(PROVIDERS), n
    calls = batches + (len(PROVIDERS) if has_instr else 0) + runs + judges + 4
    minutes = MIN_FIXED + n * MIN_PER_TASK
    names = ", ".join(v["name"] + (" (baseline)" if v.get("baseline") else " (keyless MCP)")
                      for v in PROVIDER_INFO.values())
    when = f"{minutes / 60:.1f} hours" if minutes >= 90 else f"{minutes:.0f} minutes"
    fewer = f" (all your transcripts hold; you asked for {want})" if n < want else ""
    out = [f"Plan: up to {n} tasks{fewer} x {len(PROVIDERS)} providers: {names}. The task builder may drop a few.",
           f"About {calls} model runs: {runs} replays, {judges} judge calls, ~{batches} task-builder calls, "
           f"{str(len(PROVIDERS)) + ' instruction rewrites, ' if has_instr else ''}~4 report calls.",
           "Usage: runs on your Claude plan, with no API charges.",
           f"Estimated wall time: about {when}, {WORKERS} runs at a time."]
    if sub == "pro" and not quick:
        out.append("On Pro, a full run can use a large share of your weekly limit: consider `--quick` first.")
    return "\n".join(out)


# ---------- phases ----------

def prepare_task_dir(run_dir, t):
    """runs/<n>/ and grades/<n>/ belong to one task id; results left from another task are removed."""
    d = Path(run_dir) / "runs" / str(t["n"])
    tf = d / "task.json"
    if tf.exists():
        try:
            old = json.loads(tf.read_text()).get("id")
        except ValueError:
            old = None
        if old != t["id"]:
            shutil.rmtree(d, ignore_errors=True)
            shutil.rmtree(Path(run_dir) / "grades" / str(t["n"]), ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    write_json(tf, t)
    return d


def replay(run_dir, home, tasks, model, instr, prog):
    """Run every (task, provider) not yet done, task by task (all providers of a task before the next), WORKERS at a
    time, so finished tasks pile up evenly. RATE_STOP refusals from a provider's rate limit stop replaying new tasks. Returns (tasks done on every provider, lost tasks with reasons,
    MCP connections per provider, a note if replaying stopped early). Raises Paused on the Claude usage limit."""
    runs = Path(run_dir) / "runs"
    failures = runs / "connection-failures.jsonl"
    jobs, dirs = [], {}
    for t in tasks:
        d = dirs[t["n"]] = prepare_task_dir(run_dir, t)
        jobs += [(e, t, d) for e in PROVIDERS if not runner.done(d / f"{e}.jsonl", e)]
    total = len(tasks) * len(PROVIDERS)
    lock = threading.RLock()  # halt() takes it too
    state = {"finished": total - len(jobs), "stop": None, "fails": {}, "oks": {}, "refused": {}}
    stop = threading.Event()
    prog.count(state["finished"], total)

    def halt(reason):
        with lock:
            if state["stop"] is None:
                state["stop"] = reason
        stop.set()

    def go(job, attempts=2):
        """A run whose provider didn't connect, or that didn't finish, is logged and rerun once."""
        e, t, d = job
        if stop.is_set():
            return
        log = d / f"{e}.jsonl"
        st = None
        for attempt in range(1, attempts + 1):
            try:
                runner.run(e, t["task"], instr.get(e), d, model)
            except Exception as exc:
                print(f"run failed: task {t['n']} {e}: {exc}", file=sys.stderr, flush=True)
            st = runner.status(log, e)
            if st["usage_limit"]:
                return halt(("usage", st["error"]))
            if st["rate_limited"]:  # one refusal reruns the task; RATE_STOP across the run stops replaying
                with lock:
                    n = state["refused"][e] = state["refused"].get(e, 0) + st["rate_limited"]
                if n >= RATE_STOP:
                    return halt(("rate", e, t["n"]))
            if runner.done(log, e):
                break
            with failures.open("a") as f:
                f.write(json.dumps({"task": str(t["n"]), "provider": e, "attempt": attempt, "finished": st["finished"],
                                    "servers": st["servers"], "offered": st["offered"], "error": st["error"]}) + "\n")
        with lock:
            if PROVIDER_INFO[e]["mcp"]:
                key = "oks" if runner.connected(st) else "fails"
                state[key][e] = state[key].get(e, 0) + 1
                if state["fails"].get(e, 0) >= 3 and not state["oks"].get(e):
                    halt(("down", e))
            state["finished"] += 1
            prog.count(state["finished"], total)

    with ThreadPoolExecutor(WORKERS) as ex:
        list(ex.map(go, jobs))

    reason = state["stop"]
    if reason and reason[0] == "usage":
        raise Paused(f"Paused: Claude usage limit reached. Resume after it resets: {RESUME}")
    if reason and reason[0] == "down":
        name = PROVIDER_INFO[reason[1]]["name"]
        raise Failed(f"{name}'s MCP server didn't connect on any of the first tasks; it may be down. Try again "
                     f"later: {RESUME}")

    ok, lost, connect = [], [], {}
    for t in tasks:
        d, why = dirs[t["n"]], None
        for e in PROVIDERS:
            log = d / f"{e}.jsonl"
            st = runner.status(log, e)
            if PROVIDER_INFO[e]["mcp"] and log.exists():
                c = connect.setdefault(e, [0, 0])
                c[1] += 1
                c[0] += runner.connected(st)
            if why is None and not runner.done(log, e):
                name = PROVIDER_INFO[e]["name"]
                why = ("not replayed (replaying stopped early)" if not log.exists() else
                       f"{name} hit its rate limit" if st["rate_limited"] else
                       f"{name}'s MCP server didn't connect" if not runner.connected(st) else
                       f"the {name} run didn't finish")
        if why:
            lost.append({"n": str(t["n"]), "reason": why})
        else:
            ok.append(str(t["n"]))
    for e, (good, tried) in connect.items():
        if tried and tried - good > tried / 2:
            raise Failed(f"{PROVIDER_INFO[e]['name']}'s MCP server failed to connect on {tried - good} of {tried} tasks; "
                         f"it may be down. Try again later: {RESUME}")
    note = None
    if reason and reason[0] == "rate":
        name = PROVIDER_INFO[reason[1]]["name"]
        k = len(ok)
        note = (f"{name}'s keyless rate limit was reached after {k} task{'' if k == 1 else 's'}; this report covers "
                f"{'that one' if k == 1 else 'those ' + str(k)}. The run stopped there instead of waiting for the "
                f"limit to reset.")
        if not ok:
            raise Failed(f"{name}'s keyless rate limit was reached before any task finished on every provider, so there "
                         f"is nothing to report. Start a new run tomorrow ({RUN}), or use --quick.")
    return ok, lost, connect, note


RUN_NAME = re.compile(r"^\d{8}-\d{6}$")


def run_time(d):
    """When run folder d started: its name (YYYYmmdd-HHMMSS), else its config.json's mtime, else None."""
    try:
        return datetime.strptime(d.name, "%Y%m%d-%H%M%S")
    except ValueError:
        pass
    try:
        return datetime.fromtimestamp((d / "config.json").lstat().st_mtime)
    except OSError:
        return None


def remove(p):
    """Delete p without following symlinks: a link is unlinked, never its target."""
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    else:
        p.unlink()


def prune(home, keep_logs, current, cache_dir=None):
    """Retention, run at the start of each run. Deletes run folders older than RETENTION_DAYS, raw logs of all but
    the newest KEEP_RUNS runs (unless keep_logs), and cache entries unused for RETENTION_DAYS.
    Runs are ordered by start time (run_time), not name. Never touches the current run, the most recent other run, the
    run `latest` points to (so the one paused run that can be resumed), a running run while a worker holds the lock,
    or anything through a symlink. A run folder is one named by its start time, or one with a config.json.
    Returns {"runs": [...], "logs": [...], "cache": n}."""
    root = Path(home) / "runs"
    cutoff = datetime.now() - timedelta(days=RETENTION_DAYS)
    out = {"runs": [], "logs": [], "cache": 0}
    found = [(run_time(d), d) for d in (root.iterdir() if root.is_dir() else []) if d.is_dir() and not d.is_symlink()
             and (RUN_NAME.match(d.name) or (d / "config.json").exists())]  # a run: its timestamp name, or config.json
    runs = [d for t, d in sorted((x for x in found if x[0] is not None), key=lambda x: (x[0], x[1].name))]  # by start
    active = worker_active(home)  # without a worker, a "running" status is left over from a crash
    cur = Path(current).resolve()
    others = [d for d in runs if d.resolve() != cur]
    keep = {cur} | ({others[-1].resolve()} if others else set())  # the newest run besides this one: a report stays
    if (root / "latest").is_symlink():
        keep.add((root / "latest").resolve())
    for i, d in enumerate(runs):
        if d.resolve() in keep or (active and Progress(d).state.get("status") == "running"):
            continue  # a paused run stays only while it is the latest (in keep): only that one can be resumed
        t = run_time(d)
        if t is not None and t < cutoff:
            remove(d)
            out["runs"].append(d.name)
        elif not keep_logs and i < len(runs) - KEEP_RUNS:
            extra = [p for p in d.iterdir() if p.name not in KEEP_FILES]
            for p in extra:
                remove(p)
            if extra:
                out["logs"].append(d.name)
    cache = Path(cache_dir) if cache_dir else Path(home) / "cache"
    if cache.is_dir() and not cache.is_symlink():
        for f in cache.iterdir():
            try:
                st = f.lstat()
            except OSError:
                continue
            if f.is_file() and not f.is_symlink() and datetime.fromtimestamp(st.st_mtime) < cutoff:
                f.unlink()
                out["cache"] += 1
    (Path(home) / "ledger.json").unlink(missing_ok=True)  # left by v0.4.2 and earlier
    return out


def prune_line(out):
    """The terminal line saying what prune() deleted, or "" if nothing."""
    parts = []
    if out["runs"]:
        parts.append(f"deleted {len(out['runs'])} run folder{'s' if len(out['runs']) > 1 else ''} older than "
                     f"{RETENTION_DAYS} days ({', '.join(out['runs'])})")
    if out["logs"]:
        parts.append(f"deleted raw logs of {', '.join(out['logs'])} (only the last {KEEP_RUNS} runs keep them; "
                     f"reports stay)")
    if out["cache"]:
        parts.append(f"deleted {out['cache']} cache entr{'ies' if out['cache'] > 1 else 'y'} unused for "
                     f"{RETENTION_DAYS} days")
    return ("Cleanup: " + "; ".join(parts) + f". Runs are kept {RETENTION_DAYS} days, and the latest always.") \
        if parts else ""


def open_file(path):
    cmd = "open" if sys.platform == "darwin" else "xdg-open"
    if shutil.which(cmd):
        subprocess.Popen([cmd, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"Opened {path} with {cmd}.", flush=True)


def load_tasks(run_dir):
    return read_jsonl(Path(run_dir) / "tasks.jsonl")


def diagnose_quietly(run_dir, stopped_unexpectedly=False):
    """Write diagnostics.md; a failure here must never mask the run's own error."""
    try:
        return diagnostics.write(run_dir, stopped_unexpectedly)
    except Exception:
        traceback.print_exc()
        return None


def work(run_dir, home):
    run_dir, home = Path(run_dir).resolve(), Path(home)
    prog = Progress(run_dir)
    lock = acquire_patiently(home)
    if lock is None:
        prog.set(status="failed", error=f"Another run is already active. Check on it with {RUN}.")
        return
    try:
        cfg = json.loads((run_dir / "config.json").read_text())
        model = cfg.get("model")
        details = f"Details: {run_dir / 'run.log'}."
        try:
            preflight_auth()
            prog.phase("instructions", message="rewriting your CLAUDE.md per provider")
            has_instr, warns = (instructions.write_all(run_dir, model, cfg["cache_dir"]) if cfg.get("instructions", True)
                                else (False, []))
            for w in warns:
                print(f"warning: {w}", file=sys.stderr, flush=True)
            instr = ({e: (run_dir / "instructions" / f"{e}.md").read_text() for e in PROVIDERS} if has_instr else {})

            frozen = run_dir / "tasks.frozen"
            if frozen.exists():  # replay has started: the task list never changes again
                tasks, private = load_tasks(run_dir), cfg.get("private", 0)
            else:
                prog.phase("build", total=cfg["want"], message="writing self-contained tasks")
                tasks, pool, sent, private, dropped = build.build(run_dir / "harvest.jsonl", run_dir, cfg["want"], cfg["seed"],
                                                         model, progress=prog.count)
                need = min(MIN_TASKS, cfg["want"]) if dev() else MIN_TASKS
                if len(tasks) < need:
                    raise Failed(f"Only {len(tasks)} usable research tasks after the task builder's checks: too few for "
                                 f"a fair verdict ({need} needed). Do more research with Claude Code, then run {RUN} "
                                 f"again. {TRANSCRIPTS_HINT}")
                cfg.update(pool=pool, sent=sent, kept=len(tasks), private=private, dropped=dropped,
                           run_date=date.today().isoformat())
                write_json(run_dir / "config.json", cfg)
                write_text(frozen, now() + "\n")

            prog.phase("replay", message=f"{len(tasks)} tasks x {len(PROVIDERS)} providers, {WORKERS} at a time")
            ok, lost, connect, note = replay(run_dir, home, tasks, model, instr, prog)
            cfg["tool_surface"] = export.surface(run_dir / "runs")  # what each provider offered Claude, for the export
            write_json(run_dir / "config.json", cfg)
            if not ok:
                raise Failed(f"No task finished on every provider. {details} Retry the failed runs: {RESUME}")

            prog.phase("judge", total=len(ok), message="one judge per task")
            missing = grade.judge(run_dir / "runs", run_dir / "grades", cfg["run_date"], ok, WORKERS, 1, model,
                                  progress=prog.count)
            if len(missing) == len(ok):
                raise Failed(f"Every judge call failed. {details} Retry: {RESUME}")
            lost += [{"n": n, "reason": "the judge call failed"} for n in missing]

            prog.phase("report", message="verdict, why, report")
            sent, dropped = cfg.get("sent", len(tasks)), cfg.get("dropped", 0)
            stats = run_dir / "stats.json"
            write_json(stats, {"lost": sorted(lost, key=lambda x: int(x["n"])), "stop_note": note,
                               "instruction_warnings": warns,
                               "connect": connect})
            args = [str(run_dir / "tasks.jsonl"), str(run_dir / "grades"), str(run_dir / "runs"),
                    str(run_dir / "report.md"), "--harvest", str(run_dir / "harvest.jsonl"),
                    "--cache-dir", cfg["cache_dir"], "--stats", str(stats), *(["--model", model] if model else []),
                    *([] if has_instr else ["--no-instructions"])]
            html = report.main(args)
            prog.set(status="done", report=str(html), message="")
            if cfg.get("open", True):
                open_file(html)
        except UsageLimit:
            traceback.print_exc()
            prog.set(status="paused", error=f"Paused: Claude usage limit reached. Resume after it resets: {RESUME}")
        except Paused as exc:
            prog.set(status="paused", error=str(exc))
        except Failed as exc:
            prog.set(status="failed", error=f"Stopped in {prog.state.get('phase')}: {exc}", **diagnostics.failure(exc))
            diagnose_quietly(run_dir)
        except AuthError as exc:
            prog.set(status="failed", error=f"Stopped: {exc}", **diagnostics.failure(exc))
            diagnose_quietly(run_dir)
        except (Exception, SystemExit) as exc:
            traceback.print_exc()
            prog.set(status="failed", error=f"Failed in {prog.state.get('phase')}: {type(exc).__name__}: {exc}. "
                                            f"{details} Retry: {RESUME}", **diagnostics.failure(exc))
            diagnose_quietly(run_dir)
    finally:
        lock.close()


# ---------- commands ----------

def runs_root(home):
    root = Path(home) / "runs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def active_message(home):
    cur = Progress(Path(home) / "runs" / "latest").state
    return f"A run is already in progress: {(Path(home) / 'runs' / 'latest').resolve()}\n{line(cur)}"


def start(a):
    try:
        sub = preflight_auth()
    except AuthError as exc:
        sys.exit(str(exc))
    home = Path(a.home).expanduser() if getattr(a, "home", None) else default_home()
    secure(home)  # also tightens folders and files an older version wrote with looser modes
    root = runs_root(home)
    if worker_active(home):
        print(active_message(home))
        return
    if a.resume:
        run_dir = (root / "latest").resolve()
        if not (run_dir / "config.json").exists():
            sys.exit(f"No run to resume. Start one with {RUN}.")
        cfg = json.loads((run_dir / "config.json").read_text())
        if Progress(run_dir).state.get("status") == "done":
            print(f"The latest run is already done. Start a new one with {RUN}.\n")
            print((run_dir / "terminal.txt").read_text().rstrip())
            return
    else:
        projects = Path(a.projects).expanduser() if getattr(a, "projects", None) else harvest.PROJECTS
        fd, tmp = tempfile.mkstemp(prefix="harvest-", suffix=".jsonl", dir=home)
        os.close(fd)
        hs = harvest.main(tmp, projects)
        seed = random.randrange(10 ** 6)
        pool = len(build.sample(read_jsonl(tmp), seed))
        want = getattr(a, "tasks", None) or (QUICK if a.quick else FULL)
        need = min(MIN_TASKS, want) if dev() else MIN_TASKS
        if pool < need:
            os.unlink(tmp)
            sys.exit(f"Found {pool} research tasks in your Claude Code transcripts ({projects}): too few for a fair "
                     f"verdict ({need} needed). The plugin replays past sessions where Claude searched the web; do "
                     f"more research with Claude Code, then run {RUN} again. {TRANSCRIPTS_HINT}")
        run_dir = root / datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir.mkdir()
        os.replace(tmp, run_dir / "harvest.jsonl")
        cfg = {"want": want, "quick": a.quick, "model": a.model or default_model(), "keep_logs": a.keep_logs, "seed": seed,
               "instructions": not a.no_instructions,
               "cache_dir": str(Path(a.cache_dir).expanduser() if getattr(a, "cache_dir", None) else home / "cache"),
               "projects": str(projects), "open": not getattr(a, "no_open", False), "created": now(),
               "run_id": str(uuid.uuid4())}
        write_json(run_dir / "config.json", cfg)
        print(f"Harvest: {hs['tasks']} research tasks ({hs['searches']} web searches) in your transcripts.")
        print(f"Login: Claude subscription ({sub}); API keys and cloud providers are off for every run.")
        print(f"Model: {cfg['model'] or 'your default model'}.")
        has_instr = cfg["instructions"] and bool(instructions.user_instructions().strip())
        print(plan(want, pool, has_instr, sub, a.quick))
    latest = root / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(run_dir.name)
    print(f"Run folder: {run_dir}")
    pruned = prune(home, cfg.get("keep_logs"), run_dir, cfg.get("cache_dir"))
    cfg["pruned"] = {k: v if isinstance(v, int) else len(v) for k, v in pruned.items()}  # counts, for diagnostics.md
    write_json(run_dir / "config.json", cfg)
    cleaned = prune_line(pruned)
    if cleaned:
        print(cleaned)
    Progress(run_dir).set(status="running", phase="starting", step=0, steps=len(PHASES), done=0, total=0,
                          message="", error=None)
    if getattr(a, "foreground", False):
        work(run_dir, home)
        p = Progress(run_dir).state
        print(line(p))
        if p.get("status") == "done":
            print((run_dir / "terminal.txt").read_text())
        return
    argv = [sys.executable, "-I", str(Path(__file__).resolve()), "work", "--dir", str(run_dir), "--home", str(home)]
    with (run_dir / "run.log").open("a") as log:
        subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    print(f"Started in the background. It keeps running if you close this session; run {RUN} to check on it.")


def alive(home, p):
    """A worker holds the lock, or one was just started and hasn't taken it yet."""
    if worker_active(home):
        return True
    try:
        young = (datetime.now() - datetime.fromisoformat(p.get("updated", ""))).total_seconds() < 60
    except ValueError:
        young = False
    return p.get("phase") == "starting" and young


def status(a):
    home = Path(a.home).expanduser() if getattr(a, "home", None) else default_home()
    run_dir = home / "runs" / "latest"
    prog = run_dir / "progress.json"
    if not prog.exists():
        sys.exit(f"No run yet. Start one with {RUN}.")

    def read():
        try:
            return json.loads(prog.read_text())
        except (OSError, ValueError):
            return {}

    first = p = read()
    deadline = time.time() + (a.wait or 0)
    while time.time() < deadline and p.get("status") == "running" and p.get("phase") == first.get("phase"):
        if not alive(home, p):
            break
        time.sleep(3)
        p = read() or p
    if p.get("status") == "running" and not alive(home, p):
        diag = diagnose_quietly(run_dir.resolve(), stopped_unexpectedly=True)
        print(f"The run stopped unexpectedly during {p.get('phase')} (details: {run_dir.resolve() / 'run.log'}). "
              f"Continue where it left off: {RESUME}")
        if diag:
            print(f"Diagnostics: {diag}")
        return
    print(line(p))
    if p.get("status") == "failed" and (run_dir / "diagnostics.md").exists():
        print(f"Diagnostics: {(run_dir / 'diagnostics.md').resolve()}")
    if p.get("status") == "done":
        print()
        print((run_dir / "terminal.txt").read_text().rstrip())
        if p.get("message"):
            print(p["message"])


def run_arg(a, name="latest"):
    """A run folder under the outputs home's runs/ (latest, a folder name, or a path inside runs/), else exit."""
    home = Path(a.home).expanduser() if getattr(a, "home", None) else default_home()
    root = (home / "runs").resolve()
    d = (home / "runs" / name) if name == "latest" or "/" not in name else Path(name).expanduser()
    d = d.resolve()
    if root not in d.parents or not (d / "config.json").exists():
        sys.exit(f"No run at {d} in {root}. Start one with {RUN}.")
    return d


def diagnose_cmd(a):
    run_dir = run_arg(a)
    p = Progress(run_dir).state
    stopped = p.get("status") == "running" and not alive(Path(a.home).expanduser() if getattr(a, "home", None)
                                                        else default_home(), p)
    path = diagnostics.write(run_dir, stopped)
    print(path.read_text().rstrip())
    print(f"\nWritten to {path}. Nothing was sent anywhere.")


def issue_url(a):
    """Read-only: the issue title, the body file, and a pre-filled link, for filing diagnostics.md."""
    run_dir = run_arg(a, a.run)
    if not (run_dir / "diagnostics.md").exists():
        sys.exit(f"No diagnostics.md in {run_dir}; run {RUN} --diagnose first.")
    title, body, url = diagnostics.issue(run_dir)
    print(f"Repository: {diagnostics.repo()}")
    print(f"Title: {title}")
    print(f"Body file: {body}")
    print(f"Link (if gh isn't available; review it before submitting): {url}")


def purge(a):
    home = Path(a.home).expanduser() if getattr(a, "home", None) else default_home()
    if worker_active(home):
        sys.exit(f"A run is in progress; purge after it ends. Check on it with {RUN}.")
    removed = []
    cache = home / "cache"
    if cache.exists():
        shutil.rmtree(cache)
        removed.append(str(cache))
    root = home / "runs"
    n = 0
    for d in sorted(root.iterdir()) if root.exists() else []:
        if d.is_dir() and not d.is_symlink():
            for p in d.iterdir():
                if p.name not in KEEP_FILES:
                    shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
                    n += 1
    print(f"Removed the LLM cache ({cache}) and {n} raw log files and folders from {root}. Reports were kept, with "
          f"each run's config, so the {RETENTION_DAYS}-day cleanup still applies; delete {home} to remove everything."
          if removed or n else "Nothing to purge.")


def main(argv=None):
    if sys.platform == "win32":
        sys.exit("my-search-eval needs macOS (tested) or Linux/WSL (untested), not native Windows.")
    if sys.version_info < (3, 9):
        sys.exit("my-search-eval needs Python 3.9 or later.")
    os.umask(0o077)  # everything written here (start, the worker, its children) is owner-only, like ~/.claude/projects
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start", help="check the login, harvest, print the plan, then run in the background")
    s.add_argument("--quick", action="store_true", help=f"{QUICK} tasks instead of {FULL}")
    s.add_argument("--model", help="model for every call (default: your default model)")
    s.add_argument("--keep-logs", action="store_true", help="don't prune older runs' raw logs")
    s.add_argument("--no-instructions", action="store_true", help="leave your CLAUDE.md out of the replays")
    s.add_argument("--resume", action="store_true", help="continue the latest run where it stopped, with its saved options")
    t = sub.add_parser("status", help="one progress line, or the summary once done")
    t.add_argument("--wait", type=int, default=0)
    pg = sub.add_parser("purge", help="delete the cache and raw logs; keep reports and run configs")
    dg = sub.add_parser("diagnose", help="write diagnostics.md for the latest run and print it")
    iu = sub.add_parser("issue-url", help="print the issue title, body file and a pre-filled issue link (read-only)")
    iu.add_argument("run", nargs="?", default="latest", help="run folder, or latest")
    w = sub.add_parser("work")  # internal: the background worker
    w.add_argument("--dir", required=True)
    w.add_argument("--home")
    if dev():
        for p in (s, t, pg, dg, iu):
            p.add_argument("--home")
        s.add_argument("--projects")
        s.add_argument("--cache-dir")
        s.add_argument("--tasks", type=int)
        s.add_argument("--no-open", action="store_true")
        s.add_argument("--foreground", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "start":
        start(a)
    elif a.cmd == "status":
        status(a)
    elif a.cmd == "purge":
        purge(a)
    elif a.cmd == "diagnose":
        diagnose_cmd(a)
    elif a.cmd == "issue-url":
        issue_url(a)
    else:
        home = Path(a.home) if a.home and dev() else default_home()
        run_dir = Path(a.dir).resolve()
        if not dev() and (home / "runs").resolve() not in run_dir.parents:
            sys.exit("The worker only runs folders under the outputs home.")
        work(run_dir, home)


if __name__ == "__main__":
    main()
