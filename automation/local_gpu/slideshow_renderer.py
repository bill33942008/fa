"""Build short videos: ComfyUI images + edge-tts + ffmpeg."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from comfyui_client import ComfyUIClient


def split_segments(script_text: str, limit: int = 6) -> list[str]:
    plain = re.sub(r"\s+", " ", script_text or "").strip()
    chunks = [part.strip() for part in re.split(r"[。！？!?;\n]+", plain) if part.strip()]
    if not chunks and plain:
        chunks = [plain]
    return chunks[:limit]


def build_visual_prompt(segment: str, job: dict[str, Any]) -> str:
    track = str(job.get("track", "")).strip()
    style_map = {
        "child_education": "warm family education scene, parent and child, soft colors",
        "travel": "beautiful travel destination, scenic landscape, vibrant",
        "ai_funny": "funny cartoon style, humorous scene, expressive",
        "football": "sports stadium atmosphere, football theme, dynamic",
    }
    style = style_map.get(track, "cinematic social media vertical video still")
    return (
        f"vertical 9:16 composition, {style}, high detail, no text, no watermark, "
        f"topic: {segment[:160]}"
    )


def ffprobe_duration_seconds(audio_file: Path) -> float:
    ffmpeg = shutil.which("ffprobe")
    if not ffmpeg:
        return max(2.0, len(audio_file.read_bytes()) / 32000.0)
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(audio_file),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return max(0.5, float((result.stdout or "0").strip()))
    except ValueError:
        return 2.0


def format_srt_time(seconds: float) -> str:
    millis = max(0, int(round(seconds * 1000)))
    hours = millis // 3600000
    minutes = (millis % 3600000) // 60000
    secs = (millis % 60000) // 1000
    ms = millis % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def write_srt(cues: list[tuple[float, float, str]], path: Path) -> None:
    lines: list[str] = []
    for idx, (start, end, text) in enumerate(cues, start=1):
        clean = text.replace("\n", " ").strip()
        lines.extend(
            [str(idx), f"{format_srt_time(start)} --> {format_srt_time(end)}", clean, ""]
        )
    path.write_text("\ufeff" + "\n".join(lines), encoding="utf-8")


def run_command(command: list[str], *, cwd: str | None = None) -> None:
    result = subprocess.run(command, capture_output=True, text=True, check=False, cwd=cwd)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise RuntimeError(f"Command failed: {' '.join(command)} | {detail[:500]}")


def burn_subtitles(
    ffmpeg: str,
    merged_video: Path,
    subtitles: Path,
    output_file: Path,
    *,
    font_name: str = "Microsoft YaHei",
) -> None:
    workdir = str(merged_video.parent)
    style = (
        f"FontName={font_name},FontSize=22,PrimaryColour=&HFFFFFF&,"
        "OutlineColour=&H000000&,BorderStyle=3,Outline=2,Shadow=1"
    )
    filter_expr = f"subtitles=subs.srt:charenc=UTF-8:force_style='{style}'"
    try:
        run_command(
            [
                ffmpeg,
                "-y",
                "-i",
                merged_video.name,
                "-vf",
                filter_expr,
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "copy",
                str(output_file.resolve()),
            ],
            cwd=workdir,
        )
    except RuntimeError:
        shutil.copy2(merged_video, output_file)
        sidecar = output_file.with_suffix(".srt")
        shutil.copy2(subtitles, sidecar)
        print(f"[WARN] subtitle burn failed, saved sidecar: {sidecar}")


def finalize_video(
    ffmpeg: str,
    merged_video: Path,
    subtitles: Path,
    output_file: Path,
    hard_subtitles: bool = False,
    font_name: str = "Microsoft YaHei",
) -> None:
    if hard_subtitles:
        burn_subtitles(
            ffmpeg,
            merged_video,
            subtitles,
            output_file,
            font_name=font_name,
        )
        return

    shutil.copy2(merged_video, output_file)
    sidecar = output_file.with_suffix(".srt")
    shutil.copy2(subtitles, sidecar)
    print(f"[INFO] hard subtitles disabled, saved sidecar: {sidecar}")


def synthesize_tts(segment_text: str, output_audio: Path, voice: str) -> None:
    edge = shutil.which("edge-tts")
    if edge:
        run_command(
            [
                edge,
                "--voice",
                voice,
                "--text",
                segment_text[:220],
                "--write-media",
                str(output_audio),
            ]
        )
        return
    raise RuntimeError("edge-tts not found. Install with: pip install edge-tts")


def list_checkpoints(client: ComfyUIClient) -> list[str]:
    return client.list_checkpoints()


def resolve_checkpoint(client: ComfyUIClient, comfy_cfg: dict[str, Any]) -> str | None:
    configured = str(comfy_cfg.get("checkpoint_name", "")).strip()
    candidates = comfy_cfg.get(
        "checkpoint_candidates",
        [
            "dreamshaper_8.safetensors",
            "majicmixRealistic_v7.safetensors",
            "v1-5-pruned-emaonly.safetensors",
            "sd_xl_base_1.0.safetensors",
        ],
    )
    available = list_checkpoints(client)
    if configured and configured != "__CHECKPOINT__":
        if configured in available:
            return configured
        if available:
            print(f"[WARN] checkpoint_name not found: {configured}, using {available[0]}")
            return available[0]
        print(f"[WARN] checkpoint_name not found and ComfyUI has no models: {configured}")
        return None
    if available:
        for name in candidates:
            if name in available:
                return name
        return available[0]
    return None


def render_plain_slide(image_path: Path, width: int, height: int, color: str) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found in PATH")
    run_command(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s={width}x{height}",
            "-frames:v",
            "1",
            str(image_path),
        ]
    )


def prepare_image_workflow(
    template: dict[str, Any],
    *,
    positive_prompt: str,
    checkpoint_name: str,
    seed: int,
    width: int,
    height: int,
) -> dict[str, Any]:
    workflow = json.loads(json.dumps(template))
    for node in workflow.values():
        class_type = str(node.get("class_type", ""))
        inputs = node.setdefault("inputs", {})
        if class_type == "CheckpointLoaderSimple":
            inputs["ckpt_name"] = checkpoint_name
        elif class_type == "CLIPTextEncode" and inputs.get("text") == "__POSITIVE_PROMPT__":
            inputs["text"] = positive_prompt
        elif class_type == "EmptyLatentImage":
            inputs["width"] = width
            inputs["height"] = height
        elif class_type == "KSampler":
            inputs["seed"] = seed
    return workflow


def render_slideshow_video(
    job: dict[str, Any],
    comfy_cfg: dict[str, Any],
    output_file: Path,
) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found in PATH")

    base_url = str(comfy_cfg.get("url", "http://127.0.0.1:8000")).rstrip("/")
    client = ComfyUIClient(base_url)
    workflow_path = Path(str(comfy_cfg.get("workflow_dir", "workflows"))) / str(
        comfy_cfg.get("workflow_file", "short_video.api.json")
    )
    if not workflow_path.is_absolute():
        workflow_path = workflow_path.resolve()
    if not workflow_path.exists():
        raise FileNotFoundError(f"Workflow missing: {workflow_path}")

    template = client.load_workflow_file(workflow_path)
    checkpoint = resolve_checkpoint(client, comfy_cfg)
    use_plain_slides = checkpoint is None
    if use_plain_slides:
        if not bool(comfy_cfg.get("allow_plain_fallback", True)):
            raise RuntimeError(
                "ComfyUI has no checkpoint models. "
                "Download a .safetensors model into ComfyUI/models/checkpoints/, "
                "or set comfyui.allow_plain_fallback=true for color-slide preview."
            )
        print("[WARN] No checkpoint in ComfyUI; using plain color slides (add a model for AI images)")
    else:
        print(f"[OK] Using checkpoint: {checkpoint}")

    script = str(job.get("script_text", "")).strip()
    hook = str(job.get("hook_text", "")).strip()
    max_segments = int(comfy_cfg.get("max_segments", 6))
    segments = split_segments(script, limit=max_segments)
    if hook and segments:
        segments[0] = f"{hook[:100]}。{segments[0]}"[:220]
    if not segments:
        segments = [str(job.get("title", "短视频内容"))[:120]]

    voice = str(comfy_cfg.get("tts_voice", "zh-CN-XiaoxiaoNeural"))
    width = int(comfy_cfg.get("width", 720))
    height = int(comfy_cfg.get("height", 1280))
    poll_interval = float(comfy_cfg.get("poll_interval_seconds", 3))
    timeout_seconds = int(comfy_cfg.get("timeout_seconds", 3600))
    slide_colors = ["0x1e3a8a", "0x1d4ed8", "0x2563eb", "0x3b82f6", "0x1e40af", "0x172554"]

    with tempfile.TemporaryDirectory(prefix="comfy-slideshow-") as temp_dir:
        temp_path = Path(temp_dir)
        slide_videos: list[Path] = []
        cues: list[tuple[float, float, str]] = []
        timeline = 0.0

        for index, segment in enumerate(segments):
            image_path = temp_path / f"slide_{index:02d}.png"
            if use_plain_slides:
                render_plain_slide(
                    image_path,
                    width,
                    height,
                    slide_colors[index % len(slide_colors)],
                )
                print(f"[OK] plain slide {index + 1}/{len(segments)}")
            else:
                prompt = build_visual_prompt(segment, job)
                workflow = prepare_image_workflow(
                    template,
                    positive_prompt=prompt,
                    checkpoint_name=str(checkpoint),
                    seed=int(comfy_cfg.get("base_seed", 42)) + index,
                    width=width,
                    height=height,
                )
                prompt_id = client.queue_prompt(workflow)
                print(f"[OK] ComfyUI image {index + 1}/{len(segments)} prompt_id={prompt_id}")
                outputs = client.wait_for_outputs(
                    prompt_id,
                    poll_interval=poll_interval,
                    timeout_seconds=timeout_seconds,
                )
                image_info = client.pick_output_file(outputs)
                client.download_output(image_info, image_path)

            audio_path = temp_path / f"slide_{index:02d}.mp3"
            synthesize_tts(segment, audio_path, voice)
            duration = ffprobe_duration_seconds(audio_path)
            cues.append((timeline, timeline + duration, segment[:80]))
            timeline += duration + 0.2

            slide_mp4 = temp_path / f"slide_{index:02d}.mp4"
            run_command(
                [
                    ffmpeg,
                    "-y",
                    "-loop",
                    "1",
                    "-i",
                    str(image_path),
                    "-i",
                    str(audio_path),
                    "-c:v",
                    "libx264",
                    "-tune",
                    "stillimage",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-shortest",
                    str(slide_mp4),
                ]
            )
            slide_videos.append(slide_mp4)

        concat_list = temp_path / "concat.txt"
        concat_list.write_text(
            "\n".join([f"file '{path.as_posix()}'" for path in slide_videos]),
            encoding="utf-8",
        )
        merged = temp_path / "merged.mp4"
        run_command(
            [
                ffmpeg,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list),
                "-c",
                "copy",
                str(merged),
            ]
        )

        subtitles = temp_path / "subs.srt"
        write_srt(cues, subtitles)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        finalize_video(
            ffmpeg,
            merged,
            subtitles,
            output_file,
            hard_subtitles=bool(comfy_cfg.get("hard_subtitles", False)),
        )

    print(f"[OK] slideshow video rendered -> {output_file}")
    return output_file
