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
   python3 automation/pipeline.py plan-day --config automation/config.json
   ```

4. Review queue and approve drafts:

   ```bash
   python3 automation/pipeline.py list --config automation/config.json
   python3 automation/pipeline.py approve --id <queue_item_id> --config automation/config.json
   ```

5. Prepare publishing actions:

   ```bash
   python3 automation/pipeline.py publish --config automation/config.json
   ```

6. Mark content as posted after platform upload:

   ```bash
   python3 automation/pipeline.py mark-posted --id <queue_item_id> --url <post_url> --config automation/config.json
   ```

See full deployment guide in `automation/DEPLOYMENT.md`.
