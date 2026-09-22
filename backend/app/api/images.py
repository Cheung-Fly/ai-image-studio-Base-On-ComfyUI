"""图库接口。"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models import GenerationTask, ImageAsset, User
from ..schemas import ImageOut
from .auth import get_current_user

router = APIRouter(tags=["images"])


@router.get("/images", response_model=list[ImageOut])
def list_images(
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """当前用户的图库列表（通过任务归属过滤）。"""
    return (
        db.query(ImageAsset)
        .join(GenerationTask, ImageAsset.task_id == GenerationTask.id)
        .filter(GenerationTask.user_id == user.id)
        .order_by(ImageAsset.id.desc())
        .offset(offset)
        .limit(min(limit, 200))
        .all()
    )


@router.get("/images/{image_id}/file")
def download_image(
    image_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """下载资产文件（图片 / 视频，仅限本人）。"""
    img = (
        db.query(ImageAsset)
        .join(GenerationTask, ImageAsset.task_id == GenerationTask.id)
        .filter(ImageAsset.id == image_id, GenerationTask.user_id == user.id)
        .first()
    )
    if img is None:
        raise HTTPException(404, "资产不存在")
    path = settings.image_dir / img.filepath
    if not path.exists():
        raise HTTPException(404, "资产文件已丢失")

    media_type = "video/mp4" if img.media_type == "video" else "image/png"
    return FileResponse(path, media_type=media_type, filename=img.filename)
