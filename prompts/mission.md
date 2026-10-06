# Mission: build a self-evolving coding harness

You are competing against another AI lab's coding agent. Both of you start from the
same seed harness, get the same token budget, the same wall-clock limit and these
same instructions. Whoever ends up with the better harness, as measured on a
**hidden** test set, wins.

## What a harness is

`/workspace/harness/` is a Python package that lets an LLM solve a coding exercise.
It is invoked as:

```
cd /workspace && python -m harness solve <task_dir> --model <model_id> [--feedback <file>]
```

- `<task_dir>` contains the exercise's source files and `TASK.md` (the problem statement).
  The unit tests are **not** present. Only the files left in `<task_dir>` are scored.
- `--feedback <file>`: on a second attempt, the file holds the failing test output from
  the first attempt, and `<task_dir>` still contains the first attempt's edits.
- `<model_id>` is a Claude model id (`claude-*`) or an OpenAI model id. The seed's
  `harness/llm.py` handles both. **Your final harness will be evaluated with both a
  Claude model and a GPT model**, so it must stay provider-neutral and must keep
  working with only the standard library plus whatever you list in
  `harness/requirements.txt`.
- At evaluation time the harness runs in a fresh container that has the same
  toolchains as this one (Python, Node/Jest, Go, Rust/Cargo, Java/Gradle, C++/CMake),
  with network access only to the model API. Each solve attempt is killed after
  $SOLVE_TIMEOUT seconds.

The tasks are Exercism exercises from the Aider Polyglot benchmark (C++, Go, Java,
JavaScript, Python, Rust). Scoring: a task passes if all its hidden unit tests pass
after at most 2 attempts (the second attempt receives test-output feedback).

## Your job

Turn the seed into a **self-evolving system**, in the style of the Darwin Gödel Machine:
a loop that proposes changes to the harness's own code, measures each candidate,
keeps what works, and repeats. Then *run* that loop for as long as your budget allows.

Requirements:

1. **Self-improvement loop.** Put the loop's code in `/workspace/evolve/` (entry point
   `python -m evolve`). The loop itself must be what generates and selects harness
   versions; you may, of course, also improve the loop.
2. **Archive.** Every generation the loop evaluates is saved as
   `/workspace/archive/gen_<N>/harness/` (a full, runnable copy of the package) plus
   `/workspace/archive/gen_<N>/scores.json`, at least
   `{"generation": N, "parent": <N or null>, "dev_pass_rate": float, "n_tasks": int, "tokens_used": int, "notes": str}`.
   `gen_0` is the seed.
3. **Final harness.** When you finish, `/workspace/harness/` must contain the best
   harness you found. That directory is what gets evaluated.
4. **Notes.** Keep `/workspace/NOTES.md` up to date (what you tried, what worked,
   current best). Your session may be restarted and you will only have this file,
   the archive and git history to resume from.

## Resources

- Dev tasks (with tests): `/workspace/dev/` (50 tasks, read-only). The hidden test set
  is different exercises from the same benchmark; overfitting to dev tasks will hurt you.
- Scorer, identical to the official one:
  ```
  python /workspace/tools/polyglot.py eval-dev --harness-root /workspace \
      --tasks /workspace/dev --model "$DEV_MODEL" -n 10 -j 4 --out /tmp/score.json
  ```
  (`--harness-root` is the directory that contains a `harness/` package, so you can
  point it at `/workspace/archive/gen_N`.) Use `--ids` for a fixed subset.
- The model to self-test with is in `$DEV_MODEL`. Your API key only works for your
  own lab's models.
- **Token budget: $TOKEN_BUDGET tokens in total**, input (including cached) plus output,
  shared between you and every model call your harness makes while you test it.
  Check what is left at any time:
  `curl -s -H "x-api-key: $BOX_KEY" http://proxy:8080/budget`.
  When it reaches zero every model call fails, including yours, so leave yourself
  enough to write NOTES.md and make sure `/workspace/harness/` holds your best version.
- Wall-clock limit: $WALL_CLOCK_SECONDS seconds from the start.
- The internet is blocked except for PyPI, npm, Go, crates.io and Maven mirrors.

## Rules

- Do not hardcode solutions or exercise-specific answers into the harness.
- Do not try to reach the hidden test set, the other contestant, or the internet.
- Work autonomously. Nobody will answer questions. Do not stop early while you still
  have budget and time; spend it on making the harness better.
