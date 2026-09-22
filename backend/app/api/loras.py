"""LoRA 列表接口：读取 ComfyUI models/loras 目录，返回可用 LoRA 文件名。"""

from fastapi import APIRouter, Depends

from ..config import settings
from ..models import User
from .auth import get_current_user

router = APIRouter(tags=["loras"])

# 支持的 LoRA 文件扩展名
_LORA_EXTS = {".safetensors", ".ckpt", ".pt", ".bin"}


@router.get("/loras", response_model=list[str])
def list_loras(user: User = Depends(get_current_user)):
    """列出 ComfyUI 可用 LoRA 文件名（需登录）。

    未配置 comfyui_loras_dir 时返回空列表。
    """
    path = settings.comfyui_loras_path
    if path is None or not path.exists() or not path.is_dir():
        return []
    result = []
    for f in sorted(path.iterdir()):
        if f.is_file() and f.suffix.lower() in _LORA_EXTS:
            result.append(f.name)
    return result
