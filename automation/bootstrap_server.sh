#!/usr/bin/env bash
# One-shot server repair: restore config, update pipeline, run cloud media renders.
set -euo pipefail

FA_ROOT="${FA_ROOT:-/opt/fa}"
BRANCH="${BRANCH:-cursor/multi-platform-auto-ops-2a43}"
RAW_BASE="https://raw.githubusercontent.com/bill33942008/fa/refs/heads/${BRANCH}"
ILLUSTRATION_ID="${ILLUSTRATION_ID:-99d3165d18a2}"
BOOTSTRAP_VERSION="4"
TARGET_SCRIPT="$FA_ROOT/automation/bootstrap_server.sh"

# curl | bash runs a stale in-memory copy. Always download latest and re-exec from disk.
if [[ "${BOOTSTRAP_REEXEC:-}" != "1" ]]; then
  mkdir -p "$FA_ROOT/automation"
  curl -fsSL "${RAW_BASE}/automation/bootstrap_server.sh" -o "$TARGET_SCRIPT"
  chmod +x "$TARGET_SCRIPT"
  export BOOTSTRAP_REEXEC=1
  exec "$TARGET_SCRIPT" "$@"
fi

cd "$FA_ROOT"
echo "[*] bootstrap v${BOOTSTRAP_VERSION} in $FA_ROOT"

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

normalize_config() {
  "$PYTHON" - <<'PY'
import json
from pathlib import Path

path = Path("automation/config.json")
data = json.loads(path.read_text(encoding="utf-8"))
llm = data.setdefault("llm", {})
llm["enabled"] = True
llm["provider"] = "openai-compatible"
llm["api_key_env"] = "DEEPSEEK_API_KEY"
llm["base_url"] = "https://api.deepseek.com/chat/completions"
llm["model"] = "deepseek-chat"
sample = data.setdefault("sample_video", {})
sample["enabled"] = True
sample["tts_engine"] = "edge-tts"
sample["tts_binary"] = "/opt/fa/.venv/bin/edge-tts"
cloud = data.setdefault("cloud_media", {})
cloud["enabled"] = True
cloud.setdefault("image", {})["enabled"] = True
cloud.setdefault("image", {})["allow_placeholder_fallback"] = True
cloud.setdefault("video", {})["enabled"] = True
cloud.setdefault("video", {})["allow_server_fallback"] = True
data.setdefault("local_gpu", {})["enabled"] = False
path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
print("[OK] normalized config for cloud media fallbacks")
PY
}

update_code() {
  curl -fsSL "${RAW_BASE}/automation/pipeline.py" -o automation/pipeline.py
  curl -fsSL "${RAW_BASE}/automation/config.server.json" -o automation/config.server.json
  echo "[OK] updated pipeline.py and config.server.json"
}

create_venv() {
  echo "[*] creating virtualenv at $FA_ROOT/.venv"
  if ! python3 -m venv "$FA_ROOT/.venv" 2>/dev/null; then
    echo "[*] installing python3-venv..."
    apt-get update -qq && apt-get install -y python3-venv python3-full
    python3 -m venv "$FA_ROOT/.venv"
  fi
}

venv_is_healthy() {
  local python_bin="$FA_ROOT/.venv/bin/python"
  [[ -x "$python_bin" ]] || return 1
  local prefix
  prefix="$("$python_bin" -c 'import sys; print(sys.prefix)' 2>/dev/null || true)"
  [[ "$prefix" == "$FA_ROOT/.venv" ]]
}

ensure_python() {
  PYTHON="$FA_ROOT/.venv/bin/python"

  if ! venv_is_healthy; then
    echo "[WARN] repairing broken virtualenv"
    rm -rf "$FA_ROOT/.venv"
    create_venv
  fi

  "$PYTHON" -m pip install -q --upgrade pip
  echo "[OK] using $PYTHON"
}

install_deps() {
  ensure_python

  if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "[*] installing ffmpeg..."
    apt-get update -qq && apt-get install -y ffmpeg
  fi

  if ! "$PYTHON" -c "import edge_tts" >/dev/null 2>&1; then
    echo "[*] installing edge-tts into .venv..."
    if ! "$PYTHON" -m pip install -q edge-tts; then
      echo "[WARN] edge-tts install failed, recreating venv and retrying"
      rm -rf "$FA_ROOT/.venv"
      create_venv
      PYTHON="$FA_ROOT/.venv/bin/python"
      "$PYTHON" -m pip install -q --upgrade pip
      "$PYTHON" -m pip install -q edge-tts
    fi
  fi
}

run_renders() {
  ensure_python

  if [[ -f /etc/profile.d/content_ops_env.sh ]]; then
    # shellcheck disable=SC1091
    source /etc/profile.d/content_ops_env.sh
  fi

  "$PYTHON" automation/pipeline.py --config automation/config.json \
    render-illustrations --id "$ILLUSTRATION_ID" --sync-feishu

  "$PYTHON" automation/pipeline.py --config automation/config.json \
    render-cloud-videos --limit 1 --only-pending --sync-feishu
}

restore_config
update_code
install_deps
normalize_config
run_renders

echo "[DONE] bootstrap complete"
