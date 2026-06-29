# Deployment Guide (Server + Semi/Auto Publishing)

This guide deploys a daily automation pipeline for:

- `涛哥儿聊个球` (WeChat Official Account, football)
- Douyin (child education)
- WeChat Channels (child education)
- Xiaohongshu (travel)
- Kuaishou (AI comedy)

## 1) Prepare files

```bash
cp automation/config.example.json automation/config.json
```

Then adjust `automation/config.json`:

- account names and publish windows
- RSS sources and keywords
- LLM model + endpoint
- quality scoring thresholds (`quality_scoring`)
- quality guard rules (`quality_guard`) for auto-blocking low-score or empty drafts
- preview config (`preview.public_base_url`) for clickable browser preview links
- notification and Feishu Bitable config
- adapter config for WeChat Official auto-publish

## 2) Environment variables

Required:

```bash
export OPENAI_API_KEY="your_api_key"
```

Optional (email digest):

```bash
export SMTP_PASSWORD="your_smtp_password"
```

Optional (Feishu Bitable sync):

```bash
export FEISHU_APP_ID="cli_xxx"
export FEISHU_APP_SECRET="xxx"
```

Optional (WeChat Official auto publish):

```bash
export WECHAT_OFFICIAL_APPID="wx_xxx"
export WECHAT_OFFICIAL_APPSECRET="xxx"
```

## 3) Generate daily queue (with Feishu sync + reminder)

```bash
python3 automation/pipeline.py --config automation/config.json plan-day --sync-feishu --notify
python3 automation/pipeline.py --config automation/config.json list
```

This creates:

- topic snapshots: `automation/data/<date>/topics_*.json`
- draft files: `automation/outbox/<date>/<platform>/<id>.md`
- queue: `automation/state/publish_queue.json`
- quality score per item (0-100) + publish advice (`可发/需改/禁发`)
- low quality / empty drafts can be auto-marked as `auto_blocked` by quality guard
- HTML previews in `automation/previews/<date>/`:
  - article mobile-style preview page
  - short-video storyboard preview page

## 4) Review and approve

Approve draft:

```bash
python3 automation/pipeline.py --config automation/config.json approve --id <queue_id> --sync-feishu
```

Reject draft:

```bash
python3 automation/pipeline.py --config automation/config.json reject --id <queue_id> --note "rewrite hook" --sync-feishu
```

## 5) Publish actions

```bash
python3 automation/pipeline.py --config automation/config.json publish --sync-feishu
```

Behavior:

- `auto_publish=false` -> `ready_to_post`
- `auto_publish=true` + supported adapter:
  - WeChat Official currently supported via official API
  - status updates to `auto_draft_created` / `auto_publish_submitted` / `auto_publish_failed`
- unsupported adapter -> `auto_publish_pending_integration`

After manual upload:

```bash
python3 automation/pipeline.py --config automation/config.json mark-posted --id <queue_id> --url "<post_link>" --sync-feishu
```

## 6) Feishu Bitable field requirements

Create the following fields in your table:

- QueueID
- Date
- Platform
- Account
- Track
- Status
- PublishTime
- Title
- Hashtags
- SourceTopic
- SourceLink
- ContentFile
- PreviewFile
- PreviewURL
- HookText
- BodyPreview
- ContentMarkdown
- CoverText
- PostURL
- UpdatedAt
- Notes
- HookScore
- StructureScore
- PlatformFitScore
- CommercialScore
- ComplianceScore
- TotalScore
- QualityLevel
- QualityBadge
- PublishAdvice
- QualityReason

Recommended field types:

- TotalScore / HookScore / StructureScore / PlatformFitScore / CommercialScore / ComplianceScore: Number
- PublishAdvice / QualityLevel / QualityBadge: Single line text (or Single select)
- QualityReason: Long text
- HookText / BodyPreview / ContentMarkdown: Long text
- PreviewFile / PreviewURL: Single line text

Recommended coloring rules in Bitable view:

- If `PublishAdvice = 可发` -> row color green
- If `PublishAdvice = 需改` -> row color yellow
- If `PublishAdvice = 禁发` -> row color red

If you use `QualityBadge` as a front column, it will display:

- 🟢 = high quality
- 🟡 = medium quality
- 🔴 = high risk / low quality

## 6.1) Dashboard update cadence

With the cron jobs in this guide:

- 07:30: new drafts generated and synced to Feishu
- every 10 minutes: status changes synced (approved / ready_to_post / posted / auto_blocked)
- any manual action (`approve`, `publish`, `mark-posted`, `sync-feishu`) can update immediately

So the table is not static; it keeps updating daily + incremental updates during the day.

## 6.2) Visual preview access

Generate previews manually:

```bash
python3 automation/pipeline.py --config automation/config.json preview --date 2026-07-03 --sync-feishu
```

Serve previews via HTTP:

```bash
python3 -m http.server 8787 --directory automation/previews
```

If `preview.public_base_url` is set (for example `http://YOUR_SERVER_IP:8787`), Feishu
records will include direct clickable links in `PreviewURL`.

You can sync on demand:

```bash
python3 automation/pipeline.py --config automation/config.json sync-feishu
```

## 7) Reminder channels

Run anytime:

```bash
python3 automation/pipeline.py --config automation/config.json notify
python3 automation/pipeline.py --config automation/config.json email-digest
```

## 8) WeChat Official adapter notes

Enable in config:

```json
"publish_adapters": {
  "wechat_official": {
    "enabled": true,
    "thumb_media_id": "YOUR_THUMB_MEDIA_ID"
  }
}
```

Important:

- `thumb_media_id` must already exist in the WeChat Official account material library.
- This pipeline submits through official endpoints:
  - `draft/add`
  - `freepublish/submit`

## 9) Schedule with cron

Edit crontab:

```bash
crontab -e
```

Recommended jobs (Asia/Shanghai):

```cron
# 07:30 generate drafts, sync bitable, notify
30 7 * * * cd /workspace && /usr/bin/python3 automation/pipeline.py --config automation/config.json plan-day --sync-feishu --notify >> /workspace/automation_cron.log 2>&1

# 11:35 midday reminder for football article
35 11 * * * cd /workspace && /usr/bin/python3 automation/pipeline.py --config automation/config.json notify >> /workspace/automation_cron.log 2>&1

# 17:35 evening reminder for short-video windows
35 17 * * * cd /workspace && /usr/bin/python3 automation/pipeline.py --config automation/config.json notify >> /workspace/automation_cron.log 2>&1
```

## 10) Remaining roadmap

Implement official adapters one by one:

1. Douyin creator API (if account access qualifies)
2. WeChat Channels official workflow APIs/tooling
3. Xiaohongshu creator APIs/tooling
4. Kuaishou creator APIs/tooling

Do not use unauthorized automation that simulates private account login behavior.
