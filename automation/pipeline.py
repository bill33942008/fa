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
import smtplib
import ssl
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
QUEUE_FILE = STATE_DIR / "publish_queue.json"
FEISHU_MAPPING_FILE = STATE_DIR / "feishu_record_mapping.json"


def ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)


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


def to_feishu_fields(item: dict[str, Any]) -> dict[str, Any]:
    hashtags = item.get("hashtags", [])
    hashtags_text = " ".join(hashtags) if isinstance(hashtags, list) else str(hashtags)
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
        queue.append(queue_item)
        new_items += 1
        print(
            (
                f"[OK] queued {platform_cfg['platform']} -> {queue_id} "
                f"{quality['quality_badge']}{quality['total_score']} "
                f"({queue_item['title'][:38]})"
            )
        )

    save_queue(queue)
    print(f"[DONE] Created {new_items} queue items.")

    if args.sync_feishu:
        sync_queue_to_feishu_bitable(config, queue)
    if args.notify:
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

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
