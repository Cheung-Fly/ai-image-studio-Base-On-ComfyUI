"""ComfyUI HTTP 客户端：提交工作流、轮询结果、下载图片。"""
import time
from typing import Any

import httpx

from ..config import settings


class ComfyError(Exception):
    """ComfyUI 调用失败（连接、提交、超时等）。"""


class ComfyClient:
    def __init__(self, base_url: str | None = None, timeout: int | None = None):
        self.base_url = (base_url or settings.comfyui_base_url).rstrip("/")
        self.timeout = timeout or settings.comfy_timeout
        self.poll_interval = settings.comfy_poll_interval

    # ---------- 基础请求 ----------
    def _get(self, path: str) -> dict[str, Any]:
        try:
            resp = httpx.get(f"{self.base_url}{path}", timeout=self.timeout, trust_env=False)
        except httpx.HTTPError as exc:
            raise ComfyError(f"无法连接 ComfyUI（{self.base_url}）: {exc}") from exc
        if resp.status_code != 200:
            raise ComfyError(f"ComfyUI GET {path} 返回 {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = httpx.post(
                f"{self.base_url}{path}", json=payload, timeout=self.timeout, trust_env=False
            )
        except httpx.HTTPError as exc:
            raise ComfyError(f"无法连接 ComfyUI（{self.base_url}）: {exc}") from exc
        if resp.status_code != 200:
            raise ComfyError(f"ComfyUI POST {path} 返回 {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    # ---------- 业务方法 ----------
    def system_stats(self) -> dict[str, Any]:
        return self._get("/system_stats")

    def list_checkpoints(self) -> list[str]:
        info = self._get("/object_info/CheckpointLoaderSimple")
        options = info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
        return list(options)

    # ---------- 控制类接口（MCP 工具使用） ----------
    def _post_no_body(self, path: str, payload: dict[str, Any]) -> None:
        """POST 后不解析响应体（/interrupt、/free、清队列等返回空 body）。"""
        try:
            resp = httpx.post(
                f"{self.base_url}{path}", json=payload, timeout=self.timeout, trust_env=False
            )
        except httpx.HTTPError as exc:
            raise ComfyError(f"无法连接 ComfyUI（{self.base_url}）: {exc}") from exc
        if resp.status_code != 200:
            raise ComfyError(f"ComfyUI POST {path} 返回 {resp.status_code}: {resp.text[:300]}")

    def queue_info(self) -> dict[str, Any]:
        """当前执行中 / 排队中的任务。"""
        return self._get("/queue")

    def history(self, prompt_id: str = "", max_items: int = 10) -> dict[str, Any]:
        if prompt_id:
            return self._get(f"/history/{prompt_id}")
        return self._get(f"/history?max_items={int(max_items)}")

    def interrupt(self) -> None:
        self._post_no_body("/interrupt", {})

    def clear_queue(self) -> None:
        self._post_no_body("/queue", {"clear": True})

    def delete_queued(self, prompt_ids: list[str]) -> None:
        self._post_no_body("/queue", {"delete": prompt_ids})

    def free_memory(self, unload_models: bool = True, free_memory: bool = True) -> None:
        self._post_no_body("/free", {"unload_models": unload_models, "free_memory": free_memory})

    def object_info(self, node_class: str) -> dict[str, Any]:
        return self._get(f"/object_info/{node_class}")

    def list_node_options(self, node_class: str, input_name: str) -> list[str]:
        """读取某节点某个下拉输入的可选值（如 UNETLoader.unet_name、LoraLoader.lora_name）。"""
        info = self.object_info(node_class)
        try:
            spec = info[node_class]["input"]
            entry = (spec.get("required") or {}).get(input_name) or (
                spec.get("optional") or {}
            ).get(input_name)
            return list(entry[0])
        except (KeyError, TypeError, IndexError) as exc:
            raise ComfyError(f"无法读取 {node_class}.{input_name} 的可选项") from exc

    def submit_prompt(self, workflow: dict[str, Any]) -> str:
        """提交工作流，返回 prompt_id。"""
        resp = self._post("/prompt", {"prompt": workflow})
        prompt_id = resp.get("prompt_id")
        if not prompt_id:
            raise ComfyError(f"ComfyUI 未返回 prompt_id: {resp}")
        return prompt_id

    def wait_for_images(
        self, prompt_id: str, timeout: float | None = None
    ) -> list[dict[str, str]]:
        """轮询 /history 直到任务完成，返回 SaveImage 输出的图片元信息列表。"""
        deadline = time.monotonic() + (timeout or self.timeout)
        while time.monotonic() < deadline:
            history = self._get(f"/history/{prompt_id}")
            entry = history.get(prompt_id)
            if entry is not None:
                # 任务真正结束（status.completed / status.status_str）
                status = entry.get("status") or {}
                if status.get("completed") or status.get("status_str") in ("success", "completed"):
                    images = self._collect_images(entry)
                    if images:
                        return images
                    raise ComfyError("ComfyUI 任务完成但未找到输出图片")
                if status.get("status_str") in ("error", "failed"):
                    raise ComfyError(f"ComfyUI 执行失败: {status.get('messages', entry)[-1:]}")
            time.sleep(self.poll_interval)
        raise ComfyError(f"等待 ComfyUI 任务超时（> {timeout or self.timeout}s）: {prompt_id}")

    def _collect_images(self, history_entry: dict[str, Any]) -> list[dict[str, str]]:
        images: list[dict[str, str]] = []
        for output in (history_entry.get("outputs") or {}).values():
            for img in output.get("images", []):
                images.append(
                    {
                        "filename": img["filename"],
                        "subfolder": img.get("subfolder", ""),
                        "type": img.get("type", "output"),
                    }
                )
        return images

    def _collect_media(self, history_entry: dict[str, Any]) -> list[dict[str, str]]:
        """收集输出媒体：图片（images）与视频（gifs，VHS_VideoCombine 输出）。"""
        media: list[dict[str, str]] = []
        for output in (history_entry.get("outputs") or {}).values():
            for img in output.get("images", []):
                media.append(
                    {
                        "filename": img["filename"],
                        "subfolder": img.get("subfolder", ""),
                        "type": img.get("type", "output"),
                        "kind": "image",
                    }
                )
            # 视频输出在 gifs 字段（VHS_VideoCombine）
            for gif in output.get("gifs", []):
                media.append(
                    {
                        "filename": gif["filename"],
                        "subfolder": gif.get("subfolder", ""),
                        "type": gif.get("type", "output"),
                        "kind": "video",
                    }
                )
        return media

    def wait_for_media(
        self, prompt_id: str, timeout: float | None = None
    ) -> list[dict[str, str]]:
        """轮询 /history 直到完成，返回所有输出媒体（图片 + 视频）元信息。"""
        deadline = time.monotonic() + (timeout or self.timeout)
        while time.monotonic() < deadline:
            history = self._get(f"/history/{prompt_id}")
            entry = history.get(prompt_id)
            if entry is not None:
                status = entry.get("status") or {}
                if status.get("completed") or status.get("status_str") in ("success", "completed"):
                    media = self._collect_media(entry)
                    if media:
                        return media
                    raise ComfyError("ComfyUI 任务完成但未找到输出")
                if status.get("status_str") in ("error", "failed"):
                    raise ComfyError(f"ComfyUI 执行失败: {status.get('messages', entry)[-1:]}")
            time.sleep(self.poll_interval)
        raise ComfyError(f"等待 ComfyUI 任务超时（> {timeout or self.timeout}s）: {prompt_id}")

    def download_image(self, filename: str, subfolder: str = "", img_type: str = "output") -> bytes:
        params = {"filename": filename, "type": img_type}
        if subfolder:
            params["subfolder"] = subfolder
        try:
            resp = httpx.get(
                f"{self.base_url}/view", params=params, timeout=self.timeout, trust_env=False
            )
        except httpx.HTTPError as exc:
            raise ComfyError(f"下载图片失败: {exc}") from exc
        if resp.status_code != 200:
            raise ComfyError(f"下载图片 {filename} 返回 {resp.status_code}")
        return resp.content
