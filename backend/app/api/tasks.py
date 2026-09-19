"""任务查询接口。"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import GenerationTask
from ..schemas import TaskOut

router = APIRouter(tags=["tasks"])


@router.get("/tasks", response_model=List[TaskOut])
def list_tasks(
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """任务列表，支持按 status 过滤与分页（按创建时间倒序）。"""
    query = db.query(GenerationTask)
    if status:
        if status not in {"pending", "processing", "completed", "failed"}:
            raise HTTPException(400, f"非法状态: {status}")
        query = query.filter(GenerationTask.status == status)
    return (
        query.order_by(GenerationTask.id.desc()).offset(offset).limit(min(limit, 200)).all()
    )


@router.get("/tasks/{task_id}", response_model=TaskOut)
def get_task(task_id: int, db: Session = Depends(get_db)):
    task = db.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return task
