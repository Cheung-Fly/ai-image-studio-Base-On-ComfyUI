"""工作流模板加载与参数注入。

模板为 ComfyUI API 格式 JSON（节点 id 为 key，含 class_type / inputs），
参数以 {{name}} 占位符书写，注入时替换为实际值（int/float/str）。
"""
import json
import re
from typing import Any, Dict

from ..config import settings

_PLACEHOLDER_RE = re.compile(r"^\{\{(\w+)\}\}$")


class WorkflowError(Exception):
    pass


def load_template() -> Dict[str, Any]:
    path = settings.workflow_path
    if not path.exists():
        raise WorkflowError(f"工作流模板不存在: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"工作流模板 JSON 解析失败: {exc}") from exc
    if not isinstance(data, dict) or not data:
        raise WorkflowError("工作流模板为空或格式不正确")
    return data


def inject_params(workflow: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
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

    result: Dict[str, Any] = {}
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
) -> Dict[str, Any]:
    """加载模板并注入参数，得到可直接提交给 ComfyUI 的 prompt 对象。"""
    params: Dict[str, Any] = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "aspect_ratio": aspect_ratio,
        "megapixels": float(megapixels),
        "seed": int(seed),
    }
    return inject_params(load_template(), params)
