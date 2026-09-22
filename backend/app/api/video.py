"""视频生成接口：提交 MiniMax H3 Ref2VA 任务。"""
import json

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import GenerationTask, User
from ..schemas import GenerateVideoRequest, TaskOut
from ..workers.tasks import run_generation
from .auth import get_current_user

router = APIRouter(tags=["video"])


@router.post("/generate-video", response_model=TaskOut, status_code=202)
def generate_video(
    req: GenerateVideoRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """提交一个视频生成任务，返回 task_id；实际生成由 Celery worker 异步执行（需登录）。"""
    task = GenerationTask(
        user_id=user.id,
        task_type="video",
        prompt=req.prompt,
        negative_prompt="",
        workflow="",  # 视频工作流由 video_workflow.py 固定构建
        model_preset="",
        aspect_ratio=req.aspect_ratio,
        megapixels=req.megapixels,
        seed=req.seed,
        duration=req.duration,
        steps=req.steps,
        loras=json.dumps([ref.model_dump() for ref in req.loras]),
        ref_images=",".join(req.ref_images),
        ref_videos=",".join(req.ref_videos),
        ref_audios=",".join(req.ref_audios),
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    run_generation.delay(task.id)
    db.refresh(task)
    return task
