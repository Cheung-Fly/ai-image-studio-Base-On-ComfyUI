"""多 Agent 流水线接口：提交一句话生图请求。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..schemas import AgentGenerateRequest
from ..workers.tasks import run_agent_pipeline
from .auth import get_current_user

router = APIRouter(tags=["agent"])


@router.post("/agent/generate", status_code=202)
def agent_generate(
    req: AgentGenerateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """提交一句话生图请求，返回 Celery 任务 ID（流水线异步执行）。"""
    result = run_agent_pipeline.delay(req.request, user.id)
    return {"task_id": result.id, "request": req.request}
