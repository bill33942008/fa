#!/usr/bin/env bash
set -euo pipefail

FA_ROOT="${FA_ROOT:-/opt/fa}"
COUNT="${DAILY_GENERATE_COUNT:-2}"
LOCK_FILE="/tmp/fa_daily_generate.lock"

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
    echo "[WARN] daily generation already running"
    exit 0
  }
  "$PYTHON" automation/pipeline.py --config automation/config.json generate-account --count "$COUNT"
) 9>"$LOCK_FILE"
