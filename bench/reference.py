"""Run upstream reference solutions through the scorer.

Used in two places:
  * image build (--warm): fills the offline Gradle / Cargo caches by running one
    reference solution per distinct build file, so tests can later run offline.
  * smoke test: proves every language toolchain in the image scores a correct
    solution as PASS (otherwise the benchmark would be measuring the image).

  python reference.py --bench /path/to/polyglot-benchmark [--ids a,b] [--warm] [-j 4]
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import polyglot  # noqa: E402

LANGS = ["cpp", "go", "java", "javascript", "python", "rust"]


def install_reference(ex: Path, work: Path) -> None:
    files = json.loads((ex / ".meta" / "config.json").read_text())["files"]
    lang = polyglot.load_task(ex)["lang"]
    if lang == "java":
        ref = ex / ".meta" / "src" / "reference" / "java"
        shutil.copytree(ref, work / "src" / "main" / "java", dirs_exist_ok=True)
        return
    if lang == "rust":
        shutil.copy(ex / ".meta" / "example.rs", work / "src" / "lib.rs")
        if (ex / ".meta" / "Cargo-example.toml").exists():
            shutil.copy(ex / ".meta" / "Cargo-example.toml", work / "Cargo.toml")
        return
    solutions = files["solution"]
    for e in files["example"]:
        suffix = Path(e).suffix
        target = next((s for s in solutions if Path(s).suffix == suffix), None)
        if target:
            shutil.copy(ex / e, work / target)


def check(ex: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="ref_") as tmp:
        work = Path(tmp) / ex.name
        shutil.copytree(ex, work)
        install_reference(ex, work)
        r = polyglot.run_tests(ex, work)
        return {"task": f"{ex.parts[-4]}__{ex.name}", "passed": r["passed"],
                "output": "" if r["passed"] else r["output"][-1500:]}


def exercises(bench: Path) -> list[Path]:
    return [p for lang in LANGS for p in sorted((bench / lang / "exercises" / "practice").iterdir()) if p.is_dir()]


def warm(bench: Path) -> None:
    for ex in sorted((bench / "rust" / "exercises" / "practice").iterdir()):
        for toml in (ex / "Cargo.toml", ex / ".meta" / "Cargo-example.toml"):
            if toml.exists() and "[dependencies]" in toml.read_text():
                with tempfile.TemporaryDirectory() as tmp:
                    shutil.copytree(ex, Path(tmp) / ex.name)
                    shutil.copy(toml, Path(tmp) / ex.name / "Cargo.toml")
                    subprocess.run(["cargo", "fetch"], cwd=Path(tmp) / ex.name, check=True)
    groups: dict[str, Path] = {}
    for ex in sorted((bench / "java" / "exercises" / "practice").iterdir()):
        h = hashlib.md5((ex / "build.gradle").read_bytes()).hexdigest()
        groups.setdefault(h, ex)
    for ex in groups.values():
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / ex.name
            shutil.copytree(ex, work)
            install_reference(ex, work)
            (work / "gradlew").chmod(0o755)
            subprocess.run(["./gradlew", "--no-daemon", "test"], cwd=work, check=False)
    print(f"warmed cargo + {len(groups)} gradle configurations")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", required=True)
    ap.add_argument("--ids", default="")
    ap.add_argument("--warm", action="store_true")
    ap.add_argument("-j", "--jobs", type=int, default=4)
    args = ap.parse_args()
    bench = Path(args.bench)
    if args.warm:
        warm(bench)
        return
    exs = exercises(bench)
    if args.ids:
        wanted = set(args.ids.split(","))
        exs = [e for e in exs if f"{e.parts[-4]}__{e.name}" in wanted]
    with cf.ThreadPoolExecutor(args.jobs) as pool:
        results = list(pool.map(check, exs))
    bad = [r for r in results if not r["passed"]]
    for r in bad:
        print(f"FAIL {r['task']}\n{r['output']}\n", file=sys.stderr)
    by_lang = {}
    for r in results:
        lang = r["task"].split("__")[0]
        ok, n = by_lang.get(lang, (0, 0))
        by_lang[lang] = (ok + r["passed"], n + 1)
    print(json.dumps({"passed": len(results) - len(bad), "total": len(results),
                      "by_lang": {k: f"{a}/{b}" for k, (a, b) in by_lang.items()},
                      "failed": [r["task"] for r in bad]}, indent=2))


if __name__ == "__main__":
    main()
