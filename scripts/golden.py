#!/usr/bin/env python3
"""Run the golden tasks (tests/golden/) against a route and print pass rate, cost per pass and time.

    python3 scripts/golden.py --route baseline            # against tests/fake_claude.py, as CI does
    python3 scripts/golden.py --route baseline --route savings --real
    python3 scripts/golden.py --list

A route is a named change to try (tests/golden/routes.json): a worker model, frontmatter keys added to every task,
or settings for .env. Each golden task is run alone through a throwaway copy of the queue, so its cost is exactly
what its calls spent. A task counts as right when it ends the way its `expect:` says (default pass), so the task
whose fake worker only claims success is right when it FAILS. `fake_only: true` tasks are skipped with --real.

With the fake claude the money is made up and only the plumbing is tested. With --real the run spends real
usage: run it by hand, on the Pi, with the savings off and then on, as the queue user:

    sudo -u taskq .venv/bin/python scripts/golden.py --route baseline --route savings --real

Stdlib only. The dispatcher it starts runs under the checkout's .venv python when there is one. With --real the
claude binary and login token come from the environment, else from the checkout's .env (so `sudo` is not a problem).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GOLDEN = REPO / "tests" / "golden"
FAKE_CLAUDE = REPO / "tests" / "fake_claude.py"
GOLDEN_ONLY_KEYS = ("expect", "fake_only")  # for this script; the dispatcher would reject them
WORKER, REVIEWER = "claude-sonnet-5", "claude-haiku-4-5-20251001"


def queue_python():
    """The python to start the dispatcher with: the checkout's venv (it has the libraries), else this one."""
    venv = REPO / ".venv" / "bin" / "python3"
    return str(venv) if venv.exists() else sys.executable


def read_env(path):
    """KEY=VALUE lines of a .env file as a dict; comments, blanks and surrounding quotes are dropped."""
    found = {}
    if not path.exists():
        return found
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and key and not key.startswith("#"):
            found[key] = value.strip().strip("\"'")
    return found


def real_settings(environ, queue_env):
    """The claude binary and login token for a --real run: the environment first, then the queue's .env."""
    claude = environ.get("CLAUDE_BIN") or shutil.which("claude") or queue_env.get("CLAUDE_BIN") or ""
    token = environ.get("CLAUDE_CODE_OAUTH_TOKEN") or queue_env.get("CLAUDE_CODE_OAUTH_TOKEN") or ""
    return {"CLAUDE_BIN": claude, **({"CLAUDE_CODE_OAUTH_TOKEN": token} if token else {})}


def split_task(path):
    """(frontmatter dict, body) of a golden task file."""
    text = path.read_text()
    _, front, body = text.split("---\n", 2)
    meta = {}
    for line in front.splitlines():
        key, _, value = line.partition(":")
        if key.strip():
            meta[key.strip()] = value.strip()
    return meta, body


def load_routes():
    return json.loads((GOLDEN / "routes.json").read_text())


def golden_tasks(only=()):
    found = sorted(p.parent for p in GOLDEN.glob("*/task.md"))
    return [p for p in found if not only or p.name in only]


def make_queue(root, route, real):
    """A throwaway copy of the queue with a .env for this route."""
    base = root / "queue"
    base.mkdir()
    for name in ("dispatcher.py", "worker_preamble.md"):
        shutil.copy2(REPO / name, base / name)
    shutil.copytree(REPO / "taskq_lib", base / "taskq_lib", ignore=shutil.ignore_patterns("__pycache__"))
    settings = {"MAX_RATE_LIMIT_RETRIES": "0", "RATE_LIMIT_BASE_DELAY": "0", "MAX_ATTEMPTS_PER_DAY": "0",
                "DEFAULT_TIMEOUT_MINUTES": "30" if real else "1", "REVIEW_TIMEOUT_MINUTES": "10" if real else "1",
                **route.get("env", {})}
    if real:
        settings.update(real_settings(os.environ, read_env(REPO / ".env")))
    else:
        settings["CLAUDE_BIN"] = str(FAKE_CLAUDE)
    (base / ".env").write_text("".join(f"{k}={v}\n" for k, v in settings.items()))
    return base


def usage_rows(base, name):
    path = base / "logs" / "usage.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("task") == name]


def run_one(base, task_dir, route, real, root):
    """Run one golden task to the end. Returns a result dict."""
    meta, body = split_task(task_dir / "task.md")
    expect = meta.get("expect", "pass")
    work = root / "work" / task_dir.name
    shutil.copytree(task_dir / "files", work)
    frontmatter = {"model": route.get("model") or WORKER, "review_model": route.get("review_model") or REVIEWER,
                   "escalation_model": route.get("model") or WORKER, "attempts": "0", "cwd": str(work)}
    frontmatter.update({k: v for k, v in meta.items() if k not in GOLDEN_ONLY_KEYS})
    frontmatter.update(route.get("frontmatter", {}))
    name = task_dir.name
    pending = base / "tasks" / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    (pending / f"{name}.md").write_text("---\n" + "".join(f"{k}: {v}\n" for k, v in frontmatter.items()) + "---\n" + body)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", str(root)), "LC_ALL": "C.UTF-8"}
    if not real:
        scenario = root / f"{name}.scenario.json"
        scenario.write_text((task_dir / "fake.json").read_text())
        env["FAKE_CLAUDE_SCENARIO"] = str(scenario)
    started = time.monotonic()
    outcome, error = None, ""
    for _ in range(int(frontmatter.get("max_attempts", 3)) + 1):
        proc = subprocess.run([queue_python(), str(base / "dispatcher.py")], cwd=base, env=env,
                              capture_output=True, text=True, timeout=3600)
        if proc.returncode != 0:
            lines = (proc.stderr or proc.stdout).strip().splitlines()
            error = lines[-1] if lines else f"dispatcher exited {proc.returncode}"
            break
        outcome = next((s for s in ("done", "failed") if (base / "tasks" / s / f"{name}.md").exists()), None)
        if outcome:
            break
    seconds = time.monotonic() - started
    rows = usage_rows(base, name)
    return {
        "task": name, "outcome": outcome or "stuck", "expect": "pass" if expect == "pass" else "fail",
        "right": (outcome == "done") == (expect == "pass") and outcome is not None,
        "cost": sum(r.get("cost_usd") or 0 for r in rows), "calls": len(rows),
        "reviews": sum(r.get("label") == "review" for r in rows), "seconds": seconds, "error": error,
    }


def run_route(name, route, real, only=()):
    rows = []
    with tempfile.TemporaryDirectory(prefix="taskq-golden-") as tmp:
        root = Path(tmp)
        base = make_queue(root, route, real)
        for task_dir in golden_tasks(only):
            if real and split_task(task_dir / "task.md")[0].get("fake_only") == "true":
                continue
            rows.append(run_one(base, task_dir, route, real, root))
    return rows


def summary(rows):
    right = sum(r["right"] for r in rows)
    cost = sum(r["cost"] for r in rows)
    return {"right": right, "total": len(rows), "cost": cost, "per_pass": cost / right if right else None,
            "seconds": sum(r["seconds"] for r in rows), "calls": sum(r["calls"] for r in rows)}


def show(name, rows):
    print(f"\nroute: {name}")
    print(f"{'task':<16} {'result':<10} {'expected':<8} {'right':<5} {'calls':>5} {'cost_usd':>9} {'seconds':>8}")
    for r in rows:
        print(f"{r['task']:<16} {r['outcome']:<10} {r['expect']:<8} {'yes' if r['right'] else 'NO':<5} "
              f"{r['calls']:>5} {r['cost']:>9.4f} {r['seconds']:>8.1f}")
        if r["error"]:
            print(f"  dispatcher error: {r['error']}")
    s = summary(rows)
    per = f"{s['per_pass']:.4f}" if s["per_pass"] is not None else "n/a"
    print(f"pass rate {s['right']}/{s['total']}   cost per pass {per}   total cost {s['cost']:.4f}   "
          f"calls {s['calls']}   time {s['seconds']:.1f}s")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--route", action="append", help="a route from tests/golden/routes.json (repeat to compare)")
    parser.add_argument("--real", action="store_true", help="call the real claude and spend real usage")
    parser.add_argument("--only", action="append", default=[], help="run just this golden task (repeatable)")
    parser.add_argument("--list", action="store_true", help="list the routes and tasks")
    args = parser.parse_args(argv)
    routes = load_routes()
    if args.list:
        for name, route in routes.items():
            print(f"route {name}: {route.get('about', '')}")
        for task_dir in golden_tasks():
            print(f"task  {task_dir.name}")
        return 0
    if args.real and not real_settings(os.environ, read_env(REPO / ".env"))["CLAUDE_BIN"]:
        print("--real needs the claude binary: set CLAUDE_BIN in the environment or in .env, or put claude on PATH",
              file=sys.stderr)
        return 2
    names = args.route or ["baseline"]
    unknown = [n for n in names if n not in routes]
    if unknown:
        print(f"unknown route {', '.join(unknown)}; choose from {', '.join(routes)}", file=sys.stderr)
        return 2
    results = {}
    for name in names:
        results[name] = run_route(name, routes[name], args.real, args.only)
        show(name, results[name])
    if len(names) > 1:
        first = summary(results[names[0]])
        for name in names[1:]:
            other = summary(results[name])
            if first["per_pass"] and other["per_pass"] is not None:
                ratio = 100 * other["per_pass"] / first["per_pass"]
                same = other["right"] >= first["right"]
                print(f"\n{name} costs {ratio:.0f}% of {names[0]} per passing task; pass rate "
                      f"{other['right']}/{other['total']} vs {first['right']}/{first['total']}"
                      + ("" if same else "  (LOWER: this change costs correctness)"))
    return 0 if all(r["right"] for rows in results.values() for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
