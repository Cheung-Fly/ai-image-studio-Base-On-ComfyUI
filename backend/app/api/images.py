"""图库接口。"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models import GenerationTask, ImageAsset, User
from ..schemas import ImageOut
from .auth import get_current_user

router = APIRouter(tags=["images"])


@router.get("/images", response_model=List[ImageOut])
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
    """下载图片文件（仅限本人图片）。"""
    img = (
        db.query(ImageAsset)
        .join(GenerationTask, ImageAsset.task_id == GenerationTask.id)
        .filter(ImageAsset.id == image_id, GenerationTask.user_id == user.id)
        .first()
    )
    if img is None:
        raise HTTPException(404, "图片不存在")
    path = settings.image_dir / img.filepath
    if not path.exists():
        raise HTTPException(404, "图片文件已丢失")
    return FileResponse(path, media_type="image/png", filename=img.filename)
