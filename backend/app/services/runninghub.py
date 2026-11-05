"""RunningHub Workflow API client and safe media download helpers."""

import json
import math
import mimetypes
import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

BASE_URL = "https://www.runninghub.ai"
ALLOWED_OUTPUT_HOSTS = {
    "runninghub.ai",
    "runninghub.cn",
    "rh-images.xiaoyaoyou.com",
    "rh-images-tos.xiaoyaoyou.com",
}
MAX_OUTPUT_BYTES = 100 * 1024 * 1024
MAX_INPUT_BYTES = 100 * 1024 * 1024
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv"}
EXTENSION_KIND = {
    **{extension: "image" for extension in IMAGE_EXTENSIONS},
    **{extension: "video" for extension in VIDEO_EXTENSIONS},
}
FILE_TYPE_EXTENSION = {
    "image": ".png",
    "png": ".png",
    "jpg": ".jpg",
    "jpeg": ".jpg",
    "webp": ".webp",
    "gif": ".gif",
    "video": ".mp4",
    "mp4": ".mp4",
    "mov": ".mov",
    "webm": ".webm",
    "mkv": ".mkv",
}


def workflow_text_fields(workflow: dict) -> list[dict[str, str]]:
    candidates = []
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        node_type = str(node.get("class_type") or "unknown")
        metadata = node.get("_meta")
        title = (
            str(metadata.get("title") or node_type) if isinstance(metadata, dict) else node_type
        )
        for field_name, value in inputs.items():
            if isinstance(value, str):
                candidates.append(
                    {
                        "node_id": str(node_id),
                        "node_type": node_type,
                        "node_title": title,
                        "field_name": str(field_name),
                    }
                )
    candidates.sort(
        key=lambda field: (
            field["field_name"].lower() not in {"text", "prompt"},
            "cliptextencode" not in field["node_type"].lower(),
            field["node_id"],
            field["field_name"],
        )
    )
    return candidates


def workflow_media_fields(workflow: dict) -> dict[str, list[dict[str, str]]]:
    fields: dict[str, list[dict[str, str]]] = {
        "image": [],
        "video": [],
        "audio": [],
    }
    ordered_nodes = sorted(
        workflow.items(),
        key=lambda item: (
            0 if str(item[0]).isdigit() else 1,
            int(item[0]) if str(item[0]).isdigit() else str(item[0]),
        ),
    )
    for node_id, node in ordered_nodes:
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            continue
        node_type = str(node.get("class_type") or "").casefold()
        inputs = node["inputs"]
        if "loadimage" in node_type:
            kind, candidates = "image", ("image", "filename", "file")
        elif "loadvideo" in node_type:
            kind, candidates = "video", ("video", "filename", "file", "video_path")
        elif "loadaudio" in node_type:
            kind, candidates = "audio", ("audio", "filename", "file", "audio_path")
        else:
            continue
        field_name = next(
            (field for field in candidates if isinstance(inputs.get(field), str)),
            None,
        )
        if field_name:
            fields[kind].append(
                {"node_id": str(node_id), "field_name": field_name}
            )
    return fields


def workflow_parameter_node_info(
    workflow: dict,
    aspect_ratio: str,
    megapixels: float,
    *,
    seed: int | None = None,
    steps: int | None = None,
    refine_steps: int | None = None,
    duration_seconds: float | None = None,
    fps: int = 24,
) -> list[dict[str, str]]:
    """Map recognized generation settings to conventional workflow input names."""
    match = re.match(r"^\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)", aspect_ratio)
    if not match:
        raise RunningHubError("无法从画幅比例计算 RunningHub 工作流尺寸")
    width_ratio, height_ratio = map(float, match.groups())
    if width_ratio <= 0 or height_ratio <= 0 or not math.isfinite(megapixels):
        raise RunningHubError("RunningHub 工作流尺寸参数无效")
    ratio = width_ratio / height_ratio
    width = max(64, round(math.sqrt(megapixels * 1_000_000 * ratio) / 2) * 2)
    height = max(64, round((width / ratio) / 2) * 2)
    ratio_value = f"{width_ratio:g}:{height_ratio:g}"
    values: dict[str, str | None] = {
        "width": str(width),
        "height": str(height),
        "aspect_ratio": ratio_value,
        "aspect": ratio_value,
        "ratio": ratio_value,
        "resolution": f"{width}x{height}",
        "megapixels": f"{megapixels:g}",
        "seed": str(seed) if seed is not None else None,
        "noise_seed": str(seed) if seed is not None else None,
        "steps": str(steps) if steps is not None else None,
        "refine_steps": str(refine_steps) if refine_steps is not None else None,
        "duration": f"{duration_seconds:g}" if duration_seconds is not None else None,
        "duration_seconds": (
            f"{duration_seconds:g}" if duration_seconds is not None else None
        ),
        "seconds": f"{duration_seconds:g}" if duration_seconds is not None else None,
        "fps": str(fps) if duration_seconds is not None else None,
        "frame_rate": str(fps) if duration_seconds is not None else None,
        "frames": (
            str(max(1, round(duration_seconds * fps)))
            if duration_seconds is not None
            else None
        ),
        "num_frames": (
            str(max(1, round(duration_seconds * fps)))
            if duration_seconds is not None
            else None
        ),
    }
    mapped = []
    for node_id, node in workflow.items():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict):
            continue
        for field_name, current_value in inputs.items():
            normalized_field = str(field_name).casefold()
            value = values.get(normalized_field)
            if (
                normalized_field in {"aspect", "ratio"}
                and isinstance(current_value, (int, float))
                and not isinstance(current_value, bool)
            ):
                value = f"{ratio:g}"
            if value is not None and not isinstance(current_value, (list, dict)):
                mapped.append(
                    {
                        "nodeId": str(node_id),
                        "fieldName": str(field_name),
                        "fieldValue": value,
                    }
                )
    return mapped


class RunningHubError(RuntimeError):
    """A safe, user-displayable RunningHub API or output error."""


class RunningHubClient:
    def __init__(
        self,
        api_key: str,
        *,
        http_client: httpx.Client | None = None,
        poll_interval: float = 3.0,
        task_timeout: float = 900.0,
    ):
        self.api_key = api_key
        self.http = http_client or httpx.Client(timeout=30.0, follow_redirects=False)
        self._owns_client = http_client is None
        self.poll_interval = poll_interval
        self.task_timeout = task_timeout

    def close(self) -> None:
        if self._owns_client:
            self.http.close()

    def _post(self, path: str, payload: dict) -> dict:
        try:
            response = self.http.post(
                f"{BASE_URL}{path}",
                json={**payload, "apiKey": self.api_key},
                headers={"Host": "www.runninghub.ai"},
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as exc:
            raise RunningHubError(
                f"RunningHub API returned HTTP {exc.response.status_code}"
            ) from exc
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise RunningHubError("无法连接 RunningHub API 或响应格式无效") from exc
        if not isinstance(body, dict):
            raise RunningHubError("RunningHub API 返回的数据格式无效")
        return body

    def _data(self, body: dict, operation: str, allowed_codes: tuple[int, ...] = (0,)):
        code = body.get("code", 0)
        if code not in allowed_codes:
            message = str(body.get("msg") or body.get("message") or "请求失败")
            if self.api_key:
                message = message.replace(self.api_key, "[已隐藏]")
            raise RunningHubError(f"RunningHub {operation}失败（{code}）：{message[:250]}")
        return body.get("data")

    def get_workflow_text_fields(self, workflow_id: str) -> list[dict[str, str]]:
        return workflow_text_fields(self.get_workflow(workflow_id))

    def get_workflow(self, workflow_id: str) -> dict:
        """Return the API-format node graph for compatibility checks and input mapping."""
        body = self._post(
            "/api/openapi/getJsonApiFormat",
            {"workflowId": workflow_id},
        )
        data = self._data(body, "读取工作流")
        workflow = data.get("prompt") if isinstance(data, dict) else None
        if isinstance(workflow, str):
            try:
                workflow = json.loads(workflow)
            except json.JSONDecodeError as exc:
                raise RunningHubError("RunningHub 工作流 JSON 格式无效") from exc
        if not isinstance(workflow, dict):
            raise RunningHubError("RunningHub 未返回可识别的 API 工作流")
        return workflow

    def upload_media(self, path: Path) -> str:
        """Upload a local reference file and return RunningHub's node-ready fileName."""
        suffix = path.suffix.casefold()
        allowed = IMAGE_EXTENSIONS | {".mp3", ".wav", ".flac", ".mp4", ".avi", ".mov", ".mkv"}
        if suffix not in allowed:
            raise RunningHubError("RunningHub 参考素材格式不受支持")
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise RunningHubError("读取 RunningHub 参考素材失败") from exc
        if size <= 0 or size > MAX_INPUT_BYTES:
            raise RunningHubError("RunningHub 参考素材为空或超过 100 MB")
        try:
            with path.open("rb") as source:
                response = self.http.post(
                    "https://www.runninghub.cn/openapi/v2/media/upload/binary",
                    headers={"Authorization": self.api_key},
                    files={
                        "file": (
                            path.name,
                            source,
                            mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                        )
                    },
                    timeout=90.0,
                    follow_redirects=False,
                )
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPStatusError as exc:
            raise RunningHubError(
                f"RunningHub 参考素材上传失败（HTTP {exc.response.status_code}）"
            ) from exc
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise RunningHubError("无法上传 RunningHub 参考素材") from exc
        if not isinstance(body, dict):
            raise RunningHubError("RunningHub 参考素材上传响应格式无效")
        data = self._data(body, "上传参考素材")
        filename = data.get("fileName") if isinstance(data, dict) else None
        if not isinstance(filename, str) or not filename.strip():
            raise RunningHubError("RunningHub 未返回参考素材 fileName")
        normalized = filename.replace("\\", "/")
        if normalized.startswith("/") or any(part == ".." for part in normalized.split("/")):
            raise RunningHubError("RunningHub 返回了无效的参考素材路径")
        return normalized

    def create_task(self, workflow_id: str, node_info_list: list[dict[str, str]]) -> str:
        body = self._post(
            "/task/openapi/create",
            {"workflowId": workflow_id, "nodeInfoList": node_info_list},
        )
        data = self._data(body, "创建任务")
        task_id = data.get("taskId") if isinstance(data, dict) else None
        if not task_id:
            raise RunningHubError("RunningHub 创建任务成功但未返回 taskId")
        return str(task_id)

    def _task_status(self, task_id: str) -> str:
        body = self._post("/task/openapi/status", {"taskId": task_id})
        code = body.get("code", 0)
        if code == 804:
            return "RUNNING"
        data = self._data(body, "查询任务状态")
        if isinstance(data, dict):
            status = data.get("status") or data.get("taskStatus") or data.get("task_status")
        else:
            status = data
        if not isinstance(status, str):
            raise RunningHubError("RunningHub 未返回有效的任务状态")
        return status.upper()

    def wait_for_completion(
        self,
        task_id: str,
        cancel_check: Callable[[], None] | None = None,
        status_callback: Callable[[str, float], None] | None = None,
    ) -> None:
        started_at = time.monotonic()
        deadline = started_at + self.task_timeout
        last_status = None
        last_reported_at = 0.0
        while time.monotonic() < deadline:
            if cancel_check:
                cancel_check()
            status = self._task_status(task_id)
            elapsed = time.monotonic() - started_at
            if status_callback and (
                status != last_status or elapsed - last_reported_at >= 30
            ):
                status_callback(status, elapsed)
                last_status = status
                last_reported_at = elapsed
            if status in {"SUCCESS", "COMPLETED"}:
                return
            if status in {"FAILED", "ERROR", "CANCELLED"}:
                raise RunningHubError(f"RunningHub 任务未成功完成（{status}）")
            if status not in {"QUEUED", "PENDING", "RUNNING", "PROCESSING"}:
                raise RunningHubError(f"RunningHub 返回未知任务状态：{status[:80]}")
            time.sleep(self.poll_interval)
        raise RunningHubError("RunningHub 任务等待超时")

    def get_outputs(self, task_id: str) -> list[dict]:
        body = self._post("/task/openapi/outputs", {"taskId": task_id})
        data = self._data(body, "读取任务结果")
        if isinstance(data, dict):
            data = data.get("outputs") or data.get("files")
        if not isinstance(data, list):
            raise RunningHubError("RunningHub 未返回输出文件列表")
        return [item for item in data if isinstance(item, dict)]

    @staticmethod
    def _validated_output_url(value: str) -> str:
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in ALLOWED_OUTPUT_HOSTS
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise RunningHubError("RunningHub 返回了不受信任的输出文件地址")
        return value

    def _download_output(self, item: dict, output_dir: Path) -> dict[str, str]:
        url = item.get("fileUrl")
        if not isinstance(url, str):
            raise RunningHubError("RunningHub 输出缺少 fileUrl")
        safe_url = self._validated_output_url(url)
        suffix = Path(unquote(urlparse(safe_url).path)).suffix.lower()
        kind = EXTENSION_KIND.get(suffix)
        if kind is None:
            file_type = str(item.get("fileType") or "").lower().lstrip(".")
            suffix = FILE_TYPE_EXTENSION.get(file_type, "")
            kind = EXTENSION_KIND.get(suffix)
        if kind is None:
            raise RunningHubError("RunningHub 返回了不支持的媒体格式")

        try:
            with self.http.stream("GET", safe_url, follow_redirects=False) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if content_type.startswith("image/") and kind != "image":
                    raise RunningHubError("RunningHub 输出文件类型与媒体内容不匹配")
                if content_type.startswith("video/") and kind != "video":
                    raise RunningHubError("RunningHub 输出文件类型与媒体内容不匹配")
                chunks = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_OUTPUT_BYTES:
                        raise RunningHubError("RunningHub 输出文件超过 100 MB 限制")
                    chunks.append(chunk)
        except RunningHubError:
            raise
        except httpx.HTTPStatusError as exc:
            raise RunningHubError(
                f"下载 RunningHub 输出失败（HTTP {exc.response.status_code}）"
            ) from exc
        except httpx.HTTPError as exc:
            raise RunningHubError("无法下载 RunningHub 输出文件") from exc

        output_dir.mkdir(parents=True, exist_ok=True)
        filename = f"runninghub-{uuid.uuid4().hex}{suffix}"
        target = output_dir / filename
        target.write_bytes(b"".join(chunks))
        return {"path": str(target), "filename": filename, "kind": kind}

    def generate(
        self,
        workflow_id: str,
        node_info_list: list[dict[str, str]],
        output_dir: Path,
        cancel_check: Callable[[], None] | None = None,
        status_callback: Callable[[str, float], None] | None = None,
    ) -> dict:
        task_id = self.create_task(workflow_id, node_info_list)
        self.wait_for_completion(
            task_id,
            cancel_check=cancel_check,
            status_callback=status_callback,
        )
        outputs = self.get_outputs(task_id)
        if not outputs:
            raise RunningHubError("RunningHub 任务成功，但没有可用的媒体输出")
        files = [self._download_output(item, output_dir) for item in outputs]
        return {"task_id": task_id, "files": files}
