"""个人资料接口（个人主页）。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import GenerationTask, ImageAsset, User
from ..schemas import UserProfileOut
from .auth import get_current_user

router = APIRouter(tags=["me"])


@router.get("/me", response_model=UserProfileOut)
def get_me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """返回当前用户资料 + 资产统计（需登录）。"""
    task_count = db.query(GenerationTask).filter(GenerationTask.user_id == user.id).count()
    image_count = (
        db.query(ImageAsset)
        .join(GenerationTask, ImageAsset.task_id == GenerationTask.id)
        .filter(GenerationTask.user_id == user.id)
        .count()
    )
    return UserProfileOut(
        username=user.username,
        created_at=user.created_at,
        task_count=task_count,
        image_count=image_count,
    )
