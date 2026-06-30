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
import collections, json
from pathlib import Path
path = Path("automation/state/publish_queue.json")
queue = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
today = max([str(item.get("date", "")) for item in queue] or [""])
items = [item for item in queue if item.get("date") == today]
print(json.dumps({
    "total": len(queue),
    "today": today,
    "accounts": collections.Counter(item.get("account_name", "") for item in items),
}, ensure_ascii=False))
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
print(json.dumps({
    "total": len(queue),
    "today": today,
    "accounts": counts,
}, ensure_ascii=False))
PY

  "$PYTHON" - "$BEFORE_FILE" "$AFTER_FILE" "$STATUS" <<'PY' | tee -a "$SUMMARY_LOG"
import json, sys
from datetime import datetime
before = json.loads(open(sys.argv[1], encoding="utf-8").read())
after = json.loads(open(sys.argv[2], encoding="utf-8").read())
status = sys.argv[3]
before_accounts = before.get("accounts", {})
after_accounts = after.get("accounts", {})
all_accounts = sorted(set(before_accounts) | set(after_accounts))
added = int(after.get("total", 0)) - int(before.get("total", 0))
print(f"{datetime.now().strftime('%F %T')} [{status}] daily generate finished date={after.get('today','')} added={added} total={after.get('total',0)}")
print("  本次新增：")
for account in all_accounts:
    delta = int(after_accounts.get(account, 0)) - int(before_accounts.get(account, 0))
    if delta:
        print(f"  - {account}: +{delta}")
print("  当天累计：")
for account in all_accounts:
    print(f"  - {account}: {after_accounts.get(account, 0)}")
PY
  [[ "$STATUS" == "SUCCESS" ]]
) 9>"$LOCK_FILE"
