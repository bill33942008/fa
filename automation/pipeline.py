#!/usr/bin/env python3
"""
Automated multi-platform content pipeline.

Capabilities:
- Collect topic candidates from RSS sources
- Generate platform-specific drafts with an LLM (or deterministic fallback)
- Create and maintain a review/publishing queue
- Support semi-automated publishing workflow
- Sync queue status to Feishu Bitable
- Push daily reminders to Feishu webhook / email
- Auto-submit WeChat Official Account drafts (official API path)
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import email.utils
import html
import http.server
import io
import json
import os
import re
import shlex
import shutil
import smtplib
import ssl
import subprocess
import tempfile
import threading
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
import xml.etree.ElementTree as et
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
DATA_DIR = BASE_DIR / "data"
OUTBOX_DIR = BASE_DIR / "outbox"
PREVIEW_DIR = BASE_DIR / "previews"
QUEUE_FILE = STATE_DIR / "publish_queue.json"
FEISHU_MAPPING_FILE = STATE_DIR / "feishu_record_mapping.json"
VIDEO_JOBS_DIR = STATE_DIR / "video_jobs"

FEISHU_FIELD_DEFINITIONS: dict[str, dict[str, Any]] = {
    "QueueID": {"type": 1},
    "Date": {"type": 1},
    "Platform": {"type": 1},
    "Account": {"type": 1},
    "Track": {"type": 1},
    "Status": {"type": 1},
    "PublishTime": {"type": 1},
    "Title": {"type": 1},
    "Hashtags": {"type": 1},
    "SourceTopic": {"type": 1},
    "SourceLink": {"type": 1},
    "ContentFile": {"type": 1},
    "PreviewFile": {"type": 1},
    "PreviewURL": {"type": 1},
    "SampleVideoFile": {"type": 1},
    "SampleVideoURL": {"type": 1},
    "SampleAudioFile": {"type": 1},
    "IllustrationFiles": {"type": 1},
    "IllustrationURLs": {"type": 1},
    "CloudVideoProvider": {"type": 1},
    "CloudImageModel": {"type": 1},
    "CloudVideoModel": {"type": 1},
    "CloudVideoPredictionID": {"type": 1},
    "HookText": {"type": 1},
    "BodyPreview": {"type": 1},
    "ContentMarkdown": {"type": 1},
    "CoverText": {"type": 1},
    "PostURL": {"type": 1},
    "UpdatedAt": {"type": 1},
    "Notes": {"type": 1},
    "HookScore": {"type": 2, "property": {"formatter": "0"}},
    "StructureScore": {"type": 2, "property": {"formatter": "0"}},
    "PlatformFitScore": {"type": 2, "property": {"formatter": "0"}},
    "CommercialScore": {"type": 2, "property": {"formatter": "0"}},
    "ComplianceScore": {"type": 2, "property": {"formatter": "0"}},
    "TotalScore": {"type": 2, "property": {"formatter": "0"}},
    "QualityLevel": {"type": 1},
    "QualityBadge": {"type": 1},
    "PublishAdvice": {"type": 1},
    "QualityReason": {"type": 1},
}


def ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def apply_server_runtime_defaults(cfg: dict[str, Any]) -> dict[str, Any]:
    preview = cfg.setdefault("preview", {})
    if not str(preview.get("public_base_url", "")).strip() or "YOUR_SERVER_IP" in str(
        preview.get("public_base_url", "")
    ):
        preview["public_base_url"] = "http://118.25.178.116:8787"
    cloud_media = cfg.setdefault("cloud_media", {})
    cloud_media["enabled"] = True
    cloud_media.setdefault("image", {})["enabled"] = True
    cloud_media.setdefault("video", {})["enabled"] = True
    cfg.setdefault("local_gpu", {})["enabled"] = False
    sample_video = cfg.setdefault("sample_video", {})
    sample_video["enabled"] = True
    if not str(sample_video.get("public_base_url", "")).strip() or "YOUR_SERVER_IP" in str(
        sample_video.get("public_base_url", "")
    ):
        sample_video["public_base_url"] = preview.get("public_base_url", "http://118.25.178.116:8787")
    return cfg


def ensure_config_file(path: Path) -> None:
    if path.exists():
        return
    parent = path.parent
    candidates = [
        parent / "config.server.json",
        parent / "config.example.json",
    ]
    for candidate in candidates:
        if not candidate.exists():
            continue
        payload = load_json(candidate, {})
        if candidate.name == "config.example.json":
            payload = apply_server_runtime_defaults(payload)
        save_json(path, payload)
        print(
            f"[INFO] Created missing config: {path} "
            f"(from {candidate.name}). Review Feishu/cloud settings if needed."
        )
        return
    raise FileNotFoundError(
        f"Config not found: {path}\n"
        "Run: curl -fsSL https://raw.githubusercontent.com/bill33942008/fa/"
        "refs/heads/cursor/multi-platform-auto-ops-2a43/automation/bootstrap_server.sh | bash"
    )


def load_config(path: Path) -> dict[str, Any]:
    ensure_config_file(path)
    return load_json(path, {})


def now_local() -> dt.datetime:
    return dt.datetime.now().astimezone()


def parse_datetime(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    try:
        return email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None


def http_post_json(
    url: str, payload: dict[str, Any], headers: dict[str, str] | None = None, timeout: int = 30
) -> dict[str, Any]:
    merged_headers = {"Content-Type": "application/json"}
    if headers:
        merged_headers.update(headers)
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=merged_headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def http_get_json(
    url: str, headers: dict[str, str] | None = None, timeout: int = 30
) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def http_get_bytes(url: str, headers: dict[str, str] | None = None, timeout: int = 60) -> bytes:
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def cloud_media_enabled(config: dict[str, Any]) -> bool:
    return bool(config.get("cloud_media", {}).get("enabled", False))


def dashscope_headers(cfg: dict[str, Any], *, async_mode: bool = False) -> dict[str, str]:
    key_env = str(cfg.get("api_key_env", "DASHSCOPE_API_KEY")).strip()
    key = os.getenv(key_env, "")
    if not key:
        raise ValueError(f"Missing DashScope API key env: {key_env}")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if async_mode:
        headers["X-DashScope-Async"] = "enable"
    return headers


def dashscope_base_url(cfg: dict[str, Any]) -> str:
    return str(cfg.get("base_url", "https://dashscope.aliyuncs.com/api/v1")).rstrip("/")


def dashscope_create_task(cfg: dict[str, Any], endpoint_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{dashscope_base_url(cfg)}{endpoint_path}"
    try:
        return http_post_json(
            url,
            payload,
            headers=dashscope_headers(cfg, async_mode=True),
            timeout=int(cfg.get("request_timeout_seconds", 60)),
        )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1200]
        raise RuntimeError(f"DashScope create task HTTP {exc.code}: {detail}") from exc


def dashscope_model_candidates(cfg: dict[str, Any], defaults: list[str]) -> list[str]:
    first = str(cfg.get("model", "")).strip()
    user_candidates = cfg.get("model_candidates", [])
    candidates: list[str] = []
    if first:
        candidates.append(first)
    if isinstance(user_candidates, list):
        for model in user_candidates:
            m = str(model).strip()
            if m:
                candidates.append(m)
    for model in defaults:
        m = str(model).strip()
        if m:
            candidates.append(m)
    uniq: list[str] = []
    seen: set[str] = set()
    for model in candidates:
        if model in seen:
            continue
        seen.add(model)
        uniq.append(model)
    return uniq


def is_dashscope_model_retryable_error(exc: Exception) -> bool:
    text = str(exc)
    markers = [
        "Model.AccessDenied",
        "Model not exist",
        '"code":"InvalidParameter"',
        "InvalidParameter",
    ]
    return any(marker in text for marker in markers)


def dashscope_extract_task_id(resp: dict[str, Any]) -> str:
    output = resp.get("output", {}) if isinstance(resp, dict) else {}
    if isinstance(output, dict):
        for key in ("task_id", "taskId", "id"):
            value = str(output.get(key, "")).strip()
            if value:
                return value
    for key in ("task_id", "taskId", "id"):
        value = str(resp.get(key, "")).strip() if isinstance(resp, dict) else ""
        if value:
            return value
    return ""


def dashscope_poll_task(cfg: dict[str, Any], task_id: str) -> dict[str, Any]:
    timeout_seconds = int(cfg.get("timeout_seconds", 1200))
    poll_interval = float(cfg.get("poll_interval_seconds", 5))
    started = now_local().timestamp()
    url = f"{dashscope_base_url(cfg)}/tasks/{task_id}"
    while True:
        try:
            status = http_get_json(url, headers=dashscope_headers(cfg), timeout=30)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            if exc.code in {429, 503, 502}:
                print("[WARN] DashScope busy/rate-limit, backing off.")
                time.sleep(max(2.0, poll_interval * 2))
                continue
            raise RuntimeError(f"DashScope poll HTTP {exc.code}: {detail}") from exc
        output = status.get("output", {}) if isinstance(status, dict) else {}
        task_status = str(output.get("task_status", output.get("status", ""))).upper()
        if task_status in {"SUCCEEDED", "SUCCESS", "COMPLETED"}:
            return status
        if task_status in {"FAILED", "CANCELED", "CANCELLED"}:
            raise RuntimeError(f"DashScope task failed: {status}")
        if now_local().timestamp() - started > timeout_seconds:
            raise TimeoutError(f"DashScope task timeout after {timeout_seconds}s: {task_id}")
        time.sleep(poll_interval)


def replicate_headers(cfg: dict[str, Any]) -> dict[str, str]:
    token_env = str(cfg.get("api_token_env", "REPLICATE_API_TOKEN")).strip()
    token = os.getenv(token_env, "")
    if not token:
        raise ValueError(f"Missing Replicate token env: {token_env}")
    return {
        "Authorization": f"Token {token}",
        "Content-Type": "application/json",
    }


def resolve_replicate_version(cfg: dict[str, Any]) -> str:
    cached = str(cfg.get("_resolved_version", "")).strip()
    if cached:
        return cached
    explicit_version = str(cfg.get("version", "")).strip()
    if explicit_version:
        cfg["_resolved_version"] = explicit_version
        return explicit_version
    model = str(cfg.get("model", "")).strip()
    if not model:
        raise ValueError("cloud_media.*.model or cloud_media.*.version is required")
    if "/" not in model:
        # If user already passed a version-like value, allow it.
        cfg["_resolved_version"] = model
        return model
    try:
        meta = http_get_json(
            f"https://api.replicate.com/v1/models/{model}",
            headers=replicate_headers(cfg),
            timeout=30,
        )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1200]
        raise RuntimeError(
            f"Replicate model metadata HTTP {exc.code}: {detail}. "
            "If you see Cloudflare 1010, the current server egress IP is blocked by Replicate."
        ) from exc
    latest = meta.get("latest_version", {}) if isinstance(meta, dict) else {}
    version = str(latest.get("id", "")).strip()
    if not version:
        raise ValueError(f"Cannot resolve latest Replicate version for model: {model}")
    cfg["_resolved_version"] = version
    return version


def replicate_create_prediction(cfg: dict[str, Any], payload_input: dict[str, Any]) -> dict[str, Any]:
    version = resolve_replicate_version(cfg)
    payload = {"version": version, "input": payload_input}
    try:
        return http_post_json(
            "https://api.replicate.com/v1/predictions",
            payload,
            headers=replicate_headers(cfg),
            timeout=int(cfg.get("request_timeout_seconds", 60)),
        )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1200]
        raise RuntimeError(f"Replicate create prediction HTTP {exc.code}: {detail}") from exc


def replicate_create_prediction_with_retries(
    cfg: dict[str, Any], payload_input: dict[str, Any]
) -> dict[str, Any]:
    max_retries = int(cfg.get("max_retries", 4))
    base_sleep = float(cfg.get("retry_backoff_seconds", 2))
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return replicate_create_prediction(cfg, payload_input)
        except Exception as exc:  # pylint: disable=broad-except
            last_error = exc
            text = str(exc)
            retryable = "HTTP 429" in text or "HTTP 503" in text or "HTTP 502" in text
            if not retryable or attempt >= max_retries:
                raise
            sleep_seconds = base_sleep * (2**attempt)
            print(f"[WARN] Replicate busy/rate-limit, retry in {sleep_seconds:.1f}s")
            time.sleep(sleep_seconds)
    raise RuntimeError(f"Replicate create prediction failed: {last_error}")


def replicate_create_prediction_candidates(
    cfg: dict[str, Any], input_candidates: list[dict[str, Any]]
) -> dict[str, Any]:
    errors: list[str] = []
    for idx, payload_input in enumerate(input_candidates, start=1):
        try:
            return replicate_create_prediction_with_retries(cfg, payload_input)
        except Exception as exc:  # pylint: disable=broad-except
            errors.append(f"candidate#{idx}: {exc}")
    raise RuntimeError("All Replicate input candidates failed: " + " | ".join(errors[:3]))


def replicate_poll_prediction(cfg: dict[str, Any], prediction_id: str) -> dict[str, Any]:
    headers = replicate_headers(cfg)
    timeout_seconds = int(cfg.get("timeout_seconds", 900))
    poll_interval = float(cfg.get("poll_interval_seconds", 3))
    started = now_local().timestamp()
    while True:
        try:
            status = http_get_json(
                f"https://api.replicate.com/v1/predictions/{prediction_id}",
                headers=headers,
                timeout=30,
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            if exc.code == 429:
                print("[WARN] Replicate poll hit 429, backing off.")
                time.sleep(max(2.0, poll_interval * 2))
                continue
            raise RuntimeError(f"Replicate poll HTTP {exc.code}: {detail}") from exc
        state = str(status.get("status", "")).lower()
        if state == "succeeded":
            return status
        if state in {"failed", "canceled"}:
            raise RuntimeError(f"Replicate prediction failed: {status.get('error', status)}")
        if now_local().timestamp() - started > timeout_seconds:
            raise TimeoutError(f"Replicate prediction timeout after {timeout_seconds}s: {prediction_id}")
        time.sleep(poll_interval)


def normalize_prediction_urls(output: Any) -> list[str]:
    if output is None:
        return []
    if isinstance(output, str) and output.startswith("http"):
        return [output]
    if isinstance(output, list):
        urls: list[str] = []
        for item in output:
            urls.extend(normalize_prediction_urls(item))
        return urls
    if isinstance(output, dict):
        urls: list[str] = []
        for key in ("url", "image", "video_url", "file_url"):
            value = output.get(key)
            if isinstance(value, str) and value.startswith("http"):
                urls.append(value)
        for value in output.values():
            urls.extend(normalize_prediction_urls(value))
        seen: set[str] = set()
        ordered: list[str] = []
        for url in urls:
            if url not in seen:
                seen.add(url)
                ordered.append(url)
        return ordered
    return []


def fetch_rss(url: str) -> list[dict[str, Any]]:
    request = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (ContentAutomationBot/1.0)"}
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            xml_bytes = response.read()
    except urllib.error.URLError as exc:
        print(f"[WARN] RSS fetch failed: {url} -> {exc}")
        return []

    try:
        root = et.fromstring(xml_bytes)
    except et.ParseError as exc:
        print(f"[WARN] RSS parse failed: {url} -> {exc}")
        return []

    items: list[dict[str, Any]] = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        description = (item.findtext("description") or "").strip()
        pub_date_raw = (item.findtext("pubDate") or "").strip()
        pub_dt = parse_datetime(pub_date_raw)
        items.append(
            {
                "title": title,
                "link": link,
                "description": description,
                "pub_date_raw": pub_date_raw,
                "pub_date_iso": pub_dt.isoformat() if pub_dt else None,
            }
        )
    return items


def score_topic(topic: dict[str, Any], keywords: list[str]) -> float:
    text = f"{topic.get('title', '')} {topic.get('description', '')}".lower()
    score = 0.0
    for kw in keywords:
        if kw.lower() in text:
            score += 2.0
    raw = topic.get("pub_date_raw")
    pub_dt = parse_datetime(raw)
    if pub_dt is not None:
        delta_hours = (now_local() - pub_dt.astimezone()).total_seconds() / 3600.0
        freshness = max(0.0, 72.0 - delta_hours)
        score += freshness / 18.0
    return round(score, 3)


def collect_track_topics(
    track_name: str, track_cfg: dict[str, Any], per_track_limit: int
) -> list[dict[str, Any]]:
    keywords = track_cfg.get("keywords", [])
    all_items: list[dict[str, Any]] = []
    seen_titles: set[str] = set()

    for source in track_cfg.get("rss_sources", []):
        for item in fetch_rss(source):
            title = item.get("title", "").strip()
            if not title:
                continue
            title_key = re.sub(r"\s+", " ", title.lower())
            if title_key in seen_titles:
                continue
            seen_titles.add(title_key)
            item["track"] = track_name
            item["source"] = source
            item["score"] = score_topic(item, keywords)
            all_items.append(item)

    all_items.sort(
        key=lambda i: (float(i.get("score", 0.0)), i.get("pub_date_iso") or ""),
        reverse=True,
    )
    return all_items[:per_track_limit]


def extract_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def llm_generate(
    llm_cfg: dict[str, Any], system_prompt: str, user_prompt: str
) -> dict[str, Any] | None:
    if not llm_cfg.get("enabled", False):
        return None
    api_key = os.getenv(llm_cfg.get("api_key_env", "OPENAI_API_KEY"), "")
    if not api_key:
        print("[WARN] LLM key not found, fallback mode enabled.")
        return None

    endpoint = llm_cfg.get("base_url", "").strip()
    if not endpoint:
        print("[WARN] LLM base_url missing, fallback mode enabled.")
        return None

    payload = {
        "model": llm_cfg.get("model", "gpt-4.1-mini"),
        "temperature": llm_cfg.get("temperature", 0.7),
        "max_tokens": llm_cfg.get("max_tokens", 1400),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    try:
        raw = http_post_json(
            endpoint,
            payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=35,
        )
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        print(f"[WARN] LLM request failed, fallback mode enabled: {exc}")
        return None

    try:
        content = raw["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        print("[WARN] Unexpected LLM response shape, fallback mode enabled.")
        return None

    return extract_json_object(content)


def fallback_draft(
    platform: dict[str, Any], track: dict[str, Any], topic: dict[str, Any]
) -> dict[str, Any]:
    title = topic.get("title", "今日主题")
    topic_link = topic.get("link", "")
    description = topic.get("description", "无摘要，建议补充自己的观察。")
    keywords = track.get("keywords", [])[:5]
    hashtags = [f"#{k}" for k in keywords]
    body = textwrap.dedent(
        f"""
        ## 选题来源
        - 标题：{title}
        - 链接：{topic_link}

        ## 内容主旨
        围绕以上选题，结合账号定位给出可执行观点，避免空泛结论。

        ## 结构建议
        1. 3秒钩子/开头冲突
        2. 关键观点（2-3条）
        3. 行动建议或结尾互动

        ## 参考摘要
        {description}
        """
    ).strip()
    return {
        "title": f"{platform['account_name']} | {title}",
        "hook": "用一个反常识观点开场，提升完播和阅读意愿。",
        "body_markdown": body,
        "cover_text": f"{platform['account_name']} 今日内容",
        "hashtags": hashtags,
    }


def generate_draft(
    config: dict[str, Any],
    platform_cfg: dict[str, Any],
    track_cfg: dict[str, Any],
    topic: dict[str, Any],
) -> dict[str, Any]:
    system_prompt = track_cfg.get(
        "system_prompt", "你是内容运营编辑，输出可发布草稿。"
    )
    user_prompt = textwrap.dedent(
        f"""
        请根据以下信息生成一个可发布草稿，并且仅输出 JSON 对象：
        {{
          "title": "标题",
          "hook": "开头钩子",
          "body_markdown": "正文Markdown",
          "cover_text": "封面文案",
          "hashtags": ["#标签1", "#标签2"]
        }}

        账号名: {platform_cfg["account_name"]}
        平台: {platform_cfg["platform"]}
        内容格式: {platform_cfg.get("post_format", "generic")}
        发布时段: {platform_cfg.get("publish_time", "20:00")}
        赛道定位: {track_cfg.get("description", "")}

        选题标题: {topic.get("title", "")}
        选题摘要: {topic.get("description", "")}
        选题链接: {topic.get("link", "")}
        """
    ).strip()

    generated = llm_generate(config.get("llm", {}), system_prompt, user_prompt)
    if generated:
        return generated
    return fallback_draft(platform_cfg, track_cfg, topic)


def clamp_score(value: Any, minimum: int = 0, maximum: int = 20) -> int:
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError):
        return minimum
    return max(minimum, min(maximum, score))


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def extract_markdown_section(markdown_text: str, heading: str, next_heading: str) -> str:
    pattern = rf"{re.escape(heading)}\s*(.*?)(?:{re.escape(next_heading)}|\Z)"
    match = re.search(pattern, markdown_text, flags=re.DOTALL)
    if not match:
        return ""
    return match.group(1).strip()


def extract_content_payload(item: dict[str, Any]) -> dict[str, str]:
    hook_text = str(item.get("hook_text", "")).strip()
    body_markdown = str(item.get("body_markdown", "")).strip()
    cover_text = str(item.get("cover_text", "")).strip()
    markdown_text = ""

    content_file = str(item.get("content_file", "")).strip()
    if content_file:
        file_path = Path(content_file)
        if file_path.exists():
            markdown_text = file_path.read_text(encoding="utf-8")
            if not hook_text:
                hook_text = extract_markdown_section(markdown_text, "## Hook", "## Body")
            if not body_markdown:
                body_markdown = extract_markdown_section(
                    markdown_text, "## Body", "## Cover Text"
                )
            if not cover_text:
                cover_text = extract_markdown_section(
                    markdown_text, "## Cover Text", "## Hashtags"
                )

    if not markdown_text:
        markdown_text = str(item.get("content_markdown", "")).strip()

    body_preview = strip_markdown(body_markdown)[:320]
    markdown_trimmed = markdown_text[:8000]
    return {
        "hook_text": hook_text,
        "body_markdown": body_markdown,
        "cover_text": cover_text,
        "body_preview": body_preview,
        "content_markdown": markdown_trimmed,
    }


def apply_quality_guard(config: dict[str, Any], queue: list[dict[str, Any]]) -> dict[str, Any]:
    guard_cfg = config.get("quality_guard", {})
    if not guard_cfg.get("enabled", True):
        return {"changed": 0, "blocked_items": []}

    block_threshold = int(guard_cfg.get("block_score_threshold", 60))
    min_body_chars = int(guard_cfg.get("min_body_chars", 120))
    min_hook_chars = int(guard_cfg.get("min_hook_chars", 10))
    target_statuses = set(
        guard_cfg.get("target_statuses", ["pending_review", "approved", "ready_to_post"])
    )
    placeholder_patterns = guard_cfg.get(
        "placeholder_patterns",
        [
            "围绕以上选题，结合账号定位给出可执行观点，避免空泛结论。",
            "用一个反常识观点开场，提升完播和阅读意愿。",
            "无摘要，建议补充自己的观察。",
        ],
    )

    changed = 0
    blocked_items: list[dict[str, Any]] = []
    for item in queue:
        if item.get("status") not in target_statuses:
            continue

        content = extract_content_payload(item)
        score = safe_int(item.get("total_score", 0), default=0)
        title_text = str(item.get("title", "")).strip()
        hook_text = content["hook_text"]
        body_text = content["body_markdown"]
        body_plain = strip_markdown(body_text)
        reasons: list[str] = []

        if score < block_threshold:
            reasons.append(f"总分{score}低于{block_threshold}")
        if not title_text:
            reasons.append("标题为空")
        if len(strip_markdown(hook_text)) < min_hook_chars:
            reasons.append("开头hook过短")
        if len(body_plain) < min_body_chars:
            reasons.append("正文为空或过短")
        if any(pattern in (hook_text + "\n" + body_text) for pattern in placeholder_patterns):
            reasons.append("命中模板占位内容")

        if not reasons:
            continue

        reason_text = "；".join(dict.fromkeys(reasons))
        item["status"] = "auto_blocked"
        item["publish_advice"] = "禁发"
        item["quality_level"] = "low"
        item["quality_badge"] = "🔴"
        item["total_score"] = min(score, max(0, block_threshold - 1))

        existing_reason = str(item.get("quality_reason", "")).strip()
        if existing_reason:
            item["quality_reason"] = f"{existing_reason}；{reason_text}"[:260]
        else:
            item["quality_reason"] = reason_text[:260]

        guard_note = f"自动质检拦截：{reason_text}"
        existing_notes = str(item.get("notes", "")).strip()
        if guard_note not in existing_notes:
            item["notes"] = f"{existing_notes} | {guard_note}".strip(" |")
        item["updated_at"] = now_local().isoformat()

        blocked_items.append(
            {
                "id": item.get("id", ""),
                "platform": item.get("platform", ""),
                "score": item.get("total_score", 0),
                "reason": reason_text,
            }
        )
        changed += 1

    return {"changed": changed, "blocked_items": blocked_items}


def build_block_alert(blocked_items: list[dict[str, Any]]) -> str:
    lines = [
        f"Quality Guard Alert: auto-blocked {len(blocked_items)} item(s)",
        "",
    ]
    for item in blocked_items[:12]:
        lines.append(
            f"- {item['id']} | {item['platform']} | score={item['score']} | {item['reason']}"
        )
    return "\n".join(lines)


def quality_level_from_score(total_score: int, quality_cfg: dict[str, Any]) -> tuple[str, str, str]:
    publish_threshold = int(quality_cfg.get("publish_threshold", 80))
    revise_threshold = int(quality_cfg.get("revise_threshold", 60))
    if total_score >= publish_threshold:
        return ("high", "🟢", "可发")
    if total_score >= revise_threshold:
        return ("medium", "🟡", "需改")
    return ("low", "🔴", "禁发")


def heuristic_quality_review(
    platform_cfg: dict[str, Any], track_cfg: dict[str, Any], draft: dict[str, Any]
) -> dict[str, Any]:
    title = str(draft.get("title", "")).strip()
    hook = str(draft.get("hook", "")).strip()
    body = str(draft.get("body_markdown", "")).strip()
    hashtags = draft.get("hashtags", [])
    body_len = len(body)

    hook_score = clamp_score(6 + min(len(hook) / 6.0, 12))
    structure_points = 8
    structure_points += 4 if "## " in body else 0
    structure_points += 4 if any(ch in body for ch in ["1.", "2.", "3.", "- "]) else 0
    structure_points += 4 if body_len >= 240 else 0
    structure_score = clamp_score(structure_points)

    platform = platform_cfg.get("platform", "")
    post_format = platform_cfg.get("post_format", "")
    platform_fit = 10
    if post_format == "long_article":
        platform_fit += 6 if body_len >= 450 else -3
    elif post_format == "short_video_script":
        platform_fit += 5 if 80 <= body_len <= 800 else -2
        platform_fit += 3 if len(hook) >= 18 else -2
    elif post_format == "graphic_post":
        platform_fit += 4 if any(token in body for token in ["预算", "路线", "避坑", "清单"]) else -1
    if title:
        platform_fit += 2
    if platform in {"douyin", "wechat_channels", "kuaishou"} and len(title) > 36:
        platform_fit -= 2
    platform_fit_score = clamp_score(platform_fit)

    cta_keywords = ["评论", "关注", "收藏", "私信", "转发", "点击", "领取", "清单", "下期"]
    cta_hits = sum(1 for keyword in cta_keywords if keyword in body or keyword in hook or keyword in title)
    commercial_score = clamp_score(8 + cta_hits * 2 + (2 if hashtags else 0))

    compliance_score = 18
    risky_words = ["稳赚", "包赢", "内幕", "下注", "赌博", "保过", "躺赚", "暴富", "医疗奇迹"]
    if track_cfg.get("description", "").lower().find("football") >= 0:
        risky_words.extend(["单场必胜", "杀庄", "倍投"])
    for word in risky_words:
        if word in body or word in hook or word in title:
            compliance_score -= 4

    # Sensitive events should not be used as pure comedy hooks.
    if platform == "kuaishou" and any(word in (title + body + hook) for word in ["地震", "灾难", "伤亡"]):
        compliance_score -= 6
    compliance_score = clamp_score(compliance_score)

    scores = {
        "hook_score": hook_score,
        "structure_score": structure_score,
        "platform_fit_score": platform_fit_score,
        "commercial_score": commercial_score,
        "compliance_score": compliance_score,
    }
    lowest = sorted(scores.items(), key=lambda kv: kv[1])[:2]
    reason = "；".join([f"{name}:{value}" for name, value in lowest])
    return {**scores, "reason": f"启发式评分，建议优先优化 {reason}"}


def llm_quality_review(
    config: dict[str, Any],
    platform_cfg: dict[str, Any],
    track_cfg: dict[str, Any],
    topic: dict[str, Any],
    draft: dict[str, Any],
) -> dict[str, Any] | None:
    quality_cfg = config.get("quality_scoring", {})
    if not quality_cfg.get("llm_review_enabled", True):
        return None

    system_prompt = (
        "你是内容质检编辑。请对内容打分并输出 JSON，不要输出任何额外文字。"
    )
    user_prompt = textwrap.dedent(
        f"""
        请按以下五个维度分别打分（0-20，整数）：
        1) hook_score（开头抓力）
        2) structure_score（结构清晰度）
        3) platform_fit_score（平台适配度）
        4) commercial_score（变现相关度）
        5) compliance_score（合规安全度）

        并返回：
        {{
          "hook_score": 0,
          "structure_score": 0,
          "platform_fit_score": 0,
          "commercial_score": 0,
          "compliance_score": 0,
          "reason": "一句话说明主要短板"
        }}

        平台: {platform_cfg.get("platform", "")}
        账号: {platform_cfg.get("account_name", "")}
        内容格式: {platform_cfg.get("post_format", "")}
        赛道: {track_cfg.get("description", "")}
        选题: {topic.get("title", "")}
        内容标题: {draft.get("title", "")}
        Hook: {draft.get("hook", "")}
        正文:
        {draft.get("body_markdown", "")}
        """
    ).strip()

    return llm_generate(config.get("llm", {}), system_prompt, user_prompt)


def evaluate_draft_quality(
    config: dict[str, Any],
    platform_cfg: dict[str, Any],
    track_cfg: dict[str, Any],
    topic: dict[str, Any],
    draft: dict[str, Any],
) -> dict[str, Any]:
    quality_cfg = config.get("quality_scoring", {})
    if not quality_cfg.get("enabled", True):
        return {
            "hook_score": 0,
            "structure_score": 0,
            "platform_fit_score": 0,
            "commercial_score": 0,
            "compliance_score": 0,
            "total_score": 0,
            "quality_level": "unknown",
            "quality_badge": "⚪",
            "publish_advice": "需改",
            "reason": "质量评分已关闭",
        }

    review = llm_quality_review(config, platform_cfg, track_cfg, topic, draft)
    if review is None:
        review = heuristic_quality_review(platform_cfg, track_cfg, draft)

    hook_score = clamp_score(review.get("hook_score", 0))
    structure_score = clamp_score(review.get("structure_score", 0))
    platform_fit_score = clamp_score(review.get("platform_fit_score", 0))
    commercial_score = clamp_score(review.get("commercial_score", 0))
    compliance_score = clamp_score(review.get("compliance_score", 0))
    total_score = hook_score + structure_score + platform_fit_score + commercial_score + compliance_score
    quality_level, quality_badge, publish_advice = quality_level_from_score(total_score, quality_cfg)

    return {
        "hook_score": hook_score,
        "structure_score": structure_score,
        "platform_fit_score": platform_fit_score,
        "commercial_score": commercial_score,
        "compliance_score": compliance_score,
        "total_score": total_score,
        "quality_level": quality_level,
        "quality_badge": quality_badge,
        "publish_advice": publish_advice,
        "reason": str(review.get("reason", "")).strip()[:200],
    }


def render_markdown(
    queue_id: str,
    platform_cfg: dict[str, Any],
    topic: dict[str, Any],
    draft: dict[str, Any],
    quality: dict[str, Any] | None = None,
) -> str:
    hashtags = draft.get("hashtags", [])
    hashtag_line = " ".join(hashtags) if isinstance(hashtags, list) else str(hashtags)
    lines = [
        f"# {draft.get('title', 'Untitled')}",
        "",
        f"- Queue ID: `{queue_id}`",
        f"- Account: {platform_cfg['account_name']}",
        f"- Platform: {platform_cfg['platform']}",
        f"- Scheduled Time: {platform_cfg.get('publish_time', 'N/A')}",
        f"- Source Topic: {topic.get('title', '')}",
        f"- Source Link: {topic.get('link', '')}",
        "",
        "## Hook",
        draft.get("hook", ""),
        "",
        "## Body",
        draft.get("body_markdown", ""),
        "",
        "## Cover Text",
        draft.get("cover_text", ""),
        "",
        "## Hashtags",
        hashtag_line,
        "",
    ]
    if quality:
        lines.extend(
            [
                "## Quality",
                (
                    f"{quality.get('quality_badge', '⚪')} 总分 {quality.get('total_score', 0)}/100 | "
                    f"发布建议：{quality.get('publish_advice', '需改')}"
                ),
                (
                    "维度："
                    f"Hook {quality.get('hook_score', 0)}/20, "
                    f"结构 {quality.get('structure_score', 0)}/20, "
                    f"平台适配 {quality.get('platform_fit_score', 0)}/20, "
                    f"变现 {quality.get('commercial_score', 0)}/20, "
                    f"合规 {quality.get('compliance_score', 0)}/20"
                ),
                f"说明：{quality.get('reason', '')}",
                "",
            ]
        )
    return "\n".join(lines)


def load_queue() -> list[dict[str, Any]]:
    return load_json(QUEUE_FILE, [])


def save_queue(items: list[dict[str, Any]]) -> None:
    save_json(QUEUE_FILE, items)


def update_item_status(
    queue: list[dict[str, Any]], item_id: str, new_status: str, notes: str = ""
) -> bool:
    for item in queue:
        if item["id"] == item_id:
            item["status"] = new_status
            item["updated_at"] = now_local().isoformat()
            if notes:
                item["notes"] = notes
            return True
    return False


def strip_markdown(text: str) -> str:
    no_links = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", text)
    no_marks = re.sub(r"[#>*`_~\-]", " ", no_links)
    return re.sub(r"\s+", " ", no_marks).strip()


def markdown_to_simple_html(markdown_text: str) -> str:
    lines = markdown_text.splitlines()
    html_lines: list[str] = []
    in_ul = False
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            if in_ul:
                html_lines.append("</ul>")
                in_ul = False
            continue
        if line.startswith("### "):
            if in_ul:
                html_lines.append("</ul>")
                in_ul = False
            html_lines.append(f"<h3>{html.escape(line[4:])}</h3>")
        elif line.startswith("## "):
            if in_ul:
                html_lines.append("</ul>")
                in_ul = False
            html_lines.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("# "):
            if in_ul:
                html_lines.append("</ul>")
                in_ul = False
            html_lines.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line.lstrip().startswith("- "):
            if not in_ul:
                html_lines.append("<ul>")
                in_ul = True
            item = line.lstrip()[2:]
            html_lines.append(f"<li>{html.escape(item)}</li>")
        else:
            if in_ul:
                html_lines.append("</ul>")
                in_ul = False
            html_lines.append(f"<p>{html.escape(line)}</p>")
    if in_ul:
        html_lines.append("</ul>")
    return "\n".join(html_lines)


def sanitize_filename_part(text: str, max_len: int = 36) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", str(text).strip())
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._ ")
    return (cleaned or "untitled")[:max_len]


def image_card_html(url: str, label: str) -> str:
    safe_url = html.escape(str(url))
    return (
        f"<p class='image-marker'>【插入{html.escape(label)}】</p>"
        "<figure class='inline-image'>"
        f"<img src='{safe_url}' loading='lazy' />"
        f"<figcaption>{html.escape(label)}</figcaption>"
        "</figure>"
    )


def clean_image_html(url: str, label: str) -> str:
    safe_url = html.escape(str(url))
    return (
        f"<p class='image-marker'>【插入{html.escape(label)}】</p>"
        f"<p><img src='{safe_url}' alt='{html.escape(label)}' style='max-width:100%;height:auto;' /></p>"
    )


def markdown_to_html_with_inline_images(markdown_text: str, image_urls: list[str]) -> str:
    lines = markdown_text.splitlines()
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.strip():
            current.append(line)
            continue
        if current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    if not blocks:
        return markdown_to_simple_html(markdown_text)

    image_after: dict[int, list[str]] = {}
    for idx, url in enumerate(image_urls):
        block_idx = min(len(blocks) - 1, int((idx + 1) * len(blocks) / (len(image_urls) + 1)))
        image_after.setdefault(block_idx, []).append(url)

    html_parts: list[str] = []
    rendered_image_idx = 0
    for block_idx, block in enumerate(blocks):
        html_parts.append(markdown_to_simple_html("\n".join(block)))
        for url in image_after.get(block_idx, []):
            rendered_image_idx += 1
            html_parts.append(image_card_html(url, f"配图{rendered_image_idx:02d}：对应上方段落"))
    return "\n".join(html_parts)


def markdown_to_clean_rich_html(markdown_text: str, image_urls: list[str]) -> str:
    lines = markdown_text.splitlines()
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.strip():
            current.append(line)
            continue
        if current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    if not blocks:
        return markdown_to_simple_html(markdown_text)
    image_after: dict[int, list[str]] = {}
    for idx, url in enumerate(image_urls):
        block_idx = min(len(blocks) - 1, int((idx + 1) * len(blocks) / (len(image_urls) + 1)))
        image_after.setdefault(block_idx, []).append(url)
    html_parts: list[str] = []
    rendered_image_idx = 0
    for block_idx, block in enumerate(blocks):
        html_parts.append(markdown_to_simple_html("\n".join(block)))
        for url in image_after.get(block_idx, []):
            rendered_image_idx += 1
            html_parts.append(clean_image_html(url, f"配图{rendered_image_idx:02d}：对应上方段落"))
    return "\n".join(html_parts)


def markdown_with_image_markers(markdown_text: str, image_count: int) -> str:
    if image_count <= 0:
        return markdown_text
    lines = markdown_text.splitlines()
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.strip():
            current.append(line)
            continue
        if current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    if not blocks:
        return markdown_text

    markers_after: dict[int, list[int]] = {}
    for idx in range(image_count):
        block_idx = min(len(blocks) - 1, int((idx + 1) * len(blocks) / (image_count + 1)))
        markers_after.setdefault(block_idx, []).append(idx + 1)

    parts: list[str] = []
    for block_idx, block in enumerate(blocks):
        parts.append("\n".join(block))
        for image_idx in markers_after.get(block_idx, []):
            parts.append(f"【插入配图{image_idx:02d}：对应上方段落】")
    return "\n\n".join(parts)


def build_preview_url(config: dict[str, Any], preview_file: Path) -> str:
    preview_cfg = config.get("preview", {})
    public_base_url = str(preview_cfg.get("public_base_url", "")).strip()
    if not public_base_url:
        public_base_url = str(
            config.get("sample_video", {}).get("public_base_url", "")
        ).strip()
    if not public_base_url:
        return ""
    try:
        relative_path = preview_file.relative_to(PREVIEW_DIR)
    except ValueError:
        relative_path = Path(preview_file.name)
    encoded_path = urllib.parse.quote(relative_path.as_posix(), safe="/:@?&=+$,;~")
    return f"{public_base_url.rstrip('/')}/{encoded_path}"


def cache_bust_token(item: dict[str, Any]) -> str:
    raw = str(item.get("updated_at", "")).strip() or str(item.get("created_at", "")).strip()
    token = re.sub(r"\D", "", raw)[:14]
    return token or str(safe_int(item.get("total_score", 0), default=0))


def display_datetime(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = dt.datetime.fromisoformat(raw)
        return parsed.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return raw[:16]


STATUS_LABELS = {
    "pending_review": "待筛选",
    "approved": "已选用",
    "ready_to_post": "待发布",
    "posted": "已发布",
    "rejected": "已丢弃",
    "auto_blocked": "系统拦截",
    "draft": "草稿",
}


PLATFORM_LABELS = {
    "wechat_official": "公众号",
    "xiaohongshu": "小红书",
    "douyin": "抖音",
    "wechat_channels": "视频号",
    "kuaishou": "快手",
}


POST_FORMAT_LABELS = {
    "long_article": "长图文",
    "graphic_post": "图文笔记",
    "short_video_script": "剪映素材",
}


def status_label(status: Any) -> str:
    raw = str(status or "").strip()
    return STATUS_LABELS.get(raw, raw or "未知")


def platform_label(platform: Any) -> str:
    raw = str(platform or "").strip()
    return PLATFORM_LABELS.get(raw, raw or "未知平台")


def post_format_label(post_format: Any) -> str:
    raw = str(post_format or "").strip()
    return POST_FORMAT_LABELS.get(raw, raw or "内容")


def next_step_for_item(item: dict[str, Any]) -> str:
    status = str(item.get("status", "")).strip()
    if status == "posted":
        return "已发布：如需复盘，可记录发布链接和数据。"
    if status == "rejected":
        return "已丢弃：无需处理，也可重新生成同账号内容。"
    if status in {"approved", "ready_to_post"}:
        return "下一步：复制发布文案，按插图标记上传图片，发布后点击“已发布”。"
    if len(item.get("illustration_urls") or []) == 0:
        return "下一步：先点击“重新配图”，再筛选是否发布。"
    return "下一步：检查标题、正文和配图，满意就点击“选用”。"


def split_video_segments(body_markdown: str, limit: int = 8) -> list[str]:
    plain = strip_markdown(body_markdown)
    chunks = [segment.strip() for segment in re.split(r"[。！？!?;\n]+", plain) if segment.strip()]
    if not chunks and plain:
        chunks = [plain]
    return chunks[:limit]


def local_gpu_enabled(config: dict[str, Any]) -> bool:
    return bool(config.get("local_gpu", {}).get("enabled", False))


def video_jobs_root(config: dict[str, Any]) -> Path:
    jobs_dir = str(config.get("local_gpu", {}).get("jobs_dir", "state/video_jobs")).strip()
    if jobs_dir.startswith("/"):
        return Path(jobs_dir)
    return BASE_DIR / jobs_dir


def video_jobs_pending_dir(config: dict[str, Any]) -> Path:
    return video_jobs_root(config) / "pending"


def video_jobs_completed_dir(config: dict[str, Any]) -> Path:
    return video_jobs_root(config) / "completed"


def video_jobs_failed_dir(config: dict[str, Any]) -> Path:
    return video_jobs_root(config) / "failed"


def ensure_video_job_dirs(config: dict[str, Any]) -> None:
    for path in (
        video_jobs_pending_dir(config),
        video_jobs_completed_dir(config),
        video_jobs_failed_dir(config),
    ):
        path.mkdir(parents=True, exist_ok=True)


def filter_queue_items(
    queue: list[dict[str, Any]],
    *,
    item_id: str = "",
    date: str = "",
    account: str = "",
    platform: str = "",
    limit: int = 0,
    only_pending: bool = False,
    include_blocked: bool = False,
    post_format: str = "",
) -> list[dict[str, Any]]:
    filtered = queue
    if item_id:
        return [item for item in filtered if item.get("id") == item_id]
    if date:
        filtered = [item for item in filtered if item.get("date") == date]
    if account:
        needle = account.strip().lower()
        filtered = [
            item
            for item in filtered
            if needle in str(item.get("account_name", "")).lower()
            or needle in str(item.get("account_id", "")).lower()
        ]
    if platform:
        needle = platform.strip().lower()
        filtered = [
            item
            for item in filtered
            if needle == str(item.get("platform", "")).strip().lower()
        ]
    if only_pending:
        filtered = [
            item
            for item in filtered
            if item.get("status") in {"pending_review", "approved", "ready_to_post"}
        ]
    if not include_blocked:
        filtered = [
            item
            for item in filtered
            if item.get("status") != "auto_blocked" and item.get("publish_advice") != "禁发"
        ]
    if post_format:
        filtered = [item for item in filtered if item.get("post_format") == post_format]
    if limit > 0:
        return filtered[-limit:]
    return filtered


def platform_account_id(platform_cfg: dict[str, Any]) -> str:
    explicit = str(platform_cfg.get("account_id", "")).strip()
    if explicit:
        return explicit
    raw = f"{platform_cfg.get('platform', '')}-{platform_cfg.get('account_name', '')}"
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", raw).strip("-").lower()
    return slug or uuid.uuid4().hex[:8]


def find_platform_configs(
    config: dict[str, Any], account: str = "", platform: str = ""
) -> list[dict[str, Any]]:
    platforms = list(config.get("platforms", []))
    account_needle = account.strip().lower()
    platform_needle = platform.strip().lower()
    if account_needle:
        platforms = [
            cfg
            for cfg in platforms
            if account_needle in str(cfg.get("account_name", "")).lower()
            or account_needle in str(cfg.get("account_id", "")).lower()
            or account_needle in platform_account_id(cfg).lower()
        ]
    if platform_needle:
        platforms = [
            cfg
            for cfg in platforms
            if platform_needle == str(cfg.get("platform", "")).strip().lower()
        ]
    return platforms


def build_queue_item(
    config: dict[str, Any],
    date: str,
    platform_cfg: dict[str, Any],
    track_cfg: dict[str, Any],
    track_name: str,
    topic: dict[str, Any],
) -> dict[str, Any]:
    queue_id = uuid.uuid4().hex[:12]
    draft = generate_draft(config, platform_cfg, track_cfg, topic)
    quality = evaluate_draft_quality(config, platform_cfg, track_cfg, topic, draft)
    output_dir = OUTBOX_DIR / date / platform_cfg["platform"]
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{queue_id}.md"
    markdown = render_markdown(queue_id, platform_cfg, topic, draft, quality=quality)
    output_file.write_text(markdown, encoding="utf-8")
    return {
        "id": queue_id,
        "date": date,
        "status": "pending_review",
        "platform": platform_cfg["platform"],
        "account_id": platform_account_id(platform_cfg),
        "account_name": platform_cfg["account_name"],
        "track": track_name,
        "publish_time": platform_cfg.get("publish_time"),
        "auto_publish": bool(platform_cfg.get("auto_publish", False)),
        "post_format": platform_cfg.get("post_format", "generic"),
        "title": draft.get("title", ""),
        "hashtags": draft.get("hashtags", []),
        "source_topic": topic.get("title"),
        "source_link": topic.get("link"),
        "content_file": str(output_file),
        "hook_text": str(draft.get("hook", "")).strip(),
        "body_markdown": str(draft.get("body_markdown", "")).strip(),
        "cover_text": str(draft.get("cover_text", "")).strip(),
        "body_preview": strip_markdown(str(draft.get("body_markdown", "")))[:320],
        "content_markdown": markdown[:8000],
        "sample_video_file": "",
        "sample_video_url": "",
        "sample_audio_file": "",
        "created_at": now_local().isoformat(),
        "updated_at": now_local().isoformat(),
        "post_url": None,
        "notes": "",
        "hook_score": quality["hook_score"],
        "structure_score": quality["structure_score"],
        "platform_fit_score": quality["platform_fit_score"],
        "commercial_score": quality["commercial_score"],
        "compliance_score": quality["compliance_score"],
        "total_score": quality["total_score"],
        "quality_level": quality["quality_level"],
        "quality_badge": quality["quality_badge"],
        "publish_advice": quality["publish_advice"],
        "quality_reason": quality["reason"],
    }


def build_video_script_text(item: dict[str, Any]) -> str:
    content = extract_content_payload(item)
    hook = content.get("hook_text", "").strip()
    body = strip_markdown(content.get("body_markdown", "")).strip()
    if hook and body:
        return f"{hook}\n\n{body}"
    return hook or body or str(item.get("title", "")).strip()


def build_cloud_image_prompts(item: dict[str, Any], count: int = 3) -> list[str]:
    content = extract_content_payload(item)
    segments = split_video_segments(content.get("body_markdown", ""), limit=max(3, count + 1))
    title = str(item.get("title", "")).strip()
    track = str(item.get("track", "")).strip()
    style_map = {
        "football": "sports editorial illustration, tactical board, dramatic stadium lighting",
        "child_education": "warm parenting education scene, lifestyle photography style",
        "travel": "travel guide editorial illustration, cinematic destination view",
        "ai_funny": "comic digital illustration, vivid expressive characters",
    }
    style = style_map.get(track, "editorial illustration, clean visual storytelling")
    prompts: list[str] = []
    for idx in range(count):
        seed_text = segments[idx] if idx < len(segments) else title
        prompts.append(
            f"Chinese social media article illustration, no text overlay, {style}. "
            f"Topic: {title}. Scene: {seed_text[:160]}"
        )
    return prompts


def _svg_escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def create_placeholder_illustrations_for_item(
    config: dict[str, Any], item: dict[str, Any], count: int = 3
) -> dict[str, Any]:
    queue_id = str(item.get("id", uuid.uuid4().hex[:12]))
    item_date = str(item.get("date", now_local().strftime("%Y-%m-%d")))
    media_dir = PREVIEW_DIR / "media" / item_date / "illustrations"
    media_dir.mkdir(parents=True, exist_ok=True)
    title = str(item.get("title", "内容插图")).strip()[:36]
    subtitle = str(item.get("track", "")).strip() or str(item.get("platform", "")).strip()
    palette = [("#1d4ed8", "#0ea5e9"), ("#7c3aed", "#ec4899"), ("#059669", "#10b981")]
    files: list[str] = []
    urls: list[str] = []
    for idx in range(max(1, count)):
        c1, c2 = palette[idx % len(palette)]
        svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">
  <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0%" stop-color="{c1}"/><stop offset="100%" stop-color="{c2}"/>
  </linearGradient></defs>
  <rect width="1024" height="1024" fill="url(#g)"/>
  <rect x="80" y="80" width="864" height="864" rx="36" fill="rgba(255,255,255,0.15)"/>
  <text x="120" y="220" font-size="56" font-family="Microsoft YaHei, sans-serif" fill="white">{_svg_escape(title)}</text>
  <text x="120" y="300" font-size="34" font-family="Microsoft YaHei, sans-serif" fill="white" opacity="0.9">{_svg_escape(subtitle)}</text>
  <text x="120" y="900" font-size="26" font-family="Microsoft YaHei, sans-serif" fill="white" opacity="0.8">Auto Illustration Placeholder #{idx+1}</text>
</svg>"""
        out_file = media_dir / f"{queue_id}_placeholder_{idx+1}.svg"
        out_file.write_text(svg, encoding="utf-8")
        files.append(str(out_file))
        urls.append(build_preview_url(config, out_file))

    item["illustration_files"] = files
    item["illustration_urls"] = urls
    item["cloud_image_model"] = "placeholder-svg-fallback"
    item["updated_at"] = now_local().isoformat()
    return {"status": "ok", "count": len(files), "urls": urls, "fallback": True}


def build_asset_pack_for_item(config: dict[str, Any], item: dict[str, Any]) -> dict[str, str]:
    queue_id = str(item.get("id", uuid.uuid4().hex[:12]))
    item_date = str(item.get("date", now_local().strftime("%Y-%m-%d")))
    pack_dir = PREVIEW_DIR / "media" / item_date / "packs"
    pack_dir.mkdir(parents=True, exist_ok=True)
    account_name = str(item.get("account_name", "account")).strip()
    title = str(item.get("title", "content")).strip()
    score = safe_int(item.get("total_score", 0), default=0)
    platform = str(item.get("platform", "")).strip()
    status = str(item.get("status", "")).strip()
    pack_name = "_".join(
        [
            sanitize_filename_part(account_name, 20),
            sanitize_filename_part(platform, 16),
            item_date,
            f"{score}分",
            sanitize_filename_part(title, 32),
            queue_id,
        ]
    )
    pack_file = pack_dir / f"{pack_name}.zip"
    content = extract_content_payload(item)
    hashtags = item.get("hashtags", [])
    hashtag_line = " ".join(hashtags) if isinstance(hashtags, list) else str(hashtags)
    image_files = [Path(str(path)) for path in item.get("illustration_files") or []]
    image_manifest_lines = ["# 配图对应关系", ""]
    segments = split_video_segments(content.get("body_markdown", ""), limit=max(3, len(image_files)))
    if str(item.get("post_format", "")) == "short_video_script":
        for idx, local_path in enumerate(image_files, start=1):
            scene = segments[idx - 1] if idx - 1 < len(segments) else str(item.get("title", ""))
            image_manifest_lines.append(f"- 图片 {idx:02d}: 对应第 {idx} 镜 / {scene}")
    else:
        for idx, local_path in enumerate(image_files, start=1):
            image_manifest_lines.append(f"- 图片 {idx:02d}: 插入正文第 {idx} 个重点段落附近")

    publish_steps = textwrap.dedent(
        f"""
        # 发布步骤

        ## 基本信息
        - 账号：{account_name}
        - 平台：{platform}
        - 日期：{item_date}
        - 评分：{score}
        - 状态：{status}
        - Queue ID：{queue_id}

        ## 公众号/小红书发布
        1. 打开 `01_content/full_publish_text.md`。
        2. 复制标题和正文到发布后台。
        3. 按 `02_images/image_manifest.md` 的位置插入图片。
        4. 复制 `01_content/hashtags.txt` 中的话题/标签。
        5. 发布前检查封面文案和敏感词。

        ## 剪映素材使用
        1. 打开 `01_content/capcut_script.txt`，复制为配音/字幕文本。
        2. 将 `02_images/` 下的图片按编号导入剪映。
        3. 按 `02_images/image_manifest.md` 对应关系排列画面。
        4. 导出视频后回到工作台，将内容状态改为“已发布”。

        ## 文件说明
        - `01_content/`: 可复制文案、标题、正文、剪映口播稿、标签。
        - `02_images/`: 配图文件和对应关系。
        - `03_publish_steps/`: 发布步骤说明。
        - `04_metadata/`: 机器可读元数据。
        """
    ).strip()
    with zipfile.ZipFile(pack_file, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        content_file = str(item.get("content_file", "")).strip()
        if content_file and Path(content_file).exists():
            zf.write(content_file, "01_content/original_markdown.md")
        else:
            zf.writestr("01_content/original_markdown.md", content.get("content_markdown", ""))
        full_publish_text = "\n\n".join(
            [
                part
                for part in [
                    str(item.get("title", "")).strip(),
                    content.get("hook_text", ""),
                    markdown_with_image_markers(content.get("body_markdown", ""), len(image_files)),
                    content.get("cover_text", ""),
                    hashtag_line,
                ]
                if str(part).strip()
            ]
        )
        zf.writestr("01_content/title.txt", str(item.get("title", "")).strip())
        zf.writestr("01_content/body.md", content.get("body_markdown", ""))
        zf.writestr("01_content/full_publish_text.md", full_publish_text)
        zf.writestr("01_content/capcut_script.txt", strip_markdown(content.get("body_markdown", "")))
        zf.writestr("01_content/hashtags.txt", hashtag_line)
        zf.writestr("02_images/image_manifest.md", "\n".join(image_manifest_lines))
        for idx, local_path in enumerate(image_files, start=1):
            if local_path.exists():
                suffix = local_path.suffix or ".jpg"
                zf.write(local_path, f"02_images/{idx:02d}_{sanitize_filename_part(title, 18)}{suffix}")
        zf.writestr("03_publish_steps/README.md", publish_steps)
        zf.writestr(
            "04_metadata/item.json",
            json.dumps(
                {
                    "id": queue_id,
                    "date": item_date,
                    "account_name": account_name,
                    "platform": platform,
                    "title": title,
                    "score": score,
                    "status": status,
                    "preview_url": item.get("preview_url", ""),
                    "image_count": len(image_files),
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    item["asset_pack_file"] = str(pack_file)
    item["asset_pack_url"] = build_preview_url(config, pack_file)
    item["updated_at"] = now_local().isoformat()
    return {"asset_pack_file": str(pack_file), "asset_pack_url": item["asset_pack_url"]}


def build_bulk_asset_pack(
    config: dict[str, Any],
    queue: list[dict[str, Any]],
    *,
    date: str = "",
    status: str = "all",
) -> dict[str, str]:
    target_date = date or max([str(item.get("date", "")) for item in queue] or [now_local().strftime("%Y-%m-%d")])
    selected = [item for item in queue if item.get("date") == target_date]
    if status == "selected":
        selected = [
            item
            for item in selected
            if item.get("status") in {"approved", "ready_to_post", "posted"}
        ]
    elif status and status != "all":
        selected = [item for item in selected if item.get("status") == status]
    if not selected:
        raise ValueError("No items matched bulk asset pack filters.")

    bulk_dir = PREVIEW_DIR / "media" / target_date / "bulk"
    bulk_dir.mkdir(parents=True, exist_ok=True)
    suffix = "selected" if status == "selected" else status or "all"
    bulk_file = bulk_dir / f"{target_date}_{sanitize_filename_part(suffix, 20)}_asset_packs.zip"
    newest_item_update = max(
        [
            str(item.get("updated_at") or item.get("created_at") or "")
            for item in selected
        ]
        or [""]
    )
    marker_file = bulk_dir / f"{bulk_file.stem}.meta"
    if bulk_file.exists() and marker_file.exists() and marker_file.read_text(encoding="utf-8") == newest_item_update:
        return {"bulk_pack_file": str(bulk_file), "bulk_pack_url": build_preview_url(config, bulk_file)}
    with zipfile.ZipFile(bulk_file, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        manifest_lines = [f"# 批量素材包 {target_date}", ""]
        for item in selected:
            build_asset_pack_for_item(config, item)
            pack_path = Path(str(item.get("asset_pack_file", "")))
            folder = "_".join(
                [
                    sanitize_filename_part(item.get("account_name", ""), 16),
                    sanitize_filename_part(item.get("platform", ""), 14),
                    sanitize_filename_part(item.get("title", ""), 28),
                    str(item.get("id", "")),
                ]
            )
            manifest_lines.append(
                f"- {item.get('account_name', '')} / {platform_label(item.get('platform'))} / "
                f"{status_label(item.get('status'))} / {item.get('title', '')}"
            )
            if pack_path.exists():
                zf.write(pack_path, f"{folder}/{pack_path.name}")
        zf.writestr("README.md", "\n".join(manifest_lines))
    marker_file.write_text(newest_item_update, encoding="utf-8")
    return {"bulk_pack_file": str(bulk_file), "bulk_pack_url": build_preview_url(config, bulk_file)}


def write_cloud_asset_from_url(url: str, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    blob = http_get_bytes(url, timeout=120)
    target.write_bytes(blob)
    return target


def render_cloud_illustrations_for_item(config: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    media_cfg = config.get("cloud_media", {})
    image_cfg = media_cfg.get("image", {})
    if not media_cfg.get("enabled", False) or not image_cfg.get("enabled", False):
        return {"status": "disabled"}
    provider = str(image_cfg.get("provider", "replicate")).lower().strip()
    if provider not in {"replicate", "dashscope"}:
        raise ValueError(f"Unsupported cloud image provider: {provider}")

    queue_id = str(item.get("id", uuid.uuid4().hex[:12]))
    item_date = str(item.get("date", now_local().strftime("%Y-%m-%d")))
    count = max(1, int(image_cfg.get("images_per_item", 3)))
    prompts = build_cloud_image_prompts(item, count=count)
    media_dir = PREVIEW_DIR / "media" / item_date / "illustrations"
    urls: list[str] = []
    files: list[str] = []
    used_model = ""
    image_models = dashscope_model_candidates(
        image_cfg,
        defaults=["wanx-v1", "wan2.5-t2i-preview", "wan2.2-t2i-flash", "wan2.2-t2i-plus"],
    )
    for idx, prompt in enumerate(prompts, start=1):
        if provider == "replicate":
            input_candidates = [
                {
                    "prompt": prompt,
                    "aspect_ratio": str(image_cfg.get("aspect_ratio", "9:16")),
                    "output_format": str(image_cfg.get("output_format", "jpg")),
                    "num_outputs": 1,
                },
                {"prompt": prompt, "num_outputs": 1},
                {"prompt": prompt},
            ]
            prediction = replicate_create_prediction_candidates(image_cfg, input_candidates)
            prediction_id = str(prediction.get("id", "")).strip()
            if not prediction_id:
                raise RuntimeError(f"Replicate image prediction failed: {prediction}")
            done = replicate_poll_prediction(image_cfg, prediction_id)
            out_urls = normalize_prediction_urls(done.get("output"))
        else:
            last_error: Exception | None = None
            created: dict[str, Any] | None = None
            for candidate_model in image_models:
                payload = {
                    "model": candidate_model,
                    "input": {"prompt": prompt},
                    "parameters": {
                        "size": str(image_cfg.get("size", "1024*1024")),
                        "n": 1,
                        "negative_prompt": str(image_cfg.get("negative_prompt", "")),
                    },
                }
                try:
                    created = dashscope_create_task(
                        image_cfg, "/services/aigc/text2image/image-synthesis", payload
                    )
                    used_model = candidate_model
                    break
                except Exception as exc:  # pylint: disable=broad-except
                    last_error = exc
                    if is_dashscope_model_retryable_error(exc):
                        continue
                    raise
            if created is None:
                raise RuntimeError(f"DashScope image models unavailable: {last_error}")
            task_id = dashscope_extract_task_id(created)
            if not task_id:
                raise RuntimeError(f"DashScope image task create failed: {created}")
            done = dashscope_poll_task(image_cfg, task_id)
            out_urls = normalize_prediction_urls(done)
        if not out_urls:
            raise RuntimeError(
                f"No image URL returned from cloud provider. raw={json.dumps(done, ensure_ascii=False)[:1200]}"
            )
        source_url = out_urls[0]
        ext = ".jpg" if image_cfg.get("output_format", "jpg") == "jpg" else f".{image_cfg.get('output_format')}"
        local_file = media_dir / f"{queue_id}_{idx}{ext}"
        write_cloud_asset_from_url(source_url, local_file)
        files.append(str(local_file))
        urls.append(build_preview_url(config, local_file))

    item["illustration_files"] = files
    item["illustration_urls"] = urls
    if used_model:
        item["cloud_image_model"] = used_model
    item["updated_at"] = now_local().isoformat()
    return {"status": "ok", "count": len(files), "urls": urls}


def render_cloud_video_for_item(config: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    media_cfg = config.get("cloud_media", {})
    video_cfg = media_cfg.get("video", {})
    if not media_cfg.get("enabled", False) or not video_cfg.get("enabled", False):
        return {"status": "disabled"}
    provider = str(video_cfg.get("provider", "replicate")).lower().strip()
    if provider not in {"replicate", "dashscope"}:
        raise ValueError(f"Unsupported cloud video provider: {provider}")

    queue_id = str(item.get("id", uuid.uuid4().hex[:12]))
    item_date = str(item.get("date", now_local().strftime("%Y-%m-%d")))
    script_text = build_video_script_text(item)
    prompt = (
        f"Create a vertical short social video for this script. "
        f"Title: {item.get('title','')}. Script: {script_text[:1200]}"
    )
    used_model = ""
    video_models = dashscope_model_candidates(
        video_cfg,
        defaults=["wan2.6-t2v", "wan2.7-t2v-2026-04", "wanx2.1-t2v-turbo"],
    )
    if provider == "replicate":
        input_candidates = [
            {
                "prompt": prompt,
                "aspect_ratio": str(video_cfg.get("aspect_ratio", "9:16")),
                "duration": int(video_cfg.get("duration_seconds", 8)),
            },
            {"prompt": prompt, "duration": int(video_cfg.get("duration_seconds", 8))},
            {"prompt": prompt},
        ]
        prediction = replicate_create_prediction_candidates(video_cfg, input_candidates)
        prediction_id = str(prediction.get("id", "")).strip()
        if not prediction_id:
            raise RuntimeError(f"Replicate video prediction failed: {prediction}")
        done = replicate_poll_prediction(video_cfg, prediction_id)
        out_urls = normalize_prediction_urls(done.get("output"))
    else:
        last_error: Exception | None = None
        created: dict[str, Any] | None = None
        for candidate_model in video_models:
            payload = {
                "model": candidate_model,
                "input": {"prompt": prompt},
                "parameters": {
                    "size": str(video_cfg.get("size", "720*1280")),
                    "duration": int(video_cfg.get("duration_seconds", 8)),
                },
            }
            try:
                created = dashscope_create_task(
                    video_cfg, "/services/aigc/video-generation/video-synthesis", payload
                )
                used_model = candidate_model
                break
            except Exception as exc:  # pylint: disable=broad-except
                last_error = exc
                if is_dashscope_model_retryable_error(exc):
                    continue
                raise
        if created is None:
            raise RuntimeError(f"DashScope video models unavailable: {last_error}")
        prediction_id = dashscope_extract_task_id(created)
        if not prediction_id:
            raise RuntimeError(f"DashScope video task create failed: {created}")
        done = dashscope_poll_task(video_cfg, prediction_id)
        out_urls = normalize_prediction_urls(done)
    if not out_urls:
        raise RuntimeError(
            f"No video URL from prediction {prediction_id}. raw={json.dumps(done, ensure_ascii=False)[:1200]}"
        )
    source_url = out_urls[0]
    media_dir = PREVIEW_DIR / "media" / item_date
    media_dir.mkdir(parents=True, exist_ok=True)
    out_video = media_dir / f"{queue_id}.mp4"
    write_cloud_asset_from_url(source_url, out_video)

    item["sample_video_file"] = str(out_video)
    item["sample_video_url"] = build_preview_url(config, out_video)
    item["cloud_video_provider"] = provider
    if used_model:
        item["cloud_video_model"] = used_model
    item["cloud_video_prediction_id"] = prediction_id
    item["updated_at"] = now_local().isoformat()
    return {
        "status": "ok",
        "id": queue_id,
        "video_file": str(out_video),
        "video_url": item["sample_video_url"],
        "prediction_id": prediction_id,
    }


def render_cloud_illustrations_for_items(
    config: dict[str, Any], items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    image_cfg = config.get("cloud_media", {}).get("image", {})
    item_delay = float(image_cfg.get("item_delay_seconds", 1.5))
    for item in items:
        if item.get("post_format") not in {"long_article", "graphic_post", "short_video_script"}:
            continue
        try:
            result = render_cloud_illustrations_for_item(config, item)
            results.append({"id": item.get("id"), **result})
            if result.get("status") == "ok":
                print(f"[OK] illustrations rendered {item.get('id')} -> {result.get('count', 0)} image(s)")
        except Exception as exc:  # pylint: disable=broad-except
            if bool(image_cfg.get("allow_placeholder_fallback", True)):
                fallback = create_placeholder_illustrations_for_item(
                    config, item, count=int(image_cfg.get("images_per_item", 3))
                )
                results.append({"id": item.get("id"), **fallback})
                print(
                    f"[WARN] cloud illustrations failed {item.get('id')}: {exc} | "
                    "used placeholder fallback"
                )
                print(
                    f"[OK] illustrations fallback {item.get('id')} -> "
                    f"{fallback.get('count', 0)} image(s)"
                )
            else:
                item["notes"] = (str(item.get("notes", "")).strip() + f" | 云插图失败: {exc}").strip(" |")
                item["updated_at"] = now_local().isoformat()
                print(f"[WARN] cloud illustrations failed {item.get('id')}: {exc}")
        if item_delay > 0:
            time.sleep(item_delay)
    return results


def render_cloud_videos_for_items(
    config: dict[str, Any], items: list[dict[str, Any]], *, include_blocked: bool = False
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    video_cfg = config.get("cloud_media", {}).get("video", {})
    item_delay = float(video_cfg.get("item_delay_seconds", 2.5))
    for item in items:
        if item.get("post_format") != "short_video_script":
            continue
        if not include_blocked and (
            item.get("status") == "auto_blocked" or item.get("publish_advice") == "禁发"
        ):
            continue
        try:
            result = render_cloud_video_for_item(config, item)
            results.append({"id": item.get("id"), **result})
            if result.get("status") == "ok":
                print(f"[OK] cloud video rendered {item.get('id')} -> {result.get('video_file','')}")
        except Exception as exc:  # pylint: disable=broad-except
            if bool(video_cfg.get("allow_server_fallback", True)):
                try:
                    fallback = render_sample_video_for_item(config, item)
                    item["cloud_video_provider"] = "server-ffmpeg-fallback"
                    item["updated_at"] = now_local().isoformat()
                    results.append({"id": item.get("id"), **fallback, "fallback": True})
                    print(
                        f"[WARN] cloud video failed {item.get('id')}: {exc} | "
                        "used server fallback video"
                    )
                    print(
                        f"[OK] cloud video fallback {item.get('id')} -> "
                        f"{fallback.get('video_file', '')}"
                    )
                except Exception as fallback_exc:  # pylint: disable=broad-except
                    item["notes"] = (
                        str(item.get("notes", "")).strip()
                        + f" | 云视频失败: {exc} | 服务器兜底失败: {fallback_exc}"
                    ).strip(" |")
                    item["updated_at"] = now_local().isoformat()
                    print(f"[WARN] cloud video + fallback failed {item.get('id')}: {fallback_exc}")
            else:
                item["notes"] = (str(item.get("notes", "")).strip() + f" | 云视频失败: {exc}").strip(" |")
                item["updated_at"] = now_local().isoformat()
                print(f"[WARN] cloud video failed {item.get('id')}: {exc}")
        if item_delay > 0:
            time.sleep(item_delay)
    return results


def build_video_job_payload(config: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    local_cfg = config.get("local_gpu", {})
    comfy_cfg = local_cfg.get("comfyui", {})
    queue_id = str(item.get("id", uuid.uuid4().hex[:12]))
    item_date = str(item.get("date", now_local().strftime("%Y-%m-%d")))
    content = extract_content_payload(item)
    script_text = build_video_script_text(item)
    return {
        "job_id": queue_id,
        "date": item_date,
        "platform": str(item.get("platform", "")),
        "account_name": str(item.get("account_name", "")),
        "track": str(item.get("track", "")),
        "title": str(item.get("title", "")).strip(),
        "hook_text": content.get("hook_text", "").strip(),
        "cover_text": content.get("cover_text", "").strip(),
        "script_text": script_text,
        "output_filename": f"{queue_id}.mp4",
        "remote_media_path": f"{item_date}/{queue_id}.mp4",
        "status": "pending",
        "created_at": now_local().isoformat(),
        "comfyui_url": str(local_cfg.get("comfyui_url", "http://127.0.0.1:8000")).rstrip("/"),
        "comfyui": {
            "url": str(comfy_cfg.get("url", local_cfg.get("comfyui_url", "http://127.0.0.1:8000"))).rstrip("/"),
            "render_mode": str(comfy_cfg.get("render_mode", "slideshow")),
            "workflow_file": str(comfy_cfg.get("workflow_file", "short_video.api.json")),
            "poll_interval_seconds": int(comfy_cfg.get("poll_interval_seconds", 3)),
            "timeout_seconds": int(comfy_cfg.get("timeout_seconds", 3600)),
        },
        "upload": local_cfg.get("upload", {}),
    }


def export_video_jobs_for_items(
    config: dict[str, Any], items: list[dict[str, Any]]
) -> list[dict[str, str]]:
    if not local_gpu_enabled(config):
        return []
    ensure_video_job_dirs(config)
    pending_dir = video_jobs_pending_dir(config)
    results: list[dict[str, str]] = []
    for item in items:
        if item.get("post_format") != "short_video_script":
            continue
        queue_id = str(item.get("id", "")).strip()
        if not queue_id:
            continue
        payload = build_video_job_payload(config, item)
        job_file = pending_dir / f"{queue_id}.json"
        save_json(job_file, payload)
        item["video_job_status"] = "exported"
        item["video_job_file"] = str(job_file)
        item["updated_at"] = now_local().isoformat()
        results.append(
            {
                "id": queue_id,
                "status": "exported",
                "job_file": str(job_file),
            }
        )
        print(f"[OK] video job exported {queue_id} -> {job_file}")
    return results


def import_local_video_for_item(
    config: dict[str, Any],
    queue: list[dict[str, Any]],
    job_id: str,
    video_path: Path | None = None,
    *,
    regenerate_preview: bool = True,
) -> dict[str, str]:
    item = next((entry for entry in queue if entry.get("id") == job_id), None)
    if item is None:
        raise ValueError(f"Queue item not found: {job_id}")

    item_date = str(item.get("date", now_local().strftime("%Y-%m-%d")))
    media_dir = PREVIEW_DIR / "media" / item_date
    media_dir.mkdir(parents=True, exist_ok=True)
    target_video = media_dir / f"{job_id}.mp4"

    source = video_path
    if source is None:
        completed_job = video_jobs_completed_dir(config) / f"{job_id}.json"
        if completed_job.exists():
            job_meta = load_json(completed_job, {})
            candidate = str(job_meta.get("local_video_file", "")).strip()
            if candidate:
                source = Path(candidate)
        if source is None and target_video.exists():
            source = target_video
    if source is None or not source.exists():
        raise FileNotFoundError(f"Video file not found for job {job_id}")

    if source.resolve() != target_video.resolve():
        shutil.copy2(source, target_video)

    item["sample_video_file"] = str(target_video)
    item["sample_video_url"] = build_preview_url(config, target_video)
    item["video_job_status"] = "completed"
    item["updated_at"] = now_local().isoformat()
    if regenerate_preview:
        generate_preview_for_item(config, item)
    return {
        "status": "ok",
        "id": job_id,
        "video_file": str(target_video),
        "video_url": item.get("sample_video_url", ""),
    }


def synthesize_tts_segment(
    segment_text: str,
    segment_audio: Path,
    sample_cfg: dict[str, Any],
    tts_binary: str | None,
) -> None:
    engine = str(sample_cfg.get("tts_engine", "espeak-ng")).strip().lower()
    voice = str(sample_cfg.get("tts_voice", "zh-CN-XiaoxiaoNeural"))
    speed = int(sample_cfg.get("tts_speed", 165))

    if engine == "edge-tts":
        binary = tts_binary or ensure_binary("edge-tts")
        run_command(
            [
                binary,
                "--voice",
                voice,
                "--text",
                segment_text,
                "--write-media",
                str(segment_audio),
            ]
        )
        return

    binary = tts_binary or ensure_binary("espeak-ng")
    run_command(
        [
            binary,
            "-v",
            voice,
            "-s",
            str(speed),
            "-w",
            str(segment_audio),
            segment_text,
        ]
    )


def build_preview_html(config: dict[str, Any], item: dict[str, Any], content: dict[str, str]) -> str:
    queue_id = str(item.get("id", uuid.uuid4().hex[:12]))
    raw_title = str(item.get("title", "")).strip() or "未命名草稿"
    raw_hook = content.get("hook_text", "").strip()
    raw_body = content.get("body_markdown", "").strip()
    raw_cover = content.get("cover_text", "").strip()
    hashtags = item.get("hashtags", [])
    hashtag_line = " ".join(hashtags) if isinstance(hashtags, list) else str(hashtags)
    publish_text = "\n\n".join(
        [part for part in [raw_title, raw_hook, raw_body, raw_cover, hashtag_line] if part]
    )
    capcut_text = strip_markdown(raw_body or raw_hook or raw_title)
    asset_pack_url = str(item.get("asset_pack_url", "")).strip()
    if not asset_pack_url:
        asset_pack_url = build_preview_url(config, PREVIEW_DIR / "media" / str(item.get("date", "")) / "packs" / f"{queue_id}_publish_pack.zip")
    title = html.escape(str(item.get("title", "")).strip() or "未命名草稿")
    account = html.escape(str(item.get("account_name", "")).strip())
    platform = html.escape(str(item.get("platform", "")).strip())
    publish_time = html.escape(str(item.get("publish_time", "")).strip())
    source_topic = html.escape(str(item.get("source_topic", "")).strip())
    source_link = html.escape(str(item.get("source_link", "")).strip())
    hook_text = html.escape(content.get("hook_text", "").strip())
    cover_text = html.escape(content.get("cover_text", "").strip())
    badge = html.escape(str(item.get("quality_badge", "⚪")))
    score = safe_int(item.get("total_score", 0), default=0)
    advice = html.escape(str(item.get("publish_advice", "需改")))
    reason = html.escape(str(item.get("quality_reason", "")).strip())
    raw_status = str(item.get("status", "")).strip()
    status = html.escape(status_label(raw_status))
    next_step = html.escape(next_step_for_item(item))
    updated_time = html.escape(display_datetime(item.get("updated_at") or item.get("created_at")))
    body_html = markdown_to_simple_html(content.get("body_markdown", ""))
    post_format = str(item.get("post_format", ""))

    metadata_html = (
        f"<div class='meta'><span>{badge} {score}/100 · {advice}</span>"
        f"<span>状态: {status}</span><span>发布时间: {publish_time}</span><span>更新时间: {updated_time or '无'}</span></div>"
        f"<div class='meta'><span>账号: {account}</span><span>平台: {platform}</span></div>"
        f"<div class='meta'><span>选题: {source_topic}</span>"
        f"<a href='{source_link}' target='_blank' rel='noreferrer'>来源链接</a></div>"
    )

    sample_video_url = str(item.get("sample_video_url", "")).strip()
    sample_video_file = str(item.get("sample_video_file", "")).strip()
    if not sample_video_url and sample_video_file:
        sample_video_url = build_preview_url(config, Path(sample_video_file))
    illustration_urls = item.get("illustration_urls", [])
    if not illustration_urls:
        files = item.get("illustration_files", [])
        if isinstance(files, list):
            illustration_urls = [
                build_preview_url(config, Path(path))
                for path in files
                if str(path).strip()
            ]
    if not isinstance(illustration_urls, list):
        illustration_urls = []
    body_html = markdown_to_html_with_inline_images(content.get("body_markdown", ""), illustration_urls)
    clean_body_html = markdown_to_clean_rich_html(content.get("body_markdown", ""), illustration_urls)
    marker_publish_text = "\n\n".join(
        [
            part
            for part in [
                raw_title,
                raw_hook,
                markdown_with_image_markers(raw_body, len(illustration_urls)),
                raw_cover,
                hashtag_line,
            ]
            if part
        ]
    )
    rich_publish_html = (
        "<article class='rich-article'>"
        f"<h1>{title}</h1>"
        f"<p class='rich-hook'>{hook_text}</p>"
        f"{clean_body_html}"
        f"<p class='rich-cover'>封面文案：{cover_text}</p>"
        f"<p class='rich-tags'>{html.escape(hashtag_line)}</p>"
        "</article>"
    )
    copy_panel = (
        "<div class='copy-panel'>"
        "<h3>发布素材复制区</h3>"
        "<p class='copy-hint'>提示：公众号/小红书建议优先用“复制带图片位置文案”，再按标记上传图片；剪映用“复制剪映口播稿”+素材包图片。</p>"
        "<div class='copy-actions'>"
        f"<button type='button' data-copy-target='copy-title-{queue_id}' onclick='copyTarget(this)'>复制标题</button>"
        f"<button type='button' data-copy-target='copy-body-{queue_id}' onclick='copyTarget(this)'>复制正文</button>"
        f"<button type='button' data-copy-target='copy-full-{queue_id}' onclick='copyTarget(this)'>复制完整发布文案</button>"
        f"<button type='button' data-copy-target='copy-marker-{queue_id}' onclick='copyTarget(this)'>复制带图片位置文案</button>"
        f"<button type='button' data-rich-target='rich-copy-{queue_id}' onclick='copyRichTarget(this)'>复制公众号图文（含图片）</button>"
        f"<button type='button' data-copy-target='copy-capcut-{queue_id}' onclick='copyTarget(this)'>复制剪映口播稿</button>"
        f"<a class='download-pack' href='{html.escape(asset_pack_url)}' target='_blank' rel='noreferrer'>下载素材包</a>"
        "</div>"
        "<div class='status-actions'>"
        f"<button type='button' onclick=\"setStatus('{queue_id}','approved',this)\">选用</button>"
        f"<button type='button' onclick=\"setStatus('{queue_id}','posted',this)\">已发布</button>"
        f"<button type='button' class='danger' onclick=\"setStatus('{queue_id}','rejected',this)\">丢弃</button>"
        f"<button type='button' class='secondary' onclick=\"runAction('{queue_id}','images',this)\">重新配图</button>"
        f"<button type='button' class='secondary' onclick=\"runAction('{queue_id}','pack',this)\">重建素材包</button>"
        "<span class='status-result'></span>"
        "</div>"
        f"<div class='next-step'>运营提示：{next_step}</div>"
        f"<textarea id='copy-title-{queue_id}'>{html.escape(raw_title)}</textarea>"
        f"<textarea id='copy-body-{queue_id}'>{html.escape(raw_body)}</textarea>"
        f"<textarea id='copy-full-{queue_id}'>{html.escape(publish_text)}</textarea>"
        f"<textarea id='copy-marker-{queue_id}'>{html.escape(marker_publish_text)}</textarea>"
        f"<textarea id='copy-capcut-{queue_id}'>{html.escape(capcut_text)}</textarea>"
        "<details class='rich-copy-details'>"
        "<summary>公众号富文本复制区（按钮失败时，展开后框选整块 Ctrl+C）</summary>"
        f"<div id='rich-copy-{queue_id}' class='rich-copy-area' contenteditable='true'>{rich_publish_html}</div>"
        "</details>"
        "</div>"
    )
    gallery_html = ""
    if illustration_urls:
        cards = "".join(
            [
                (
                    f"<div class='ill-card'><img src='{html.escape(str(url))}' loading='lazy' />"
                    "</div>"
                )
                for url in illustration_urls
            ]
        )
        gallery_html = f"<div class='ill-gallery'><h3>文案自动插图/剪映素材</h3><div class='ill-grid'>{cards}</div></div>"

    if post_format == "short_video_script":
        segments = split_video_segments(content.get("body_markdown", ""))
        timeline_rows: list[str] = []
        for idx, segment in enumerate(segments):
            start = idx * 4
            end = start + 4
            segment_image = ""
            if illustration_urls:
                segment_image = image_card_html(
                    str(illustration_urls[idx % len(illustration_urls)]),
                    f"第 {idx + 1} 镜配图",
                )
            timeline_rows.append(
                "<li>"
                f"<span class='time'>{start:02d}s-{end:02d}s</span>"
                f"<span class='line'>{html.escape(segment)}</span>"
                f"{segment_image}"
                "</li>"
            )
        timeline_html = "\n".join(timeline_rows) if timeline_rows else "<li><span class='line'>暂无分镜</span></li>"
        narration_text = html.escape(strip_markdown(content.get("body_markdown", "")).strip())
        image_hint = "<div class='video-missing'>暂未配图，请执行 render-illustrations。配图后可直接下载导入剪映。</div>" if not illustration_urls else ""
        content_card = (
            "<div class='phone video'>"
            f"{copy_panel}"
            "<div class='video-cover'>"
            f"<div class='cover-text'>{cover_text or title}</div>"
            f"<div class='updated-time'>更新时间：{updated_time or '无'}</div>"
            f"<div class='video-hook'>{hook_text}</div>"
            "</div>"
            "<div class='timeline'><h3>剪映口播稿</h3>"
            f"<div class='script-box'>{narration_text}</div>"
            "<h3>分镜/画面节奏</h3><ol>"
            f"{timeline_html}"
            "</ol></div>"
            f"{image_hint}"
            "<div class='cover'>剪映使用：下载上方配图，按分镜顺序导入；口播稿可直接复制为字幕/配音文本。</div>"
            "</div>"
        )
    else:
        content_card = (
            "<div class='phone article'>"
            f"{copy_panel}"
            f"<h1>{title}</h1>"
            f"<div class='updated-time'>更新时间：{updated_time or '无'}</div>"
            f"<div class='hook'>{hook_text}</div>"
            f"<div class='body'>{body_html}</div>"
            f"<div class='cover'>封面文案：{cover_text}</div>"
            "</div>"
        )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>{title} - 发布预览</title>
  <style>
    body {{
      margin: 0; padding: 24px; background: #f5f7fb; color: #1f2937;
      font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;
    }}
    .container {{ max-width: 860px; margin: 0 auto; }}
    .panel {{ background: #fff; border-radius: 14px; padding: 18px 20px; margin-bottom: 16px; box-shadow: 0 4px 16px rgba(0,0,0,.06); }}
    .meta {{ display: flex; gap: 14px; flex-wrap: wrap; font-size: 13px; color: #4b5563; margin-top: 8px; }}
    .meta a {{ color: #2563eb; text-decoration: none; }}
    .phone {{
      width: min(520px, 100%); margin: 0 auto; border: 1px solid #e5e7eb;
      border-radius: 18px; background: #fff; padding: 16px; box-shadow: inset 0 0 0 1px #f3f4f6;
    }}
    .copy-panel {{ background:#f8fafc; border:1px solid #e5e7eb; border-radius:12px; padding:12px; margin-bottom:14px; }}
    .copy-panel h3 {{ margin:0 0 8px; font-size:14px; }}
    .copy-hint {{ margin:0 0 10px; color:#64748b; font-size:12px; line-height:1.6; }}
    .copy-actions {{ display:flex; flex-wrap:wrap; gap:8px; }}
    .copy-actions button {{ border:0; background:#2563eb; color:#fff; border-radius:8px; padding:7px 10px; cursor:pointer; font-size:12px; }}
    .download-pack {{ display:inline-flex; align-items:center; background:#059669; color:#fff; border-radius:8px; padding:7px 10px; text-decoration:none; font-size:12px; }}
    .status-actions {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-top:10px; }}
    .status-actions button {{ border:0; background:#0f766e; color:#fff; border-radius:8px; padding:7px 10px; cursor:pointer; font-size:12px; }}
    .status-actions button.danger {{ background:#dc2626; }}
    .status-actions button.secondary {{ background:#475569; }}
    .status-result {{ color:#64748b; font-size:12px; }}
    .next-step {{ margin-top:10px; background:#ecfdf5; color:#065f46; border:1px solid #a7f3d0; border-radius:8px; padding:8px 10px; font-size:12px; line-height:1.6; }}
    .copy-panel textarea {{ position:absolute; left:-9999px; top:-9999px; }}
    .rich-copy-details {{ margin-top: 10px; color:#475569; font-size:13px; }}
    .rich-copy-area {{ margin-top:8px; background:#fff; border:1px dashed #94a3b8; border-radius:10px; padding:14px; color:#111827; }}
    .rich-copy-area h1 {{ font-size:22px; line-height:1.45; }}
    .rich-copy-area p, .rich-copy-area li {{ font-size:15px; line-height:1.8; }}
    .rich-copy-area img {{ max-width:100%; border-radius:8px; margin:8px 0; }}
    .rich-hook {{ background:#eff6ff; border-left:3px solid #3b82f6; padding:8px 10px; }}
    .rich-cover, .rich-tags {{ color:#64748b; }}
    .article h1 {{ font-size: 21px; line-height: 1.4; margin: 0 0 12px; }}
    .updated-time {{ color:#64748b; font-size:12px; margin: -4px 0 10px; }}
    .hook {{ background: #eff6ff; border-left: 3px solid #3b82f6; padding: 8px 10px; border-radius: 6px; margin-bottom: 12px; font-size: 14px; }}
    .body p, .body li {{ font-size: 14px; line-height: 1.75; }}
    .cover {{ margin-top: 14px; font-size: 13px; color: #6b7280; }}
    .video-cover {{
      background: linear-gradient(140deg,#111827,#1f2937 65%,#374151);
      color: #fff; border-radius: 12px; min-height: 220px;
      display: flex; flex-direction: column; justify-content: space-between; padding: 14px;
    }}
    .cover-text {{ font-size: 20px; line-height: 1.35; font-weight: 700; }}
    .video-hook {{ font-size: 13px; line-height: 1.6; opacity: .9; }}
    .video-player {{ margin-top: 12px; }}
    .video-player video {{ width: 100%; border-radius: 10px; background: #000; min-height: 220px; }}
    .video-missing {{
      margin-top: 12px; background: #fff7ed; color: #9a3412; border: 1px solid #fed7aa;
      border-radius: 8px; padding: 10px; font-size: 13px;
    }}
    .timeline h3 {{ margin: 14px 0 8px; font-size: 14px; }}
    .timeline ol {{ margin: 0; padding-left: 18px; }}
    .timeline li {{ margin: 8px 0; font-size: 13px; line-height: 1.6; }}
    .script-box {{ white-space: pre-wrap; background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 8px; padding: 10px; font-size: 13px; line-height: 1.7; }}
    .time {{ display: inline-block; width: 70px; color: #2563eb; font-weight: 600; }}
    .line {{ color: #1f2937; }}
    .ill-gallery {{ margin: 10px 0 12px; }}
    .ill-gallery h3 {{ margin: 0 0 8px; font-size: 14px; color: #374151; }}
    .ill-grid {{ display: grid; gap: 8px; grid-template-columns: 1fr; }}
    .ill-card img {{ width: 100%; border-radius: 10px; border: 1px solid #e5e7eb; }}
    .inline-image {{ margin: 12px 0; }}
    .inline-image img {{ width: 100%; border-radius: 10px; border: 1px solid #e5e7eb; }}
    .inline-image figcaption {{ color:#64748b; font-size:12px; margin-top:5px; }}
    .image-marker {{ background:#fef3c7; border:1px solid #f59e0b; color:#92400e; border-radius:8px; padding:7px 9px; font-size:13px; font-weight:600; }}
    .rich-copy-area .image-marker {{ background:#fff7ed; color:#9a3412; }}
  </style>
  <script>
    async function copyToClipboard(text) {{
      if (navigator.clipboard && window.isSecureContext) {{
        await navigator.clipboard.writeText(text);
        return true;
      }}
      const temp = document.createElement('textarea');
      temp.value = text;
      temp.setAttribute('readonly', '');
      temp.style.position = 'fixed';
      temp.style.left = '-9999px';
      temp.style.top = '0';
      document.body.appendChild(temp);
      temp.focus();
      temp.select();
      let ok = false;
      try {{ ok = document.execCommand('copy'); }} catch (err) {{ ok = false; }}
      document.body.removeChild(temp);
      if (!ok) throw new Error('浏览器阻止复制，请手动选中文案复制');
      return true;
    }}
    async function copyTarget(btn) {{
      const el = document.getElementById(btn.dataset.copyTarget);
      if (!el) return;
      const old = btn.innerText;
      try {{
        await copyToClipboard(el.value || el.textContent || '');
        btn.innerText = '已复制';
      }} catch (err) {{
        btn.innerText = '复制失败';
        alert(err.message || err);
      }}
      setTimeout(() => btn.innerText = old, 1600);
    }}
    async function copyRichTarget(btn) {{
      const el = document.getElementById(btn.dataset.richTarget);
      if (!el) return;
      const old = btn.innerText;
      try {{
        const clone = el.cloneNode(true);
        const imgs = Array.from(clone.querySelectorAll('img'));
        for (const img of imgs) {{
          try {{
            const absoluteUrl = new URL(img.getAttribute('src'), window.location.href).toString();
            const resp = await fetch(absoluteUrl, {{cache: 'no-store'}});
            const blob = await resp.blob();
            const dataUrl = await new Promise((resolve, reject) => {{
              const reader = new FileReader();
              reader.onload = () => resolve(reader.result);
              reader.onerror = reject;
              reader.readAsDataURL(blob);
            }});
            img.setAttribute('src', dataUrl);
          }} catch (err) {{
            // Keep original src if conversion fails; manual upload remains available via素材包.
          }}
        }}
        const html = clone.innerHTML;
        const text = clone.innerText || clone.textContent || '';
        if (navigator.clipboard && window.ClipboardItem && window.isSecureContext) {{
          await navigator.clipboard.write([
            new ClipboardItem({{
              'text/html': new Blob([html], {{type: 'text/html'}}),
              'text/plain': new Blob([text], {{type: 'text/plain'}})
            }})
          ]);
        }} else {{
          const range = document.createRange();
          range.selectNodeContents(el);
          const selection = window.getSelection();
          selection.removeAllRanges();
          selection.addRange(range);
          const ok = document.execCommand('copy');
          selection.removeAllRanges();
          if (!ok) throw new Error('浏览器阻止富文本复制，请展开下方富文本复制区，手动框选 Ctrl+C');
        }}
        btn.innerText = '图文已复制';
      }} catch (err) {{
        btn.innerText = '复制失败';
        const details = el.closest('details');
        if (details) details.open = true;
        alert((err && err.message) || '复制失败，请展开富文本复制区手动复制');
      }}
      setTimeout(() => btn.innerText = old, 1800);
    }}
    async function setStatus(id, status, btn) {{
      const box = btn.closest('.status-actions').querySelector('.status-result');
      box.innerText = '处理中...';
      const resp = await fetch('/status?id=' + encodeURIComponent(id) + '&status=' + encodeURIComponent(status));
      box.innerText = await resp.text();
    }}
    async function runAction(id, action, btn) {{
      const box = btn.closest('.status-actions').querySelector('.status-result');
      box.innerText = '处理中...';
      btn.disabled = true;
      try {{
        const resp = await fetch('/action?id=' + encodeURIComponent(id) + '&action=' + encodeURIComponent(action));
        box.innerText = await resp.text();
      }} finally {{
        btn.disabled = false;
      }}
    }}
  </script>
</head>
<body>
  <div class="container">
    <div class="panel">
      <h2 style="margin:0;font-size:20px;">发布预览</h2>
      {metadata_html}
      <div class="meta"><span>质量说明：{reason or "无"}</span></div>
    </div>
    <div class="panel">{content_card}</div>
  </div>
</body>
</html>
"""


def generate_preview_for_item(config: dict[str, Any], item: dict[str, Any]) -> None:
    preview_cfg = config.get("preview", {})
    if not preview_cfg.get("enabled", True):
        return
    build_asset_pack_for_item(config, item)
    item_date = str(item.get("date", now_local().strftime("%Y-%m-%d")))
    queue_id = str(item.get("id", uuid.uuid4().hex[:12]))
    content = extract_content_payload(item)
    preview_dir = PREVIEW_DIR / item_date
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_file = preview_dir / f"{queue_id}.html"
    preview_file.write_text(build_preview_html(config, item, content), encoding="utf-8")
    item["preview_file"] = str(preview_file)
    item["preview_url"] = build_preview_url(config, preview_file)


def build_preview_index(config: dict[str, Any], items: list[dict[str, Any]], date: str) -> dict[str, str]:
    if not items:
        return {"index_file": "", "index_url": ""}
    preview_dir = PREVIEW_DIR / date
    preview_dir.mkdir(parents=True, exist_ok=True)
    rows: list[str] = []
    for item in items:
        title = html.escape(str(item.get("title", "")).strip()[:70] or "未命名草稿")
        advice = html.escape(str(item.get("publish_advice", "需改")))
        badge = html.escape(str(item.get("quality_badge", "⚪")))
        score = safe_int(item.get("total_score", 0), default=0)
        platform = html.escape(str(item.get("platform", "")))
        preview_url = html.escape(str(item.get("preview_url", "")))
        preview_file = Path(str(item.get("preview_file", "")).strip() or "#")
        local_link = html.escape(preview_file.name) if preview_file != Path("#") else "#"
        target_href = preview_url or local_link
        bust = cache_bust_token(item)
        if bust:
            sep = "&" if "?" in target_href else "?"
            target_href = f"{target_href}{sep}v={bust}"
        sample_video_url = html.escape(str(item.get("sample_video_url", "")).strip())
        sample_video_link = (
            f"<a href='{sample_video_url}' target='_blank' rel='noreferrer'>播放样片</a>"
            if sample_video_url
            else "-"
        )
        rows.append(
            "<tr>"
            f"<td>{item.get('id','')}</td><td>{platform}</td>"
            f"<td>{badge}{score}</td><td>{advice}</td>"
            f"<td><a href='{target_href}' target='_blank' rel='noreferrer'>{title}</a></td>"
            f"<td>{sample_video_link}</td>"
            "</tr>"
        )
    index_html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8" /><title>发布预览索引 {date}</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;background:#f6f8fb;padding:20px;}}
.card{{background:#fff;border-radius:12px;padding:16px;max-width:980px;margin:0 auto;box-shadow:0 4px 16px rgba(0,0,0,.06);}}
table{{width:100%;border-collapse:collapse;font-size:14px;}}
th,td{{border-bottom:1px solid #eef2f7;padding:10px;text-align:left;vertical-align:top;}}
th{{background:#f8fafc;}}
a{{color:#2563eb;text-decoration:none;}}
</style></head><body><div class="card">
<h2 style="margin-top:0;">发布预览索引（{date}）</h2>
<table><thead><tr><th>QueueID</th><th>平台</th><th>评分</th><th>建议</th><th>预览链接</th><th>样片</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div></body></html>"""
    index_file = preview_dir / "index.html"
    index_file.write_text(index_html, encoding="utf-8")
    index_url = build_preview_url(config, index_file)
    return {"index_file": str(index_file), "index_url": index_url}


def build_accounts_index(
    config: dict[str, Any], queue: list[dict[str, Any]], date: str = ""
) -> dict[str, str]:
    preview_cfg = config.get("preview", {})
    if not preview_cfg.get("enabled", True):
        return {"index_file": "", "index_url": ""}
    platforms = config.get("platforms", [])
    if date:
        visible_items = [item for item in queue if item.get("date") == date]
    else:
        visible_items = list(queue)
    latest_date = date or (max([str(item.get("date", "")) for item in visible_items] or [""]) or "")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in visible_items:
        grouped.setdefault(str(item.get("account_id") or item.get("account_name") or ""), []).append(item)

    cards: list[str] = []
    for platform_cfg in platforms:
        account_id = platform_account_id(platform_cfg)
        account_name = str(platform_cfg.get("account_name", "")).strip()
        platform = str(platform_cfg.get("platform", "")).strip()
        post_format = str(platform_cfg.get("post_format", "")).strip()
        track = str(platform_cfg.get("track", "")).strip()
        publish_time = str(platform_cfg.get("publish_time", "")).strip()
        mode = "公众号/小红书完整图文" if post_format != "short_video_script" else "剪映图文素材包"
        items = sorted(
            grouped.get(account_id, [])
            + [
                item
                for item in visible_items
                if not item.get("account_id") and item.get("account_name") == account_name
            ],
            key=lambda item: str(item.get("created_at", "")),
            reverse=True,
        )
        ready_count = len(items)
        image_ready = sum(1 for item in items if len(item.get("illustration_urls") or item.get("illustration_files") or []) > 0)
        best_score = max([safe_int(item.get("total_score", 0), default=0) for item in items] or [0])
        item_rows: list[str] = []
        for item in items[:8]:
            preview_file = Path(str(item.get("preview_file", "")).strip() or "#")
            href = html.escape(preview_file.name) if preview_file != Path("#") else "#"
            title = html.escape(str(item.get("title", "")).strip()[:64] or "未命名草稿")
            badge = html.escape(str(item.get("quality_badge", "⚪")))
            score = safe_int(item.get("total_score", 0), default=0)
            ill_count = len(item.get("illustration_urls") or item.get("illustration_files") or [])
            material = f"{ill_count} 张图" if ill_count else "待配图"
            raw_status = str(item.get("status", "pending_review"))
            status = html.escape(status_label(raw_status))
            updated = html.escape(display_datetime(item.get("updated_at") or item.get("created_at")) or "-")
            item_rows.append(
                f"<li data-status='{html.escape(raw_status)}'>"
                f"<a href='{href}' target='_blank' rel='noreferrer'>{title}</a>"
                f"<span>{badge}{score}</span><span>{material}</span><span>{status}</span><span>{updated}</span>"
                "</li>"
            )
        if not item_rows:
            item_rows.append("<li><span>暂无候选内容，先运行生成命令。</span></li>")
        cards.append(
            f"<section class='account-card' data-account='{html.escape(account_name)}' data-platform='{html.escape(platform)}'>"
            f"<div class='account-head'><h2>{html.escape(account_name)}</h2><span>{html.escape(platform_label(platform))}</span></div>"
            f"<p class='position'>定位：{html.escape(track)} · {html.escape(mode)} · 发布时间：{html.escape(publish_time or '-')}</p>"
            f"<div class='stats'><span>候选 {ready_count}</span><span>已配图 {image_ready}</span><span>最高分 {best_score}</span></div>"
            "<div class='generate-row'>"
            "<input type='number' min='1' max='10' value='3' title='生成条数' />"
            f"<button type='button' onclick=\"runGenerate(this)\" data-account=\"{html.escape(account_name)}\">点击生成</button>"
            "</div>"
            "<pre class='run-log'></pre>"
            "<ul class='items'>"
            f"{''.join(item_rows)}"
            "</ul></section>"
        )
    title_suffix = f"（{html.escape(latest_date)}）" if latest_date else ""
    accounts_html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8" /><meta name="viewport" content="width=device-width,initial-scale=1" />
<title>按账号生成内容{title_suffix}</title>
<style>
body{{margin:0;background:#f3f5f9;color:#111827;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;}}
.wrap{{max-width:1160px;margin:0 auto;padding:24px;}}
.hero{{background:linear-gradient(135deg,#111827,#1d4ed8);color:#fff;border-radius:18px;padding:22px;margin-bottom:18px;}}
.hero h1{{margin:0 0 8px;font-size:24px;}} .hero p{{margin:0;opacity:.9;line-height:1.7;}}
.toolbar{{display:flex;gap:10px;align-items:center;margin:0 0 16px;}}
.toolbar input{{width:min(420px,100%);border:1px solid #dbe3ef;border-radius:10px;padding:10px 12px;font-size:14px;}}
.status-filter{{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 16px;}}
.status-filter button{{background:#fff;color:#334155;border:1px solid #cbd5e1;margin:0;}}
.status-filter button.active{{background:#2563eb;color:#fff;border-color:#2563eb;}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:16px;}}
.account-card{{background:#fff;border-radius:16px;padding:16px;box-shadow:0 6px 18px rgba(15,23,42,.08);}}
.account-head{{display:flex;align-items:center;justify-content:space-between;gap:12px;}}
.account-head h2{{margin:0;font-size:19px;}} .account-head span{{font-size:12px;background:#eff6ff;color:#1d4ed8;border-radius:999px;padding:4px 9px;}}
.position{{font-size:13px;color:#4b5563;line-height:1.6;}}
.stats{{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 10px;}}
.stats span{{font-size:12px;background:#f1f5f9;color:#334155;border-radius:999px;padding:4px 8px;}}
.generate-row{{display:flex;gap:8px;align-items:center;}}
.generate-row input{{width:64px;border:1px solid #dbe3ef;border-radius:9px;padding:8px;font-size:14px;}}
button{{border:0;background:#2563eb;color:#fff;border-radius:9px;padding:8px 11px;cursor:pointer;margin-bottom:10px;}}
button.secondary{{background:#64748b;}}
.run-log{{display:none;white-space:pre-wrap;background:#0f172a;color:#dbeafe;border-radius:10px;padding:10px;font-size:12px;max-height:220px;overflow:auto;}}
.items{{list-style:none;margin:0;padding:0;display:grid;gap:8px;}}
    .items li{{display:grid;grid-template-columns:1fr auto auto auto auto;gap:8px;align-items:center;border-top:1px solid #eef2f7;padding-top:8px;font-size:13px;}}
a{{color:#2563eb;text-decoration:none;}} .items span{{color:#6b7280;white-space:nowrap;}}
</style>
<script>
async function runGenerate(btn){{
  const card = btn.closest('.account-card');
  const log = card.querySelector('.run-log');
  const count = card.querySelector('.generate-row input')?.value || '3';
  log.style.display='block'; log.textContent='正在创建生成任务...';
  btn.disabled=true;
  try {{
    const resp = await fetch('/generate?async=1&sync_feishu=0&count=' + encodeURIComponent(count) + '&account=' + encodeURIComponent(btn.dataset.account || ''));
    const payload = await resp.json();
    if (!resp.ok) throw new Error(payload.error || '创建任务失败');
    await pollJob(payload.job_id, log);
  }} catch (err) {{
    log.textContent = '生成失败：' + err;
  }} finally {{
    btn.disabled=false;
  }}
}}
async function pollJob(jobId, log){{
  while (true) {{
    const resp = await fetch('/job-status?id=' + encodeURIComponent(jobId));
    const payload = await resp.json();
    log.textContent = (payload.lines || []).join('\\n');
    if (payload.status === 'done') {{
      log.textContent += '\\n\\n生成完成，刷新页面即可看到新候选内容。';
      return;
    }}
    if (payload.status === 'failed') {{
      log.textContent += '\\n\\n生成失败，请看上方错误。';
      return;
    }}
    await new Promise(resolve => setTimeout(resolve, 1500));
  }}
}}
function filterAccounts(input){{
  const q = (input.value || '').toLowerCase();
  document.querySelectorAll('.account-card').forEach(card => {{
    const hay = ((card.dataset.account || '') + ' ' + (card.dataset.platform || '')).toLowerCase();
    card.style.display = hay.includes(q) ? '' : 'none';
  }});
}}
function filterStatus(status, btn){{
  document.querySelectorAll('.status-filter button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('.items li').forEach(row => {{
    row.style.display = (!status || row.dataset.status === status) ? '' : 'none';
  }});
}}
</script></head><body><div class="wrap">
<div class="hero"><h1>按账号生成/选择内容{title_suffix}</h1>
<p>公众号和小红书输出可直接粘贴的完整图文；抖音/视频号/快手输出剪映可用的口播稿、分镜和配图素材，不再生成视频。</p></div>
<div class="toolbar"><input placeholder="搜索账号或平台，例如 小红书 / 公众号 / 快手" oninput="filterAccounts(this)" /></div>
<div class="status-filter">
  <button type="button" class="active" onclick="filterStatus('', this)">全部</button>
  <button type="button" onclick="filterStatus('pending_review', this)">待筛选</button>
  <button type="button" onclick="filterStatus('approved', this)">已选用</button>
  <button type="button" onclick="filterStatus('posted', this)">已发布</button>
  <button type="button" onclick="filterStatus('rejected', this)">已丢弃</button>
</div>
<div class="grid">{''.join(cards)}</div></div></body></html>"""
    target_dir = PREVIEW_DIR / latest_date if latest_date else PREVIEW_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    index_file = target_dir / "accounts.html"
    index_file.write_text(accounts_html, encoding="utf-8")
    return {"index_file": str(index_file), "index_url": build_preview_url(config, index_file)}


def build_dashboard_index(config: dict[str, Any], queue: list[dict[str, Any]], date: str = "") -> dict[str, str]:
    if date:
        items = [item for item in queue if item.get("date") == date]
        dashboard_date = date
    else:
        dashboard_date = max([str(item.get("date", "")) for item in queue] or [now_local().strftime("%Y-%m-%d")])
        items = [item for item in queue if item.get("date") == dashboard_date]
    counts = {
        "total": len(items),
        "pending": sum(1 for item in items if item.get("status") == "pending_review"),
        "approved": sum(1 for item in items if item.get("status") in {"approved", "ready_to_post"}),
        "posted": sum(1 for item in items if item.get("status") == "posted"),
        "rejected": sum(1 for item in items if item.get("status") == "rejected"),
        "images": sum(1 for item in items if len(item.get("illustration_urls") or []) > 0),
    }
    latest_update = max(
        [display_datetime(item.get("updated_at") or item.get("created_at")) for item in items]
        or [""]
    )
    by_account: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        by_account.setdefault(str(item.get("account_name", "")), []).append(item)
    platform_cfg_by_account = {
        str(cfg.get("account_name", "")): cfg for cfg in config.get("platforms", [])
    }
    platform_counts: dict[str, int] = {}
    for item in items:
        platform_counts[str(item.get("platform", ""))] = platform_counts.get(str(item.get("platform", "")), 0) + 1
    priority_items = sorted(
        [
            item
            for item in items
            if item.get("status") in {"pending_review", "approved", "ready_to_post"}
            and item.get("publish_advice") != "禁发"
        ],
        key=lambda item: (
            str(item.get("status", "")) != "approved",
            -safe_int(item.get("total_score", 0), default=0),
            str(item.get("updated_at", "")),
        ),
    )[:8]
    priority_rows = []
    for item in priority_items:
        preview_file = Path(str(item.get("preview_file", "")).strip() or "#")
        href = "#"
        if preview_file != Path("#"):
            try:
                href = preview_file.relative_to(PREVIEW_DIR).as_posix()
            except ValueError:
                href = preview_file.name
        priority_rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('account_name', '')))}</td>"
            f"<td>{html.escape(platform_label(item.get('platform')))}</td>"
            f"<td><a href='{html.escape(href)}'>{html.escape(str(item.get('title', ''))[:52])}</a></td>"
            f"<td>{html.escape(status_label(item.get('status')))}</td>"
            f"<td>{safe_int(item.get('total_score', 0), default=0)}</td>"
            f"<td>{html.escape(display_datetime(item.get('updated_at') or item.get('created_at')) or '-')}</td>"
            f"<td>{html.escape(next_step_for_item(item))}</td>"
            "</tr>"
        )
    account_rows = []
    coverage_rows = []
    schedule_rows = []
    for account, account_items in sorted(by_account.items()):
        first_item = account_items[0] if account_items else {}
        platform_cfg = platform_cfg_by_account.get(account, {})
        platform = platform_cfg.get("platform") or first_item.get("platform", "")
        publish_time = platform_cfg.get("publish_time") or first_item.get("publish_time", "")
        post_format = platform_cfg.get("post_format") or first_item.get("post_format", "")
        best = max([safe_int(item.get("total_score", 0), default=0) for item in account_items] or [0])
        ready = sum(1 for item in account_items if item.get("status") in {"approved", "ready_to_post"})
        posted = sum(1 for item in account_items if item.get("status") == "posted")
        latest = max([display_datetime(item.get("updated_at") or item.get("created_at")) for item in account_items] or [""])
        suggestion = "已覆盖" if ready or posted else "建议先选 1 条"
        account_rows.append(
            "<tr>"
            f"<td>{html.escape(account)}</td>"
            f"<td>{html.escape(platform_label(platform))}</td>"
            f"<td>{html.escape(str(publish_time) or '-')}</td>"
            f"<td>{html.escape(post_format_label(post_format))}</td>"
            f"<td>{len(account_items)}</td><td>{ready}</td><td>{best}</td><td>{html.escape(latest or '-')}</td>"
            "</tr>"
        )
        coverage_rows.append(
            f"<tr><td>{html.escape(account)}</td><td>{html.escape(platform_label(platform))}</td><td>{ready}</td><td>{posted}</td><td>{html.escape(latest or '-')}</td><td>{html.escape(suggestion)}</td></tr>"
        )
        schedule_rows.append(
            (
                str(publish_time) or "99:99",
                "<tr>"
                f"<td>{html.escape(str(publish_time) or '-')}</td>"
                f"<td>{html.escape(account)}</td>"
                f"<td>{html.escape(platform_label(platform))}</td>"
                f"<td>{html.escape(post_format_label(post_format))}</td>"
                f"<td>{len(account_items)}</td>"
                f"<td>{ready or posted}</td>"
                f"<td>{html.escape(latest or '-')}</td>"
                "</tr>",
            )
        )
    schedule_html = "".join(row for _, row in sorted(schedule_rows, key=lambda item: item[0]))
    platform_stats = "".join(
        f"<span class='pill'>{html.escape(platform_label(platform))}: {count}</span>"
        for platform, count in sorted(platform_counts.items())
    )
    dashboard_html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8" /><meta name="viewport" content="width=device-width,initial-scale=1" />
<title>发布工作台 {dashboard_date}</title>
<style>
body{{margin:0;background:#f3f5f9;color:#111827;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;}}
.wrap{{max-width:1080px;margin:0 auto;padding:24px;}}
.hero{{background:linear-gradient(135deg,#0f172a,#0f766e);color:#fff;border-radius:18px;padding:22px;margin-bottom:18px;}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px;}}
.stat{{background:#fff;border-radius:14px;padding:15px;box-shadow:0 6px 18px rgba(15,23,42,.08);}}
.stat b{{display:block;font-size:26px;margin-top:6px;}}
.pill-row{{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 18px;}}
.pill{{background:#e0f2fe;color:#075985;border-radius:999px;padding:7px 10px;font-size:13px;}}
.actions{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px;}}
.actions a{{background:#2563eb;color:#fff;border-radius:10px;padding:10px 13px;text-decoration:none;}}
.actions a.green{{background:#059669;}}
.card{{background:#fff;border-radius:14px;padding:16px;box-shadow:0 6px 18px rgba(15,23,42,.08);}}
table{{width:100%;border-collapse:collapse;font-size:14px;}} th,td{{border-bottom:1px solid #eef2f7;padding:10px;text-align:left;}} th{{background:#f8fafc;}}
</style></head><body><div class="wrap">
<div class="hero"><h1>发布工作台 {dashboard_date}</h1><p>先在账号页生成/筛选内容，单条页里复制文案、下载素材包，发布后点“已发布”。</p></div>
<div class="grid">
<div class="stat">总候选<b>{counts['total']}</b></div>
<div class="stat">待筛选<b>{counts['pending']}</b></div>
<div class="stat">已选用<b>{counts['approved']}</b></div>
<div class="stat">已发布<b>{counts['posted']}</b></div>
<div class="stat">已丢弃<b>{counts['rejected']}</b></div>
<div class="stat">已配图<b>{counts['images']}</b></div>
<div class="stat">最近更新<b style="font-size:16px;">{html.escape(latest_update or '-')}</b></div>
</div>
<div class="pill-row">{platform_stats}</div>
<div class="actions">
<a href="{dashboard_date}/accounts.html">进入账号工作台</a>
<a href="{dashboard_date}/index.html">查看全部候选</a>
<a class="green" href="/export-selected?date={dashboard_date}" target="_blank">导出已选清单</a>
<a class="green" href="/download-packs?date={dashboard_date}&status=all" target="_blank">下载今日全部素材包</a>
<a class="green" href="/download-packs?date={dashboard_date}&status=selected" target="_blank">下载已选素材包</a>
<a href="/daily-log" target="_blank">查看每日自动生成日志</a>
</div>
<div class="card"><h2>今日优先处理</h2><table><thead><tr><th>账号</th><th>平台</th><th>内容</th><th>状态</th><th>评分</th><th>更新时间</th><th>下一步</th></tr></thead><tbody>{''.join(priority_rows) or '<tr><td colspan="7">暂无待处理内容</td></tr>'}</tbody></table></div>
<br />
<div class="card"><h2>今日发布排期</h2><table><thead><tr><th>时间</th><th>账号</th><th>平台</th><th>形式</th><th>候选</th><th>是否已覆盖</th><th>更新时间</th></tr></thead><tbody>{schedule_html}</tbody></table></div>
<br />
<div class="card"><h2>账号发布覆盖</h2><table><thead><tr><th>账号</th><th>平台</th><th>已选</th><th>已发布</th><th>更新时间</th><th>建议</th></tr></thead><tbody>{''.join(coverage_rows)}</tbody></table></div>
<br />
<div class="card"><h2>账号概览</h2><table><thead><tr><th>账号</th><th>平台</th><th>发布时间</th><th>内容形式</th><th>候选</th><th>已选</th><th>最高分</th><th>更新时间</th></tr></thead><tbody>{''.join(account_rows)}</tbody></table></div>
</div></body></html>"""
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    try:
        build_bulk_asset_pack(config, queue, date=dashboard_date, status="all")
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[WARN] build bulk asset pack failed: {exc}")
    index_file = PREVIEW_DIR / "dashboard.html"
    index_file.write_text(dashboard_html, encoding="utf-8")
    return {"index_file": str(index_file), "index_url": build_preview_url(config, index_file)}


def list_preview_dates() -> list[str]:
    if not PREVIEW_DIR.exists():
        return []
    dates: list[str] = []
    for entry in PREVIEW_DIR.iterdir():
        if not entry.is_dir():
            continue
        # Keep only day folders like 2026-07-04.
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry.name):
            continue
        if not (entry / "index.html").exists():
            continue
        dates.append(entry.name)
    return sorted(dates, reverse=True)


def build_preview_portal(config: dict[str, Any]) -> dict[str, str]:
    dates = list_preview_dates()
    if not dates:
        return {"portal_file": "", "portal_url": ""}

    links: list[str] = []
    for date in dates:
        date_index = PREVIEW_DIR / date / "index.html"
        account_index = PREVIEW_DIR / date / "accounts.html"
        version = str(int(date_index.stat().st_mtime)) if date_index.exists() else "0"
        account_version = str(int(account_index.stat().st_mtime)) if account_index.exists() else version
        links.append(
            (
                f"<a class='date-link' href='{date}/index.html?v={version}' target='date-content-frame' "
                f"onclick=\"document.getElementById('current-date').innerText='{date}';\">{date}</a>"
                f"<a class='account-link' href='{date}/accounts.html?v={account_version}' target='date-content-frame' "
                f"onclick=\"document.getElementById('current-date').innerText='{date} 按账号';\">按账号查看</a>"
            )
        )

    default_date = dates[0]
    dashboard_index = PREVIEW_DIR / "dashboard.html"
    default_index = dashboard_index if dashboard_index.exists() else PREVIEW_DIR / default_date / "accounts.html"
    if not default_index.exists():
        default_index = PREVIEW_DIR / default_date / "index.html"
    default_version = str(int(default_index.stat().st_mtime)) if default_index.exists() else "0"
    portal_html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>内容预览门户</title>
  <style>
    body {{
      margin: 0; background: #f3f5f9; color: #1f2937;
      font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'PingFang SC','Microsoft YaHei',sans-serif;
    }}
    .layout {{
      display: grid; grid-template-columns: 260px 1fr; min-height: 100vh;
    }}
    .sidebar {{
      border-right: 1px solid #e5e7eb; background: #fff; padding: 16px; overflow: auto;
    }}
    .sidebar h2 {{ margin: 0 0 12px; font-size: 18px; }}
    .sidebar .hint {{ font-size: 12px; color: #6b7280; margin-bottom: 10px; }}
    .date-link {{
      display: block; padding: 10px 12px; border-radius: 8px; color: #111827; text-decoration: none;
      margin-bottom: 6px; background: #f8fafc;
    }}
    .date-link:hover {{ background: #e8f1ff; color: #1d4ed8; }}
    .account-link {{
      display: block; padding: 7px 12px; border-radius: 8px; color: #1d4ed8; text-decoration: none;
      margin: -2px 0 10px 10px; background: #eff6ff; font-size: 13px;
    }}
    .account-link:hover {{ background: #dbeafe; }}
    .content {{
      padding: 16px;
    }}
    .header {{
      background: #fff; border-radius: 12px; padding: 12px 14px; margin-bottom: 12px;
      box-shadow: 0 3px 12px rgba(0,0,0,.06);
    }}
    .header h1 {{ margin: 0; font-size: 18px; }}
    iframe {{
      width: 100%; height: calc(100vh - 120px); border: 0; border-radius: 12px; background: #fff;
      box-shadow: 0 4px 16px rgba(0,0,0,.08);
    }}
  </style>
</head>
<body>
  <div class="layout">
    <aside class="sidebar">
      <h2>日期列表</h2>
      <div class="hint">点击左侧日期，在右侧查看当天内容预览</div>
      {''.join(links)}
    </aside>
    <main class="content">
      <div class="header">
        <h1>当前日期：<span id="current-date">{default_date}</span></h1>
      </div>
      <iframe name="date-content-frame" src="{default_index.relative_to(PREVIEW_DIR).as_posix()}?v={default_version}"></iframe>
    </main>
  </div>
</body>
</html>"""
    portal_file = PREVIEW_DIR / "index.html"
    portal_file.write_text(portal_html, encoding="utf-8")
    portal_url = build_preview_url(config, portal_file)
    return {"portal_file": str(portal_file), "portal_url": portal_url}


def ensure_binary(binary_name: str) -> str:
    path = shutil.which(binary_name)
    if not path:
        raise ValueError(
            f"Required binary not found: {binary_name}. "
            f"Install it on server first."
        )
    return path


def run_command(command: list[str], cwd: str | None = None) -> None:
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        detail = stderr or stdout or "unknown error"
        raise RuntimeError(f"Command failed: {' '.join(command)} | {detail[:500]}")


def ffprobe_duration_seconds(audio_file: Path) -> float:
    ensure_binary("ffprobe")
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
    if result.returncode != 0:
        return 0.0
    try:
        return max(0.0, float((result.stdout or "0").strip()))
    except ValueError:
        return 0.0


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
        clean_text = text.replace("\n", " ").strip()
        lines.extend(
            [
                str(idx),
                f"{format_srt_time(start)} --> {format_srt_time(end)}",
                clean_text,
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def render_sample_video_for_item(config: dict[str, Any], item: dict[str, Any]) -> dict[str, str]:
    sample_cfg = config.get("sample_video", {})
    if not sample_cfg.get("enabled", True):
        return {"status": "disabled", "message": "sample_video disabled"}

    ensure_binary("ffmpeg")
    ensure_binary("ffprobe")
    tts_engine = str(sample_cfg.get("tts_engine", "edge-tts")).strip().lower()
    tts_binary = (
        ensure_binary(sample_cfg.get("tts_binary", "edge-tts"))
        if tts_engine == "edge-tts"
        else ensure_binary(sample_cfg.get("tts_binary", "espeak-ng"))
    )
    segment_ext = ".mp3" if tts_engine == "edge-tts" else ".wav"

    queue_id = str(item.get("id", uuid.uuid4().hex[:12]))
    item_date = str(item.get("date", now_local().strftime("%Y-%m-%d")))
    content = extract_content_payload(item)
    segments = split_video_segments(
        content.get("body_markdown", "") or content.get("body_preview", ""),
        limit=int(sample_cfg.get("max_segments", 8)),
    )
    if not segments:
        raise ValueError("No script segments available for sample rendering.")

    media_dir = PREVIEW_DIR / "media" / item_date
    media_dir.mkdir(parents=True, exist_ok=True)
    out_video = media_dir / f"{queue_id}.mp4"
    out_audio = media_dir / f"{queue_id}.wav"

    segment_gap = float(sample_cfg.get("segment_gap_seconds", 0.25))
    lead_in = float(sample_cfg.get("lead_in_seconds", 0.4))
    min_duration = float(sample_cfg.get("min_duration_seconds", 8.0))
    width = int(sample_cfg.get("width", 720))
    height = int(sample_cfg.get("height", 1280))
    fps = int(sample_cfg.get("fps", 25))
    bg_color = str(sample_cfg.get("background_color", "#1e3a8a"))

    with tempfile.TemporaryDirectory(prefix=f"sample-{queue_id}-") as temp_dir:
        temp_path = Path(temp_dir)
        audio_segments: list[Path] = []
        cues: list[tuple[float, float, str]] = []
        current = lead_in
        for idx, segment in enumerate(segments):
            segment_text = segment.strip()[:120]
            if not segment_text:
                continue
            segment_audio = temp_path / f"segment_{idx:02d}{segment_ext}"
            synthesize_tts_segment(segment_text, segment_audio, sample_cfg, tts_binary)
            duration = ffprobe_duration_seconds(segment_audio)
            if duration < 0.1:
                continue
            audio_segments.append(segment_audio)
            cues.append((current, current + duration, segment_text))
            current += duration + segment_gap

        if not audio_segments:
            raise ValueError("TTS generated no usable audio segments.")

        title_text = str(item.get("title", "")).strip()
        if title_text:
            title_dur = min(3.0, max(1.5, len(title_text) / 22.0))
            cues.insert(0, (0.2, 0.2 + title_dur, title_text[:80]))

        concat_file = temp_path / "audio_concat.txt"
        concat_file.write_text(
            "\n".join([f"file '{segment.as_posix()}'" for segment in audio_segments]),
            encoding="utf-8",
        )
        narration_audio = temp_path / "narration.wav"
        run_command(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-c:a",
                "pcm_s16le",
                str(narration_audio),
            ]
        )

        total_duration = max(min_duration, current + 0.8)
        subtitles_file = temp_path / "subtitles.srt"
        write_srt(cues, subtitles_file)

        rendered_video = temp_path / "preview.mp4"
        subtitle_filter = "subtitles=subtitles.srt"
        run_command(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={bg_color}:s={width}x{height}:r={fps}:d={total_duration:.2f}",
                "-i",
                str(narration_audio),
                "-filter_complex",
                (
                    f"[1:a]showwaves=s={width}x220:mode=line:rate={fps}:colors=0x93c5fd[sw];"
                    f"[0:v][sw]overlay=0:H-h-70,{subtitle_filter}[v]"
                ),
                "-map",
                "[v]",
                "-map",
                "1:a",
                "-shortest",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "24",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                str(rendered_video),
            ],
            cwd=temp_dir,
        )
        shutil.move(str(rendered_video), str(out_video))
        shutil.move(str(narration_audio), str(out_audio))

    item["sample_video_file"] = str(out_video)
    item["sample_video_url"] = build_preview_url(config, out_video)
    item["sample_audio_file"] = str(out_audio)
    item["updated_at"] = now_local().isoformat()
    return {
        "status": "ok",
        "video_file": str(out_video),
        "video_url": item["sample_video_url"],
    }


def render_samples_for_items(
    config: dict[str, Any], items: list[dict[str, Any]], *, include_blocked: bool = False
) -> list[dict[str, str]]:
    if local_gpu_enabled(config):
        print("[INFO] local_gpu.enabled=true, skipping server render-samples.")
        return export_video_jobs_for_items(
            config,
            filter_queue_items(
                items,
                include_blocked=include_blocked,
                post_format="short_video_script",
            ),
        )
    results: list[dict[str, str]] = []
    for item in items:
        if item.get("post_format") != "short_video_script":
            continue
        if not include_blocked and (
            item.get("status") == "auto_blocked" or item.get("publish_advice") == "禁发"
        ):
            continue
        try:
            result = render_sample_video_for_item(config, item)
            result["id"] = str(item.get("id", ""))
            result["platform"] = str(item.get("platform", ""))
            results.append(result)
            print(
                f"[OK] sample video rendered {item.get('id')} -> {result.get('video_file','')}"
            )
        except Exception as exc:  # pylint: disable=broad-except
            item["notes"] = (str(item.get("notes", "")).strip() + f" | 样片生成失败: {exc}").strip(" |")
            item["updated_at"] = now_local().isoformat()
            print(f"[WARN] sample video failed {item.get('id')}: {exc}")
    return results


def build_queue_summary(queue: list[dict[str, Any]]) -> str:
    if not queue:
        return "Queue is empty."
    grouped: dict[str, int] = {}
    for item in queue:
        grouped[item["status"]] = grouped.get(item["status"], 0) + 1
    lines = [
        f"Queue summary ({now_local().strftime('%Y-%m-%d %H:%M')}):",
        "",
    ]
    for status, count in sorted(grouped.items()):
        lines.append(f"- {status}: {count}")
    advice_grouped: dict[str, int] = {}
    for item in queue:
        advice = item.get("publish_advice", "")
        if advice:
            advice_grouped[advice] = advice_grouped.get(advice, 0) + 1
    if advice_grouped:
        lines.append("")
        lines.append("Quality advice summary:")
        for advice, count in sorted(advice_grouped.items()):
            lines.append(f"- {advice}: {count}")
    blocked = [item for item in queue if item.get("status") == "auto_blocked"]
    if blocked:
        lines.append("")
        lines.append(f"Auto-blocked items: {len(blocked)}")
        for item in blocked[:8]:
            lines.append(
                (
                    f"- {item.get('id', '')} | {item.get('platform', '')} | "
                    f"{item.get('quality_badge', '🔴')}{item.get('total_score', 0)} | "
                    f"{item.get('quality_reason', '')[:60]}"
                )
            )
    lines.append("")
    lines.append("Top pending items:")
    pending = [item for item in queue if item["status"] in {"pending_review", "approved"}][:8]
    if not pending:
        lines.append("- none")
    else:
        for item in pending:
            lines.append(
                (
                    f"- {item['id']} | {item['platform']} | "
                    f"{item.get('quality_badge', '⚪')}{item.get('total_score', 0)} | "
                    f"{item.get('publish_time', '--')} | {item.get('title', '')[:36]}"
                )
            )
    return "\n".join(lines)


def send_email_digest(config: dict[str, Any], body: str) -> bool:
    email_cfg = config.get("notification", {}).get("email", {})
    if not email_cfg.get("enabled", False):
        print("[INFO] Email digest disabled.")
        return False

    password = os.getenv(email_cfg.get("password_env", "SMTP_PASSWORD"), "")
    if not password:
        raise ValueError("SMTP password env var is missing.")

    msg = MIMEText(body, _subtype="plain", _charset="utf-8")
    msg["Subject"] = f"[ContentOps] Queue Digest {now_local().strftime('%Y-%m-%d')}"
    msg["From"] = email_cfg["sender"]
    msg["To"] = ", ".join(email_cfg.get("receivers", []))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(
        email_cfg["smtp_host"], int(email_cfg.get("smtp_port", 465)), context=context
    ) as smtp:
        smtp.login(email_cfg["sender"], password)
        smtp.sendmail(email_cfg["sender"], email_cfg.get("receivers", []), msg.as_string())
    print("[OK] digest email sent")
    return True


def send_feishu_webhook(config: dict[str, Any], body: str) -> bool:
    feishu_cfg = config.get("notification", {}).get("feishu", {})
    if not feishu_cfg.get("enabled", False):
        print("[INFO] Feishu webhook disabled.")
        return False
    webhook = feishu_cfg.get("webhook_url", "").strip()
    if not webhook:
        raise ValueError("Feishu webhook is enabled but webhook_url is missing.")

    payload = {"msg_type": "text", "content": {"text": body}}
    response = http_post_json(webhook, payload, timeout=20)
    if response.get("StatusCode") not in (0, None):
        raise ValueError(f"Feishu webhook failed: {response}")
    print("[OK] feishu notification sent")
    return True


def get_feishu_tenant_access_token(bitable_cfg: dict[str, Any]) -> str:
    app_id = os.getenv(bitable_cfg.get("app_id_env", "FEISHU_APP_ID"), "")
    app_secret = os.getenv(bitable_cfg.get("app_secret_env", "FEISHU_APP_SECRET"), "")
    if not app_id or not app_secret:
        raise ValueError("Feishu Bitable app credentials env vars are missing.")

    payload = {"app_id": app_id, "app_secret": app_secret}
    response = http_post_json(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        payload,
        timeout=20,
    )
    if response.get("code", 0) != 0:
        raise ValueError(f"Feishu auth failed: {response}")
    token = response.get("tenant_access_token", "")
    if not token:
        raise ValueError("Feishu auth succeeded but tenant_access_token is empty.")
    return token


def list_feishu_field_names(token: str, app_token: str, table_id: str) -> set[str]:
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/"
        f"{table_id}/fields?page_size=500"
    )
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}, method="GET"
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        data = json.loads(response.read().decode("utf-8"))
    if data.get("code", 0) != 0:
        raise ValueError(f"list fields failed: {data}")
    items = data.get("data", {}).get("items", [])
    return {str(item.get("field_name", "")).strip() for item in items if item.get("field_name")}


def ensure_feishu_fields(token: str, app_token: str, table_id: str) -> None:
    existing = list_feishu_field_names(token, app_token, table_id)
    created: list[str] = []
    for field_name, definition in FEISHU_FIELD_DEFINITIONS.items():
        if field_name in existing:
            continue
        payload: dict[str, Any] = {"field_name": field_name, "type": definition["type"]}
        if "property" in definition:
            payload["property"] = definition["property"]
        url = (
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/"
            f"{table_id}/fields"
        )
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=25) as response:
            data = json.loads(response.read().decode("utf-8"))
        if data.get("code", 0) != 0:
            raise ValueError(f"create field failed {field_name}: {data}")
        created.append(field_name)
    if created:
        print(f"[OK] Feishu fields created: {', '.join(created)}")


def to_feishu_fields(item: dict[str, Any]) -> dict[str, Any]:
    hashtags = item.get("hashtags", [])
    hashtags_text = " ".join(hashtags) if isinstance(hashtags, list) else str(hashtags)
    content = extract_content_payload(item)
    return {
        "QueueID": item.get("id", ""),
        "Date": item.get("date", ""),
        "Platform": item.get("platform", ""),
        "Account": item.get("account_name", ""),
        "Track": item.get("track", ""),
        "Status": item.get("status", ""),
        "PublishTime": item.get("publish_time", ""),
        "Title": item.get("title", ""),
        "Hashtags": hashtags_text,
        "SourceTopic": item.get("source_topic", ""),
        "SourceLink": item.get("source_link", ""),
        "ContentFile": item.get("content_file", ""),
        "PreviewFile": item.get("preview_file", ""),
        "PreviewURL": item.get("preview_url", ""),
        "SampleVideoFile": item.get("sample_video_file", ""),
        "SampleVideoURL": item.get("sample_video_url", ""),
        "SampleAudioFile": item.get("sample_audio_file", ""),
        "IllustrationFiles": "\n".join(item.get("illustration_files", []))
        if isinstance(item.get("illustration_files"), list)
        else str(item.get("illustration_files", "")),
        "IllustrationURLs": "\n".join(item.get("illustration_urls", []))
        if isinstance(item.get("illustration_urls"), list)
        else str(item.get("illustration_urls", "")),
        "CloudVideoProvider": item.get("cloud_video_provider", ""),
        "CloudImageModel": item.get("cloud_image_model", ""),
        "CloudVideoModel": item.get("cloud_video_model", ""),
        "CloudVideoPredictionID": item.get("cloud_video_prediction_id", ""),
        "HookText": content["hook_text"][:2000],
        "BodyPreview": content["body_preview"][:1200],
        "ContentMarkdown": content["content_markdown"][:8000],
        "CoverText": content["cover_text"][:500],
        "PostURL": item.get("post_url") or "",
        "UpdatedAt": item.get("updated_at", ""),
        "Notes": item.get("notes", ""),
        "HookScore": clamp_score(item.get("hook_score", 0)),
        "StructureScore": clamp_score(item.get("structure_score", 0)),
        "PlatformFitScore": clamp_score(item.get("platform_fit_score", 0)),
        "CommercialScore": clamp_score(item.get("commercial_score", 0)),
        "ComplianceScore": clamp_score(item.get("compliance_score", 0)),
        "TotalScore": max(0, min(100, safe_int(item.get("total_score", 0), default=0))),
        "QualityLevel": item.get("quality_level", ""),
        "QualityBadge": item.get("quality_badge", "⚪"),
        "PublishAdvice": item.get("publish_advice", "需改"),
        "QualityReason": item.get("quality_reason", ""),
    }


def sync_queue_to_feishu_bitable(config: dict[str, Any], queue: list[dict[str, Any]]) -> bool:
    bitable_cfg = config.get("feishu_bitable", {})
    if not bitable_cfg.get("enabled", False):
        print("[INFO] Feishu Bitable sync disabled.")
        return False

    app_token = bitable_cfg.get("app_token", "").strip()
    table_id = bitable_cfg.get("table_id", "").strip()
    if not app_token or not table_id:
        raise ValueError("Feishu Bitable enabled but app_token/table_id is missing.")

    token = get_feishu_tenant_access_token(bitable_cfg)
    if bitable_cfg.get("auto_create_fields", True):
        ensure_feishu_fields(token, app_token, table_id)
    headers = {"Authorization": f"Bearer {token}"}
    base_url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
    )
    mapping: dict[str, str] = load_json(FEISHU_MAPPING_FILE, {})
    created = 0
    updated = 0

    for item in queue:
        queue_id = item["id"]
        fields = to_feishu_fields(item)
        record_id = mapping.get(queue_id, "")
        try:
            if record_id:
                request = urllib.request.Request(
                    f"{base_url}/{record_id}",
                    data=json.dumps({"fields": fields}).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": headers["Authorization"],
                    },
                    method="PUT",
                )
                with urllib.request.urlopen(request, timeout=25) as response:
                    data = json.loads(response.read().decode("utf-8"))
                if data.get("code", 0) != 0:
                    raise ValueError(f"update failed: {data}")
                updated += 1
            else:
                data = http_post_json(base_url, {"fields": fields}, headers=headers, timeout=25)
                if data.get("code", 0) != 0:
                    raise ValueError(f"create failed: {data}")
                new_id = data.get("data", {}).get("record", {}).get("record_id", "")
                if new_id:
                    mapping[queue_id] = new_id
                created += 1
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")[:500]
            print(f"[WARN] Feishu Bitable sync skipped for {queue_id}: http={exc.code} {detail}")
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, ValueError) as exc:
            print(f"[WARN] Feishu Bitable sync skipped for {queue_id}: {exc}")

    save_json(FEISHU_MAPPING_FILE, mapping)
    print(f"[OK] Feishu Bitable sync done. created={created}, updated={updated}")
    return True


def get_platform_adapter_cfg(config: dict[str, Any], platform: str) -> dict[str, Any]:
    adapters = config.get("publish_adapters", {})
    return adapters.get(platform, {})


def wechat_get_access_token(adapter_cfg: dict[str, Any]) -> str:
    appid = os.getenv(adapter_cfg.get("appid_env", "WECHAT_OFFICIAL_APPID"), "")
    appsecret = os.getenv(
        adapter_cfg.get("appsecret_env", "WECHAT_OFFICIAL_APPSECRET"), ""
    )
    if not appid or not appsecret:
        raise ValueError("Missing WeChat app credentials env vars.")

    params = urllib.parse.urlencode(
        {
            "grant_type": "client_credential",
            "appid": appid,
            "secret": appsecret,
        }
    )
    response = http_get_json(f"https://api.weixin.qq.com/cgi-bin/token?{params}", timeout=20)
    if response.get("errcode", 0) not in (0, None):
        raise ValueError(f"WeChat token request failed: {response}")
    token = response.get("access_token", "")
    if not token:
        raise ValueError("WeChat token response missing access_token.")
    return token


def read_markdown_content(path: str) -> str:
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Content file not found: {path}")
    return file_path.read_text(encoding="utf-8")


def auto_publish_wechat_official(
    item: dict[str, Any], adapter_cfg: dict[str, Any]
) -> tuple[str, str]:
    if not adapter_cfg.get("enabled", False):
        return (
            "auto_publish_pending_integration",
            "WeChat adapter is disabled in publish_adapters.wechat_official.",
        )

    thumb_media_id = adapter_cfg.get("thumb_media_id", "").strip()
    if not thumb_media_id:
        return (
            "auto_publish_failed",
            "Missing thumb_media_id for WeChat draft add API.",
        )

    token = wechat_get_access_token(adapter_cfg)
    markdown_body = read_markdown_content(item.get("content_file", ""))
    content_html = markdown_to_simple_html(markdown_body)
    digest_limit = int(adapter_cfg.get("digest_max_length", 110))
    digest = strip_markdown(markdown_body)[:digest_limit]
    author = adapter_cfg.get("author", item.get("account_name", ""))

    draft_payload = {
        "articles": [
            {
                "title": item.get("title", "")[:64],
                "author": author,
                "digest": digest,
                "content": content_html,
                "content_source_url": item.get("source_link", ""),
                "thumb_media_id": thumb_media_id,
                "need_open_comment": int(adapter_cfg.get("need_open_comment", 0)),
                "only_fans_can_comment": int(
                    adapter_cfg.get("only_fans_can_comment", 0)
                ),
            }
        ]
    }
    draft_url = f"https://api.weixin.qq.com/cgi-bin/draft/add?access_token={token}"
    draft_response = http_post_json(draft_url, draft_payload, timeout=25)
    if draft_response.get("errcode", 0) not in (0, None):
        return ("auto_publish_failed", f"WeChat draft add failed: {draft_response}")
    media_id = draft_response.get("media_id", "")
    if not media_id:
        return ("auto_publish_failed", "WeChat draft add missing media_id.")

    if not adapter_cfg.get("submit_to_publish", True):
        return ("auto_draft_created", f"WeChat draft created. media_id={media_id}")

    submit_url = f"https://api.weixin.qq.com/cgi-bin/freepublish/submit?access_token={token}"
    submit_response = http_post_json(submit_url, {"media_id": media_id}, timeout=25)
    if submit_response.get("errcode", 0) not in (0, None):
        return ("auto_publish_failed", f"WeChat submit failed: {submit_response}")

    publish_id = submit_response.get("publish_id", "")
    if publish_id:
        return (
            "auto_publish_submitted",
            f"WeChat submitted. media_id={media_id}, publish_id={publish_id}",
        )
    return ("auto_publish_submitted", f"WeChat submitted. media_id={media_id}")


def run_notify(config: dict[str, Any], queue: list[dict[str, Any]]) -> None:
    summary = build_queue_summary(queue)
    sent_any = False
    try:
        sent_any = send_feishu_webhook(config, summary) or sent_any
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[WARN] Feishu notify failed: {exc}")
    try:
        sent_any = send_email_digest(config, summary) or sent_any
    except Exception as exc:  # pylint: disable=broad-except
        print(f"[WARN] Email notify failed: {exc}")
    if not sent_any:
        print("[INFO] No notification channel enabled.")


def command_plan_day(args: argparse.Namespace) -> None:
    ensure_dirs()
    config = load_config(Path(args.config))
    date = args.date or now_local().strftime("%Y-%m-%d")
    per_track_limit = int(config.get("generation", {}).get("topics_per_track", 6))

    tracks: dict[str, Any] = config.get("tracks", {})
    platforms: list[dict[str, Any]] = config.get("platforms", [])
    if not tracks or not platforms:
        raise ValueError("Config missing tracks/platforms.")

    track_topics: dict[str, list[dict[str, Any]]] = {}
    for track_name, track_cfg in tracks.items():
        topics = collect_track_topics(track_name, track_cfg, per_track_limit)
        track_topics[track_name] = topics
        save_json(DATA_DIR / date / f"topics_{track_name}.json", topics)
        print(f"[INFO] {track_name}: collected {len(topics)} topics")

    selection_index: dict[str, int] = {}
    queue = load_queue()
    new_items = 0
    newly_created_items: list[dict[str, Any]] = []

    for platform_cfg in platforms:
        track_name = platform_cfg["track"]
        topics = track_topics.get(track_name, [])
        if not topics:
            print(f"[WARN] No topics for track={track_name}, skip {platform_cfg['platform']}")
            continue

        idx = selection_index.get(track_name, 0) % len(topics)
        selection_index[track_name] = idx + 1
        topic = topics[idx]

        queue_item = build_queue_item(
            config, date, platform_cfg, tracks[track_name], track_name, topic
        )
        generate_preview_for_item(config, queue_item)
        queue.append(queue_item)
        newly_created_items.append(queue_item)
        new_items += 1
        print(
            (
                f"[OK] queued {platform_cfg['platform']} -> {queue_item['id']} "
                f"{queue_item['quality_badge']}{queue_item['total_score']} "
                f"({queue_item['title'][:38]})"
            )
        )

    guard_result = apply_quality_guard(config, queue)
    if guard_result["changed"]:
        print(
            f"[INFO] Quality guard auto-blocked {len(guard_result['blocked_items'])} item(s)."
        )

    preview_index = build_preview_index(config, newly_created_items, date)
    if preview_index["index_file"]:
        print(f"[OK] Preview index: {preview_index['index_file']}")
        if preview_index["index_url"]:
            print(f"[OK] Preview URL: {preview_index['index_url']}")
    accounts_index = build_accounts_index(config, queue, date)
    if accounts_index["index_file"]:
        print(f"[OK] Accounts index: {accounts_index['index_file']}")
        if accounts_index["index_url"]:
            print(f"[OK] Accounts URL: {accounts_index['index_url']}")
    dashboard_index = build_dashboard_index(config, queue, date)
    if dashboard_index["index_file"]:
        print(f"[OK] Dashboard index: {dashboard_index['index_file']}")
    preview_portal = build_preview_portal(config)
    if preview_portal["portal_file"]:
        print(f"[OK] Preview portal: {preview_portal['portal_file']}")
        if preview_portal["portal_url"]:
            print(f"[OK] Preview portal URL: {preview_portal['portal_url']}")

    sample_cfg = config.get("sample_video", {})
    local_cfg = config.get("local_gpu", {})
    cloud_cfg = config.get("cloud_media", {})
    sample_results: list[dict[str, str]] = []
    if local_cfg.get("enabled", False) and local_cfg.get("auto_export_on_plan_day", True):
        sample_results = export_video_jobs_for_items(config, newly_created_items)
        if sample_results:
            print(f"[OK] Video jobs exported for local GPU: {len(sample_results)}")
    elif sample_cfg.get("auto_render_on_plan_day", False):
        sample_results = render_samples_for_items(
            config,
            newly_created_items,
            include_blocked=bool(sample_cfg.get("include_blocked", False)),
        )
        if sample_results:
            print(f"[OK] Sample videos rendered: {len(sample_results)}")

    if cloud_cfg.get("enabled", False):
        image_cfg = cloud_cfg.get("image", {})
        video_cfg = cloud_cfg.get("video", {})
        if image_cfg.get("enabled", False) and image_cfg.get("auto_render_on_plan_day", False):
            image_results = render_cloud_illustrations_for_items(config, newly_created_items)
            ok_count = len([x for x in image_results if x.get("status") == "ok"])
            if ok_count:
                print(f"[OK] Cloud illustrations rendered: {ok_count}")
        if video_cfg.get("enabled", False) and video_cfg.get("auto_render_on_plan_day", False):
            video_results = render_cloud_videos_for_items(
                config,
                newly_created_items,
                include_blocked=bool(video_cfg.get("include_blocked", False)),
            )
            ok_count = len([x for x in video_results if x.get("status") == "ok"])
            if ok_count:
                print(f"[OK] Cloud videos rendered: {ok_count}")

    save_queue(queue)
    print(f"[DONE] Created {new_items} queue items.")

    notified = False
    guard_cfg = config.get("quality_guard", {})
    if guard_result["blocked_items"] and guard_cfg.get("notify_on_block", True):
        alert_body = build_block_alert(guard_result["blocked_items"])
        try:
            notified = send_feishu_webhook(config, alert_body) or notified
        except Exception as exc:  # pylint: disable=broad-except
            print(f"[WARN] block alert feishu failed: {exc}")
        try:
            notified = send_email_digest(config, alert_body) or notified
        except Exception as exc:  # pylint: disable=broad-except
            print(f"[WARN] block alert email failed: {exc}")

    if args.sync_feishu:
        sync_queue_to_feishu_bitable(config, queue)
    if args.notify and not notified:
        run_notify(config, queue)


def command_list(_: argparse.Namespace) -> None:
    ensure_dirs()
    queue = load_queue()
    if not queue:
        print("Queue is empty.")
        return

    print("ID           | STATUS                    | SCORE | ADVICE | PLATFORM         | TIME   | TITLE")
    print("-" * 138)
    for item in queue:
        print(
            f"{item['id']:<12} | "
            f"{item['status']:<25} | "
            f"{(str(item.get('quality_badge', '⚪')) + str(item.get('total_score', 0))).ljust(5)} | "
            f"{str(item.get('publish_advice', '需改')):<5} | "
            f"{item['platform']:<16} | "
            f"{(item.get('publish_time') or '--'): <6} | "
            f"{item.get('title', '')[:36]}"
        )


def command_approve(args: argparse.Namespace) -> None:
    ensure_dirs()
    config = load_config(Path(args.config))
    queue = load_queue()
    ok = update_item_status(queue, args.id, "approved", notes=args.note or "")
    if not ok:
        raise ValueError(f"Queue item not found: {args.id}")
    save_queue(queue)
    print(f"[OK] approved {args.id}")
    if args.sync_feishu:
        sync_queue_to_feishu_bitable(config, queue)


def command_reject(args: argparse.Namespace) -> None:
    ensure_dirs()
    config = load_config(Path(args.config))
    queue = load_queue()
    ok = update_item_status(queue, args.id, "rejected", notes=args.note or "")
    if not ok:
        raise ValueError(f"Queue item not found: {args.id}")
    save_queue(queue)
    print(f"[OK] rejected {args.id}")
    if args.sync_feishu:
        sync_queue_to_feishu_bitable(config, queue)


def command_publish(args: argparse.Namespace) -> None:
    ensure_dirs()
    config = load_config(Path(args.config))
    queue = load_queue()
    guard_result = apply_quality_guard(config, queue)
    if guard_result["changed"]:
        print(
            f"[INFO] Quality guard auto-blocked {len(guard_result['blocked_items'])} item(s) before publish."
        )
    changed = 0
    for item in queue:
        if item["status"] != "approved":
            continue

        if item.get("auto_publish", False):
            platform = item.get("platform", "")
            if platform == "wechat_official":
                adapter_cfg = get_platform_adapter_cfg(config, "wechat_official")
                try:
                    new_status, notes = auto_publish_wechat_official(item, adapter_cfg)
                except Exception as exc:  # pylint: disable=broad-except
                    new_status = "auto_publish_failed"
                    notes = f"WeChat auto publish exception: {exc}"
                item["status"] = new_status
                item["notes"] = notes
            else:
                item["status"] = "auto_publish_pending_integration"
                item["notes"] = (
                    f"Enable official adapter under publish_adapters.{platform}."
                )
        else:
            item["status"] = "ready_to_post"
            item["notes"] = "Manual upload required. Content file already generated."

        item["updated_at"] = now_local().isoformat()
        changed += 1
        print(f"[OK] publish action set for {item['id']} -> {item['status']}")

    save_queue(queue)
    print(f"[DONE] Updated {changed} queue items.")
    guard_cfg = config.get("quality_guard", {})
    if guard_result["blocked_items"] and guard_cfg.get("notify_on_block", True):
        alert_body = build_block_alert(guard_result["blocked_items"])
        try:
            send_feishu_webhook(config, alert_body)
        except Exception as exc:  # pylint: disable=broad-except
            print(f"[WARN] block alert feishu failed: {exc}")
        try:
            send_email_digest(config, alert_body)
        except Exception as exc:  # pylint: disable=broad-except
            print(f"[WARN] block alert email failed: {exc}")
    if args.sync_feishu:
        sync_queue_to_feishu_bitable(config, queue)


def command_mark_posted(args: argparse.Namespace) -> None:
    ensure_dirs()
    config = load_config(Path(args.config))
    queue = load_queue()
    found = False
    for item in queue:
        if item["id"] != args.id:
            continue
        item["status"] = "posted"
        item["post_url"] = args.url
        item["updated_at"] = now_local().isoformat()
        found = True
        print(f"[OK] marked posted: {args.id}")
        break
    if not found:
        raise ValueError(f"Queue item not found: {args.id}")
    save_queue(queue)
    if args.sync_feishu:
        sync_queue_to_feishu_bitable(config, queue)


def command_email_digest(args: argparse.Namespace) -> None:
    ensure_dirs()
    config = load_config(Path(args.config))
    queue = load_queue()
    send_email_digest(config, build_queue_summary(queue))


def command_sync_feishu(args: argparse.Namespace) -> None:
    ensure_dirs()
    config = load_config(Path(args.config))
    queue = load_queue()
    sync_queue_to_feishu_bitable(config, queue)


def command_notify(args: argparse.Namespace) -> None:
    ensure_dirs()
    config = load_config(Path(args.config))
    queue = load_queue()
    run_notify(config, queue)


def command_reset_generated(args: argparse.Namespace) -> None:
    if not bool(args.yes):
        raise ValueError("Refusing to clear generated data without --yes")
    for path in [DATA_DIR, OUTBOX_DIR, PREVIEW_DIR, VIDEO_JOBS_DIR]:
        if path.exists():
            shutil.rmtree(path)
            print(f"[OK] removed {path}")
    if QUEUE_FILE.exists():
        QUEUE_FILE.unlink()
        print(f"[OK] removed {QUEUE_FILE}")
    ensure_dirs()
    save_queue([])
    print("[DONE] Generated data cleared. Config and Feishu mapping were preserved.")


def command_generate_account(args: argparse.Namespace) -> None:
    ensure_dirs()
    config = load_config(Path(args.config))
    date = str(args.date or now_local().strftime("%Y-%m-%d"))
    count = max(1, int(args.count))
    platforms = find_platform_configs(
        config,
        account=str(args.account or "").strip(),
        platform=str(args.platform or "").strip(),
    )
    if not platforms:
        raise ValueError("No account matched. Use --account with account name or --platform.")
    tracks: dict[str, Any] = config.get("tracks", {})
    if not tracks:
        raise ValueError("Config missing tracks.")

    per_track_limit = max(count * len(platforms), int(config.get("generation", {}).get("topics_per_track", 6)))
    track_topics: dict[str, list[dict[str, Any]]] = {}
    for platform_cfg in platforms:
        track_name = str(platform_cfg.get("track", "")).strip()
        if track_name in track_topics:
            continue
        track_cfg = tracks.get(track_name)
        if not track_cfg:
            print(f"[WARN] missing track config: {track_name}")
            continue
        topics = collect_track_topics(track_name, track_cfg, per_track_limit)
        track_topics[track_name] = topics
        save_json(DATA_DIR / date / f"topics_{track_name}.json", topics)
        print(f"[INFO] {track_name}: collected {len(topics)} topics")

    queue = load_queue()
    created_items: list[dict[str, Any]] = []
    for platform_cfg in platforms:
        track_name = str(platform_cfg.get("track", "")).strip()
        track_cfg = tracks.get(track_name, {})
        topics = track_topics.get(track_name, [])
        if not topics:
            print(f"[WARN] no topics for {platform_cfg.get('account_name')} ({track_name})")
            continue
        for idx in range(count):
            topic = topics[idx % len(topics)]
            item = build_queue_item(config, date, platform_cfg, track_cfg, track_name, topic)
            queue.append(item)
            created_items.append(item)
            print(
                f"[OK] generated {item['account_name']} -> {item['id']} "
                f"{item['quality_badge']}{item['total_score']} {item['title'][:42]}"
            )

    if not created_items:
        print("[DONE] No content generated.")
        return

    guard_result = apply_quality_guard(config, queue)
    if guard_result["changed"]:
        print(f"[INFO] Quality guard auto-blocked {len(guard_result['blocked_items'])} item(s).")

    cloud_cfg = config.get("cloud_media", {})
    image_cfg = cloud_cfg.get("image", {})
    if bool(args.render_images) and cloud_cfg.get("enabled", False) and image_cfg.get("enabled", False):
        image_results = render_cloud_illustrations_for_items(config, created_items)
        ok_count = len([x for x in image_results if x.get("status") == "ok"])
        print(f"[OK] illustrations ready: {ok_count}/{len(created_items)}")

    for item in created_items:
        generate_preview_for_item(config, item)

    date_items = [item for item in queue if item.get("date") == date]
    preview_index = build_preview_index(config, date_items, date)
    accounts_index = build_accounts_index(config, queue, date)
    build_dashboard_index(config, queue, date)
    preview_portal = build_preview_portal(config)
    save_queue(queue)

    print(f"[OK] preview index: {preview_index.get('index_url') or preview_index.get('index_file')}")
    print(f"[OK] accounts page: {accounts_index.get('index_url') or accounts_index.get('index_file')}")
    if preview_portal.get("portal_url"):
        print(f"[OK] preview portal: {preview_portal['portal_url']}")
    print(f"[DONE] Generated {len(created_items)} item(s).")
    if args.sync_feishu:
        sync_queue_to_feishu_bitable(config, queue)


def command_serve_review(args: argparse.Namespace) -> None:
    ensure_dirs()
    config_path = str(args.config)
    host = str(args.host)
    port = int(args.port)
    jobs: dict[str, dict[str, Any]] = {}
    jobs_lock = threading.Lock()

    class JobLogWriter:
        def __init__(self, job_id: str) -> None:
            self.job_id = job_id
            self._buffer = ""

        def write(self, text: str) -> int:
            self._buffer += text
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                if line.strip():
                    with jobs_lock:
                        jobs[self.job_id].setdefault("lines", []).append(line)
            return len(text)

        def flush(self) -> None:
            if self._buffer.strip():
                with jobs_lock:
                    jobs[self.job_id].setdefault("lines", []).append(self._buffer.strip())
            self._buffer = ""

    def start_generation_job(account: str, platform: str, count: int, sync_feishu: bool) -> str:
        job_id = uuid.uuid4().hex[:10]
        with jobs_lock:
            jobs[job_id] = {
                "status": "running",
                "lines": [
                    f"[START] 账号={account or '全部'} 平台={platform or '全部'} 数量={count}",
                    "[STEP] 收集选题 -> 生成文案 -> DashScope配图 -> 重建预览/素材包",
                ],
                "created_at": now_local().isoformat(),
            }

        def runner() -> None:
            writer = JobLogWriter(job_id)
            try:
                with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                    command_generate_account(
                        argparse.Namespace(
                            config=config_path,
                            account=account,
                            platform=platform,
                            date=None,
                            count=count,
                            render_images=True,
                            sync_feishu=sync_feishu,
                        )
                    )
                writer.flush()
                with jobs_lock:
                    jobs[job_id]["status"] = "done"
                    jobs[job_id].setdefault("lines", []).append("[DONE] 生成完成")
            except Exception as exc:  # pylint: disable=broad-except
                writer.flush()
                with jobs_lock:
                    jobs[job_id]["status"] = "failed"
                    jobs[job_id].setdefault("lines", []).append(f"[ERROR] {exc}")

        threading.Thread(target=runner, daemon=True).start()
        return job_id

    class ReviewHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *handler_args: Any, **handler_kwargs: Any) -> None:
            super().__init__(*handler_args, directory=str(PREVIEW_DIR), **handler_kwargs)

        def end_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def do_GET(self) -> None:  # noqa: N802 - stdlib API
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/generate":
                params = urllib.parse.parse_qs(parsed.query)
                account = (params.get("account") or [""])[0]
                platform = (params.get("platform") or [""])[0]
                count = safe_int((params.get("count") or ["3"])[0], default=3)
                sync_feishu = (params.get("sync_feishu") or ["1"])[0] not in {"0", "false", "False"}
                async_mode = (params.get("async") or ["0"])[0] in {"1", "true", "True"}
                if async_mode:
                    try:
                        job_id = start_generation_job(account, platform, count, sync_feishu)
                        payload = {"job_id": job_id, "status": "running"}
                        self.send_response(200)
                    except Exception as exc:  # pylint: disable=broad-except
                        payload = {"error": str(exc)}
                        self.send_response(500)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                    return
                buffer = io.StringIO()
                status = 200
                with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                    try:
                        command_generate_account(
                            argparse.Namespace(
                                config=config_path,
                                account=account,
                                platform=platform,
                                date=None,
                                count=count,
                                render_images=True,
                                sync_feishu=sync_feishu,
                            )
                        )
                    except Exception as exc:  # pylint: disable=broad-except
                        status = 500
                        print(f"[ERROR] {exc}")
                payload = buffer.getvalue()
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(payload.encode("utf-8", errors="replace"))
                return
            if parsed.path == "/job-status":
                params = urllib.parse.parse_qs(parsed.query)
                job_id = (params.get("id") or [""])[0]
                with jobs_lock:
                    payload = dict(jobs.get(job_id) or {"status": "missing", "lines": ["任务不存在或服务已重启"]})
                    payload["lines"] = list(payload.get("lines", []))[-120:]
                self.send_response(200 if payload.get("status") != "missing" else 404)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                return
            if parsed.path == "/status":
                params = urllib.parse.parse_qs(parsed.query)
                item_id = (params.get("id") or [""])[0]
                status = (params.get("status") or [""])[0]
                allowed = {"pending_review", "approved", "ready_to_post", "posted", "rejected"}
                if not item_id or status not in allowed:
                    self.send_response(400)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.end_headers()
                    self.wfile.write("参数错误".encode("utf-8"))
                    return
                try:
                    config = load_config(Path(config_path))
                    queue = load_queue()
                    found = False
                    for item in queue:
                        if item.get("id") != item_id:
                            continue
                        item["status"] = status
                        item["updated_at"] = now_local().isoformat()
                        generate_preview_for_item(config, item)
                        found = True
                        item_date = str(item.get("date", now_local().strftime("%Y-%m-%d")))
                        build_preview_index(config, [entry for entry in queue if entry.get("date") == item_date], item_date)
                        build_accounts_index(config, queue, item_date)
                        build_dashboard_index(config, queue, item_date)
                        build_preview_portal(config)
                        break
                    if not found:
                        raise ValueError(f"Queue item not found: {item_id}")
                    save_queue(queue)
                    message = f"已更新为 {status}"
                    self.send_response(200)
                except Exception as exc:  # pylint: disable=broad-except
                    message = f"更新失败：{exc}"
                    self.send_response(500)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(message.encode("utf-8", errors="replace"))
                return
            if parsed.path == "/action":
                params = urllib.parse.parse_qs(parsed.query)
                item_id = (params.get("id") or [""])[0]
                action = (params.get("action") or [""])[0]
                if not item_id or action not in {"images", "pack"}:
                    self.send_response(400)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.end_headers()
                    self.wfile.write("参数错误".encode("utf-8"))
                    return
                try:
                    config = load_config(Path(config_path))
                    queue = load_queue()
                    item = next((entry for entry in queue if entry.get("id") == item_id), None)
                    if item is None:
                        raise ValueError(f"Queue item not found: {item_id}")
                    if action == "images":
                        render_cloud_illustrations_for_items(config, [item])
                        message = "已重新配图"
                    else:
                        build_asset_pack_for_item(config, item)
                        message = "素材包已重建"
                    generate_preview_for_item(config, item)
                    item_date = str(item.get("date", now_local().strftime("%Y-%m-%d")))
                    build_preview_index(config, [entry for entry in queue if entry.get("date") == item_date], item_date)
                    build_accounts_index(config, queue, item_date)
                    build_dashboard_index(config, queue, item_date)
                    build_preview_portal(config)
                    save_queue(queue)
                    self.send_response(200)
                except Exception as exc:  # pylint: disable=broad-except
                    message = f"操作失败：{exc}"
                    self.send_response(500)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(message.encode("utf-8", errors="replace"))
                return
            if parsed.path == "/export-selected":
                params = urllib.parse.parse_qs(parsed.query)
                date = (params.get("date") or [""])[0]
                account = (params.get("account") or [""])[0]
                platform = (params.get("platform") or [""])[0]
                buffer = io.StringIO()
                status_code = 200
                with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                    try:
                        command_export_selected(
                            argparse.Namespace(
                                config=config_path,
                                date=date,
                                account=account,
                                platform=platform,
                            )
                        )
                    except Exception as exc:  # pylint: disable=broad-except
                        status_code = 500
                        print(f"[ERROR] {exc}")
                self.send_response(status_code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(buffer.getvalue().encode("utf-8", errors="replace"))
                return
            if parsed.path == "/daily-log":
                log_path = BASE_DIR / "daily_generate_summary.log"
                detail_path = BASE_DIR / "daily_generate.log"
                parts = ["# 每日自动生成日志", ""]
                if log_path.exists():
                    parts.append("## 摘要")
                    parts.append(log_path.read_text(encoding="utf-8", errors="replace")[-8000:])
                else:
                    parts.append("暂无摘要日志。")
                if detail_path.exists():
                    parts.append("\n## 最近详细日志")
                    parts.append(detail_path.read_text(encoding="utf-8", errors="replace")[-8000:])
                payload = "\n".join(parts)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(payload.encode("utf-8", errors="replace"))
                return
            if parsed.path == "/download-packs":
                params = urllib.parse.parse_qs(parsed.query)
                date = (params.get("date") or [""])[0]
                status = (params.get("status") or ["all"])[0]
                try:
                    config = load_config(Path(config_path))
                    queue = load_queue()
                    result = build_bulk_asset_pack(config, queue, date=date, status=status)
                    pack_path = Path(result["bulk_pack_file"])
                    payload = pack_path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/zip")
                    self.send_header(
                        "Content-Disposition",
                        f"attachment; filename*=UTF-8''{urllib.parse.quote(pack_path.name)}",
                    )
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except Exception as exc:  # pylint: disable=broad-except
                    self.send_response(500)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(f"批量打包失败：{exc}".encode("utf-8", errors="replace"))
                return
            if parsed.path == "/":
                self.path = "/index.html"
            return super().do_GET()

    server = http.server.ThreadingHTTPServer((host, port), ReviewHandler)
    print(f"[OK] Review server running: http://{host}:{port}")
    server.serve_forever()


def command_export_selected(args: argparse.Namespace) -> None:
    ensure_dirs()
    config = load_config(Path(args.config))
    queue = load_queue()
    selected = filter_queue_items(
        queue,
        date=str(args.date or "").strip(),
        account=str(args.account or "").strip(),
        platform=str(args.platform or "").strip(),
        include_blocked=True,
    )
    selected = [item for item in selected if item.get("status") in {"approved", "ready_to_post", "posted"}]
    if not selected:
        print("No selected/publishable items matched filters.")
        return
    export_date = str(args.date or now_local().strftime("%Y-%m-%d"))
    out_file = OUTBOX_DIR / export_date / "selected_publish_list.md"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# 已选发布内容清单 {export_date}", ""]
    for item in selected:
        content = extract_content_payload(item)
        lines.extend(
            [
                f"## {item.get('account_name', '')} / {item.get('platform', '')} / {item.get('id', '')}",
                "",
                f"- 状态：{status_label(item.get('status'))}",
                f"- 下一步：{next_step_for_item(item)}",
                f"- 预览：{item.get('preview_url', '')}",
                f"- 素材包：{item.get('asset_pack_url', '')}",
                "",
                f"### 标题\n{item.get('title', '')}",
                "",
                f"### 正文\n{content.get('body_markdown', '')}",
                "",
                f"### 配图\n" + "\n".join([f"- {url}" for url in item.get("illustration_urls", [])]),
                "",
            ]
        )
    out_file.write_text("\n".join(lines), encoding="utf-8")
    print(f"[OK] exported selected list: {out_file}")


def command_export_packs(args: argparse.Namespace) -> None:
    ensure_dirs()
    config = load_config(Path(args.config))
    queue = load_queue()
    result = build_bulk_asset_pack(
        config,
        queue,
        date=str(args.date or "").strip(),
        status=str(args.status or "all").strip(),
    )
    print(f"[OK] bulk asset pack: {result['bulk_pack_file']}")
    if result.get("bulk_pack_url"):
        print(f"[OK] bulk asset pack URL: {result['bulk_pack_url']}")


def command_preview(args: argparse.Namespace) -> None:
    ensure_dirs()
    config = load_config(Path(args.config))
    queue = load_queue()
    if not queue:
        print("Queue is empty.")
        return

    selected = filter_queue_items(
        queue,
        item_id=str(args.id or "").strip(),
        date=str(args.date or "").strip(),
        account=str(getattr(args, "account", "") or "").strip(),
        platform=str(getattr(args, "platform", "") or "").strip(),
        limit=int(args.limit),
    )
    if not selected:
        print("No queue items matched preview filters.")
        return

    grouped_by_date: dict[str, list[dict[str, Any]]] = {}
    for item in selected:
        generate_preview_for_item(config, item)
        item_date = str(item.get("date", now_local().strftime("%Y-%m-%d")))
        grouped_by_date.setdefault(item_date, []).append(item)

    for item_date, items in grouped_by_date.items():
        preview_index = build_preview_index(config, items, item_date)
        print(f"[OK] preview index ({item_date}): {preview_index['index_file']}")
        if preview_index["index_url"]:
            print(f"[OK] preview url ({item_date}): {preview_index['index_url']}")
        accounts_index = build_accounts_index(config, queue, item_date)
        print(f"[OK] accounts index ({item_date}): {accounts_index['index_file']}")
        dashboard_index = build_dashboard_index(config, queue, item_date)
        print(f"[OK] dashboard index ({item_date}): {dashboard_index['index_file']}")

    preview_portal = build_preview_portal(config)
    if preview_portal["portal_file"]:
        print(f"[OK] preview portal: {preview_portal['portal_file']}")
        if preview_portal["portal_url"]:
            print(f"[OK] preview portal url: {preview_portal['portal_url']}")

    save_queue(queue)
    if args.sync_feishu:
        sync_queue_to_feishu_bitable(config, queue)


def command_render_samples(args: argparse.Namespace) -> None:
    ensure_dirs()
    config = load_config(Path(args.config))
    queue = load_queue()
    if not queue:
        print("Queue is empty.")
        return

    selected = filter_queue_items(
        queue,
        item_id=str(args.id or "").strip(),
        date=str(args.date or "").strip(),
        limit=int(args.limit),
        only_pending=bool(args.only_pending),
        include_blocked=bool(args.include_blocked),
        post_format="short_video_script",
    )
    if not selected:
        print("No queue items matched render-samples filters.")
        return

    results = render_samples_for_items(
        config,
        selected,
        include_blocked=bool(args.include_blocked),
    )
    save_queue(queue)
    print(f"[DONE] Sample render complete. success={len(results)}")
    if args.sync_feishu:
        sync_queue_to_feishu_bitable(config, queue)


def command_render_illustrations(args: argparse.Namespace) -> None:
    ensure_dirs()
    config = load_config(Path(args.config))
    if not cloud_media_enabled(config):
        print("[WARN] cloud_media.enabled is false. Enable it to render illustrations.")
        return
    queue = load_queue()
    if not queue:
        print("Queue is empty.")
        return
    selected = filter_queue_items(
        queue,
        item_id=str(args.id or "").strip(),
        date=str(args.date or "").strip(),
        account=str(getattr(args, "account", "") or "").strip(),
        platform=str(getattr(args, "platform", "") or "").strip(),
        limit=int(args.limit),
        only_pending=bool(args.only_pending),
        include_blocked=bool(args.include_blocked),
    )
    if not selected:
        print("No queue items matched render-illustrations filters.")
        return
    results = render_cloud_illustrations_for_items(config, selected)
    grouped_by_date: dict[str, list[dict[str, Any]]] = {}
    for item in selected:
        generate_preview_for_item(config, item)
        item_date = str(item.get("date", now_local().strftime("%Y-%m-%d")))
        grouped_by_date.setdefault(item_date, []).append(item)
    for item_date, items in grouped_by_date.items():
        build_preview_index(config, [item for item in queue if item.get("date") == item_date], item_date)
        build_accounts_index(config, queue, item_date)
    build_preview_portal(config)
    save_queue(queue)
    ok_count = len([x for x in results if x.get("status") == "ok"])
    print(f"[DONE] Cloud illustration render complete. success={ok_count}")
    if args.sync_feishu:
        sync_queue_to_feishu_bitable(config, queue)


def command_render_cloud_videos(args: argparse.Namespace) -> None:
    ensure_dirs()
    config = load_config(Path(args.config))
    if not cloud_media_enabled(config):
        print("[WARN] cloud_media.enabled is false. Enable it to render cloud videos.")
        return
    queue = load_queue()
    if not queue:
        print("Queue is empty.")
        return
    selected = filter_queue_items(
        queue,
        item_id=str(args.id or "").strip(),
        date=str(args.date or "").strip(),
        limit=int(args.limit),
        only_pending=bool(args.only_pending),
        include_blocked=bool(args.include_blocked),
        post_format="short_video_script",
    )
    if not selected:
        print("No queue items matched render-cloud-videos filters.")
        return
    results = render_cloud_videos_for_items(
        config, selected, include_blocked=bool(args.include_blocked)
    )
    for item in selected:
        generate_preview_for_item(config, item)
    save_queue(queue)
    ok_count = len([x for x in results if x.get("status") == "ok"])
    print(f"[DONE] Cloud video render complete. success={ok_count}")
    if args.sync_feishu:
        sync_queue_to_feishu_bitable(config, queue)


def command_export_video_jobs(args: argparse.Namespace) -> None:
    ensure_dirs()
    config = load_config(Path(args.config))
    if not local_gpu_enabled(config):
        print("[WARN] local_gpu.enabled is false. Enable it in config to use export-video-jobs.")
    queue = load_queue()
    if not queue:
        print("Queue is empty.")
        return

    selected = filter_queue_items(
        queue,
        item_id=str(args.id or "").strip(),
        date=str(args.date or "").strip(),
        limit=int(args.limit),
        only_pending=bool(args.only_pending),
        include_blocked=bool(args.include_blocked),
        post_format="short_video_script",
    )
    if not selected:
        print("No queue items matched export-video-jobs filters.")
        return

    results = export_video_jobs_for_items(config, selected)
    save_queue(queue)
    print(f"[DONE] Video job export complete. exported={len(results)}")
    pending_dir = video_jobs_pending_dir(config)
    print(f"[INFO] Pending jobs directory: {pending_dir}")
    if args.sync_feishu:
        sync_queue_to_feishu_bitable(config, queue)


def command_import_local_video(args: argparse.Namespace) -> None:
    ensure_dirs()
    config = load_config(Path(args.config))
    queue = load_queue()
    if not queue:
        print("Queue is empty.")
        return

    job_id = str(args.id or "").strip()
    if not job_id:
        print("--id is required.")
        return

    video_path = Path(args.video_path).expanduser() if args.video_path else None
    result = import_local_video_for_item(config, queue, job_id, video_path)
    save_queue(queue)
    print(f"[OK] Imported local video {job_id} -> {result.get('video_file', '')}")
    if result.get("video_url"):
        print(f"[OK] Sample video URL: {result['video_url']}")

    preview_portal = build_preview_portal(config)
    if preview_portal.get("portal_url"):
        print(f"[OK] Preview portal url: {preview_portal['portal_url']}")
    if args.sync_feishu:
        sync_queue_to_feishu_bitable(config, queue)


def command_list_video_jobs(args: argparse.Namespace) -> None:
    ensure_dirs()
    config = load_config(Path(args.config))
    ensure_video_job_dirs(config)
    status = str(args.status or "all").strip().lower()
    dirs: list[tuple[str, Path]] = []
    if status in {"all", "pending"}:
        dirs.append(("pending", video_jobs_pending_dir(config)))
    if status in {"all", "completed"}:
        dirs.append(("completed", video_jobs_completed_dir(config)))
    if status in {"all", "failed"}:
        dirs.append(("failed", video_jobs_failed_dir(config)))

    total = 0
    for label, directory in dirs:
        jobs = sorted(directory.glob("*.json"))
        if not jobs:
            print(f"[{label}] (empty)")
            continue
        print(f"[{label}] {len(jobs)} job(s)")
        for job_file in jobs:
            payload = load_json(job_file, {})
            total += 1
            print(
                f"  - {payload.get('job_id', job_file.stem)} | "
                f"{payload.get('date', '')} | "
                f"{payload.get('platform', '')} | "
                f"{payload.get('status', label)} | "
                f"{payload.get('title', '')[:42]}"
            )
    if total == 0:
        print("No video jobs found.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Multi-platform content automation")
    parser.add_argument(
        "--config",
        default=str(BASE_DIR / "config.json"),
        help="Path to config JSON file.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan-day", help="Collect topics and generate drafts")
    p_plan.add_argument("--date", default=None, help="Date in YYYY-MM-DD")
    p_plan.add_argument(
        "--sync-feishu", action="store_true", help="Sync queue to Feishu Bitable after run"
    )
    p_plan.add_argument(
        "--notify", action="store_true", help="Send reminder after queue generation"
    )
    p_plan.set_defaults(func=command_plan_day)

    p_list = sub.add_parser("list", help="List queue items")
    p_list.set_defaults(func=command_list)

    p_approve = sub.add_parser("approve", help="Approve one queue item")
    p_approve.add_argument("--id", required=True, help="Queue item ID")
    p_approve.add_argument("--note", default="", help="Optional note")
    p_approve.add_argument(
        "--sync-feishu", action="store_true", help="Sync queue to Feishu Bitable"
    )
    p_approve.set_defaults(func=command_approve)

    p_reject = sub.add_parser("reject", help="Reject one queue item")
    p_reject.add_argument("--id", required=True, help="Queue item ID")
    p_reject.add_argument("--note", default="", help="Optional note")
    p_reject.add_argument(
        "--sync-feishu", action="store_true", help="Sync queue to Feishu Bitable"
    )
    p_reject.set_defaults(func=command_reject)

    p_publish = sub.add_parser("publish", help="Prepare publishing actions")
    p_publish.add_argument(
        "--sync-feishu", action="store_true", help="Sync queue to Feishu Bitable"
    )
    p_publish.set_defaults(func=command_publish)

    p_mark = sub.add_parser("mark-posted", help="Mark one item as posted")
    p_mark.add_argument("--id", required=True, help="Queue item ID")
    p_mark.add_argument("--url", default="", help="Published post URL")
    p_mark.add_argument(
        "--sync-feishu", action="store_true", help="Sync queue to Feishu Bitable"
    )
    p_mark.set_defaults(func=command_mark_posted)

    p_mail = sub.add_parser("email-digest", help="Send digest email summary")
    p_mail.set_defaults(func=command_email_digest)

    p_sync = sub.add_parser("sync-feishu", help="Sync queue to Feishu Bitable")
    p_sync.set_defaults(func=command_sync_feishu)

    p_notify = sub.add_parser("notify", help="Send queue summary to notification channels")
    p_notify.set_defaults(func=command_notify)

    p_reset = sub.add_parser("reset-generated", help="Clear generated queue/outbox/preview data")
    p_reset.add_argument("--yes", action="store_true", help="Required confirmation")
    p_reset.set_defaults(func=command_reset_generated)

    p_gen_account = sub.add_parser(
        "generate-account",
        help="Generate several candidate posts for one account and render images",
    )
    p_gen_account.add_argument("--account", default="", help="Account name or account id")
    p_gen_account.add_argument("--platform", default="", help="Optional exact platform filter")
    p_gen_account.add_argument("--date", default=None, help="Date in YYYY-MM-DD")
    p_gen_account.add_argument("--count", type=int, default=3, help="How many candidates per account")
    p_gen_account.add_argument(
        "--no-render-images", dest="render_images", action="store_false", help="Skip DashScope images"
    )
    p_gen_account.add_argument(
        "--sync-feishu", action="store_true", help="Sync generated items to Feishu Bitable"
    )
    p_gen_account.set_defaults(func=command_generate_account, render_images=True)

    p_serve = sub.add_parser("serve-review", help="Serve preview UI with click-to-generate endpoint")
    p_serve.add_argument("--host", default="0.0.0.0", help="Bind host")
    p_serve.add_argument("--port", type=int, default=8787, help="Bind port")
    p_serve.set_defaults(func=command_serve_review)

    p_export_selected = sub.add_parser("export-selected", help="Export approved/posted content list")
    p_export_selected.add_argument("--date", default="", help="Date filter YYYY-MM-DD")
    p_export_selected.add_argument("--account", default="", help="Account filter")
    p_export_selected.add_argument("--platform", default="", help="Exact platform filter")
    p_export_selected.set_defaults(func=command_export_selected)

    p_export_packs = sub.add_parser("export-packs", help="Export a bulk ZIP of asset packs")
    p_export_packs.add_argument("--date", default="", help="Date filter YYYY-MM-DD")
    p_export_packs.add_argument(
        "--status",
        default="all",
        choices=["all", "selected", "pending_review", "approved", "ready_to_post", "posted", "rejected"],
        help="Which items to include",
    )
    p_export_packs.set_defaults(func=command_export_packs)

    p_preview = sub.add_parser("preview", help="Generate visual preview HTML pages")
    p_preview.add_argument("--id", default="", help="Queue item ID")
    p_preview.add_argument("--date", default="", help="Date filter YYYY-MM-DD")
    p_preview.add_argument("--account", default="", help="Account filter")
    p_preview.add_argument("--platform", default="", help="Exact platform filter")
    p_preview.add_argument("--limit", type=int, default=10, help="How many recent items")
    p_preview.add_argument(
        "--sync-feishu", action="store_true", help="Sync preview fields to Feishu Bitable"
    )
    p_preview.set_defaults(func=command_preview)

    p_sample = sub.add_parser("render-samples", help="Render auto samples with subtitles and TTS")
    p_sample.add_argument("--id", default="", help="Queue item ID")
    p_sample.add_argument("--date", default="", help="Date filter YYYY-MM-DD")
    p_sample.add_argument("--limit", type=int, default=10, help="How many recent items")
    p_sample.add_argument(
        "--only-pending", action="store_true", help="Only process pending/approved items"
    )
    p_sample.add_argument(
        "--include-blocked", action="store_true", help="Include auto-blocked items"
    )
    p_sample.add_argument(
        "--sync-feishu", action="store_true", help="Sync sample video fields to Feishu Bitable"
    )
    p_sample.set_defaults(func=command_render_samples)

    p_ill = sub.add_parser(
        "render-illustrations",
        help="Render cloud illustrations for article/graphic posts",
    )
    p_ill.add_argument("--id", default="", help="Queue item ID")
    p_ill.add_argument("--date", default="", help="Date filter YYYY-MM-DD")
    p_ill.add_argument("--account", default="", help="Account filter")
    p_ill.add_argument("--platform", default="", help="Exact platform filter")
    p_ill.add_argument("--limit", type=int, default=10, help="How many recent items")
    p_ill.add_argument(
        "--only-pending", action="store_true", help="Only process pending/approved items"
    )
    p_ill.add_argument(
        "--include-blocked", action="store_true", help="Include auto-blocked items"
    )
    p_ill.add_argument(
        "--sync-feishu", action="store_true", help="Sync illustration fields to Feishu Bitable"
    )
    p_ill.set_defaults(func=command_render_illustrations)

    p_cloud_video = sub.add_parser(
        "render-cloud-videos",
        help="Render short videos via cloud provider",
    )
    p_cloud_video.add_argument("--id", default="", help="Queue item ID")
    p_cloud_video.add_argument("--date", default="", help="Date filter YYYY-MM-DD")
    p_cloud_video.add_argument("--limit", type=int, default=10, help="How many recent items")
    p_cloud_video.add_argument(
        "--only-pending", action="store_true", help="Only process pending/approved items"
    )
    p_cloud_video.add_argument(
        "--include-blocked", action="store_true", help="Include auto-blocked items"
    )
    p_cloud_video.add_argument(
        "--sync-feishu", action="store_true", help="Sync cloud video fields to Feishu Bitable"
    )
    p_cloud_video.set_defaults(func=command_render_cloud_videos)

    p_export = sub.add_parser(
        "export-video-jobs",
        help="Export short-video jobs for local ComfyUI rendering",
    )
    p_export.add_argument("--id", default="", help="Queue item ID")
    p_export.add_argument("--date", default="", help="Date filter YYYY-MM-DD")
    p_export.add_argument("--limit", type=int, default=10, help="How many recent items")
    p_export.add_argument(
        "--only-pending", action="store_true", help="Only process pending/approved items"
    )
    p_export.add_argument(
        "--include-blocked", action="store_true", help="Include auto-blocked items"
    )
    p_export.add_argument(
        "--sync-feishu", action="store_true", help="Sync queue fields to Feishu Bitable"
    )
    p_export.set_defaults(func=command_export_video_jobs)

    p_import = sub.add_parser(
        "import-local-video",
        help="Import a locally rendered MP4 into preview media and queue",
    )
    p_import.add_argument("--id", required=True, help="Queue item ID")
    p_import.add_argument(
        "--video-path",
        default="",
        help="Local MP4 path. If omitted, uses completed job metadata or existing media file.",
    )
    p_import.add_argument(
        "--sync-feishu", action="store_true", help="Sync sample video fields to Feishu Bitable"
    )
    p_import.set_defaults(func=command_import_local_video)

    p_jobs = sub.add_parser("list-video-jobs", help="List exported local GPU video jobs")
    p_jobs.add_argument(
        "--status",
        default="all",
        choices=["all", "pending", "completed", "failed"],
        help="Filter by job status directory",
    )
    p_jobs.set_defaults(func=command_list_video_jobs)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
