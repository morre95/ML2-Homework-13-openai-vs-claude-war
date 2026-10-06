"""Harness entry point. This CLI is the contract the evaluator relies on:

    python -m harness solve <task_dir> --model <model_id> [--feedback <file>]

<task_dir> contains the exercise files plus TASK.md (the problem statement).
Tests are hidden. --feedback points at a file with test output from a previous
failed attempt on the same task_dir. Exit code is ignored; only the files left
in <task_dir> are scored.
"""

import argparse
import json
import sys
from pathlib import Path

from .agent import solve


def main() -> None:
    ap = argparse.ArgumentParser(prog="harness")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("solve")
    s.add_argument("task_dir")
    s.add_argument("--model", required=True)
    s.add_argument("--feedback", default=None)
    args = ap.parse_args()
    feedback = Path(args.feedback).read_text() if args.feedback else None
    result = solve(Path(args.task_dir).resolve(), args.model, feedback)
    print(json.dumps(result), file=sys.stderr)


if __name__ == "__main__":
    main()
