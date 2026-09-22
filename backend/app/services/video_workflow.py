"""视频工作流构建：MiniMax H3 Ref2VA（参考图/视频/音频 -> 视频+原生音频）。

基于 workflows/MiniMaxH3_ref2va_video.json 骨架，动态完成：
1. 占位符注入（prompt / duration / seed / aspect_ratio / megapixels）；
2. 素材节点动态生成与接线（ref_images / ref_videos / ref_audios）：
   - 图片：最多 9 张（LoadImage）
   - 视频：最多 3 个（VHS_LoadVideo）
   - 音频：最多 3 个（LoadAudio）
   - 素材可完全不传（纯文生视频）；传多少就动态生成多少节点并接线。
3. 顺序启用：视频/音频槽默认不接；音频依赖「有图或有视频」（前端按此解锁）。

实现：模板中保留各类型的首个素材节点作为「复制模板」，按需动态复制并分配新 ID，
移除模板中预置的多余素材节点，避免残留孤立节点。
"""
import json
from typing import Any

from .workflow import WorkflowError, inject_params

# 视频工作流模板文件名（相对 workflow_dir）
VIDEO_WORKFLOW_FILE = "MiniMaxH3_ref2va_video.json"

# 模板中预置的素材节点（作为复制模板 + 需清理的残留）
_IMAGE_TEMPLATE_NODE = "137"          # LoadImage 复制模板
_IMAGE_RESIDUAL_NODES = ["137", "139", "147", "165", "166", "167", "177"]  # 7 个预置 LoadImage
_VIDEO_TEMPLATE_NODE = "170"          # VHS_LoadVideo 复制模板
_AUDIO_TEMPLATE_NODE = "171"          # LoadAudio 复制模板
_REF_NODE_ID = "429"                  # MiniMaxH3ReferenceToVideo

# Lora 相关
_LORA_TEMPLATE_NODE = "144"           # LoraLoaderModelOnly 复制模板
_ATTENTION_NODE_ID = "183"            # ModelAttentionBackend（模型链终点）
_UNET_NODE_ID = "127"                 # UNETLoader（Lora 链起点）

# 动态生成节点的起始 ID（避开模板已有 ID，模板最大约 431）
_DYNAMIC_ID_START = 900

# 各类型上限
MAX_IMAGES = 9
MAX_VIDEOS = 3
MAX_AUDIOS = 3
MAX_LORAS = 5


class VideoWorkflowError(WorkflowError):
    pass


def _load_video_template() -> dict[str, Any]:
    """加载视频工作流骨架（相对 workflow_dir）。"""
    from pathlib import Path

    from ..config import BASE_DIR, settings

    base = Path(settings.workflow_dir)
    base = base if base.is_absolute() else BASE_DIR / base
    path = base / VIDEO_WORKFLOW_FILE
    if not path.exists():
        raise VideoWorkflowError(f"视频工作流模板不存在: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise VideoWorkflowError(f"视频工作流模板 JSON 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise VideoWorkflowError("视频工作流模板格式不正确")
    return data


def build_video_workflow(
    prompt: str,
    duration: float = 4.0,
    aspect_ratio: str = "16:9 (Widescreen)",
    megapixels: float = 1.0,
    seed: int = 0,
    steps: int = 20,
    loras: list[dict[str, Any]] | None = None,
    ref_images: list[str] | None = None,
    ref_videos: list[str] | None = None,
    ref_audios: list[str] | None = None,
) -> dict[str, Any]:
    """构建 MiniMax H3 Ref2VA 视频工作流。

    ref_images / ref_videos / ref_audios 均为「已上传到 ComfyUI input 目录的文件名」列表。
    - 素材可全部为空（纯文生视频）。
    - 图片 ≤9、视频 ≤3、音频 ≤3；传多少动态生成多少节点并接线。

    steps：基本调度器（BasicScheduler, 节点 173）的采样步数。
    loras：LoRA 列表，每项 {"name": 文件名, "strength": 强度}。
           - 空列表（默认）→ 模型直连 UNET，不加载 LoRA。
           - 非空 → 按顺序串联多个 LoRA：UNET → lora_0 → lora_1 → ...
    """
    images = ref_images or []
    videos = ref_videos or []
    audios = ref_audios or []
    lora_list = loras or []

    if len(images) > MAX_IMAGES:
        raise VideoWorkflowError(f"参考图最多 {MAX_IMAGES} 张，收到 {len(images)}")
    if len(videos) > MAX_VIDEOS:
        raise VideoWorkflowError(f"参考视频最多 {MAX_VIDEOS} 个，收到 {len(videos)}")
    if len(audios) > MAX_AUDIOS:
        raise VideoWorkflowError(f"参考音频最多 {MAX_AUDIOS} 个，收到 {len(audios)}")
    if len(lora_list) > MAX_LORAS:
        raise VideoWorkflowError(f"LoRA 最多 {MAX_LORAS} 个，收到 {len(lora_list)}")

    wf = _load_video_template()

    # 1) 占位符注入
    # seed 处理：RandomNoise 节点的 noise_seed 要求 >= 0（无 -1 随机语义）。
    # 前端传 -1 表示随机，这里转成 0 ~ 2^53 范围内的随机正整数。
    if seed < 0:
        import random

        seed = random.randint(0, (1 << 53) - 1)
    params: dict[str, Any] = {
        "prompt": prompt,
        "duration": float(duration),
        "seed": int(seed),
        "aspect_ratio": aspect_ratio,
        "megapixels": float(megapixels),
        "steps": int(steps),
    }
    wf = inject_params(wf, params)

    # 2) 移除模板中预置的素材节点（复制模板节点先留作复制源，稍后统一清理）
    #    注意：复制模板节点的原始 inputs 结构先保存下来
    image_tpl = dict(wf[_IMAGE_TEMPLATE_NODE]) if _IMAGE_TEMPLATE_NODE in wf else None
    video_tpl = dict(wf[_VIDEO_TEMPLATE_NODE]) if _VIDEO_TEMPLATE_NODE in wf else None
    audio_tpl = dict(wf[_AUDIO_TEMPLATE_NODE]) if _AUDIO_TEMPLATE_NODE in wf else None
    lora_tpl = dict(wf[_LORA_TEMPLATE_NODE]) if _LORA_TEMPLATE_NODE in wf else None

    # 移除所有预置素材节点（图片残留 + 视频 + 音频）
    for nid in _IMAGE_RESIDUAL_NODES:
        wf.pop(nid, None)
    wf.pop(_VIDEO_TEMPLATE_NODE, None)
    wf.pop(_AUDIO_TEMPLATE_NODE, None)
    # 移除模板里的 Lora 模板节点（稍后按需重建并串联）
    wf.pop(_LORA_TEMPLATE_NODE, None)

    # 3) 按素材数量动态生成节点并记录接线
    next_id = _DYNAMIC_ID_START
    image_node_ids: list[str] = []
    video_node_ids: list[str] = []
    audio_node_ids: list[str] = []

    for name in images:
        nid = str(next_id)
        node = _clone_node(image_tpl, image=name) if image_tpl else None
        if node is None:
            raise VideoWorkflowError("模板缺少 LoadImage 节点，无法生成参考图节点")
        wf[nid] = node
        image_node_ids.append(nid)
        next_id += 1

    for name in videos:
        nid = str(next_id)
        node = _clone_node(video_tpl, video=name) if video_tpl else None
        if node is None:
            raise VideoWorkflowError("模板缺少 VHS_LoadVideo 节点，无法生成参考视频节点")
        wf[nid] = node
        video_node_ids.append(nid)
        next_id += 1

    for name in audios:
        nid = str(next_id)
        node = _clone_node(audio_tpl, audio=name) if audio_tpl else None
        if node is None:
            raise VideoWorkflowError("模板缺少 LoadAudio 节点，无法生成参考音频节点")
        wf[nid] = node
        audio_node_ids.append(nid)
        next_id += 1

    # 4) 动态接线 ref 节点（429）：先清空所有 ref_* 接线，再按需接
    ref_inputs = dict(wf[_REF_NODE_ID]["inputs"])
    for key in list(ref_inputs.keys()):
        prefixes = ("ref_images.", "ref_videos.", "ref_audios.", "ref_video_audios.")
        if key.startswith(prefixes):
            del ref_inputs[key]

    for i, nid in enumerate(image_node_ids):
        ref_inputs[f"ref_images.ref_image_{i}"] = [nid, 0]
    for i, nid in enumerate(video_node_ids):
        ref_inputs[f"ref_videos.ref_video_{i}"] = [nid, 0]
    for i, nid in enumerate(audio_node_ids):
        ref_inputs[f"ref_audios.ref_audio_{i}"] = [nid, 0]

    wf[_REF_NODE_ID]["inputs"] = ref_inputs

    # 5) LoRA 链：串联多个 Lora，并接到模型链终点（183 ModelAttentionBackend）
    _build_lora_chain(wf, lora_tpl, lora_list, next_id)

    return wf


def _build_lora_chain(
    wf: dict[str, Any],
    lora_tpl: dict[str, Any] | None,
    lora_list: list[dict[str, Any]],
    next_id: int,
) -> None:
    """按 lora_list 动态生成串联的 Lora 节点，并接线到 183（模型链终点）。

    - lora_list 为空：183.model 直接接 127（UNET），不加载任何 LoRA。
    - 非空：生成 N 个 Lora 节点，串联 127 -> lora_0 -> lora_1 -> ...，183.model 接最后一个。
    """
    if not lora_list:
        # 无 LoRA：183 直接接 UNET
        wf[_ATTENTION_NODE_ID]["inputs"]["model"] = [_UNET_NODE_ID, 0]
        return

    if lora_tpl is None:
        raise VideoWorkflowError("模板缺少 LoraLoaderModelOnly 节点，无法生成 LoRA 节点")

    # 串联起点是 UNET
    prev = _UNET_NODE_ID
    for i, item in enumerate(lora_list):
        name = str(item.get("name", ""))
        strength = float(item.get("strength", 0.7))
        if not name:
            raise VideoWorkflowError("LoRA 文件名为空")
        nid = str(next_id + i)
        node = _clone_node(lora_tpl, lora_name=name, strength_model=strength, model=[prev, 0])
        wf[nid] = node
        prev = nid

    # 最后一个 Lora 输出接到 183
    wf[_ATTENTION_NODE_ID]["inputs"]["model"] = [prev, 0]


def _clone_node(template: dict[str, Any] | None, **override_inputs: Any) -> dict[str, Any]:
    """复制一个素材节点模板，覆盖指定 input 字段，返回新节点。"""
    node = dict(template)
    node = {
        "class_type": node.get("class_type"),
        "inputs": dict(node.get("inputs", {})),
        "_meta": dict(node.get("_meta", {})),
    }
    for k, v in override_inputs.items():
        node["inputs"][k] = v
    return node
