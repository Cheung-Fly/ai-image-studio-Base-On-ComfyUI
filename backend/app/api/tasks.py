"""任务查询接口。"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import GenerationTask, User
from ..schemas import TaskOut
from .auth import get_current_user

router = APIRouter(tags=["tasks"])


@router.get("/tasks", response_model=list[TaskOut])
def list_tasks(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """当前用户的任务列表，支持按 status 过滤与分页（按创建时间倒序）。"""
    query = db.query(GenerationTask).filter(GenerationTask.user_id == user.id)
    if status:
        if status not in {"pending", "processing", "completed", "failed"}:
            raise HTTPException(400, f"非法状态: {status}")
        query = query.filter(GenerationTask.status == status)
    return (
        query.order_by(GenerationTask.id.desc()).offset(offset).limit(min(limit, 200)).all()
    )


@router.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(
    task_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """查询单个任务（仅限本人任务）。"""
    task = (
        db.query(GenerationTask)
        .filter(GenerationTask.id == task_id, GenerationTask.user_id == user.id)
        .first()
    )
    if task is None:
        raise HTTPException(404, "任务不存在")
    return task
