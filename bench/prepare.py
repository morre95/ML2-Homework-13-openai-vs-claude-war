"""Clone the pinned Aider Polyglot benchmark and build the dev / held-out splits.

Output (all under data/, gitignored):
  data/polyglot-benchmark/          pinned upstream clone
  data/tasks/dev/<lang>__<name>/     50 tasks, mounted into both build boxes
  data/tasks/heldout/<lang>__<name>/ 100 tasks, never mounted into a build box
  data/splits.json

Reference solutions (.meta/example*) are removed from every task copy.
The split is stratified by language with a fixed seed.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
from pathlib import Path

REPO = "https://github.com/Aider-AI/polyglot-benchmark"
COMMIT = "7e0611e77b54e2dea774cdc0aa00cf9f7ed6144f"
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LANGS = ["cpp", "go", "java", "javascript", "python", "rust"]


def clone(dst: Path) -> None:
    if not (dst / ".git").exists():
        subprocess.run(["git", "clone", "-q", REPO, str(dst)], check=True)
    subprocess.run(["git", "-C", str(dst), "checkout", "-q", COMMIT], check=True)


def stratified_split(by_lang: dict[str, list[str]], n_dev: int, n_heldout: int, seed: int):
    rng = random.Random(seed)
    total = sum(len(v) for v in by_lang.values())
    dev, heldout = [], []
    for lang in sorted(by_lang):
        items = sorted(by_lang[lang])
        rng.shuffle(items)
        k_dev = round(n_dev * len(items) / total)
        k_ho = round(n_heldout * len(items) / total)
        dev += items[:k_dev]
        heldout += items[k_dev : k_dev + k_ho]
    # fix rounding drift so the totals are exact
    leftovers = [t for v in by_lang.values() for t in v if t not in dev and t not in heldout]
    rng.shuffle(leftovers)
    while len(dev) > n_dev:
        leftovers.append(dev.pop())
    while len(heldout) > n_heldout:
        leftovers.append(heldout.pop())
    while len(dev) < n_dev:
        dev.append(leftovers.pop())
    while len(heldout) < n_heldout:
        heldout.append(leftovers.pop())
    return sorted(dev), sorted(heldout), sorted(leftovers)


def copy_task(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    config = json.loads((dst / ".meta" / "config.json").read_text())
    for ex in config.get("files", {}).get("example", []):
        (dst / ex).unlink(missing_ok=True)
    for p in (dst / ".meta").glob("example*"):
        if p.is_file():
            p.unlink()
        else:
            shutil.rmtree(p)
    shutil.rmtree(dst / ".meta" / "src", ignore_errors=True)  # java reference solutions live here


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dev", type=int, default=50)
    ap.add_argument("--heldout", type=int, default=100)
    ap.add_argument("--seed", type=int, default=1337)
    args = ap.parse_args()

    src = DATA / "polyglot-benchmark"
    DATA.mkdir(exist_ok=True)
    clone(src)

    by_lang = {}
    for lang in LANGS:
        practice = src / lang / "exercises" / "practice"
        by_lang[lang] = [f"{lang}__{p.name}" for p in sorted(practice.iterdir()) if p.is_dir()]
    dev, heldout, unused = stratified_split(by_lang, args.dev, args.heldout, args.seed)

    for split, ids in (("dev", dev), ("heldout", heldout)):
        out = DATA / "tasks" / split
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        for tid in ids:
            lang, name = tid.split("__", 1)
            copy_task(src / lang / "exercises" / "practice" / name, out / tid)

    (DATA / "splits.json").write_text(json.dumps(
        {"repo": REPO, "commit": COMMIT, "seed": args.seed, "dev": dev, "heldout": heldout, "unused": unused},
        indent=2))
    for split, ids in (("dev", dev), ("heldout", heldout)):
        counts = {l: sum(t.startswith(l + "__") for t in ids) for l in LANGS}
        print(f"{split:8s} {len(ids):3d}  {counts}")


if __name__ == "__main__":
    main()
