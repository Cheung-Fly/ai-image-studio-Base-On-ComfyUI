"""多 Agent 流水线接口：提交一句话生图请求。"""
from celery.result import AsyncResult
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..schemas import AgentGenerateRequest
from ..workers.celery_app import celery_app
from ..workers.tasks import run_agent_pipeline, run_mcp_agent_task
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


@router.post("/agent/mcp", status_code=202)
def agent_mcp(
    req: AgentGenerateRequest,
    user: User = Depends(get_current_user),
):
    """自然语言控制 ComfyUI：Agent 通过 MCP 工具自主决策（查询/生图/中断/释放显存等）。"""
    result = run_mcp_agent_task.delay(req.request, "")
    return {"task_id": result.id, "request": req.request}


@router.get("/agent/result/{task_id}")
def agent_result(task_id: str, user: User = Depends(get_current_user)):
    """查询 Agent 任务结果（Celery 任务状态 + 返回值）。"""
    res = AsyncResult(task_id, app=celery_app)
    return {
        "task_id": task_id,
        "state": res.state,
        "result": res.result if res.successful() else None,
    }

