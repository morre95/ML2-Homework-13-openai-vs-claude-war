"""Turn eval results into results/report.md, results/eval/summary.json and plots.

  uv run eval/analyze.py
"""

from __future__ import annotations

import json
import math
import random
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "results"
EVAL = RES / "eval"
PLOTS = RES / "plots"
B = 5000
CONTESTANTS = ("claude", "codex")


def read_jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def per_task(rows: list[dict], harness: str, model: str, key: str = "passed") -> dict[str, float]:
    """task -> mean pass over seeds."""
    acc: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        if r["harness"] == harness and r["model"] == model:
            acc[r["task"]].append(int(r[key]))
    return {t: sum(v) / len(v) for t, v in acc.items()}


def bootstrap(fn, tasks: list[str], seed: int = 0) -> tuple[float, float]:
    rng = random.Random(seed)
    vals = sorted(fn([rng.choice(tasks) for _ in tasks]) for _ in range(B))
    return vals[int(0.025 * B)], vals[int(0.975 * B) - 1]


def mcnemar(rows: list[dict], a: str, b: str) -> dict:
    """Exact McNemar on outcomes paired by (model, seed, task)."""
    idx = {(r["harness"], r["model"], r["seed"], r["task"]): r["passed"] for r in rows}
    only_a = only_b = both = neither = 0
    for (h, m, s, t), pa in idx.items():
        if h != a or (b, m, s, t) not in idx:
            continue
        pb = idx[(b, m, s, t)]
        only_a += pa and not pb
        only_b += pb and not pa
        both += pa and pb
        neither += not pa and not pb
    n = only_a + only_b
    k = min(only_a, only_b)
    p = 1.0 if n == 0 else min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)
    return {"a": a, "b": b, "only_a": only_a, "only_b": only_b, "both": both, "neither": neither, "p_value": p}


def usage_total(name: str) -> dict:
    rows = read_jsonl(RES / "usage" / f"{name}.jsonl")
    return {"billed": sum(r.get("billed", 0) for r in rows), "requests": len(rows)}


def main() -> None:
    rows = read_jsonl(EVAL / "runs.jsonl")
    if not rows:
        raise SystemExit("no results/eval/runs.jsonl yet; run eval/run_eval.py first")
    PLOTS.mkdir(parents=True, exist_ok=True)
    harnesses = sorted({r["harness"] for r in rows}, key=lambda h: (h != "seed", h))
    models = sorted({r["model"] for r in rows})
    model_ids = {r["model"]: r["model_id"] for r in rows}

    cells: dict = {}
    for h in harnesses:
        for m in models:
            pt = per_task(rows, h, m)
            if not pt:
                continue
            tasks = sorted(pt)
            rate = sum(pt.values()) / len(pt)
            lo, hi = bootstrap(lambda s: sum(pt[t] for t in s) / len(s), tasks)
            a1 = per_task(rows, h, m, "pass_attempt1")
            usage = usage_total(f"eval-{h}-{m}")
            solved = sum(r["passed"] for r in rows if r["harness"] == h and r["model"] == m)
            cells[(h, m)] = {"pass_rate": rate, "ci95": [lo, hi], "n_tasks": len(pt),
                             "n_runs": sum(1 for r in rows if r["harness"] == h and r["model"] == m),
                             "pass_rate_attempt1": sum(a1.values()) / len(a1),
                             "eval_tokens": usage["billed"],
                             "tokens_per_solved": usage["billed"] / solved if solved else None}

    # headline: average over models, CI by bootstrapping tasks common to all models
    headline = {}
    for h in harnesses:
        pts = {m: per_task(rows, h, m) for m in models if (h, m) in cells}
        common = sorted(set.intersection(*(set(p) for p in pts.values()))) if pts else []
        if not common:
            continue
        f = lambda s, pts=pts: sum(sum(p[t] for t in s) / len(s) for p in pts.values()) / len(pts)
        headline[h] = {"pass_rate": f(common), "ci95": list(bootstrap(f, common)), "models": list(pts)}

    deltas = {}
    if "seed" in harnesses:
        for h in harnesses:
            if h == "seed":
                continue
            for m in models:
                a, s = per_task(rows, h, m), per_task(rows, "seed", m)
                common = sorted(set(a) & set(s))
                if not common:
                    continue
                f = lambda t, a=a, s=s: sum(a[x] - s[x] for x in t) / len(t)
                deltas[(h, m)] = {"delta": f(common), "ci95": list(bootstrap(f, common)),
                                  "relative": (f(common) / (sum(s[x] for x in common) / len(common)))
                                  if sum(s[x] for x in common) else None}

    tests = []
    pairs = [("claude", "codex")] + [(h, "seed") for h in CONTESTANTS]
    for a, b in pairs:
        if a in harnesses and b in harnesses:
            tests.append(mcnemar(rows, a, b))

    langs = sorted({r["lang"] for r in rows})
    by_lang = {h: {l: (sum(r["passed"] for r in rows if r["harness"] == h and r["lang"] == l)
                       / max(1, sum(1 for r in rows if r["harness"] == h and r["lang"] == l)))
                   for l in langs} for h in harnesses}

    build = json.loads((RES / "build" / "summary.json").read_text()) if (RES / "build" / "summary.json").exists() else {}
    spot = read_jsonl(EVAL / "spotcheck.jsonl")
    spot_rates = defaultdict(dict)
    for name in sorted({r["harness"] for r in spot}):
        box, gen = name.split("@")
        rs = [r for r in spot if r["harness"] == name]
        spot_rates[box][int(gen.split("_")[1])] = sum(r["passed"] for r in rs) / len(rs)
    integrity = json.loads((EVAL / "integrity.json").read_text()) if (EVAL / "integrity.json").exists() else {}

    # ---------------------------------------------------------------- plots
    fig, ax = plt.subplots(figsize=(8, 4.5))
    w = 0.8 / max(1, len(models))
    for j, m in enumerate(models):
        xs, ys, errs = [], [], [[], []]
        for i, h in enumerate(harnesses):
            c = cells.get((h, m))
            if not c:
                continue
            xs.append(i + (j - (len(models) - 1) / 2) * w)
            ys.append(100 * c["pass_rate"])
            errs[0].append(100 * (c["pass_rate"] - c["ci95"][0]))
            errs[1].append(100 * (c["ci95"][1] - c["pass_rate"]))
        ax.bar(xs, ys, w, yerr=errs, capsize=3, label=f"{m} ({model_ids[m]})")
    ax.set_xticks(range(len(harnesses)), [f"{h} harness" for h in harnesses])
    ax.set_ylabel("held-out pass rate (%)")
    ax.set_title("Polyglot held-out pass rate, harness x model (95% bootstrap CI)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(PLOTS / "pass_rates.png", dpi=130)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 4.5))
    plotted = False
    for box in CONTESTANTS:
        gens = [g for g in build.get(box, {}).get("archive", [])
                if isinstance(g.get("generation"), int) and isinstance(g.get("dev_pass_rate"), (int, float))]
        if gens:
            gens.sort(key=lambda g: g["generation"])
            ax.plot([g["generation"] for g in gens], [100 * g["dev_pass_rate"] for g in gens], "o-",
                    label=f"{box}: self-reported dev")
            plotted = True
        if spot_rates.get(box):
            g = sorted(spot_rates[box])
            ax.plot(g, [100 * spot_rates[box][x] for x in g], "s--", label=f"{box}: held-out spot check")
            plotted = True
    if plotted:
        ax.set_xlabel("generation")
        ax.set_ylabel("pass rate (%)")
        ax.set_title("Evolution curve")
        ax.legend()
        fig.tight_layout()
        fig.savefig(PLOTS / "evolution.png", dpi=130)
    plt.close(fig)

    # ---------------------------------------------------------------- report
    pct = lambda x: f"{100 * x:.1f}%"
    ci = lambda c: f"[{pct(c[0])}, {pct(c[1])}]"
    L = ["# Self-evolving harness showdown: results", ""]
    contenders = [h for h in CONTESTANTS if h in headline]
    if len(contenders) == 2:
        a, b = sorted(contenders, key=lambda h: -headline[h]["pass_rate"])
        t = next((x for x in tests if {x["a"], x["b"]} == {"claude", "codex"}), None)
        sig = f"exact McNemar p = {t['p_value']:.3g}" if t else ""
        verdict = "a statistically significant win" if t and t["p_value"] < 0.05 else "not statistically significant"
        L += [f"**Winner: {a} harness**, {pct(headline[a]['pass_rate'])} vs {pct(headline[b]['pass_rate'])} "
              f"held-out pass rate averaged over both models ({sig}; {verdict}).", ""]
    L += ["## Headline: held-out pass rate averaged over models", "",
          "| Harness | Pass rate | 95% CI |", "|---|---|---|"]
    for h, v in headline.items():
        L.append(f"| {h} | {pct(v['pass_rate'])} | {ci(v['ci95'])} |")
    L += ["", "## 2x2 (plus seed baseline)", "",
          "| Harness | Model | Pass rate (2 tries) | 95% CI | Pass on 1st try | Runs | Eval tokens | Tokens / solved |",
          "|---|---|---|---|---|---|---|---|"]
    for (h, m), c in cells.items():
        tps = f"{c['tokens_per_solved']:,.0f}" if c["tokens_per_solved"] else "-"
        L.append(f"| {h} | {m} (`{model_ids[m]}`) | {pct(c['pass_rate'])} | {ci(c['ci95'])} | "
                 f"{pct(c['pass_rate_attempt1'])} | {c['n_runs']} | {c['eval_tokens']:,} | {tps} |")
    if deltas:
        L += ["", "## Improvement over the seed (DGM headline metric)", "",
              "| Harness | Model | Delta (pp) | 95% CI | Relative |", "|---|---|---|---|---|"]
        for (h, m), d in deltas.items():
            rel = f"{100 * d['relative']:+.0f}%" if d["relative"] is not None else "-"
            L.append(f"| {h} | {m} | {100 * d['delta']:+.1f} | [{100 * d['ci95'][0]:+.1f}, {100 * d['ci95'][1]:+.1f}] | {rel} |")
    if tests:
        L += ["", "## Paired significance (exact McNemar, pairs = model x seed x task)", "",
              "| A vs B | only A passes | only B passes | both | neither | p |", "|---|---|---|---|---|---|"]
        for t in tests:
            L.append(f"| {t['a']} vs {t['b']} | {t['only_a']} | {t['only_b']} | {t['both']} | {t['neither']} | {t['p_value']:.3g} |")
    L += ["", "## By language (pass rate over all models and seeds)", "",
          "| Harness | " + " | ".join(langs) + " |", "|---|" + "---|" * len(langs)]
    for h in harnesses:
        L.append(f"| {h} | " + " | ".join(pct(by_lang[h][l]) for l in langs) + " |")
    if build:
        L += ["", "## Build phase", "",
              "| Box | Tokens billed | Requests | Sessions | Wall time | Generations archived | evolve/ loop |",
              "|---|---|---|---|---|---|---|"]
        for box in CONTESTANTS:
            b = build.get(box)
            if not b:
                continue
            wall = f"{b['wall_seconds'] / 3600:.1f} h" if b.get("wall_seconds") else "-"
            L.append(f"| {box} | {b['usage']['billed']:,} | {b['usage']['requests']} | {b.get('sessions')} | {wall} | "
                     f"{b['generations']} | {'yes' if b['has_evolve_loop'] else 'no'} |")
    if spot_rates:
        L += ["", "## Evolution spot check (archived generations on a held-out slice, own-vendor model)", ""]
        for box, d in spot_rates.items():
            dev = {g["generation"]: g.get("dev_pass_rate") for g in build.get(box, {}).get("archive", [])
                   if isinstance(g.get("generation"), int)}
            for gen in sorted(d):
                dv = dev.get(gen)
                L.append(f"- {box} gen_{gen}: held-out slice {pct(d[gen])}, self-reported dev "
                         f"{pct(dv) if isinstance(dv, (int, float)) else 'n/a'}")
    flagged = {k: v for k, v in integrity.items() if v}
    L += ["", "## Integrity", "",
          ("Held-out exercise names found in harness source (inspect manually): " + json.dumps(flagged))
          if flagged else "No held-out exercise names found in any evaluated harness source."]
    L += ["", "![pass rates](plots/pass_rates.png)", ""]
    if (PLOTS / "evolution.png").exists():
        L += ["![evolution](plots/evolution.png)", ""]
    (RES / "report.md").write_text("\n".join(L))

    summary = {"headline": headline,
               "cells": {f"{h}|{m}": c for (h, m), c in cells.items()},
               "delta_vs_seed": {f"{h}|{m}": d for (h, m), d in deltas.items()},
               "mcnemar": tests, "by_lang": by_lang, "spotcheck": spot_rates, "integrity": integrity}
    (EVAL / "summary.json").write_text(json.dumps(summary, indent=2))
    print((RES / "report.md").read_text())


if __name__ == "__main__":
    main()
