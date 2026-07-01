#!/usr/bin/env bash
set -euo pipefail

FA_ROOT="${FA_ROOT:-/opt/fa}"
WINDOW_MINUTES="${REMINDER_WINDOW_MINUTES:-20}"
LOCK_FILE="/tmp/fa_publish_reminders.lock"

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
    echo "$(date '+%F %T') [WARN] reminder job already running" >> "$FA_ROOT/automation/publish_reminders.log"
    exit 0
  }
  "$PYTHON" automation/pipeline.py --config automation/config.json publish-reminders --window-minutes "$WINDOW_MINUTES"
) 9>"$LOCK_FILE"
