# Multi-Platform Content Automation

This repository provides a deployable starter kit for automated content operations
across Chinese social platforms.

Current target matrix:

- WeChat Official Account: `涛哥儿聊个球` (football analysis)
- Douyin: child education short videos
- WeChat Channels: child education short videos
- Xiaohongshu: travel content
- Kuaishou: AI comedy short videos

## Quick start

1. Copy example config:

   ```bash
   cp automation/config.example.json automation/config.json
   ```

2. Set API key env var used by your LLM provider:

   ```bash
   export OPENAI_API_KEY="your_api_key"
   ```

3. Generate daily drafts for all platforms:

   ```bash
   python3 automation/pipeline.py --config automation/config.json plan-day --sync-feishu --notify
   ```

   Each item is automatically scored with a quality badge:
   - 🟢 可发
   - 🟡 需改
   - 🔴 禁发
   - Low-score / empty-content drafts are auto-blocked by quality guard.

4. Review queue and approve drafts:

   ```bash
   python3 automation/pipeline.py --config automation/config.json list
   python3 automation/pipeline.py --config automation/config.json approve --id <queue_item_id> --sync-feishu
   ```

5. Prepare publishing actions:

   ```bash
   python3 automation/pipeline.py --config automation/config.json publish --sync-feishu
   ```

6. Mark content as posted after platform upload:

   ```bash
   python3 automation/pipeline.py --config automation/config.json mark-posted --id <queue_item_id> --url <post_url> --sync-feishu
   ```

7. Send reminders manually if needed:

   ```bash
   python3 automation/pipeline.py --config automation/config.json notify
   ```

8. Sync detailed content to Feishu dashboard:

   ```bash
   python3 automation/pipeline.py --config automation/config.json sync-feishu
   ```

   Feishu table now includes `HookText`, `BodyPreview`, and `ContentMarkdown`
   so you can read actual draft content directly in the table.

See full deployment guide in `automation/DEPLOYMENT.md`.
