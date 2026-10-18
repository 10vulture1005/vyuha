#!/usr/bin/env bash
#
# run_pipeline.sh — step-by-step VYUHA pipeline runner.
#
# Runs each stage in its own process so you can stop, inspect and restart
# between any two of them. Every stage prints a wall-clock timing; the run
# ends with a breakdown of where the time went.
#
#   ./scripts/run_pipeline.sh swing              # daily swing pipeline
#   ./scripts/run_pipeline.sh investor           # investor-mode pipeline
#   ./scripts/run_pipeline.sh swing sentiment    # one stage only
#   ./scripts/run_pipeline.sh swing --fast       # skip the Tier 2 LLM
#   ./scripts/run_pipeline.sh swing --list       # stages, nothing runs
#
# Stages marked [MUTATING] in --list write to the ledger and are NOT
# idempotent — re-running `sip` or `alloc` spends real ledger rows.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# This project uses a uv-managed venv; it has no ./bin/pip, so resolve the
# interpreter explicitly rather than assuming `python` is the right one.
if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  PY="$REPO_ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
else
  echo "error: no python interpreter found (expected .venv/bin/python)" >&2
  exit 1
fi

RUNNER="$REPO_ROOT/scripts/run_step_by_step.py"
[[ -f "$RUNNER" ]] || { echo "error: $RUNNER not found" >&2; exit 1; }

# Swing stages, in dependency order. `ohlc` first so the technical scan sees
# today's bars; `watchlist` because the technical scan drops entries whose
# fundamentals are older than 7 days.
# `digest` is last and picks up --dry-run via DIGEST_ARGS.
SWING_STAGES=(preflight ohlc watchlist sentiment technical risk alloc valuation digest)

# Investor stages. `news` feeds `decide`; `decide` checkpoints the order book
# that `fill` consumes, so they must not be reordered.
INVESTOR_STAGES=(preflight news decide persist digest status)

FAST=0
LIST=0
SEND_TELEGRAM=0
ONLY=""

usage() {
  # Print only the leading comment block. A fixed line range was wrong before
  # and printed a line of shell code; stop at the first non-comment instead.
  sed -n '2,/^set -/p' "${BASH_SOURCE[0]}" | sed 's/^#\{1,\} \{0,1\}//' | sed '$d'
  cat <<'EOF'

Arguments:
  swing | investor     which pipeline to run (default: swing)
  <stage-name>         run only this stage, in the given pipeline
  --fast               set SENTIMENT_TIER2_ENABLED=false (regex + FinBERT only)
  --dry-telegram       print the digest instead of sending it
  --send-telegram      actually send the digest (default: --dry-telegram)
  --list               list stages and exit
  -h | --help          this message

Examples:
  ./scripts/run_pipeline.sh swing
  ./scripts/run_pipeline.sh swing --fast --dry-telegram
  ./scripts/run_pipeline.sh swing sentiment
  STRATEGY_MODE=investor ./scripts/run_pipeline.sh investor
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    swing|investor)  PIPELINE="$1"; shift ;;
    --fast)          FAST=1; shift ;;
    --send-telegram) SEND_TELEGRAM=1; shift ;;
    --dry-telegram)  SEND_TELEGRAM=0; shift ;;
    --list|-l)       LIST=1; shift ;;
    -h|--help)       usage; exit 0 ;;
    -*)              echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    *)               ONLY="$1"; shift ;;
  esac
done

PIPELINE="${PIPELINE:-swing}"

if [[ "$LIST" -eq 1 ]]; then
  exec "$PY" "$RUNNER" --list
fi

# Investor stages refuse to run unless the profile is explicitly selected —
# same guard as `vyuha invest`. Fail early with a clear message rather than
# letting each stage abort in turn.
if [[ "$PIPELINE" == "investor" ]]; then
  current="$(grep -E '^STRATEGY_MODE=' .env 2>/dev/null | cut -d= -f2 | tr -d '"' || true)"
  if [[ "${STRATEGY_MODE:-$current}" != "investor" ]]; then
    cat >&2 <<EOF
error: investor pipeline requires STRATEGY_MODE=investor
       found: STRATEGY_MODE=${STRATEGY_MODE:-$current}

  Set it in .env, or for a single run:
      STRATEGY_MODE=investor $0 investor
EOF
    exit 2
  fi
  export STRATEGY_MODE=investor
fi

if [[ "$FAST" -eq 1 ]]; then
  export SENTIMENT_TIER2_ENABLED=false
  echo "[run] --fast: Tier 2 LLM disabled (regex + FinBERT only)"
fi

# Default to printing the digest. `digest` is a real stage now, so sending is
# opt-in via --send-telegram rather than something a plain run triggers.
if [[ "$SEND_TELEGRAM" -eq 1 ]]; then
  DIGEST_ARGS=()
  echo "[run] --send-telegram: digest WILL be sent to Telegram"
else
  DIGEST_ARGS=(--dry-run)
  echo "[run] digest will be printed, not sent (use --send-telegram to send)"
fi

if [[ -n "$ONLY" ]]; then
  echo "[run] single stage: $PIPELINE/$ONLY"
  exec "$PY" "$RUNNER" "$PIPELINE" --stage "$ONLY" "${DIGEST_ARGS[@]}"
fi

case "$PIPELINE" in
  swing)    STAGES=("${SWING_STAGES[@]}") ;;
  investor) STAGES=("${INVESTOR_STAGES[@]}") ;;
  *) echo "error: unknown pipeline '$PIPELINE' (swing|investor)" >&2; exit 2 ;;
esac

echo "[run] pipeline : $PIPELINE"
echo "[run] stages   : ${STAGES[*]}"
echo "[run] python   : $PY"
echo

TIMINGS=()

# Defined before the loop: the failure path below calls it, and bash resolves
# function names at call time, not definition order within a file this way.
print_timings() {
  [[ ${#TIMINGS[@]} -eq 0 ]] && return 0
  echo "--------------------------------------------------------------"
  echo "  PIPELINE TIMINGS"
  echo "--------------------------------------------------------------"
  local total=0 name secs
  for entry in "${TIMINGS[@]}"; do
    name="${entry%%|*}"
    secs="${entry##*|}"
    total=$(echo "$total + $secs" | bc)
    printf "  %-14s %8.2fs\n" "$name" "$secs"
  done
  printf "  %-14s %8.2fs\n" "TOTAL" "$total"
}

for stage in "${STAGES[@]}"; do
  echo "──────────────────────────────────────────────────────────────"
  echo "▶ $stage"
  echo "──────────────────────────────────────────────────────────────"
  start=$(date +%s.%N)
  if "$PY" "$RUNNER" "$PIPELINE" --stage "$stage" "${DIGEST_ARGS[@]}"; then
    elapsed=$(echo "$(date +%s.%N) - $start" | bc)
    TIMINGS+=("$stage|$elapsed")
  else
    rc=$?
    echo
    echo "[run] stage '$stage' failed with exit code $rc — stopping." >&2
    print_timings
    exit "$rc"
  fi
  echo
done

print_timings

echo
echo "[run] complete — final state:"
"$PY" "$RUNNER" "$PIPELINE" --stage inspect || true