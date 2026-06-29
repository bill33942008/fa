# ComfyUI Workflow Setup

Worker 只调用 **ComfyUI API**，不再依赖 Pixelle-Video。

## 一次性配置（3 步）

### 1. 在 ComfyUI 里打开你常用的短视频工作流

确保能手动跑通（含 TTS、字幕、出片等）。

### 2. 导出 API 格式工作流

ComfyUI 菜单：**Save (API Format)** 或 **开发模式 → 保存 API 格式**

保存为：

```
D:\content-ops\workflows\short_video.api.json
```

### 3. 运行 worker 探测

```powershell
python D:\content-ops\scripts\worker.py --config D:\content-ops\local_config.json --probe
```

## 脚本注入规则

默认会自动把任务文案写入工作流里的文本节点（如 `CLIPTextEncode`）：

| 顺序 | 注入内容 |
|------|----------|
| 第 1 个文本节点 | 标题 `title` |
| 第 2 个 | 开头 hook |
| 第 3 个及以后 | 完整脚本 `script_text` |

如需精确控制，在 `local_config.json` 里配置：

```json
"comfyui": {
  "injections": [
    {"node_id": "6", "input": "text", "from": "script_text"},
    {"node_id": "12", "input": "text", "from": "title"}
  ],
  "auto_inject_text": false
}
```

`from` 可选：`script_text` / `title` / `hook_text` / `cover_text`

## 节点 ID 怎么查

打开 `short_video.api.json`，顶层 key 就是 node_id，例如 `"6": {"class_type": "CLIPTextEncode", ...}`
