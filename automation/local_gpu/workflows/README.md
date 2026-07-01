# 内置短视频工作流（已随安装包提供）

无需自己搭建工作流。默认模式 `slideshow` 会：

1. 把脚本拆成 6 段以内
2. **ComfyUI** 为每段生成竖屏配图（720×1280）
3. **edge-tts** 生成中文配音
4. **ffmpeg** 合成带字幕 MP4

## 文件

| 文件 | 说明 |
|------|------|
| `short_video.api.json` | 内置 SD 竖屏配图工作流（仅 ComfyUI 核心节点） |

## 你需要准备

1. ComfyUI 运行在 `http://127.0.0.1:8000`
2. **至少一个 SD 模型** 放到 `ComfyUI/models/checkpoints/`
   - 推荐：`dreamshaper_8.safetensors` 或任意已下载的 checkpoint
3. 本机安装：
   ```powershell
   pip install edge-tts
   ```
   ffmpeg 加入 PATH（若还没有）

## 指定模型（可选）

如果自动检测失败，在 `local_config.json` 填写：

```json
"comfyui": {
  "checkpoint_name": "你的模型文件名.safetensors"
}
```

## 高级模式

`render_mode: "workflow"` 时，可把 ComfyUI 导出的完整 API 工作流放到此目录，由 worker 直接调用（需自行导出）。
