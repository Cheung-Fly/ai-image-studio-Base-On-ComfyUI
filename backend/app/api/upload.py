"""素材上传接口：图片 / 视频 / 音频，落盘到 ComfyUI input 目录。

供生图参考图与视频生成素材使用。上传后返回文件名，前端/后端在工作流里引用该文件名。
"""
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from ..config import settings
from ..models import User
from ..schemas import UploadOut
from .auth import get_current_user

router = APIRouter(tags=["upload"])

# 允许的媒体类型 -> (扩展名集合, 大小上限字节)
_ALLOWED = {
    "image": ({"png", "jpg", "jpeg", "webp"}, 20 * 1024 * 1024),
    "video": ({"mp4", "mov", "webm", "mkv"}, 500 * 1024 * 1024),
    "audio": ({"mp3", "wav", "flac", "aac", "m4a", "ogg"}, 100 * 1024 * 1024),
}


def _detect_type(filename: str) -> str:
    suffix = Path(filename).suffix.lower().lstrip(".")
    for media_type, (exts, _) in _ALLOWED.items():
        if suffix in exts:
            return media_type
    raise HTTPException(400, f"不支持的文件类型: .{suffix}（支持图片/视频/音频）")


@router.post("/upload", response_model=UploadOut, status_code=201)
async def upload_asset(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
):
    """上传素材，返回文件名（供工作流引用）。需登录。"""
    if not file.filename:
        raise HTTPException(400, "缺少文件名")
    media_type = _detect_type(file.filename)
    _, max_size = _ALLOWED[media_type]

    data = await file.read()
    if not data:
        raise HTTPException(400, "文件内容为空")
    if len(data) > max_size:
        raise HTTPException(413, f"文件过大（{media_type} 上限 {max_size // 1024 // 1024}MB）")

    # 落盘到 ComfyUI input 目录，文件名加随机前缀防冲突、防覆盖
    out_dir: Path = settings.comfyui_input_path
    out_dir.mkdir(parents=True, exist_ok=True)

    suffix = Path(file.filename).suffix.lower()
    safe_stem = Path(file.filename).stem
    # 安全化文件名：仅保留字母数字下划线连字符与中文
    import re

    safe_stem = re.sub(r"[^\w\u4e00-\u9fff-]", "_", safe_stem) or "asset"
    stored_name = f"{safe_stem}_{uuid.uuid4().hex[:8]}{suffix}"
    target = out_dir / stored_name
    target.write_bytes(data)

    return UploadOut(filename=stored_name, media_type=media_type, size=len(data))
