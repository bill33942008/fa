#!/usr/bin/env python3
"""
Local GPU worker for ComfyUI + Pixelle-Video rendering.

Run on your Windows PC (RTX 4070) after:
1. ComfyUI is running (default http://127.0.0.1:8188)
2. Pixelle-Video API is running (default http://127.0.0.1:8000)
3. Server exported jobs via: export-video-jobs

Usage:
  python automation/local_gpu/worker.py --config automation/local_gpu/local_config.json
  python automation/local_gpu/worker.py --config automation/local_gpu/local_config.json --once
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def http_post_json(url: str, payload: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def http_get_json(url: str, timeout: int = 60) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def ssh_base_args(upload_cfg: dict[str, Any]) -> list[str]:
    args = [
        "ssh",
        "-p",
        str(int(upload_cfg.get("server_port", 22))),
    ]
    identity = str(upload_cfg.get("ssh_identity_file", "")).strip()
    if identity:
        args.extend(["-i", identity])
    args.append(f"{upload_cfg.get('server_user', 'root')}@{upload_cfg.get('server_host', '')}")
    return args


def scp_base_args(upload_cfg: dict[str, Any]) -> list[str]:
    args = [
        "scp",
        "-P",
        str(int(upload_cfg.get("server_port", 22))),
    ]
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


def build_generation_request(job: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    generation = job.get("generation", {})
    merged = {**defaults, **generation}
    payload: dict[str, Any] = {
        "text": job.get("script_text") or job.get("title", ""),
        "mode": merged.get("mode", "fixed"),
        "title": job.get("title", ""),
        "frame_template": merged.get("frame_template", "1080x1920/image_default.html"),
        "tts_workflow": merged.get("tts_workflow", "tts_edge.json"),
        "media_workflow": merged.get("media_workflow", "image_flux.json"),
        "bgm_volume": float(merged.get("bgm_volume", 0.25)),
    }
    prompt_prefix = str(merged.get("prompt_prefix", "")).strip()
    if prompt_prefix:
        payload["prompt_prefix"] = prompt_prefix
    if payload["mode"] == "generate":
        payload["n_scenes"] = int(merged.get("n_scenes", 5))
    return payload


def download_pixelle_video(api_base: str, video_url: str, output_file: Path) -> None:
    if video_url.startswith("http://") or video_url.startswith("https://"):
        request = urllib.request.Request(video_url, method="GET")
        with urllib.request.urlopen(request, timeout=300) as response:
            output_file.write_bytes(response.read())
        return
    local_candidate = Path(video_url)
    if local_candidate.exists():
        shutil.copy2(local_candidate, output_file)
        return
    file_url = f"{api_base.rstrip('/')}/api/files/{video_url.lstrip('/')}"
    request = urllib.request.Request(file_url, method="GET")
    with urllib.request.urlopen(request, timeout=300) as response:
        output_file.write_bytes(response.read())


def generate_with_pixelle(job: dict[str, Any], config: dict[str, Any]) -> Path:
    api_base = str(
        job.get("pixelle_api_url") or config.get("pixelle_api_url", "http://127.0.0.1:8000")
    ).rstrip("/")
    defaults = config.get("generation_defaults", {})
    generation = {**defaults, **job.get("generation", {})}
    request_payload = build_generation_request(job, defaults)
    poll_interval = int(generation.get("poll_interval_seconds", 5))
    timeout_seconds = int(generation.get("timeout_seconds", 1800))
    use_async = bool(generation.get("use_async_api", True))

    output_dir = Path(config.get("output_dir", "rendered_videos"))
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / str(job.get("output_filename", f"{job.get('job_id', 'video')}.mp4"))

    if use_async:
        create_resp = http_post_json(f"{api_base}/api/video/generate/async", request_payload)
        task_id = str(create_resp.get("task_id", "")).strip()
        if not task_id:
            raise RuntimeError(f"Pixelle async task creation failed: {create_resp}")
        started = time.time()
        while True:
            status_resp = http_get_json(f"{api_base}/api/tasks/{task_id}")
            status = str(status_resp.get("status", "")).lower()
            if status in {"completed", "success", "done"}:
                result = status_resp.get("result", status_resp)
                video_url = str(result.get("video_url", "")).strip()
                if not video_url:
                    raise RuntimeError(f"Pixelle task completed without video_url: {status_resp}")
                download_pixelle_video(api_base, video_url, output_file)
                return output_file
            if status in {"failed", "error", "cancelled"}:
                raise RuntimeError(f"Pixelle task failed: {status_resp}")
            if time.time() - started > timeout_seconds:
                raise RuntimeError(f"Pixelle task timeout after {timeout_seconds}s: {task_id}")
            time.sleep(poll_interval)
    else:
        sync_resp = http_post_json(
            f"{api_base}/api/video/generate/sync",
            request_payload,
            timeout=timeout_seconds,
        )
        if not sync_resp.get("success", True):
            raise RuntimeError(f"Pixelle sync generation failed: {sync_resp}")
        video_url = str(sync_resp.get("video_url", "")).strip()
        if not video_url:
            raise RuntimeError(f"Pixelle sync response missing video_url: {sync_resp}")
        download_pixelle_video(api_base, video_url, output_file)
        return output_file


def upload_video_to_server(job: dict[str, Any], local_video: Path, config: dict[str, Any]) -> None:
    upload_cfg = {**config.get("upload", {}), **job.get("upload", {})}
    if not upload_cfg.get("enabled", True):
        print("[INFO] upload disabled, keeping local file only.")
        return

    remote_media_dir = str(upload_cfg.get("remote_media_dir", "")).rstrip("/")
    remote_path = f"{remote_media_dir}/{job.get('remote_media_path', local_video.name)}"
    scp_command = scp_base_args(upload_cfg) + [str(local_video), f"{upload_cfg.get('server_user', 'root')}@{upload_cfg.get('server_host', '')}:{remote_path}"]
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
        ssh_command = ssh_base_args(upload_cfg) + [remote_cmd]
        run_command(ssh_command)
        print(f"[OK] remote import-local-video completed for {job.get('job_id')}")


def process_job(job_file: Path, config: dict[str, Any], jobs_dir: Path) -> bool:
    job = load_json(job_file, {})
    job_id = str(job.get("job_id", job_file.stem))
    print(f"[RUN] processing job {job_id}")
    try:
        local_video = generate_with_pixelle(job, config)
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


def process_pending_jobs(config: dict[str, Any]) -> tuple[int, int]:
    jobs_dir = Path(config.get("jobs_dir", "video_jobs"))
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
    parser = argparse.ArgumentParser(description="Local GPU video worker (ComfyUI + Pixelle-Video)")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent / "local_config.json"),
        help="Path to local worker config JSON",
    )
    parser.add_argument("--once", action="store_true", help="Process pending jobs once and exit")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    config_path = Path(args.config).expanduser()
    if not config_path.exists():
        print(f"Config not found: {config_path}")
        print("Copy automation/local_gpu/local_config.example.json and edit paths.")
        sys.exit(1)
    config = load_json(config_path, {})
    poll_once = bool(args.once or config.get("poll_once", False))
    interval = int(config.get("poll_interval_seconds", 60))

    while True:
        success, failed = process_pending_jobs(config)
        print(f"[DONE] worker cycle complete. success={success} failed={failed}")
        if poll_once:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
