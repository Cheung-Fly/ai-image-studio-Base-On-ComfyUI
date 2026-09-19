"""图库接口。"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models import ImageAsset
from ..schemas import ImageOut

router = APIRouter(tags=["images"])


@router.get("/images", response_model=List[ImageOut])
def list_images(limit: int = 50, offset: int = 0, db: Session = Depends(get_db)):
    return (
        db.query(ImageAsset).order_by(ImageAsset.id.desc()).offset(offset).limit(min(limit, 200)).all()
    )


@router.get("/images/{image_id}/file")
def download_image(image_id: int, db: Session = Depends(get_db)):
    """下载图片文件。"""
    img = db.get(ImageAsset, image_id)
    if img is None:
        raise HTTPException(404, "图片不存在")
    path = settings.image_dir / img.filepath
    if not path.exists():
        raise HTTPException(404, "图片文件已丢失")
    return FileResponse(path, media_type="image/png", filename=img.filename)
