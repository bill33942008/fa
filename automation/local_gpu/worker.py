#!/usr/bin/env python3
"""
Local GPU worker — ComfyUI only.

Requires:
1. ComfyUI running (default http://127.0.0.1:8000)
2. Exported API workflow JSON (see workflows/README.md)
3. Server exported jobs via export-video-jobs
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comfyui_client import ComfyUIClient


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def ssh_base_args(upload_cfg: dict[str, Any]) -> list[str]:
    args = ["ssh", "-p", str(int(upload_cfg.get("server_port", 22)))]
    identity = str(upload_cfg.get("ssh_identity_file", "")).strip()
    if identity:
        args.extend(["-i", identity])
    args.append(f"{upload_cfg.get('server_user', 'root')}@{upload_cfg.get('server_host', '')}")
    return args


def scp_base_args(upload_cfg: dict[str, Any]) -> list[str]:
    args = ["scp", "-P", str(int(upload_cfg.get("server_port", 22)))]
    identity = str(upload_cfg.get("ssh_identity_file", "")).strip()
    if identity:
        args.extend(["-i", identity])
    return args


def run_command(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise RuntimeError(f"Command failed: {' '.join(command)} | {detail[:500]}")
    return result


def merge_comfy_cfg(config: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    defaults = dict(config.get("comfyui", {}))
    defaults.update(job.get("comfyui", {}))
    if not defaults.get("url"):
        defaults["url"] = config.get("comfyui_url", "http://127.0.0.1:8000")
    workflow_dir = str(defaults.get("workflow_dir", "")).strip()
    if not workflow_dir:
        base = Path(config.get("jobs_dir", "video_jobs")).parent
        defaults["workflow_dir"] = str(base / "workflows")
    return defaults


def convert_to_mp4_if_needed(source: Path, target_mp4: Path) -> Path:
    if source.suffix.lower() == ".mp4":
        if source.resolve() != target_mp4.resolve():
            shutil.copy2(source, target_mp4)
        return target_mp4
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        shutil.copy2(source, target_mp4.with_suffix(source.suffix))
        return target_mp4.with_suffix(source.suffix)
    run_command(
        [
            ffmpeg,
            "-y",
            "-i",
            str(source),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(target_mp4),
        ]
    )
    return target_mp4


def pull_jobs_from_server(config: dict[str, Any], jobs_dir: Path) -> int:
    pull_cfg = config.get("pull_jobs_from_server", {})
    if not pull_cfg.get("enabled", False):
        return 0
    pending_dir = jobs_dir / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    remote = (
        f"{pull_cfg.get('server_user', 'root')}@{pull_cfg.get('server_host', '')}:"
        f"{pull_cfg.get('remote_jobs_dir', '')}/*.json"
    )
    command = scp_base_args(pull_cfg) + [remote, str(pending_dir)]
    before = {path.name for path in pending_dir.glob("*.json")}
    try:
        run_command(command)
    except RuntimeError as exc:
        print(f"[WARN] pull jobs failed: {exc}")
        return 0
    after = {path.name for path in pending_dir.glob("*.json")}
    return len(after - before)


def generate_with_comfyui(job: dict[str, Any], config: dict[str, Any]) -> Path:
    comfy_cfg = merge_comfy_cfg(config, job)
    base_url = str(comfy_cfg.get("url", "http://127.0.0.1:8000")).rstrip("/")
    client = ComfyUIClient(base_url)

    output_dir = Path(config.get("output_dir", "rendered_videos"))
    output_dir.mkdir(parents=True, exist_ok=True)
    target_mp4 = output_dir / str(job.get("output_filename", f"{job.get('job_id', 'video')}.mp4"))
    temp_output = target_mp4.with_suffix(".download")

    rendered = client.render_job(job, comfy_cfg, temp_output)
    final = convert_to_mp4_if_needed(rendered, target_mp4)
    if temp_output.exists() and temp_output != final:
        temp_output.unlink(missing_ok=True)
    return final


def upload_video_to_server(job: dict[str, Any], local_video: Path, config: dict[str, Any]) -> None:
    upload_cfg = {**config.get("upload", {}), **job.get("upload", {})}
    if not upload_cfg.get("enabled", True):
        print("[INFO] upload disabled, keeping local file only.")
        return

    remote_media_dir = str(upload_cfg.get("remote_media_dir", "")).rstrip("/")
    remote_path = f"{remote_media_dir}/{job.get('remote_media_path', local_video.name)}"
    scp_command = scp_base_args(upload_cfg) + [
        str(local_video),
        f"{upload_cfg.get('server_user', 'root')}@{upload_cfg.get('server_host', '')}:{remote_path}",
    ]
    run_command(scp_command)
    print(f"[OK] uploaded video -> {remote_path}")

    if upload_cfg.get("run_remote_import", True):
        remote_python = str(upload_cfg.get("remote_python", "python3"))
        remote_config = str(upload_cfg.get("remote_config", "/opt/fa/automation/config.json"))
        remote_cmd = (
            f"cd {upload_cfg.get('remote_pipeline_dir', '/opt/fa')} && "
            f"{remote_python} automation/pipeline.py --config {remote_config} "
            f"import-local-video --id {job.get('job_id')} --sync-feishu"
        )
        run_command(ssh_base_args(upload_cfg) + [remote_cmd])
        print(f"[OK] remote import-local-video completed for {job.get('job_id')}")


def process_job(job_file: Path, config: dict[str, Any], jobs_dir: Path) -> bool:
    job = load_json(job_file, {})
    job_id = str(job.get("job_id", job_file.stem))
    print(f"[RUN] processing job {job_id} via ComfyUI")
    try:
        local_video = generate_with_comfyui(job, config)
        job["status"] = "completed"
        job["local_video_file"] = str(local_video)
        job["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        upload_video_to_server(job, local_video, config)

        completed_dir = jobs_dir / "completed"
        completed_dir.mkdir(parents=True, exist_ok=True)
        save_json(completed_dir / f"{job_id}.json", job)
        job_file.unlink(missing_ok=True)
        print(f"[OK] job completed {job_id}")
        return True
    except Exception as exc:  # pylint: disable=broad-except
        job["status"] = "failed"
        job["error"] = str(exc)
        failed_dir = jobs_dir / "failed"
        failed_dir.mkdir(parents=True, exist_ok=True)
        save_json(failed_dir / f"{job_id}.json", job)
        job_file.unlink(missing_ok=True)
        print(f"[WARN] job failed {job_id}: {exc}")
        return False


def retry_failed_jobs(jobs_dir: Path) -> int:
    failed_dir = jobs_dir / "failed"
    pending_dir = jobs_dir / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for job_file in sorted(failed_dir.glob("*.json")):
        shutil.move(str(job_file), str(pending_dir / job_file.name))
        count += 1
    return count


def probe_services(config: dict[str, Any]) -> int:
    comfy_cfg = merge_comfy_cfg(config, {})
    url = str(comfy_cfg.get("url", "http://127.0.0.1:8000")).rstrip("/")
    print(f"[*] ComfyUI: {url}")
    try:
        stats = ComfyUIClient(url).probe()
        print(f"[OK] ComfyUI reachable: {stats}")
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[ERROR] ComfyUI not reachable: {exc}")
        return 1

    workflow_file = Path(str(comfy_cfg.get("workflow_dir", "."))) / str(
        comfy_cfg.get("workflow_file", "short_video.api.json")
    )
    if workflow_file.exists():
        print(f"[OK] Workflow file found: {workflow_file}")
    else:
        print(f"[WARN] Workflow file missing: {workflow_file}")
        print("       Export API workflow from ComfyUI and save to workflows/short_video.api.json")
        return 1
    return 0


def process_pending_jobs(config: dict[str, Any], *, retry_failed: bool = False) -> tuple[int, int]:
    jobs_dir = Path(config.get("jobs_dir", "video_jobs"))
    if retry_failed:
        moved = retry_failed_jobs(jobs_dir)
        if moved:
            print(f"[OK] moved {moved} failed job(s) back to pending")

    pending_dir = jobs_dir / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    pulled = pull_jobs_from_server(config, jobs_dir)
    if pulled:
        print(f"[OK] pulled {pulled} new job(s) from server")

    job_files = sorted(pending_dir.glob("*.json"))
    if not job_files:
        print("[INFO] no pending jobs")
        return 0, 0

    success = 0
    failed = 0
    for job_file in job_files:
        if process_job(job_file, config, jobs_dir):
            success += 1
        else:
            failed += 1
    return success, failed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local ComfyUI video worker")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent / "local_config.json"),
        help="Path to local worker config JSON",
    )
    parser.add_argument("--once", action="store_true", help="Process pending jobs once and exit")
    parser.add_argument("--probe", action="store_true", help="Check ComfyUI and workflow file")
    parser.add_argument("--retry-failed", action="store_true", help="Retry failed jobs")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    config_path = Path(args.config).expanduser()
    if not config_path.exists():
        print(f"Config not found: {config_path}")
        sys.exit(1)
    config = load_json(config_path, {})

    if args.probe:
        sys.exit(probe_services(config))

    poll_once = bool(args.once or config.get("poll_once", False))
    interval = int(config.get("poll_interval_seconds", 60))
    while True:
        success, failed = process_pending_jobs(config, retry_failed=bool(args.retry_failed))
        print(f"[DONE] worker cycle complete. success={success} failed={failed}")
        if poll_once:
            break
        args.retry_failed = False
        time.sleep(interval)


if __name__ == "__main__":
    main()
