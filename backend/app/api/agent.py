"""多 Agent 流水线接口：提交一句话生图请求。"""

import time

from celery.result import AsyncResult
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from redis import Redis
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models import User
from ..schemas import (
    AgentConfirmationRequest,
    AgentGenerateRequest,
    AgentMemoryOut,
    AgentMemoryUpdate,
)
from ..services.agent_memory import clear_preferences, get_preferences, update_preferences
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
        "progress": res.info if res.state == "PROGRESS" else None,
        "confirmation": res.info if res.state == "CONFIRMATION_REQUIRED" else None,
        "result": res.result if res.successful() else None,
    }


@router.post("/agent/confirm/{task_id}")
def agent_confirm(
    task_id: str,
    req: AgentConfirmationRequest,
    user: User = Depends(get_current_user),
):
    """确认或拒绝 MCP Agent 正在等待的有影响操作。"""
    result = AsyncResult(task_id, app=celery_app)
    confirmation = result.info if result.state == "CONFIRMATION_REQUIRED" else None
    if not confirmation or req.confirmation_id != confirmation.get("confirmation_id"):
        raise HTTPException(409, "确认请求已失效或任务不在等待确认")
    if confirmation.get("expires_at", 0) <= time.time():
        raise HTTPException(409, "确认请求已超时")

    key = f"aistudio:mcp-agent-confirm:{task_id}:{req.confirmation_id}"
    client = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        stored = client.set(key, "yes" if req.confirmed else "no", ex=360, nx=True)
    finally:
        client.close()
    if not stored:
        raise HTTPException(409, "确认请求已处理")
    return {"task_id": task_id, "confirmed": req.confirmed}


@router.get("/agent/files/{filename}")
def agent_file(filename: str, user: User = Depends(get_current_user)):
    """读取 MCP Agent 生成的本地结果文件，供助手界面预览。"""
    directory = (settings.image_dir / "mcp").resolve()
    path = (directory / filename).resolve()
    if path.parent != directory or not path.is_file():
        raise HTTPException(404, "Agent 生成文件不存在")
    return FileResponse(path)


@router.get("/agent/memory", response_model=AgentMemoryOut)
def agent_memory(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """读取个人助手保存的 ComfyUI 偏好。"""
    return get_preferences(db)


@router.patch("/agent/memory", response_model=AgentMemoryOut)
def update_agent_memory(
    req: AgentMemoryUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """更新个人助手偏好；只修改请求体中明确提供的字段。"""
    try:
        return update_preferences(db, req.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete("/agent/memory", response_model=AgentMemoryOut)
def clear_agent_memory(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """清除个人助手全部偏好记忆。"""
    return clear_preferences(db)
