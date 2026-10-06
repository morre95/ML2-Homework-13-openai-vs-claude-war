#!/usr/bin/env bash
# Builder loop, identical for both boxes. Starts a fresh CLI session repeatedly
# until the token budget (enforced by the proxy) or the wall-clock cap runs out.
# Continuity between sessions is the agent's job (MISSION.md asks for NOTES.md).
set -uo pipefail

cd /workspace
LOGS=/workspace/.build_logs
mkdir -p "$LOGS"
WALL=${WALL_CLOCK_SECONDS:-21600}
MAX_SESSIONS=${MAX_SESSIONS:-30}
MIN_REMAINING=${MIN_REMAINING_TOKENS:-20000}
START=$(date +%s)
END=$((START + WALL))

budget_json() { curl -s --noproxy proxy -H "x-api-key: ${BOX_KEY}" http://proxy:8080/budget; }

if [ ! -d .git ]; then
  git init -q && git add -A && git commit -qm "seed" || true
fi

session=0
while :; do
  b=$(budget_json)
  rem=$(echo "$b" | jq -r '.remaining // 0')
  now=$(date +%s)
  left=$((END - now))
  echo "[box_entry] session=$session remaining_tokens=$rem time_left=${left}s" | tee -a "$LOGS/loop.log"
  if [ "$rem" -le "$MIN_REMAINING" ] || [ "$left" -le 60 ] || [ "$session" -ge "$MAX_SESSIONS" ]; then
    break
  fi

  if [ "$session" -eq 0 ]; then
    PROMPT="$(cat /workspace/MISSION.md)"
  else
    PROMPT="You are resuming the mission described in /workspace/MISSION.md (read it again in full).
Your previous session ended. Read /workspace/NOTES.md and the archive to see where you left off, then keep going.
Token budget remaining: ${rem}. Wall-clock time remaining: ${left} seconds."
  fi

  case "$BOX" in
    claude)
      timeout "$left" claude -p "$PROMPT" \
        --dangerously-skip-permissions --output-format stream-json --verbose \
        ${BUILDER_MODEL:+--model "$BUILDER_MODEL"} \
        < /dev/null > "$LOGS/session_${session}.jsonl" 2> "$LOGS/session_${session}.err"
      ;;
    codex)
      timeout "$left" codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox --json \
        ${BUILDER_MODEL:+-m "$BUILDER_MODEL"} "$PROMPT" \
        < /dev/null > "$LOGS/session_${session}.jsonl" 2> "$LOGS/session_${session}.err"
      ;;
    *) echo "unknown BOX=$BOX"; exit 2 ;;
  esac
  rc=$?
  dur=$(( $(date +%s) - now ))
  echo "[box_entry] session=$session exit=$rc duration=${dur}s" | tee -a "$LOGS/loop.log"
  if [ "$dur" -lt 30 ]; then
    fast_fail=$(( ${fast_fail:-0} + 1 ))
    [ "$fast_fail" -ge 3 ] && { echo "[box_entry] 3 sessions in a row ended in <30s, stopping" | tee -a "$LOGS/loop.log"; break; }
  else
    fast_fail=0
  fi
  git add -A >/dev/null 2>&1 && git commit -qm "after session $session" >/dev/null 2>&1 || true
  session=$((session + 1))
done

budget_json > "$LOGS/final_budget.json"
echo "{\"sessions\": $session, \"wall_seconds\": $(( $(date +%s) - START ))}" > "$LOGS/summary.json"
echo "[box_entry] done" | tee -a "$LOGS/loop.log"
