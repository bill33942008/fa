#!/usr/bin/env bash
set -euo pipefail

FA_ROOT="${FA_ROOT:-/opt/fa}"
COUNT="${DAILY_GENERATE_COUNT:-2}"
LOCK_FILE="/tmp/fa_daily_generate.lock"
SUMMARY_LOG="$FA_ROOT/automation/daily_generate_summary.log"
DETAIL_LOG="$FA_ROOT/automation/daily_generate.log"

cd "$FA_ROOT"

if [[ -f /etc/profile.d/content_ops_env.sh ]]; then
  # shellcheck disable=SC1091
  source /etc/profile.d/content_ops_env.sh
fi

PYTHON="$FA_ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi

(
  flock -n 9 || {
    echo "$(date '+%F %T') [WARN] daily generation already running" | tee -a "$SUMMARY_LOG"
    exit 0
  }
  START_TS="$(date '+%F %T')"
  BEFORE_FILE="$(mktemp)"
  AFTER_FILE="$(mktemp)"
  trap 'rm -f "$BEFORE_FILE" "$AFTER_FILE"' EXIT

  "$PYTHON" - <<'PY' > "$BEFORE_FILE"
import json
from pathlib import Path
path = Path("automation/state/publish_queue.json")
queue = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
print(len(queue))
PY

  echo "$START_TS [START] daily generate count=$COUNT" | tee -a "$SUMMARY_LOG" "$DETAIL_LOG"
  if "$PYTHON" automation/pipeline.py --config automation/config.json generate-account --count "$COUNT" >> "$DETAIL_LOG" 2>&1; then
    STATUS="SUCCESS"
  else
    STATUS="FAILED"
  fi

  "$PYTHON" - <<'PY' > "$AFTER_FILE"
import collections, json
from pathlib import Path
path = Path("automation/state/publish_queue.json")
queue = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
today = max([str(item.get("date", "")) for item in queue] or [""])
items = [item for item in queue if item.get("date") == today]
counts = collections.Counter(item.get("account_name", "") for item in items)
print(len(queue))
print(today)
for account, count in sorted(counts.items()):
    print(f"{account}: {count}")
PY

  BEFORE_COUNT="$(sed -n '1p' "$BEFORE_FILE")"
  AFTER_COUNT="$(sed -n '1p' "$AFTER_FILE")"
  TODAY="$(sed -n '2p' "$AFTER_FILE")"
  ADDED=$((AFTER_COUNT - BEFORE_COUNT))
  {
    echo "$(date '+%F %T') [$STATUS] daily generate finished date=$TODAY added=$ADDED total=$AFTER_COUNT"
    sed '1,2d' "$AFTER_FILE" | sed 's/^/  - /'
  } | tee -a "$SUMMARY_LOG"
  [[ "$STATUS" == "SUCCESS" ]]
) 9>"$LOCK_FILE"
