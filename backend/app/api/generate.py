"""提交生成任务。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import GenerationTask
from ..schemas import GenerateRequest, TaskOut
from ..workers.tasks import run_generation

router = APIRouter(tags=["generate"])


@router.post("/generate", response_model=TaskOut, status_code=202)
def generate(req: GenerateRequest, db: Session = Depends(get_db)):
    """提交一个文生图任务，立即返回 task_id；实际生成由 Celery worker 异步执行。"""
    task = GenerationTask(
        prompt=req.prompt,
        negative_prompt=req.negative_prompt,
        width=req.width,
        height=req.height,
        steps=req.steps,
        cfg=req.cfg,
        seed=req.seed,
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    run_generation.delay(task.id)
    # eager（冒烟测试）模式下任务已同步完成，重新读取拿到最新状态；
    # 真实异步模式下任务仍为 pending，此处读到的状态不变
    db.refresh(task)
    return task
