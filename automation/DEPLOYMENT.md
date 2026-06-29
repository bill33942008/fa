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

- Account names (already pre-filled)
- Publish times
- RSS sources and keywords
- LLM model + endpoint

## 2) Environment variables

```bash
export OPENAI_API_KEY="your_api_key"
```

Optional email digest:

```bash
export SMTP_PASSWORD="your_smtp_password"
```

## 3) Generate daily content queue

```bash
python3 automation/pipeline.py plan-day --config automation/config.json
python3 automation/pipeline.py list --config automation/config.json
```

This will create:

- Topic snapshots: `automation/data/<date>/topics_*.json`
- Draft files: `automation/outbox/<date>/<platform>/<id>.md`
- Queue: `automation/state/publish_queue.json`

## 4) Review and approve

Approve draft:

```bash
python3 automation/pipeline.py approve --id <queue_id> --config automation/config.json
```

Reject draft:

```bash
python3 automation/pipeline.py reject --id <queue_id> --note "rewrite hook" --config automation/config.json
```

## 5) Publish preparation

```bash
python3 automation/pipeline.py publish --config automation/config.json
```

- For `auto_publish=false`: status becomes `ready_to_post` (manual upload)
- For `auto_publish=true`: status becomes `auto_publish_pending_integration` (API adapter required)

After manual upload:

```bash
python3 automation/pipeline.py mark-posted --id <queue_id> --url "<post_link>" --config automation/config.json
```

## 6) Schedule with cron

Edit crontab:

```bash
crontab -e
```

Recommended jobs (Asia/Shanghai):

```cron
# 07:30 collect topics + generate drafts
30 7 * * * cd /workspace && /usr/bin/python3 automation/pipeline.py plan-day --config automation/config.json >> /workspace/automation_cron.log 2>&1

# 11:35 queue snapshot before football article window
35 11 * * * cd /workspace && /usr/bin/python3 automation/pipeline.py list --config automation/config.json >> /workspace/automation_cron.log 2>&1

# 17:35 queue snapshot before evening short-video windows
35 17 * * * cd /workspace && /usr/bin/python3 automation/pipeline.py list --config automation/config.json >> /workspace/automation_cron.log 2>&1
```

## 7) Recommended next integration step

Implement official API adapters one by one:

1. WeChat Official Account publish API
2. Douyin creator API (if account access qualifies)
3. WeChat Channels official workflow APIs/tooling
4. Xiaohongshu / Kuaishou official creator integrations

Do not use unauthorized automation that simulates private account login behavior.
