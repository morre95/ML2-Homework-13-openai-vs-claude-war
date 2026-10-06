"""Official evaluator: every harness x every model on the held-out split.

  uv run eval/run_eval.py                 # main 2x2 + seed baseline (asks before spending)
  uv run eval/run_eval.py --spotcheck     # re-score archived generations on a held-out slice
  uv run eval/run_eval.py --n-tasks 5 --seeds 1 --yes   # quick run

Each solve attempt runs in a fresh container built from the harness (network: the
metering proxy only). Tests run in another fresh container with no network.
Results are appended to results/eval/<runs|spotcheck>.jsonl; reruns resume.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
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
import uuid
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bench"))
sys.path.insert(0, str(ROOT / "scripts"))
import polyglot  # noqa: E402
from make_keys import eval_keys  # noqa: E402

OUT = ROOT / "results" / "eval"
NETWORK = "shootout_sandbox"
BASE_IMAGE = "shootout/base:latest"
EST_TOKENS_PER_ATTEMPT = 40_000
_lock = threading.Lock()


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, errors="replace", **kw)


def build_harness_image(name: str, pkg: Path) -> str:
    if not (pkg / "__main__.py").exists():
        raise SystemExit(f"{name}: {pkg} is not a harness package (no __main__.py)")
    h = hashlib.sha256()
    base_id = sh(["docker", "image", "inspect", "-f", "{{.Id}}", BASE_IMAGE], capture_output=True).stdout.strip()
    h.update(base_id.encode())
    for f in sorted(p for p in pkg.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        h.update(str(f.relative_to(pkg)).encode())
        h.update(f.read_bytes())
    tag = f"shootout/h-{re.sub(r'[^a-z0-9_.-]', '-', name.lower())}:{h.hexdigest()[:12]}"
    if sh(["docker", "image", "inspect", tag], capture_output=True).returncode == 0:
        return tag
    with tempfile.TemporaryDirectory() as ctx:
        shutil.copytree(pkg, Path(ctx) / "harness", ignore=shutil.ignore_patterns("__pycache__"))
        (Path(ctx) / "Dockerfile").write_text(
            f"FROM {BASE_IMAGE}\n"
            "COPY harness /opt/h/harness\n"
            "RUN if [ -s /opt/h/harness/requirements.txt ]; then "
            "pip install --no-cache-dir -r /opt/h/harness/requirements.txt; fi\n"
            "WORKDIR /opt/h\n")
        r = sh(["docker", "build", "-q", "-t", tag, ctx], capture_output=True)
        if r.returncode != 0:
            raise SystemExit(f"building image for {name} failed:\n{r.stderr[-3000:]}")
    return tag


def integrity_check(name: str, pkg: Path, heldout_ids: list[str]) -> list[str]:
    """Flag held-out exercise names that appear in the harness source."""
    names = {t.split("__", 1)[1] for t in heldout_ids}
    names = {n for n in names if len(n) >= 6}
    text = "\n".join(p.read_text(errors="replace").lower() for p in pkg.rglob("*")
                     if p.is_file() and p.suffix in {".py", ".md", ".txt", ".json", ".yaml", ".toml"})
    hits = sorted(n for n in names if re.search(rf"\b{re.escape(n)}\b", text)
                  or re.search(rf"\b{re.escape(n.replace('-', '_'))}\b", text))
    return hits


def run_container_solve(image: str, work: Path, model: str, key: str, seed: int, timeout: int,
                        mem: str, feedback: Path | None) -> tuple[str | int, float]:
    name = f"ev-{uuid.uuid4().hex[:12]}"
    cmd = ["docker", "run", "--rm", "--name", name, "--network", NETWORK,
           "--user", f"{os.getuid()}:{os.getgid()}", "--memory", mem, "--cpus", "2",
           "-e", "HOME=/tmp", "-e", f"HARNESS_SEED={seed}",
           "-e", "ANTHROPIC_BASE_URL=http://proxy:8080/anthropic", "-e", f"ANTHROPIC_API_KEY={key}",
           "-e", "OPENAI_BASE_URL=http://proxy:8080/openai/v1", "-e", f"OPENAI_API_KEY={key}",
           "-e", f"SOLVE_TIMEOUT={timeout}",
           "-v", f"{work}:/task"]
    args = ["python", "-m", "harness", "solve", "/task", "--model", model]
    if feedback is not None:
        cmd += ["-v", f"{feedback}:/feedback.txt:ro"]
        args += ["--feedback", "/feedback.txt"]
    t0 = time.time()
    try:
        r = sh(cmd + [image] + args, capture_output=True, timeout=timeout + 30)
        with (work.parent / f"{work.name}.solve.log").open("a") as f:
            f.write(r.stdout[-20000:] + r.stderr[-20000:])
        rc: str | int = r.returncode
    except subprocess.TimeoutExpired:
        sh(["docker", "kill", name], capture_output=True)
        rc = "timeout"
    return rc, round(time.time() - t0, 1)


def run_container_tests(task_dir: Path, work: Path) -> dict:
    cmd = ["docker", "run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
           "--memory", "4g", "--cpus", "2", "-e", "HOME=/tmp",
           "-v", f"{ROOT / 'bench'}:/opt/scorer:ro", "-v", f"{task_dir}:/taskdir:ro", "-v", f"{work}:/work",
           BASE_IMAGE, "python", "/opt/scorer/polyglot.py", "test", "/taskdir", "/work"]
    r = sh(cmd, capture_output=True, timeout=polyglot.TEST_TIMEOUT + 120)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return {"passed": False, "timed_out": False, "duration": 0, "output": r.stdout[-4000:] + r.stderr[-4000:]}


def run_job(job: dict, cfg: dict, out_file: Path) -> dict:
    task_dir = Path(job["task_dir"])
    base = OUT / "work" / job["harness"] / job["model"] / f"s{job['seed']}"
    base.mkdir(parents=True, exist_ok=True)
    work = base / task_dir.name
    if work.exists():
        shutil.rmtree(work)
    polyglot.make_workdir(task_dir, work)
    feedback = base / f"{task_dir.name}.feedback.txt"
    feedback.unlink(missing_ok=True)
    attempts = []
    for attempt in range(cfg["tries"]):
        rc, solve_s = run_container_solve(job["image"], work, job["model_id"], job["key"], job["seed"],
                                          cfg["solve_timeout"], cfg["memory"], feedback if attempt else None)
        res = run_container_tests(task_dir, work)
        attempts.append({"solve_rc": rc, "solve_s": solve_s, "passed": res["passed"],
                         "test_s": res.get("duration"), "test_timeout": res.get("timed_out")})
        if res["passed"]:
            break
        polyglot.prepare_retry(task_dir, work, res["output"], feedback)
    row = {k: job[k] for k in ("harness", "model", "model_id", "seed", "task", "lang")}
    row.update(passed=attempts[-1]["passed"], pass_attempt1=attempts[0]["passed"], attempts=attempts, ts=time.time())
    with _lock, out_file.open("a") as f:
        f.write(json.dumps(row) + "\n")
    return row


def done_keys(out_file: Path) -> set:
    if not out_file.exists():
        return set()
    rows = [json.loads(l) for l in out_file.read_text().splitlines() if l.strip()]
    return {(r["harness"], r["model"], r["seed"], r["task"]) for r in rows}


def ensure_proxy() -> None:
    env = os.environ.copy()
    if Path(ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    env.setdefault("CLAUDE_BOX_KEY", "unused")
    env.setdefault("CODEX_BOX_KEY", "unused")
    env.setdefault("EVAL_CLAUDE_MODEL", "unused")
    env.setdefault("EVAL_GPT_MODEL", "unused")
    (ROOT / "results" / "usage").mkdir(parents=True, exist_ok=True)
    sh(["docker", "compose", "up", "-d", "proxy"], cwd=ROOT, env=env, check=True, capture_output=True)
    port = env.get("PROXY_PORT", "8080")
    for _ in range(30):
        if sh(["curl", "-sf", f"http://127.0.0.1:{port}/health"], capture_output=True).returncode == 0:
            return
        time.sleep(1)
    raise SystemExit("proxy did not become healthy")


def select_tasks(cfg: dict, n: int) -> list[Path]:
    tasks = polyglot.list_tasks(ROOT / "data" / "tasks" / cfg["split"])
    return polyglot.stratified_sample(tasks, n, 2024)


def archive_generations(archive: Path, k: int) -> list[Path]:
    gens = [d for d in archive.glob("gen_*") if (d / "harness" / "__main__.py").exists()
            and d.name.split("_", 1)[1].isdigit() and d.name != "gen_0"]
    gens.sort(key=lambda d: int(d.name.split("_", 1)[1]))
    if len(gens) <= k:
        return gens
    idx = sorted({round(i * (len(gens) - 1) / (k - 1)) for i in range(k)}) if k > 1 else [len(gens) - 1]
    return [gens[i] for i in idx]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "eval" / "config.yaml"))
    ap.add_argument("--harnesses", default="", help="comma list, default: all in config")
    ap.add_argument("--models", default="", help="comma list of model aliases, default: all")
    ap.add_argument("--n-tasks", type=int, default=None)
    ap.add_argument("--seeds", type=int, default=None)
    ap.add_argument("--jobs", type=int, default=None)
    ap.add_argument("--spotcheck", action="store_true")
    ap.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    ap.add_argument("--fresh", action="store_true", help="discard previous results for this mode instead of resuming")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    for k in ("seeds", "jobs"):
        if getattr(args, k) is not None:
            cfg[k] = getattr(args, k)
    OUT.mkdir(parents=True, exist_ok=True)
    models = cfg["models"]
    if args.models:
        models = {a: models[a] for a in args.models.split(",")}

    # (name, package dir, models to use, seeds, tasks)
    plan = []
    heldout_ids = json.loads((ROOT / "data" / "splits.json").read_text())["heldout"]
    if args.spotcheck:
        sc = cfg["spotcheck"]
        out_file = OUT / "spotcheck.jsonl"
        tasks = select_tasks(cfg, args.n_tasks or sc["n_tasks"])
        own_model = {"claude": "claude", "codex": "gpt"}
        for box, archive in cfg["archives"].items():
            for gen in archive_generations(ROOT / archive, sc["generations"]):
                alias = own_model[box]
                plan.append((f"{box}@{gen.name}", gen / "harness", {alias: cfg["models"][alias]}, sc["seeds"], tasks))
    else:
        out_file = OUT / "runs.jsonl"
        tasks = select_tasks(cfg, args.n_tasks if args.n_tasks is not None else cfg["n_tasks"])
        names = args.harnesses.split(",") if args.harnesses else list(cfg["harnesses"])
        for name in names:
            plan.append((name, ROOT / cfg["harnesses"][name], models, cfg["seeds"], tasks))
    if not plan:
        raise SystemExit("nothing to evaluate")

    integrity = {}
    for name, pkg, *_ in plan:
        if not pkg.exists():
            raise SystemExit(f"{name}: {pkg} does not exist (did the build phase run?)")
        integrity[name] = integrity_check(name, pkg, heldout_ids)
    (OUT / f"integrity{'_spotcheck' if args.spotcheck else ''}.json").write_text(json.dumps(integrity, indent=2))
    for name, hits in integrity.items():
        if hits:
            print(f"WARNING {name}: held-out exercise names found in harness source: {hits}")

    if args.fresh:
        out_file.unlink(missing_ok=True)
        for name, *_ in plan:
            shutil.rmtree(OUT / "work" / name, ignore_errors=True)
            for f in (ROOT / "results" / "usage").glob(f"eval-{name}-*.jsonl"):
                f.unlink()
    done = done_keys(out_file)
    jobs = []
    key_names = sorted({f"eval-{name}-{alias}" for name, _, ms, _, _ in plan for alias in ms})
    keys = eval_keys(key_names)
    images = {}
    for name, pkg, ms, seeds, ts in plan:
        images[name] = build_harness_image(name, pkg)
        for alias, model_id in ms.items():
            for seed in range(seeds):
                for t in ts:
                    if (name, alias, seed, t.name) in done:
                        continue
                    jobs.append({"harness": name, "model": alias, "model_id": model_id, "seed": seed,
                                 "task": t.name, "lang": t.name.split("__")[0], "task_dir": str(t),
                                 "image": images[name], "key": keys[f"eval-{name}-{alias}"]})
    random.Random(7).shuffle(jobs)
    total = len(jobs) + len(done)
    est = len(jobs) * EST_TOKENS_PER_ATTEMPT * 1.5
    print(f"{len(jobs)} jobs to run ({len(done)} already done, {total} total) on {len(tasks)} tasks; "
          f"rough estimate {est / 1e6:.1f}M tokens")
    if not jobs:
        return
    if not args.yes and input("proceed? [y/N] ").strip().lower() != "y":
        return

    ensure_proxy()
    t0 = time.time()
    n_ok = 0
    with cf.ThreadPoolExecutor(cfg["jobs"]) as ex:
        futs = [ex.submit(run_job, j, cfg, out_file) for j in jobs]
        for i, fut in enumerate(cf.as_completed(futs), 1):
            try:
                r = fut.result()
            except Exception as e:  # keep going, the job will be retried on the next run
                print(f"[{i}/{len(jobs)}] ERROR {e!r}", flush=True)
                continue
            n_ok += r["passed"]
            print(f"[{i}/{len(jobs)}] {'PASS' if r['passed'] else 'fail'} {r['harness']:>14s} {r['model']:>6s} "
                  f"s{r['seed']} {r['task']}  ({n_ok}/{i} pass, {time.time() - t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
