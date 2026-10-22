#!/usr/bin/env bash
# One red-team run against a LoopDesk backend started here; .github/workflows/redteam.yml calls it.
#
#   OUT=../out/redteam LOOPDESK_LLM_MODEL=... bash redteam/run.sh
#
# The backend runs the real model (the LOOPDESK_LLM_* variables of the calling job), seeded demo data,
# SQLite. Each tenant starts from a fresh database: attacks file refunds and open handoffs, and one
# tenant's run must not start from another's leftovers. Within a tenant the attacks share one database,
# the way customers share a production system (docs/EVALUATION.md, 红队测试).
#
# promptfoo exits 0 when attacks succeed (PROMPTFOO_FAILED_TEST_EXIT_CODE): a successful attack is a
# finding for the report, not a broken run. A broken config, target or grader still fails the step.
set -euo pipefail
cd "$(dirname "$0")/.."  # backend/

: "${OUT:?OUT is the output directory}"
TENANTS="${TENANTS:-leon,echo,iris}"
TESTS_PER_PLUGIN="${TESTS_PER_PLUGIN:-2}"
STRATEGIES="${STRATEGIES-jailbreak,crescendo}"
FRESH="${FRESH:-true}"
TREND="${TREND:-true}"
REPLAY_DIR="${REPLAY_DIR:-}"
MAX_CALLS="${MAX_CALLS:-1500}"
CONCURRENCY="${CONCURRENCY:-4}"
PORT="${PORT:-8000}"
PROMPTFOO="${PROMPTFOO:-promptfoo}"
DB="${DB:-$(mktemp -d)/redteam.db}"
BASE="http://127.0.0.1:$PORT"

export PROMPTFOO_FAILED_TEST_EXIT_CODE=0
export LOOPDESK_REDTEAM_BUDGET_FILE="$OUT/.calls"
export LOOPDESK_JWT_SECRET="${LOOPDESK_JWT_SECRET:-$(python -c 'import secrets; print(secrets.token_hex(32))')}"
export LOOPDESK_RATE_LIMIT_PER_MINUTE="${LOOPDESK_RATE_LIMIT_PER_MINUTE:-1000}"
mkdir -p "$OUT"
: > "$LOOPDESK_REDTEAM_BUDGET_FILE"

server_pid=""
start_server() {
  rm -f "$DB"
  LOOPDESK_DATABASE_URL="sqlite:///$DB" python -m uvicorn app.main:app --port "$PORT" --log-level warning \
    >> "$OUT/server.log" 2>&1 &
  server_pid=$!
  for _ in $(seq 1 120); do
    if curl -sf "$BASE/api/ready" > /dev/null; then return 0; fi
    sleep 1
  done
  echo "the backend did not become ready" >&2
  tail -50 "$OUT/server.log" >&2
  exit 1
}
stop_server() {
  if [ -n "$server_pid" ]; then
    kill "$server_pid" 2> /dev/null || true
    wait "$server_pid" 2> /dev/null || true
    server_pid=""
  fi
}
trap stop_server EXIT

trend_flag=""
if [ "$TREND" = true ] && [ -f redteam/trend_set.json ]; then trend_flag="--trend"; fi
python -m app.redteam.build --out "$OUT" --tenants "$TENANTS" --tests-per-plugin "$TESTS_PER_PLUGIN" \
  --strategies "$STRATEGIES" --base-url "$BASE" --max-calls "$MAX_CALLS" $trend_flag

run_pass() {  # $1: pass directory, $2: concurrency
  local dir="$OUT/$1"
  if [ -n "$REPLAY_DIR" ]; then
    if [ ! -f "$REPLAY_DIR/$1/redteam.yaml" ]; then
      echo "::warning::$1 is not in the replayed run; skipped"
      return 0
    fi
    cp "$REPLAY_DIR/$1/redteam.yaml" "$dir/redteam.yaml"
  else
    $PROMPTFOO redteam generate -c "$dir/promptfooconfig.json" -o "$dir/redteam.yaml" --force --no-progress-bar \
      > "$dir/generate.log" 2>&1 || { tail -40 "$dir/generate.log"; return 1; }
    grep -E "Wrote|Plugin tests|Strategy tests|Error|error" "$dir/generate.log" | tail -8 || true
  fi
  export LOOPDESK_REDTEAM_CALLS_LOG="$dir/calls.jsonl"  # each turn's data-state check, joined by the report
  $PROMPTFOO redteam eval -c "$dir/redteam.yaml" -o "$dir/results.json" --no-cache -j "$2" --no-progress-bar \
    > "$dir/eval.log" 2>&1 || { tail -40 "$dir/eval.log"; return 1; }
  echo "== $1"; grep -E "passed|failed|errors|Tokens|Probes|Target|Grading" "$dir/eval.log" | tail -8 || true
}

if [ "$FRESH" = true ] || [ -n "$REPLAY_DIR" ]; then
  for slug in ${TENANTS//,/ }; do
    start_server
    run_pass "$slug" "$CONCURRENCY"
    run_pass "$slug-kb" 1  # a planted document is visible to every conversation of the tenant: one at a time
    stop_server
  done
fi

if [ -n "$trend_flag" ]; then
  start_server
  export LOOPDESK_REDTEAM_CALLS_LOG="$OUT/trend/calls.jsonl"
  $PROMPTFOO eval -c "$OUT/trend/promptfooconfig.json" -o "$OUT/trend/results.json" --no-cache -j 1 \
    --no-progress-bar > "$OUT/trend/eval.log" 2>&1 || { tail -40 "$OUT/trend/eval.log"; exit 1; }
  echo "== trend"; grep -E "passed|failed|errors" "$OUT/trend/eval.log" | tail -4 || true
  stop_server
fi

echo "chat requests used: $(cat "$LOOPDESK_REDTEAM_BUDGET_FILE") of $MAX_CALLS"
