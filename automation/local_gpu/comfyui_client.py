"""ComfyUI REST API client for local GPU video rendering."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


TEXT_NODE_INPUTS: dict[str, str] = {
    "CLIPTextEncode": "text",
    "PrimitiveStringMultiline": "value",
    "TextInput": "text",
    "Text Multiline": "text",
    "ShowText|pysssss": "text",
    "StringConstant": "string",
    "Text": "text",
}


def http_get_json(url: str, timeout: int = 30) -> Any:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def http_post_json(url: str, payload: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def http_download(url: str, output_file: Path, timeout: int = 600) -> None:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        output_file.write_bytes(response.read())


class ComfyUIClient:
    def __init__(self, base_url: str, client_id: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id or f"content-ops-{uuid.uuid4().hex[:8]}"

    def probe(self) -> dict[str, Any]:
        try:
            return http_get_json(f"{self.base_url}/system_stats", timeout=8)
        except Exception:  # pylint: disable=broad-except
            http_get_json(f"{self.base_url}/queue", timeout=8)
            return {"status": "ok"}

    def list_checkpoints(self) -> list[str]:
        found: set[str] = set()
        endpoints = [
            f"{self.base_url}/models/checkpoints",
            f"{self.base_url}/models?folder=checkpoints",
        ]
        for url in endpoints:
            try:
                data = http_get_json(url, timeout=15)
                if isinstance(data, list):
                    found.update(str(name).strip() for name in data if str(name).strip())
            except Exception:  # pylint: disable=broad-except
                continue

        for path in ("/object_info/CheckpointLoaderSimple", "/object_info"):
            try:
                data = http_get_json(f"{self.base_url}{path}", timeout=20)
                if path.endswith("CheckpointLoaderSimple"):
                    node = data.get("CheckpointLoaderSimple", data)
                else:
                    node = data.get("CheckpointLoaderSimple", {})
                ckpt_cfg = node.get("input", {}).get("required", {}).get("ckpt_name")
                if isinstance(ckpt_cfg, list) and ckpt_cfg and isinstance(ckpt_cfg[0], list):
                    found.update(str(name).strip() for name in ckpt_cfg[0] if str(name).strip())
            except Exception:  # pylint: disable=broad-except
                continue

        return sorted(found)

    def load_workflow_file(self, workflow_path: Path) -> dict[str, Any]:
        raw = json.loads(workflow_path.read_text(encoding="utf-8"))
        if "prompt" in raw and isinstance(raw["prompt"], dict):
            return raw["prompt"]
        if isinstance(raw, dict) and all(isinstance(v, dict) for v in raw.values()):
            return raw
        raise ValueError(f"Unsupported workflow format: {workflow_path}")

    def apply_injections(
        self,
        workflow: dict[str, Any],
        job: dict[str, Any],
        injections: list[dict[str, Any]] | None = None,
        *,
        auto_inject: bool = True,
    ) -> dict[str, Any]:
        prompt = json.loads(json.dumps(workflow))
        context = {
            "script_text": str(job.get("script_text", "")).strip(),
            "title": str(job.get("title", "")).strip(),
            "hook_text": str(job.get("hook_text", "")).strip(),
            "cover_text": str(job.get("cover_text", "")).strip(),
            "account_name": str(job.get("account_name", "")).strip(),
            "platform": str(job.get("platform", "")).strip(),
        }

        if injections:
            for rule in injections:
                node_id = str(rule.get("node_id", "")).strip()
                input_key = str(rule.get("input", "text")).strip()
                source = str(rule.get("from", "script_text")).strip()
                if node_id not in prompt:
                    raise ValueError(f"Workflow node not found: {node_id}")
                prompt[node_id].setdefault("inputs", {})[input_key] = context.get(
                    source, ""
                )
            return prompt

        if not auto_inject:
            return prompt

        text_nodes: list[tuple[str, str]] = []
        for node_id, node in prompt.items():
            class_type = str(node.get("class_type", ""))
            input_key = TEXT_NODE_INPUTS.get(class_type)
            if input_key and input_key in node.get("inputs", {}):
                current = node["inputs"][input_key]
                if isinstance(current, str):
                    text_nodes.append((node_id, input_key))

        if not text_nodes:
            raise ValueError(
                "No injectable text nodes found in workflow. "
                "Export API workflow from ComfyUI and set comfyui.injections in config."
            )

        values = [
            context["title"][:200],
            context["hook_text"][:500] or context["script_text"][:800],
            context["script_text"][:2000],
        ]
        for idx, (node_id, input_key) in enumerate(text_nodes):
            value = values[min(idx, len(values) - 1)]
            prompt[node_id]["inputs"][input_key] = value
        return prompt

    def queue_prompt(self, workflow: dict[str, Any]) -> str:
        payload = {"prompt": workflow, "client_id": self.client_id}
        try:
            result = http_post_json(f"{self.base_url}/prompt", payload, timeout=120)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"ComfyUI queue failed HTTP {exc.code}: {detail}") from exc
        prompt_id = str(result.get("prompt_id", "")).strip()
        if not prompt_id:
            raise RuntimeError(f"ComfyUI did not return prompt_id: {result}")
        return prompt_id

    def wait_for_outputs(
        self,
        prompt_id: str,
        *,
        poll_interval: float = 3.0,
        timeout_seconds: int = 3600,
    ) -> dict[str, Any]:
        started = time.time()
        while True:
            try:
                history = http_get_json(f"{self.base_url}/history/{prompt_id}", timeout=30)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    history = {}
                else:
                    raise
            if prompt_id in history:
                entry = history[prompt_id]
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    messages = status.get("messages", [])
                    raise RuntimeError(f"ComfyUI workflow error: {messages}")
                outputs = entry.get("outputs", {})
                if outputs:
                    return outputs
            if time.time() - started > timeout_seconds:
                raise RuntimeError(f"ComfyUI timeout after {timeout_seconds}s: {prompt_id}")
            time.sleep(poll_interval)

    def pick_output_file(self, outputs: dict[str, Any]) -> dict[str, str]:
        video_ext = {".mp4", ".webm", ".mov", ".gif"}
        image_ext = {".png", ".jpg", ".jpeg", ".webp"}
        candidates: list[tuple[int, dict[str, str]]] = []

        for node_id, node_output in outputs.items():
            for kind, priority in (("videos", 0), ("gifs", 1), ("images", 2)):
                for item in node_output.get(kind, []) or []:
                    filename = str(item.get("filename", "")).strip()
                    if not filename:
                        continue
                    ext = Path(filename).suffix.lower()
                    score = priority
                    if ext in video_ext:
                        score -= 10
                    elif ext in image_ext:
                        score += 10
                    candidates.append(
                        (
                            score,
                            {
                                "filename": filename,
                                "subfolder": str(item.get("subfolder", "")),
                                "type": str(item.get("type", "output")),
                                "node_id": str(node_id),
                            },
                        )
                    )

        if not candidates:
            raise RuntimeError(f"No output files in ComfyUI result: {outputs}")

        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    def download_output(self, file_info: dict[str, str], output_file: Path) -> None:
        params = urllib.parse.urlencode(
            {
                "filename": file_info["filename"],
                "subfolder": file_info.get("subfolder", ""),
                "type": file_info.get("type", "output"),
            }
        )
        url = f"{self.base_url}/view?{params}"
        http_download(url, output_file)

    def render_job(
        self,
        job: dict[str, Any],
        comfy_cfg: dict[str, Any],
        output_file: Path,
    ) -> Path:
        workflow_path = Path(str(comfy_cfg.get("workflow_file", "")).strip())
        if not workflow_path.is_absolute():
            base = Path(str(comfy_cfg.get("workflow_dir", ".")))
            workflow_path = (base / workflow_path).resolve()
        if not workflow_path.exists():
            raise FileNotFoundError(
                f"ComfyUI workflow not found: {workflow_path}\n"
                "Export your video workflow from ComfyUI (Save API format) and update comfyui.workflow_file."
            )

        workflow = self.load_workflow_file(workflow_path)
        prompt = self.apply_injections(
            workflow,
            job,
            comfy_cfg.get("injections"),
            auto_inject=bool(comfy_cfg.get("auto_inject_text", True)),
        )
        prompt_id = self.queue_prompt(prompt)
        print(f"[OK] ComfyUI queued prompt_id={prompt_id}")
        outputs = self.wait_for_outputs(
            prompt_id,
            poll_interval=float(comfy_cfg.get("poll_interval_seconds", 3)),
            timeout_seconds=int(comfy_cfg.get("timeout_seconds", 3600)),
        )
        file_info = self.pick_output_file(outputs)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        download_path = output_file.with_suffix(Path(file_info["filename"]).suffix)
        self.download_output(file_info, download_path)
        return download_path
