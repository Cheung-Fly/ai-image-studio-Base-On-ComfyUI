"""提交生成任务。"""
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import GenerationTask, User
from ..schemas import GenerateRequest, TaskOut
from ..services.task_reliability import TaskDispatchError, dispatch_generation
from .auth import get_current_user

router = APIRouter(tags=["generate"])


@router.post("/generate", response_model=TaskOut, status_code=202)
def generate(
    req: GenerateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """提交一个文生图任务，立即返回 task_id；实际生成由 Celery worker 异步执行。"""
    task = GenerationTask(
        user_id=user.id,
        prompt=req.prompt,
        negative_prompt=req.negative_prompt,
        workflow=req.workflow,
        model_preset=req.model_preset,
        aspect_ratio=req.aspect_ratio,
        megapixels=req.megapixels,
        seed=req.seed,
        steps=req.steps,
        refine_steps=req.refine_steps,
        loras=json.dumps([ref.model_dump() for ref in req.loras]),
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    try:
        dispatch_generation(db, task)
    except TaskDispatchError as exc:
        raise HTTPException(503, str(exc)) from exc
    # eager（冒烟测试）模式下任务已同步完成，重新读取拿到最新状态；
    # 真实异步模式下任务仍为 pending，此处读到的状态不变
    db.refresh(task)
    return task
