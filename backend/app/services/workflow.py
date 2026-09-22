"""工作流模板加载与参数注入。

模板为 ComfyUI API 格式 JSON（节点 id 为 key，含 class_type / inputs），
参数以 {{name}} 占位符书写，注入时替换为实际值（int/float/str）。

支持多工作流：workflow_dir 下的每个 *.json 都是一个可选工作流，
调用方通过文件名指定；未指定时使用配置的默认工作流（WORKFLOW_FILE）。
"""
import json
import re
from pathlib import Path
from typing import Any

from ..config import BASE_DIR, settings

_PLACEHOLDER_RE = re.compile(r"^\{\{(\w+)\}\}$")


class WorkflowError(Exception):
    pass


def list_workflows() -> list[str]:
    """列出 workflow_dir 下所有可用工作流文件名（按名称排序）。"""
    path = Path(settings.workflow_dir)
    # workflow_dir 可能是相对 backend/ 的路径，统一解析为绝对路径
    p = path if path.is_absolute() else BASE_DIR / path
    if not p.exists() or not p.is_dir():
        return []
    return sorted(f.name for f in p.glob("*.json"))


def _resolve_workflow_path(workflow_file: str = "") -> Path:
    """根据文件名解析工作流模板路径；未指定时用默认工作流。"""
    if not workflow_file:
        workflow_file = settings.workflow_file
    path = Path(workflow_file)
    if not path.is_absolute():
        # 相对路径：相对于 workflow_dir（可能又是相对 backend/ 的）
        base = Path(settings.workflow_dir)
        base = base if base.is_absolute() else BASE_DIR / base
        path = base / workflow_file
    return path


def load_template(workflow_file: str = "") -> dict[str, Any]:
    path = _resolve_workflow_path(workflow_file)
    # 安全：仅允许 workflow_dir 内的 *.json，防止路径穿越（../ 等）
    base = Path(settings.workflow_dir)
    base = base if base.is_absolute() else BASE_DIR / base
    base = base.resolve()
    resolved = path.resolve()
    if not str(resolved).startswith(str(base)) or resolved.suffix.lower() != ".json":
        raise WorkflowError(f"非法工作流文件名: {workflow_file}")
    if not path.exists():
        raise WorkflowError(f"工作流模板不存在: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"工作流模板 JSON 解析失败: {exc}") from exc
    if not isinstance(data, dict) or not data:
        raise WorkflowError("工作流模板为空或格式不正确")
    return data


def inject_params(workflow: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """把 {{name}} 占位符替换为 params 中的实际值，返回新字典。"""

    def _convert(value: Any) -> Any:
        # 只有字符串才可能是占位符；list/int/float 等原样保留
        if not isinstance(value, str):
            return value
        m = _PLACEHOLDER_RE.match(value)
        if not m:
            return value
        name = m.group(1)
        if name not in params:
            raise WorkflowError(f"工作流引用了未提供的参数: {name}")
        return params[name]

    result: dict[str, Any] = {}
    for node_id, node in workflow.items():
        node = dict(node)
        inputs = node.get("inputs", {})
        if not isinstance(inputs, dict):
            raise WorkflowError(f"节点 {node_id} 的 inputs 格式不正确")
        node["inputs"] = {k: _convert(v) for k, v in inputs.items()}
        result[node_id] = node
    return result


def build_workflow(
    prompt: str,
    negative_prompt: str = "",
    aspect_ratio: str = "1:1 (Square)",
    megapixels: float = 1.0,
    seed: int = 0,
    workflow_file: str = "",
    model_preset: str = "",
    steps: int = 8,
    refine_steps: int = 4,
    loras: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """加载模板并注入参数，得到可直接提交给 ComfyUI 的 prompt 对象。

    workflow_file 为空时使用默认工作流（settings.workflow_file）。
    model_preset 为空时使用工作流 JSON 里写死的模型；非空时按预设覆盖模型节点。

    steps：主生成采样步数（KSamplerAdvanced 阶段一，默认 8）。
    refine_steps：放大精修步数（KSampler 阶段二，默认 4）。
    loras：LoRA 列表，每项 {"name": 文件名, "strength": 强度}，串联叠加在模型预设之后。
    """
    params: dict[str, Any] = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "aspect_ratio": aspect_ratio,
        "megapixels": float(megapixels),
        "seed": int(seed),
        "steps": int(steps),
        "refine_steps": int(refine_steps),
    }
    wf = inject_params(load_template(workflow_file), params)
    if model_preset:
        from .model_preset import apply_preset

        wf = apply_preset(wf, model_preset)

    # LoRA 链：串联多个 LoRA，叠加在（可能被预设覆盖的）UNET 之后。
    # 生图区两个采样器（13 KSampler、21 KSamplerAdvanced）都引用 UNET，需统一改接 Lora 链尾。
    _apply_image_loras(wf, loras or [])
    return wf


# 生图工作流中引用 UNET 模型输出的节点（这些节点的 model 输入需改接 Lora 链尾）
_IMAGE_MODEL_CONSUMERS = ["13", "21"]
_IMAGE_UNET_NODE = "26"
_IMAGE_LORA_TEMPLATE_NODE = "27"
_IMAGE_LORA_DYNAMIC_START = 900
_IMAGE_MAX_LORAS = 5


def _apply_image_loras(wf: dict[str, Any], lora_list: list[dict[str, Any]]) -> None:
    """为生图工作流串联 LoRA 节点，并把所有模型消费节点的 model 输入改接到链尾。

    - lora_list 为空：删除 Lora 模板节点，所有消费节点 model 直接接 UNET(26)。
    - 非空：复制 Lora 模板生成 N 个节点串联 26 -> lora_0 -> lora_1 -> ...，
      所有消费节点 model 接最后一个 Lora。
    """
    lora_tpl = dict(wf[_IMAGE_LORA_TEMPLATE_NODE]) if _IMAGE_LORA_TEMPLATE_NODE in wf else None
    # 无论是否有 Lora，都先移除模板节点（稍后按需重建）
    wf.pop(_IMAGE_LORA_TEMPLATE_NODE, None)

    if not lora_list:
        # 无 Lora：所有消费节点 model 直接接 UNET
        for nid in _IMAGE_MODEL_CONSUMERS:
            if nid in wf:
                wf[nid]["inputs"]["model"] = [_IMAGE_UNET_NODE, 0]
        return

    if lora_tpl is None:
        raise WorkflowError("模板缺少 LoraLoaderModelOnly 节点，无法生成 LoRA 节点")

    prev = _IMAGE_UNET_NODE
    for i, item in enumerate(lora_list):
        name = str(item.get("name", ""))
        strength = float(item.get("strength", 0.7))
        if not name:
            raise WorkflowError("LoRA 文件名为空")
        nid = str(_IMAGE_LORA_DYNAMIC_START + i)
        node = {
            "class_type": lora_tpl.get("class_type"),
            "inputs": dict(lora_tpl.get("inputs", {})),
            "_meta": dict(lora_tpl.get("_meta", {})),
        }
        node["inputs"]["lora_name"] = name
        node["inputs"]["strength_model"] = strength
        node["inputs"]["model"] = [prev, 0]
        wf[nid] = node
        prev = nid

    # 所有消费节点 model 接链尾
    for nid in _IMAGE_MODEL_CONSUMERS:
        if nid in wf:
            wf[nid]["inputs"]["model"] = [prev, 0]
