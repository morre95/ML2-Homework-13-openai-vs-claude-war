"""Shared Polyglot task library, scorer and dev-set evaluator.

Copied verbatim into both build boxes (as /workspace/tools/polyglot.py) and used
by the official evaluator, so both sides measure progress exactly the same way.
Stdlib only. Prompts, test commands and the two-attempt rule mirror
Aider's benchmark (aider/benchmark/benchmark.py), as used by DGM.

CLI:
  python polyglot.py test <task_dir> <workdir>            # run hidden tests, print JSON
  python polyglot.py eval-dev --harness-root DIR --tasks DIR --model M [-n N] [-j J]
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TEST_TIMEOUT = 180
SOLVE_TIMEOUT = int(os.environ.get("SOLVE_TIMEOUT", "900"))
TRIES = 2
TASK_FILE = "TASK.md"
FEEDBACK_MAX_CHARS = 8000

INSTRUCTIONS_ADDENDUM = """
####

Use the above instructions to modify the supplied files: {file_list}
Don't change the names of existing functions or classes, as they may be referenced from other code like unit tests, etc.
Only use standard libraries, don't suggest installing any packages.
"""

TEST_FAILURES = """
####

See the testing errors above.
The tests are correct, don't try and change them.
Fix the code in {file_list} to resolve the errors.
"""

LANG_BY_EXT = {
    ".py": "python",
    ".rs": "rust",
    ".go": "go",
    ".js": "javascript",
    ".cpp": "cpp",
    ".java": "java",
}

NPM_INSTALL = Path(os.environ.get("NPM_INSTALL_DIR", "/npm-install"))


def load_task(task_dir: str | Path) -> dict:
    task_dir = Path(task_dir)
    config = json.loads((task_dir / ".meta" / "config.json").read_text())
    files = config.get("files", {})
    test_files = files.get("test", [])
    ignore = {"CMakeLists.txt", "Cargo.toml", *test_files, *files.get("example", [])}
    solution_files = [f for f in files.get("solution", []) if f not in ignore and not f.startswith((".meta", ".docs"))]
    lang = next((LANG_BY_EXT[Path(f).suffix] for f in test_files if Path(f).suffix in LANG_BY_EXT), None)
    if lang is None:
        raise ValueError(f"cannot infer language for {task_dir}")
    return {
        "id": task_dir.name,
        "dir": str(task_dir),
        "lang": lang,
        "solution_files": solution_files,
        "test_files": test_files,
    }


def problem_statement(task_dir: str | Path) -> str:
    task_dir = Path(task_dir)
    task = load_task(task_dir)
    docs = task_dir / ".docs"
    text = ""
    if (docs / "introduction.md").exists():
        text += (docs / "introduction.md").read_text()
    text += (docs / "instructions.md").read_text()
    if (docs / "instructions.append.md").exists():
        text += (docs / "instructions.append.md").read_text()
    file_list = " ".join(Path(f).name for f in task["solution_files"])
    return text + INSTRUCTIONS_ADDENDUM.format(file_list=file_list)


def feedback_message(test_output: str, task_dir: str | Path) -> str:
    task = load_task(task_dir)
    file_list = " ".join(Path(f).name for f in task["solution_files"])
    out = test_output
    if len(out) > FEEDBACK_MAX_CHARS:
        out = out[: FEEDBACK_MAX_CHARS // 2] + "\n...[truncated]...\n" + out[-FEEDBACK_MAX_CHARS // 2 :]
    return out + TEST_FAILURES.format(file_list=file_list)


def make_workdir(task_dir: str | Path, dst: str | Path) -> Path:
    """Copy a task into dst with tests and .meta hidden, and write TASK.md."""
    task_dir, dst = Path(task_dir), Path(dst)
    task = load_task(task_dir)
    hidden = set(task["test_files"])

    def ignore(d, names):
        rel = Path(d).relative_to(task_dir)
        out = set()
        for n in names:
            p = (rel / n).as_posix()
            if p == ".meta" or p in hidden:
                out.add(n)
        return out

    shutil.copytree(task_dir, dst, ignore=ignore, dirs_exist_ok=True)
    (dst / TASK_FILE).write_text(problem_statement(task_dir))
    return dst


def _cleanup_output(output: str, workdir: Path) -> str:
    output = re.sub(r"\bin \d+\.\d+s\b", "", output)
    return output.replace(str(workdir), workdir.name)


def run_tests(task_dir: str | Path, workdir: str | Path, timeout: int = TEST_TIMEOUT) -> dict:
    """Restore the original tests into workdir, un-skip them, and run them."""
    task_dir, workdir = Path(task_dir), Path(workdir)
    task = load_task(task_dir)
    (workdir / TASK_FILE).unlink(missing_ok=True)
    for f in task["test_files"]:
        dst = workdir / f
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(task_dir / f, dst)
        if f.endswith(".java"):
            dst.write_text(re.sub(r"@Disabled\([^)]*\)\s*\n", "", dst.read_text()))
        if f.endswith(".js"):
            dst.write_text(re.sub(r"\bxtest\(", "test(", dst.read_text()))

    lang = task["lang"]
    if lang == "python":
        cmd = [sys.executable, "-m", "pytest", "-q"]
    elif lang == "rust":
        cmd = ["cargo", "test", "--offline", "--", "--include-ignored"]
    elif lang == "go":
        cmd = ["go", "test", "./..."]
    elif lang == "java":
        gradlew = workdir / "gradlew"
        if gradlew.exists():
            gradlew.chmod(0o755)
        cmd = ["./gradlew", "test", "--offline", "--no-daemon"]
    elif lang == "javascript":
        for name in ("node_modules", "package-lock.json"):
            link = workdir / name
            if not link.exists() and (NPM_INSTALL / name).exists():
                link.symlink_to(NPM_INSTALL / name)
        cmd = ["npm", "run", "test", "--silent"]
    elif lang == "cpp":
        cmd = ["bash", "-c", "mkdir -p build && cd build && cmake -DEXERCISM_RUN_ALL_TESTS=1 -G 'Unix Makefiles' .. >/dev/null && make"]
    else:
        raise ValueError(lang)

    start = time.time()
    try:
        r = subprocess.run(cmd, cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, errors="replace", timeout=timeout)
        passed, output, timed_out = r.returncode == 0, r.stdout, False
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        passed, output, timed_out = False, out + "\nTests timed out!", True
    for junk in ("target", "build", "node_modules", ".gradle"):
        p = workdir / junk
        if p.is_symlink():
            p.unlink()
        elif p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
    return {"passed": passed, "timed_out": timed_out, "duration": round(time.time() - start, 2),
            "output": _cleanup_output(output, workdir)}


def prepare_retry(task_dir: str | Path, workdir: str | Path, test_output: str, feedback_file: str | Path) -> None:
    """After a failed attempt: write feedback, hide the tests again, restore TASK.md."""
    task_dir, workdir = Path(task_dir), Path(workdir)
    Path(feedback_file).write_text(feedback_message(test_output, task_dir))
    for f in load_task(task_dir)["test_files"]:
        (workdir / f).unlink(missing_ok=True)
    (workdir / TASK_FILE).write_text(problem_statement(task_dir))


def list_tasks(tasks_root: str | Path) -> list[Path]:
    return sorted(p for p in Path(tasks_root).iterdir() if (p / ".meta" / "config.json").exists())


# ---------------------------------------------------------------- dev evaluator

def solve_and_score(task_dir: Path, harness_root: Path, model: str, tries: int = TRIES,
                    solve_timeout: int = SOLVE_TIMEOUT) -> dict:
    """Local (in-box) equivalent of the official evaluator for one task."""
    with tempfile.TemporaryDirectory(prefix="pg_") as tmp:
        work = make_workdir(task_dir, Path(tmp) / task_dir.name)
        feedback_file = Path(tmp) / "feedback.txt"
        attempts = []
        for attempt in range(tries):
            cmd = [sys.executable, "-m", "harness", "solve", str(work), "--model", model]
            if attempt > 0:
                cmd += ["--feedback", str(feedback_file)]
            t0 = time.time()
            try:
                r = subprocess.run(cmd, cwd=harness_root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, errors="replace", timeout=solve_timeout)
                solve_rc = r.returncode
            except subprocess.TimeoutExpired:
                solve_rc = "timeout"
            solve_s = round(time.time() - t0, 1)
            res = run_tests(task_dir, work)
            attempts.append({"solve_rc": solve_rc, "solve_s": solve_s, "passed": res["passed"]})
            if res["passed"]:
                break
            prepare_retry(task_dir, work, res["output"], feedback_file)
        return {"task": task_dir.name, "passed": attempts[-1]["passed"],
                "pass_attempt1": attempts[0]["passed"], "attempts": attempts}


def eval_dev(args) -> None:
    tasks = list_tasks(args.tasks)
    if args.ids:
        wanted = set(args.ids.split(","))
        tasks = [t for t in tasks if t.name in wanted]
    if args.n and args.n < len(tasks):
        tasks = random.Random(args.seed).sample(tasks, args.n)
    harness_root = Path(args.harness_root).resolve()
    results = []
    with cf.ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futs = {ex.submit(solve_and_score, t, harness_root, args.model): t for t in tasks}
        for fut in cf.as_completed(futs):
            r = fut.result()
            results.append(r)
            print(f"[{'PASS' if r['passed'] else 'fail'}] {r['task']}", file=sys.stderr, flush=True)
    passed = sum(r["passed"] for r in results)
    summary = {"model": args.model, "n": len(results), "passed": passed,
               "pass_rate": round(passed / max(len(results), 1), 4),
               "pass_rate_attempt1": round(sum(r["pass_attempt1"] for r in results) / max(len(results), 1), 4),
               "results": sorted(results, key=lambda r: r["task"])}
    out = json.dumps(summary, indent=2)
    if args.out:
        Path(args.out).write_text(out)
    print(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("test")
    t.add_argument("task_dir")
    t.add_argument("workdir")
    w = sub.add_parser("workdir")
    w.add_argument("task_dir")
    w.add_argument("dst")
    e = sub.add_parser("eval-dev")
    e.add_argument("--harness-root", required=True, help="directory containing the `harness` package")
    e.add_argument("--tasks", required=True)
    e.add_argument("--model", required=True)
    e.add_argument("-n", type=int, default=0, help="random subset size (0 = all)")
    e.add_argument("--ids", default="", help="comma-separated task ids")
    e.add_argument("-j", "--jobs", type=int, default=4)
    e.add_argument("--seed", type=int, default=0)
    e.add_argument("--out", default="")
    args = ap.parse_args()
    if args.cmd == "test":
        print(json.dumps(run_tests(args.task_dir, args.workdir)))
    elif args.cmd == "workdir":
        make_workdir(args.task_dir, args.dst)
    else:
        eval_dev(args)


if __name__ == "__main__":
    main()
