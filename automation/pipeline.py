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
import datetime as dt
import email.utils
import html
import json
import os
import re
import shutil
import smtplib
import ssl
import subprocess
import tempfile
import textwrap
import urllib.error
import urllib.parse
import urllib.request
import uuid
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


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"Config not found: {path}\nCopy automation/config.example.json first."
        )
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
    return f"{public_base_url.rstrip('/')}/{relative_path.as_posix()}"


def cache_bust_token(item: dict[str, Any]) -> str:
    raw = str(item.get("updated_at", "")).strip() or str(item.get("created_at", "")).strip()
    token = re.sub(r"\D", "", raw)[:14]
    return token or str(safe_int(item.get("total_score", 0), default=0))


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


def build_video_script_text(item: dict[str, Any]) -> str:
    content = extract_content_payload(item)
    hook = content.get("hook_text", "").strip()
    body = strip_markdown(content.get("body_markdown", "")).strip()
    if hook and body:
        return f"{hook}\n\n{body}"
    return hook or body or str(item.get("title", "")).strip()


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
            "workflow_dir": str(comfy_cfg.get("workflow_dir", "workflows")),
            "checkpoint_name": str(comfy_cfg.get("checkpoint_name", "")),
            "checkpoint_candidates": comfy_cfg.get(
                "checkpoint_candidates",
                [
                    "dreamshaper_8.safetensors",
                    "majicmixRealistic_v7.safetensors",
                    "v1-5-pruned-emaonly.safetensors",
                ],
            ),
            "width": int(comfy_cfg.get("width", 720)),
            "height": int(comfy_cfg.get("height", 1280)),
            "max_segments": int(comfy_cfg.get("max_segments", 6)),
            "tts_voice": str(comfy_cfg.get("tts_voice", "zh-CN-XiaoxiaoNeural")),
            "base_seed": int(comfy_cfg.get("base_seed", 42)),
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
    status = html.escape(str(item.get("status", "")).strip())
    body_html = markdown_to_simple_html(content.get("body_markdown", ""))
    post_format = str(item.get("post_format", ""))

    metadata_html = (
        f"<div class='meta'><span>{badge} {score}/100 · {advice}</span>"
        f"<span>Status: {status}</span><span>发布时间: {publish_time}</span></div>"
        f"<div class='meta'><span>账号: {account}</span><span>平台: {platform}</span></div>"
        f"<div class='meta'><span>选题: {source_topic}</span>"
        f"<a href='{source_link}' target='_blank' rel='noreferrer'>来源链接</a></div>"
    )

    sample_video_url = str(item.get("sample_video_url", "")).strip()
    sample_video_file = str(item.get("sample_video_file", "")).strip()
    if not sample_video_url and sample_video_file:
        sample_video_url = build_preview_url(config, Path(sample_video_file))

    if post_format == "short_video_script":
        segments = split_video_segments(content.get("body_markdown", ""))
        timeline_rows: list[str] = []
        for idx, segment in enumerate(segments):
            start = idx * 4
            end = start + 4
            timeline_rows.append(
                "<li>"
                f"<span class='time'>{start:02d}s-{end:02d}s</span>"
                f"<span class='line'>{html.escape(segment)}</span>"
                "</li>"
            )
        timeline_html = "\n".join(timeline_rows) if timeline_rows else "<li><span class='line'>暂无分镜</span></li>"
        video_player = (
            f"<div class='video-player'><video controls playsinline preload='metadata' src='{html.escape(sample_video_url)}'></video></div>"
            if sample_video_url
            else (
                "<div class='video-missing'>暂未生成样片视频。本地 GPU 模式请执行 export-video-jobs，"
                "在电脑上通过 ComfyUI worker 渲染后回传；服务器模式请执行 render-samples。</div>"
                if local_gpu_enabled(config)
                else "<div class='video-missing'>暂未生成样片视频，请先执行 render-samples。</div>"
            )
        )
        content_card = (
            "<div class='phone video'>"
            "<div class='video-cover'>"
            f"<div class='cover-text'>{cover_text or title}</div>"
            f"<div class='video-hook'>{hook_text}</div>"
            "</div>"
            f"{video_player}"
            "<div class='timeline'><h3>视频分镜时间轴（预演）</h3><ol>"
            f"{timeline_html}"
            "</ol></div></div>"
        )
    else:
        content_card = (
            "<div class='phone article'>"
            f"<h1>{title}</h1>"
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
      width: min(430px, 100%); margin: 0 auto; border: 1px solid #e5e7eb;
      border-radius: 18px; background: #fff; padding: 16px; box-shadow: inset 0 0 0 1px #f3f4f6;
    }}
    .article h1 {{ font-size: 21px; line-height: 1.4; margin: 0 0 12px; }}
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
    .time {{ display: inline-block; width: 70px; color: #2563eb; font-weight: 600; }}
    .line {{ color: #1f2937; }}
  </style>
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
        version = str(int(date_index.stat().st_mtime)) if date_index.exists() else "0"
        links.append(
            (
                f"<a class='date-link' href='{date}/index.html?v={version}' target='date-content-frame' "
                f"onclick=\"document.getElementById('current-date').innerText='{date}';\">{date}</a>"
            )
        )

    default_date = dates[0]
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
      <iframe name="date-content-frame" src="{default_date}/index.html?v={default_version}"></iframe>
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

        queue_id = uuid.uuid4().hex[:12]
        draft = generate_draft(config, platform_cfg, tracks[track_name], topic)
        quality = evaluate_draft_quality(config, platform_cfg, tracks[track_name], topic, draft)
        output_dir = OUTBOX_DIR / date / platform_cfg["platform"]
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / f"{queue_id}.md"
        output_file.write_text(
            render_markdown(queue_id, platform_cfg, topic, draft, quality=quality), encoding="utf-8"
        )

        queue_item = {
            "id": queue_id,
            "date": date,
            "status": "pending_review",
            "platform": platform_cfg["platform"],
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
            "content_markdown": render_markdown(
                queue_id, platform_cfg, topic, draft, quality=quality
            )[:8000],
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
        generate_preview_for_item(config, queue_item)
        queue.append(queue_item)
        newly_created_items.append(queue_item)
        new_items += 1
        print(
            (
                f"[OK] queued {platform_cfg['platform']} -> {queue_id} "
                f"{quality['quality_badge']}{quality['total_score']} "
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
    preview_portal = build_preview_portal(config)
    if preview_portal["portal_file"]:
        print(f"[OK] Preview portal: {preview_portal['portal_file']}")
        if preview_portal["portal_url"]:
            print(f"[OK] Preview portal URL: {preview_portal['portal_url']}")

    sample_cfg = config.get("sample_video", {})
    local_cfg = config.get("local_gpu", {})
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

    p_preview = sub.add_parser("preview", help="Generate visual preview HTML pages")
    p_preview.add_argument("--id", default="", help="Queue item ID")
    p_preview.add_argument("--date", default="", help="Date filter YYYY-MM-DD")
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
