"""多 Agent 流水线接口：提交一句话生图请求。"""

import time

from celery.result import AsyncResult
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from redis import Redis
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models import CustomModelConfig, RunningHubConfig, User
from ..schemas import (
    AgentConfirmationRequest,
    AgentGenerateRequest,
    AgentMemoryOut,
    AgentMemoryUpdate,
)
from ..services.agent_memory import clear_preferences, get_preferences, update_preferences
from ..throttle import check_and_increment_chat_quota, check_rate_limit
from ..workers.celery_app import celery_app
from ..workers.tasks import run_agent_pipeline, run_mcp_agent_task
from .auth import get_current_user

router = APIRouter(tags=["agent"])


@router.post("/agent/generate", status_code=202)
def agent_generate(
    req: AgentGenerateRequest,
    user: User = Depends(get_current_user),
):
    """兼容旧版固定流水线 API；前端创作流程使用 MCP Agent。"""
    result = run_agent_pipeline.delay(req.request, user.id)
    return {"task_id": result.id, "request": req.request}


@router.post("/agent/mcp", status_code=202)
def agent_mcp(
    req: AgentGenerateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """提交对话式 ComfyUI Agent 请求，支持图片/视频生成与有确认的控制操作。"""
    check_rate_limit(user)

    if req.provider == "custom":
        config = (
            db.query(CustomModelConfig)
            .filter(
                CustomModelConfig.id == req.custom_config_id,
                CustomModelConfig.user_id == user.id,
            )
            .first()
        )
        if config is None:
            raise HTTPException(400, "自定义模型配置无效")

    if req.generation_backend == "runninghub":
        config = db.get(RunningHubConfig, 1)
        if (
            config is None
            or not config.api_key_encrypted
            or not config.workflow_id
            or not config.prompt_node_id
            or not config.prompt_field
        ):
            raise HTTPException(400, "请先在 Agent 的 RunningHub 设置中完成工作流配置")

    check_and_increment_chat_quota(user, db)
    result = run_mcp_agent_task.delay(
        req.request,
        req.provider,
        user.id,
        req.custom_config_id,
        [message.model_dump() for message in req.history],
        req.use_rag,
        req.quality_review_enabled,
        generation_backend=req.generation_backend,
    )
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


@router.get("/agent/files")
def list_agent_files(user: User = Depends(get_current_user)):
    """列出 Agent 已生成的媒体，用于恢复页面上的创作结果库。"""
    directory = (settings.image_dir / "mcp").resolve()
    if not directory.is_dir():
        return []
    supported = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".mov", ".webm", ".mkv"}
    files = []
    for path in directory.iterdir():
        if path.is_file() and path.suffix.lower() in supported:
            stat = path.stat()
            files.append(
                {
                    "filename": path.name,
                    "kind": (
                        "video"
                        if path.suffix.lower() in {".mp4", ".mov", ".webm", ".mkv"}
                        else "image"
                    ),
                    "created_at": stat.st_mtime,
                }
            )
    return sorted(files, key=lambda item: item["created_at"], reverse=True)


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
