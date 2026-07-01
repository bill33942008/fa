# Local ComfyUI Video Worker

内置竖屏短视频工作流，**无需自己搭建 ComfyUI 流程**。

## 成片流程（slideshow 模式）

```
脚本分段 → ComfyUI 每段生成配图 → edge-tts 中文配音 → ffmpeg 字幕合成 → 上传服务器
```

## 准备

1. ComfyUI `http://127.0.0.1:8000`
2. 至少一个 checkpoint 模型在 `ComfyUI/models/checkpoints/`
3. `pip install edge-tts` + ffmpeg

```powershell
D:\content-ops\scripts\install_render_deps.bat
```

## 安装包

http://118.25.178.116:8787/local-gpu-deploy/local-gpu-worker.zip

## 运行

```powershell
python D:\content-ops\scripts\worker.py --config D:\content-ops\local_config.json --probe
python D:\content-ops\scripts\worker.py --config D:\content-ops\local_config.json --once --retry-failed
```

工作流文件已内置：`workflows/short_video.api.json`
