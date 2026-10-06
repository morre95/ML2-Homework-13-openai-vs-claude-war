#!/usr/bin/env bash
# Build phase: start claude-box and codex-box at the same moment, wait for both,
# then freeze their final harnesses.
#   ./run_build.sh [--fresh]     --fresh wipes previous build results and usage
#   BOXES=codex ./run_build.sh   run a single box (smoke tests only; not a fair match)
set -euo pipefail
cd "$(dirname "$0")"

FRESH=0
[ "${1:-}" = "--fresh" ] && FRESH=1

set -a
[ -f .env ] && . ./.env
set +a
: "${BUILD_TOKEN_BUDGET:=2000000}" "${WALL_CLOCK_SECONDS:=21600}" "${SOLVE_TIMEOUT:=900}" "${BOXES:=claude codex}"
export BUILD_TOKEN_BUDGET WALL_CLOCK_SECONDS SOLVE_TIMEOUT
for box in $BOXES; do
  case $box in
    claude) [ -n "${ANTHROPIC_API_KEY:-}" ] || { echo "ANTHROPIC_API_KEY is not set (.env or environment)"; exit 1; } ;;
    codex)  [ -n "${OPENAI_API_KEY:-}" ]    || { echo "OPENAI_API_KEY is not set (.env or environment)"; exit 1; } ;;
    *) echo "unknown box $box"; exit 1 ;;
  esac
done

[ -d data/tasks/dev ] || uv run bench/prepare.py
docker image inspect shootout/base:latest >/dev/null 2>&1 || scripts/build_images.sh

uv run scripts/make_keys.py build
set -a; . ./.box.env; set +a

SERVICES=""
for box in $BOXES; do
  SERVICES="$SERVICES $box-box"
  dir=results/build/$box
  if [ -d "$dir" ]; then
    if [ "$FRESH" = 1 ]; then
      rm -rf "$dir" "results/usage/${box}-box.jsonl"
    else
      echo "$dir exists. Re-run with --fresh to start over."; exit 1
    fi
  fi
  ws=$dir/workspace
  mkdir -p "$ws/tools" "$ws/archive/gen_0" "$ws/dev" results/usage
  cp -r seed/harness "$ws/harness"
  cp -r seed/harness "$ws/archive/gen_0/harness"
  echo '{"generation": 0, "parent": null, "dev_pass_rate": null, "n_tasks": 0, "tokens_used": 0, "notes": "seed"}' \
    > "$ws/archive/gen_0/scores.json"
  cp bench/polyglot.py "$ws/tools/polyglot.py"
  uv run python - "$ws/MISSION.md" <<'EOF'
import os, sys, string
src = open("prompts/mission.md").read()
keep = {k: os.environ[k] for k in ("WALL_CLOCK_SECONDS", "SOLVE_TIMEOUT")}
keep["TOKEN_BUDGET"] = f"{int(os.environ['BUILD_TOKEN_BUDGET']):,}"
open(sys.argv[1], "w").write(string.Template(src).safe_substitute(keep))
EOF
done

# Restart the proxy so its in-memory counters match the (possibly wiped) usage logs.
docker compose up -d --force-recreate proxy
for _ in $(seq 30); do curl -sf "http://127.0.0.1:${PROXY_PORT:-8080}/health" >/dev/null && break; sleep 1; done

echo "starting:$SERVICES  budget ${BUILD_TOKEN_BUDGET} tokens each, wall clock ${WALL_CLOCK_SECONDS}s"
date -u +%FT%TZ > results/build/started_at
# shellcheck disable=SC2086
docker compose --profile build up -d --no-deps --force-recreate $SERVICES
# shellcheck disable=SC2086
docker compose --profile build logs -f $SERVICES &
LOGS_PID=$!
# shellcheck disable=SC2086
docker compose --profile build wait $SERVICES || true
kill "$LOGS_PID" 2>/dev/null || true
date -u +%FT%TZ > results/build/finished_at

for box in $BOXES; do
  rm -rf "results/build/$box/final_harness"
  cp -r "results/build/$box/workspace/harness" "results/build/$box/final_harness"
done
uv run scripts/build_summary.py
