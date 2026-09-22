"""模型组合预设：加载 / 查询 / 应用到工作流节点。

预设文件 model_presets.json 结构：
    {"presets": [{"name", "display_name", "unet", "clip", "vae"}, ...]}

应用逻辑：按 ComfyUI 节点的 class_type 覆盖模型文件名——
    UNETLoader -> unet，CLIPLoader -> clip，VAELoader -> vae。
不依赖工作流 JSON 里预先写占位符，任意工作流都能套用预设。
"""
import json
from typing import Any

from ..config import settings

# 节点类型 -> 预设字段映射
_NODE_FIELD_MAP = {
    "UNETLoader": ("unet_name", "unet"),
    "CLIPLoader": ("clip_name", "clip"),
    "VAELoader": ("vae_name", "vae"),
}


class ModelPresetError(Exception):
    pass


def _load_raw() -> dict[str, Any]:
    path = settings.model_presets_path
    if not path.exists():
        return {"presets": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ModelPresetError(f"模型预设文件 JSON 解析失败: {exc}") from exc
    if not isinstance(data, dict) or "presets" not in data:
        raise ModelPresetError("模型预设文件缺少 presets 字段")
    return data


def list_presets() -> list[dict[str, Any]]:
    """返回所有预设（含 display_name），供前端下拉渲染。"""
    return [
        {
            "name": p.get("name", ""),
            "display_name": p.get("display_name", p.get("name", "")),
        }
        for p in _load_raw()["presets"]
        if p.get("name")
    ]


def get_preset(name: str) -> dict[str, Any] | None:
    """按 name 查找预设；找不到返回 None。"""
    if not name:
        return None
    for p in _load_raw()["presets"]:
        if p.get("name") == name:
            return p
    return None


def apply_preset(workflow: dict[str, Any], name: str) -> dict[str, Any]:
    """把预设的模型文件名覆盖到工作流节点上，返回新字典。

    name 为空时不做任何覆盖，原样返回。
    """
    preset = get_preset(name)
    if preset is None:
        if name:
            raise ModelPresetError(f"模型预设不存在: {name}")
        return workflow

    result: dict[str, Any] = {}
    for node_id, node in workflow.items():
        node = dict(node)
        class_type = node.get("class_type")
        if class_type in _NODE_FIELD_MAP:
            input_field, preset_field = _NODE_FIELD_MAP[class_type]
            value = preset.get(preset_field)
            if value:  # 预设里提供了该字段才覆盖
                inputs = dict(node.get("inputs", {}))
                inputs[input_field] = value
                node["inputs"] = inputs
        result[node_id] = node
    return result
