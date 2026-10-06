"""Seed coding agent: one bash command per turn until the model says it is done.

Deliberately minimal (in the spirit of the DGM initial agent and mini-swe-agent),
so there is plenty of room for the builders to improve it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import llm

MAX_STEPS = 30
CMD_TIMEOUT = 60
OUTPUT_LIMIT = 10_000
DONE = "COMPLETE_TASK"

SYSTEM = f"""You are a coding agent working inside a directory of source files.
Each reply must contain exactly one ```bash code block with the next shell command to run.
You will see its output. Use commands like cat, ls, sed, python, or heredocs to inspect and edit files.
When the task is finished, reply with a bash block containing only: echo {DONE}
"""

BASH_RE = re.compile(r"```(?:bash|sh)?\s*\n(.*?)```", re.DOTALL)


def run_bash(cmd: str, cwd: Path) -> str:
    try:
        r = subprocess.run(["bash", "-c", cmd], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, errors="replace", timeout=CMD_TIMEOUT)
        out = f"[exit {r.returncode}]\n{r.stdout}"
    except subprocess.TimeoutExpired:
        out = f"[timed out after {CMD_TIMEOUT}s]"
    if len(out) > OUTPUT_LIMIT:
        out = out[: OUTPUT_LIMIT // 2] + "\n...[output truncated]...\n" + out[-OUTPUT_LIMIT // 2 :]
    return out


def solve(task_dir: Path, model: str, feedback: str | None = None) -> dict:
    task = (task_dir / "TASK.md").read_text()
    prompt = task if feedback is None else f"{task}\n\n{feedback}"
    messages = [{"role": "user", "content": prompt}]
    usage = {"input": 0, "output": 0}
    for step in range(MAX_STEPS):
        try:
            resp = llm.chat(model, SYSTEM, messages)
        except llm.BudgetExhausted:
            return {"status": "budget_exhausted", "steps": step, "usage": usage}
        for k in usage:
            usage[k] += resp["usage"][k]
        text = resp["text"]
        messages.append({"role": "assistant", "content": text})
        blocks = BASH_RE.findall(text)
        if not blocks:
            messages.append({"role": "user", "content": "Reply with exactly one ```bash block."})
            continue
        cmd = blocks[0].strip()
        if cmd == f"echo {DONE}":
            return {"status": "done", "steps": step + 1, "usage": usage}
        messages.append({"role": "user", "content": run_bash(cmd, task_dir)})
    return {"status": "max_steps", "steps": MAX_STEPS, "usage": usage}
