# Local ComfyUI Video Worker

本地 **ComfyUI** 渲染短视频，完成后自动上传到服务器预览 / 飞书。

## 架构

```
服务器 export-video-jobs → 本地 worker.py → ComfyUI API (8000)
    → 下载成片 → scp 上传 → import-local-video → 预览门户
```

## 你需要准备

1. **ComfyUI** 运行在 `http://127.0.0.1:8000`
2. 从 ComfyUI 导出 API 工作流 → `D:\content-ops\workflows\short_video.api.json`
3. Python 3.10+、OpenSSH（scp）

详见 `workflows/README.md`

## 安装包下载

http://118.25.178.116:8787/local-gpu-deploy/local-gpu-worker.zip

## 一键运行

```powershell
# 探测 ComfyUI + 工作流文件
python D:\content-ops\scripts\worker.py --config D:\content-ops\local_config.json --probe

# 拉任务、渲染、上传
python D:\content-ops\scripts\worker.py --config D:\content-ops\local_config.json --once --retry-failed
```

或双击 `start_worker.bat`

## 服务器命令

```bash
python3 automation/pipeline.py --config automation/config.json export-video-jobs --only-pending --sync-feishu
python3 automation/pipeline.py --config automation/config.json list-video-jobs
```

`local_gpu.enabled=true` 时，`render-samples` 会自动改为导出 ComfyUI 任务。
