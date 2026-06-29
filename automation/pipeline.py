#!/usr/bin/env python3
"""
Automated multi-platform content pipeline.

Capabilities:
- Collect topic candidates from RSS sources
- Generate platform-specific drafts with an LLM (or deterministic fallback)
- Create and maintain a review/publishing queue
- Support semi-automated publishing workflow
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import json
import os
import re
import smtplib
import ssl
import textwrap
import urllib.error
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
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
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


def render_markdown(
    queue_id: str,
    platform_cfg: dict[str, Any],
    topic: dict[str, Any],
    draft: dict[str, Any],
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
    return "\n".join(lines)


def load_queue() -> list[dict[str, Any]]:
    return load_json(QUEUE_FILE, [])


def save_queue(items: list[dict[str, Any]]) -> None:
    save_json(QUEUE_FILE, items)


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
        output_dir = OUTBOX_DIR / date / platform_cfg["platform"]
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / f"{queue_id}.md"
        output_file.write_text(
            render_markdown(queue_id, platform_cfg, topic, draft), encoding="utf-8"
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
        }
        queue.append(queue_item)
        new_items += 1
        print(
            f"[OK] queued {platform_cfg['platform']} -> {queue_id} ({queue_item['title'][:38]})"
        )

    save_queue(queue)
    print(f"[DONE] Created {new_items} queue items.")


def command_list(_: argparse.Namespace) -> None:
    ensure_dirs()
    queue = load_queue()
    if not queue:
        print("Queue is empty.")
        return

    print("ID           | STATUS          | PLATFORM         | TIME   | TITLE")
    print("-" * 98)
    for item in queue:
        print(
            f"{item['id']:<12} | "
            f"{item['status']:<14} | "
            f"{item['platform']:<16} | "
            f"{(item.get('publish_time') or '--'): <6} | "
            f"{item.get('title', '')[:40]}"
        )


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


def command_approve(args: argparse.Namespace) -> None:
    ensure_dirs()
    queue = load_queue()
    ok = update_item_status(queue, args.id, "approved", notes=args.note or "")
    if not ok:
        raise ValueError(f"Queue item not found: {args.id}")
    save_queue(queue)
    print(f"[OK] approved {args.id}")


def command_reject(args: argparse.Namespace) -> None:
    ensure_dirs()
    queue = load_queue()
    ok = update_item_status(queue, args.id, "rejected", notes=args.note or "")
    if not ok:
        raise ValueError(f"Queue item not found: {args.id}")
    save_queue(queue)
    print(f"[OK] rejected {args.id}")


def command_publish(_: argparse.Namespace) -> None:
    ensure_dirs()
    queue = load_queue()
    changed = 0
    for item in queue:
        if item["status"] != "approved":
            continue
        if item.get("auto_publish", False):
            # Stub hook: keep it explicit so you can connect official API later.
            item["status"] = "auto_publish_pending_integration"
            item["notes"] = (
                "Enable platform official API integration in a custom adapter."
            )
        else:
            item["status"] = "ready_to_post"
            item["notes"] = "Manual upload required. Content file already generated."
        item["updated_at"] = now_local().isoformat()
        changed += 1
        print(f"[OK] publish action set for {item['id']} -> {item['status']}")

    save_queue(queue)
    print(f"[DONE] Updated {changed} queue items.")


def command_mark_posted(args: argparse.Namespace) -> None:
    ensure_dirs()
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


def command_email_digest(args: argparse.Namespace) -> None:
    ensure_dirs()
    config = load_config(Path(args.config))
    email_cfg = config.get("notification", {}).get("email", {})
    if not email_cfg.get("enabled", False):
        raise ValueError("Email notification is disabled in config.")

    password = os.getenv(email_cfg.get("password_env", "SMTP_PASSWORD"), "")
    if not password:
        raise ValueError("SMTP password env var is missing.")

    queue = load_queue()
    if not queue:
        body = "Queue is empty."
    else:
        grouped: dict[str, int] = {}
        for item in queue:
            grouped[item["status"]] = grouped.get(item["status"], 0) + 1
        summary_lines = [f"- {status}: {count}" for status, count in sorted(grouped.items())]
        body = "Daily queue summary:\n" + "\n".join(summary_lines)

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
    p_plan.set_defaults(func=command_plan_day)

    p_list = sub.add_parser("list", help="List queue items")
    p_list.set_defaults(func=command_list)

    p_approve = sub.add_parser("approve", help="Approve one queue item")
    p_approve.add_argument("--id", required=True, help="Queue item ID")
    p_approve.add_argument("--note", default="", help="Optional note")
    p_approve.set_defaults(func=command_approve)

    p_reject = sub.add_parser("reject", help="Reject one queue item")
    p_reject.add_argument("--id", required=True, help="Queue item ID")
    p_reject.add_argument("--note", default="", help="Optional note")
    p_reject.set_defaults(func=command_reject)

    p_publish = sub.add_parser("publish", help="Prepare publishing actions")
    p_publish.set_defaults(func=command_publish)

    p_mark = sub.add_parser("mark-posted", help="Mark one item as posted")
    p_mark.add_argument("--id", required=True, help="Queue item ID")
    p_mark.add_argument("--url", default="", help="Published post URL")
    p_mark.set_defaults(func=command_mark_posted)

    p_mail = sub.add_parser("email-digest", help="Send digest email summary")
    p_mail.set_defaults(func=command_email_digest)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
