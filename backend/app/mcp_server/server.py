"""ComfyUI MCP Server：把 ComfyUI 的生图与控制能力暴露为 MCP 工具（stdio 传输）。

独立运行：``python -m app.mcp_server``（在 backend/ 目录下），
也可被任何 MCP 客户端（Claude Desktop、Copilot、本项目 Agent）接入。
不依赖数据库 / Celery，只依赖 ComfyUI HTTP API。
"""
import json
import random
import re
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..config import settings
from ..services.comfy_client import ComfyClient, ComfyError
from ..services.workflow import WorkflowError, build_workflow, list_workflows, load_template

mcp = FastMCP("comfyui")

# 可查询模型的节点与输入名
_MODEL_KINDS: dict[str, tuple[str, str]] = {
    "unet": ("UNETLoader", "unet_name"),
    "checkpoint": ("CheckpointLoaderSimple", "ckpt_name"),
    "vae": ("VAELoader", "vae_name"),
    "clip": ("CLIPLoader", "clip_name"),
    "lora": ("LoraLoader", "lora_name"),
}


def _client() -> ComfyClient:
    return ComfyClient()


def _out_dir() -> Path:
    d = settings.image_dir / "mcp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _err(exc: Exception) -> str:
    return json.dumps({"ok": False, "error": str(exc)[:500]}, ensure_ascii=False)


def _ok(**data: Any) -> str:
    return json.dumps({"ok": True, **data}, ensure_ascii=False)


def _run_and_save(workflow: dict[str, Any]) -> dict[str, Any]:
    client = _client()
    prompt_id = client.submit_prompt(workflow)
    media = client.wait_for_media(prompt_id)
    files = []
    for meta in media:
        data = client.download_image(
            meta["filename"], meta.get("subfolder", ""), meta.get("type", "output")
        )
        target = _out_dir() / f"{uuid.uuid4().hex[:8]}-{Path(meta['filename']).name}"
        target.write_bytes(data)
        files.append(
            {"path": str(target), "filename": target.name, "kind": meta.get("kind", "image")}
        )
    return {"prompt_id": prompt_id, "files": files}


@mcp.tool()
def comfy_status() -> str:
    """查看 ComfyUI 状态：版本、GPU/显存、队列中执行与等待的任务数。"""
    try:
        c = _client()
        stats = c.system_stats()
        queue = c.queue_info()
        return _ok(
            system=stats.get("system", {}),
            devices=stats.get("devices", []),
            running=len(queue.get("queue_running", [])),
            pending=len(queue.get("queue_pending", [])),
        )
    except ComfyError as exc:
        return _err(exc)


@mcp.tool()
def comfy_list_models(kind: str = "unet") -> str:
    """列出 ComfyUI 可用模型文件。kind 可选：unet / checkpoint / vae / clip / lora。"""
    spec = _MODEL_KINDS.get(kind)
    if spec is None:
        return _err(ValueError(f"kind 必须是 {sorted(_MODEL_KINDS)}"))
    try:
        return _ok(kind=kind, models=_client().list_node_options(*spec))
    except ComfyError as exc:
        return _err(exc)


@mcp.tool()
def comfy_list_workflows() -> str:
    """列出本项目内置的工作流模板文件名（可传给 comfy_generate_image 的 workflow_file）。"""
    return _ok(workflows=list_workflows(), default=settings.workflow_file)


@mcp.tool()
def comfy_workflow_details(workflow_file: str) -> str:
    """查看指定内置工作流包含的节点 ID 和节点类型，不返回庞大的完整 JSON。"""
    if workflow_file not in list_workflows():
        return _err(ValueError("工作流必须是 comfy_list_workflows 返回的内置文件名"))
    try:
        workflow = load_template(workflow_file)
        nodes = [
            {"id": node_id, "type": node.get("class_type", "unknown")}
            for node_id, node in workflow.items()
            if isinstance(node, dict)
        ]
        return _ok(filename=workflow_file, node_count=len(nodes), nodes=nodes)
    except WorkflowError as exc:
        return _err(exc)


@mcp.tool()
def comfy_describe_node(node_class: str) -> str:
    """查询 ComfyUI 节点的描述、输入项与下拉选项，便于检查自定义工作流节点。"""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", node_class):
        return _err(ValueError("非法节点类型名"))
    try:
        info = _client().object_info(node_class).get(node_class)
        if not info:
            return _err(ValueError(f"未找到节点类型: {node_class}"))
        inputs: dict[str, Any] = {}
        for group in ("required", "optional"):
            for name, spec in (info.get("input") or {}).get(group, {}).items():
                if not isinstance(spec, (list, tuple)) or not spec:
                    inputs[name] = {"type": str(spec)}
                    continue
                kind = spec[0]
                item: dict[str, Any] = {"type": kind if isinstance(kind, str) else "enum"}
                if isinstance(kind, list):
                    item["options"] = kind[:100]
                if len(spec) > 1 and isinstance(spec[1], dict):
                    item["config"] = spec[1]
                inputs[name] = item
        return _ok(
            node_class=node_class,
            display_name=info.get("display_name", node_class),
            category=info.get("category", ""),
            description=info.get("description", ""),
            inputs=inputs,
        )
    except ComfyError as exc:
        return _err(exc)


@mcp.tool()
def comfy_list_input_files() -> str:
    """列出 ComfyUI 输入目录中的图片、视频和音频素材；上传仍通过平台素材上传接口完成。"""
    directory = settings.comfyui_input_path
    if directory is None or not directory.is_dir():
        return _ok(files=[])
    supported = {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".webm", ".mkv",
                 ".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg"}
    files = [
        {"filename": path.name, "size": path.stat().st_size}
        for path in sorted(directory.iterdir(), key=lambda p: p.name.lower())
        if path.is_file() and path.suffix.lower() in supported
    ]
    return _ok(files=files)


@mcp.tool()
def comfy_delete_input_file(filename: str) -> str:
    """删除 ComfyUI 输入目录中的素材（会要求用户在助手界面确认）。"""
    directory = settings.comfyui_input_path
    if directory is None:
        return _err(ValueError("未配置 ComfyUI 输入目录"))
    root = directory.resolve()
    target = (root / filename).resolve()
    if target.parent != root or target.suffix.lower() not in {
        ".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".webm", ".mkv",
        ".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg",
    }:
        return _err(ValueError("非法素材文件名或类型"))
    try:
        target.unlink()
    except FileNotFoundError:
        return _err(ValueError("素材文件不存在"))
    except OSError as exc:
        return _err(exc)
    return _ok(deleted=filename)


@mcp.tool()
def comfy_generate_image(
    prompt: str,
    negative_prompt: str = "",
    aspect_ratio: str = "1:1 (Square)",
    megapixels: float = 1.0,
    seed: int = -1,
    steps: int = 8,
    refine_steps: int = 4,
    workflow_file: str = "",
    model_preset: str = "",
) -> str:
    """用内置工作流生成图片并保存到本地，返回图片路径。耗时可达数分钟，会阻塞到完成。

    seed=-1 表示随机。prompt 建议使用详细英文描述。
    """
    if not prompt.strip():
        return _err(ValueError("prompt 不能为空"))
    if seed < 0:
        seed = random.randint(0, 2**31 - 1)
    try:
        wf = build_workflow(
            prompt=prompt,
            negative_prompt=negative_prompt,
            aspect_ratio=aspect_ratio,
            megapixels=megapixels,
            seed=seed,
            workflow_file=workflow_file,
            model_preset=model_preset,
            steps=steps,
            refine_steps=refine_steps,
        )
        return _ok(seed=seed, **_run_and_save(wf))
    except (ComfyError, WorkflowError, OSError) as exc:
        return _err(exc)


@mcp.tool()
def comfy_run_workflow(workflow_json: str) -> str:
    """直接提交一份 ComfyUI API 格式的工作流 JSON 并等待完成，返回输出文件路径。"""
    try:
        wf = json.loads(workflow_json)
        if not isinstance(wf, dict) or not wf:
            raise ValueError("workflow_json 必须是非空 JSON 对象")
        return _ok(**_run_and_save(wf))
    except (ComfyError, ValueError, OSError) as exc:
        return _err(exc)


@mcp.tool()
def comfy_queue() -> str:
    """查看队列详情：执行中与排队中的 prompt_id。"""
    try:
        q = _client().queue_info()

        def ids(items: list) -> list:
            return [i[1] for i in items if len(i) > 1]

        return _ok(running=ids(q.get("queue_running", [])), pending=ids(q.get("queue_pending", [])))
    except ComfyError as exc:
        return _err(exc)


@mcp.tool()
def comfy_history(prompt_id: str = "", max_items: int = 5) -> str:
    """查询任务历史。给 prompt_id 查单个任务结果，否则返回最近 max_items 条摘要。"""
    try:
        h = _client().history(prompt_id, max_items)
        summary = {
            pid: {
                "status": (e.get("status") or {}).get("status_str"),
                "outputs": {
                    n: [i.get("filename") for i in (o.get("images", []) + o.get("gifs", []))]
                    for n, o in (e.get("outputs") or {}).items()
                },
            }
            for pid, e in h.items()
        }
        return _ok(history=summary)
    except ComfyError as exc:
        return _err(exc)


@mcp.tool()
def comfy_interrupt() -> str:
    """中断 ComfyUI 当前正在执行的任务。"""
    try:
        _client().interrupt()
        return _ok(message="已发送中断")
    except ComfyError as exc:
        return _err(exc)


@mcp.tool()
def comfy_clear_queue() -> str:
    """清空 ComfyUI 排队中（未开始）的任务（会要求用户在助手界面确认）。"""
    try:
        _client().clear_queue()
        return _ok(message="已清空排队任务")
    except ComfyError as exc:
        return _err(exc)


@mcp.tool()
def comfy_delete_queued(prompt_ids: list[str]) -> str:
    """从等待队列中删除指定 prompt_id 的任务（会要求用户在助手界面确认）。"""
    prompt_ids = list(dict.fromkeys(pid.strip() for pid in prompt_ids if pid.strip()))
    if not prompt_ids:
        return _err(ValueError("至少需要一个有效 prompt_id"))
    if len(prompt_ids) > 50:
        return _err(ValueError("单次最多删除 50 个排队任务"))
    try:
        _client().delete_queued(prompt_ids)
        return _ok(deleted=prompt_ids)
    except ComfyError as exc:
        return _err(exc)


@mcp.tool()
def comfy_free_memory(unload_models: bool = True) -> str:
    """释放 ComfyUI 显存/内存（可选卸载已加载模型），用于与本地 LLM 错峰。"""
    try:
        _client().free_memory(unload_models=unload_models)
        return _ok(message="已请求释放显存")
    except ComfyError as exc:
        return _err(exc)


def main() -> None:
    mcp.run(transport="stdio")
