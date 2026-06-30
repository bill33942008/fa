#!/usr/bin/env bash
# One-shot server repair: restore config, update pipeline, run cloud media renders.
set -euo pipefail

FA_ROOT="${FA_ROOT:-/opt/fa}"
BRANCH="${BRANCH:-cursor/multi-platform-auto-ops-2a43}"
RAW_BASE="https://raw.githubusercontent.com/bill33942008/fa/${BRANCH}"
ILLUSTRATION_ID="${ILLUSTRATION_ID:-99d3165d18a2}"

cd "$FA_ROOT"
echo "[*] Working in $FA_ROOT"

restore_config() {
  local target="automation/config.json"
  if [[ -f "$target" ]]; then
    echo "[OK] config.json already exists"
    return
  fi

  for backup in \
    automation/config.json.bak \
    automation/config.json.backup \
    automation/config.json~ \
    automation/config.json.old; do
    if [[ -f "$backup" ]]; then
      cp "$backup" "$target"
      echo "[OK] restored config from $backup"
      return
    fi
  done

  if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    if git stash list | grep -q .; then
      for idx in 0 1 2; do
        if git show "stash@{${idx}}:automation/config.json" > "$target" 2>/dev/null; then
          echo "[OK] restored config from git stash@{${idx}}"
          return
        fi
      done
    fi
  fi

  curl -fsSL "${RAW_BASE}/automation/config.server.json" -o "$target"
  echo "[WARN] created config.json from config.server.json"
  echo "[WARN] if Feishu sync fails, fill feishu_bitable.app_token/table_id in automation/config.json"
}

update_code() {
  mkdir -p automation/scripts
  curl -fsSL "${RAW_BASE}/automation/pipeline.py" -o automation/pipeline.py
  curl -fsSL "${RAW_BASE}/automation/config.server.json" -o automation/config.server.json
  curl -fsSL "${RAW_BASE}/automation/bootstrap_server.sh" -o automation/bootstrap_server.sh
  chmod +x automation/bootstrap_server.sh
  echo "[OK] updated pipeline.py and helper files"
}

install_deps() {
  if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "[*] installing ffmpeg..."
    apt-get update -qq && apt-get install -y ffmpeg
  fi
  if ! python3 -c "import edge_tts" >/dev/null 2>&1; then
    echo "[*] installing edge-tts..."
    python3 -m pip install -q edge-tts
  fi
}

run_renders() {
  if [[ -f /etc/profile.d/content_ops_env.sh ]]; then
    # shellcheck disable=SC1091
    source /etc/profile.d/content_ops_env.sh
  fi

  python3 automation/pipeline.py --config automation/config.json \
    render-illustrations --id "$ILLUSTRATION_ID" --sync-feishu

  python3 automation/pipeline.py --config automation/config.json \
    render-cloud-videos --limit 1 --only-pending --sync-feishu
}

restore_config
update_code
install_deps
run_renders

echo "[DONE] bootstrap complete"
