#!/bin/bash
# Run main.py against one practice trial's local env server, then score it
# with the kit's official scorer. Usage: ./test_trial.sh <trial> [port]
#   trial: 0, 1 or 2. Runs code-only (no gateway) unless the gateway env
#   vars (ANTHROPIC_BASE_URL/OPENAI_BASE_URL + keys) are exported first.
set -u
TRIAL="${1:?trial number 0/1/2}"
PORT="${2:-$((18100 + TRIAL))}"
DIR="$(cd "$(dirname "$0")" && pwd)"
KIT="$DIR/photo-geolocator-01-kit"
STATE="/tmp/geotest/state$TRIAL"
SCORES="/tmp/geotest/scores$TRIAL"
rm -rf "$STATE" "$SCORES"
mkdir -p "$STATE" "$SCORES"

echo "== starting env server for trial-$TRIAL on port $PORT =="
MAKEFUN_ENV_DATA_DIR="$KIT/practice/trial-$TRIAL/input/env" \
  MAKEFUN_STATE_DIR="$STATE" \
  MAKEFUN_ENV_PORT="$PORT" \
  node "$KIT/program/env/server.mjs" > "/tmp/geotest/env$TRIAL.log" 2>&1 &
SERVER_PID=$!
sleep 2
if ! curl -s "http://127.0.0.1:$PORT/photos" > /dev/null; then
  echo "env server did not come up"; cat "/tmp/geotest/env$TRIAL.log"
  kill -TERM "$SERVER_PID" 2>/dev/null; exit 1
fi

echo "== running agent =="
MAKEFUN_ENV_URL="http://127.0.0.1:$PORT" \
  MAKEFUN_PROMPT_FILE="$KIT/practice/trial-$TRIAL/input/prompt.txt" \
  MAKEFUN_TRIAL_SEED="trial-$TRIAL" \
  python3 -P -s "$DIR/main.py"
AGENT_EXIT=$?
echo "agent exit=$AGENT_EXIT"

echo "== stopping env server (exports state.json) =="
kill -TERM "$SERVER_PID"
sleep 2
if [ ! -f "$STATE/export/state.json" ]; then
  echo "state.json was not exported"; exit 1
fi

echo "== scoring with the official scorer =="
MAKEFUN_OUTPUT_DIR="$STATE/export" \
  MAKEFUN_REFERENCE_DIR="$KIT/practice/trial-$TRIAL/reference" \
  MAKEFUN_SCORES_DIR="$SCORES" \
  node "$KIT/program/scorer/score.mjs"
echo "== scores.json =="
cat "$SCORES/scores.json"
