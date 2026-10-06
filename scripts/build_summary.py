"""Summarize the build phase from the proxy usage logs and the archives."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "results"


def usage(name: str) -> dict:
    f = RES / "usage" / f"{name}.jsonl"
    rows = [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []
    tot = {k: sum(r.get(k, 0) for r in rows) for k in ("fresh", "cache_write", "cache_read", "output", "billed")}
    models: dict[str, int] = {}
    for r in rows:
        models[r.get("model") or "?"] = models.get(r.get("model") or "?", 0) + r.get("billed", 0)
    return {"requests": len(rows), **tot, "by_model": models}


def archive(ws: Path) -> list[dict]:
    gens = []
    for d in sorted((ws / "archive").glob("gen_*"), key=lambda p: int(p.name.split("_")[1]) if p.name.split("_")[1].isdigit() else 1e9):
        try:
            gens.append(json.loads((d / "scores.json").read_text()))
        except (FileNotFoundError, json.JSONDecodeError):
            gens.append({"generation": d.name, "error": "missing or invalid scores.json"})
    return gens


def main() -> None:
    summary = {}
    for box in ("claude", "codex"):
        ws = RES / "build" / box / "workspace"
        logs = ws / ".build_logs"
        s = json.loads((logs / "summary.json").read_text()) if (logs / "summary.json").exists() else {}
        gens = archive(ws)
        summary[box] = {
            "usage": usage(f"{box}-box"),
            "sessions": s.get("sessions"),
            "wall_seconds": s.get("wall_seconds"),
            "generations": len(gens),
            "has_evolve_loop": (ws / "evolve").is_dir(),
            "archive": gens,
        }
        u = summary[box]["usage"]
        print(f"{box:6s} billed={u['billed']:>10,} requests={u['requests']:>5} sessions={s.get('sessions')} "
              f"generations={len(gens)} evolve/={'yes' if summary[box]['has_evolve_loop'] else 'no'}")
    (RES / "build" / "summary.json").write_text(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
