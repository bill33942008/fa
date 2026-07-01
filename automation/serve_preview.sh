#!/usr/bin/env bash
# Start the interactive preview server (generate / approve / download APIs).
set -euo pipefail

FA_ROOT="${FA_ROOT:-/opt/fa}"
HOST="${PREVIEW_HOST:-0.0.0.0}"
PORT="${PREVIEW_PORT:-8787}"

cd "$FA_ROOT"

if [[ -f /etc/profile.d/content_ops_env.sh ]]; then
  # shellcheck disable=SC1091
  source /etc/profile.d/content_ops_env.sh
fi

PYTHON="$FA_ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi

exec "$PYTHON" automation/pipeline.py \
  --config automation/config.json \
  serve-review \
  --host "$HOST" \
  --port "$PORT"
