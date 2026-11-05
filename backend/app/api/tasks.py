"""任务查询、事件、重试与取消接口。"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import GenerationTask, TaskEvent, TaskStatus, User
from ..schemas import TaskActionOut, TaskEventOut, TaskOut
from ..services.comfy_client import ComfyClient
from ..services.task_reliability import (
    MAX_MANUAL_RETRIES,
    RETRYABLE_TASK_ERROR_CODES,
    TaskDispatchError,
    add_task_event,
    dispatch_generation,
    stop_comfy_prompt,
)
from ..workers.celery_app import celery_app
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
        if status not in {item.value for item in TaskStatus}:
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


@router.get("/tasks/{task_id}/events", response_model=list[TaskEventOut])
def list_task_events(
    task_id: int,
    limit: int = 100,
    offset: int = 0,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """按时间正序读取单个任务的持久生命周期事件。"""
    task = (
        db.query(GenerationTask)
        .filter(GenerationTask.id == task_id, GenerationTask.user_id == user.id)
        .first()
    )
    if task is None:
        raise HTTPException(404, "任务不存在")
    if limit < 1 or limit > 200 or offset < 0:
        raise HTTPException(400, "分页参数无效")
    return (
        db.query(TaskEvent)
        .filter(TaskEvent.task_id == task_id)
        .order_by(TaskEvent.id.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )


@router.post("/tasks/{task_id}/retry", response_model=TaskOut, status_code=202)
def retry_task(
    task_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """对安全可重试的失败任务执行一次有上限的手动重试。"""
    task = (
        db.query(GenerationTask)
        .filter(GenerationTask.id == task_id, GenerationTask.user_id == user.id)
        .first()
    )
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task.status != TaskStatus.failed:
        raise HTTPException(409, "只有失败任务可以重试")
    retryable_error = task.error_code in {
        "comfy_unavailable",
        "queue_unavailable",
    } or (task.error_code == "comfy_timeout" and bool(task.comfy_prompt_id))
    if not retryable_error:
        raise HTTPException(409, "此失败类型不支持安全重试，请调整参数后重新提交任务")
    if task.manual_retry_count >= MAX_MANUAL_RETRIES:
        raise HTTPException(409, f"手动重试次数已达到上限（{MAX_MANUAL_RETRIES} 次）")

    updated = (
        db.query(GenerationTask)
        .filter(
            GenerationTask.id == task_id,
            GenerationTask.user_id == user.id,
            GenerationTask.status == TaskStatus.failed,
            or_(
                GenerationTask.error_code.in_(
                    RETRYABLE_TASK_ERROR_CODES - {"comfy_timeout"}
                ),
                and_(
                    GenerationTask.error_code == "comfy_timeout",
                    GenerationTask.comfy_prompt_id.is_not(None),
                ),
            ),
            GenerationTask.manual_retry_count < MAX_MANUAL_RETRIES,
        )
        .update(
            {
                GenerationTask.status: TaskStatus.pending,
                GenerationTask.error: None,
                GenerationTask.error_code: None,
                GenerationTask.manual_retry_count: GenerationTask.manual_retry_count + 1,
                GenerationTask.updated_at: datetime.utcnow(),
            },
            synchronize_session=False,
        )
    )
    if not updated:
        db.rollback()
        db.refresh(task)
        if task.manual_retry_count >= MAX_MANUAL_RETRIES:
            raise HTTPException(409, f"手动重试次数已达到上限（{MAX_MANUAL_RETRIES} 次）")
        raise HTTPException(409, "任务状态已变化，请刷新后重试")

    db.refresh(task)
    add_task_event(
        db,
        task,
        "manual_retry_requested",
        f"已请求手动重试（第 {task.manual_retry_count}/{MAX_MANUAL_RETRIES} 次）。",
    )
    db.commit()
    try:
        dispatch_generation(db, task)
    except TaskDispatchError as exc:
        raise HTTPException(503, str(exc)) from exc
    db.refresh(task)
    return task


@router.post("/tasks/{task_id}/cancel", response_model=TaskActionOut)
def cancel_task(
    task_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """取消排队或执行中的任务，并尽力停止对应的 ComfyUI prompt。"""
    task = (
        db.query(GenerationTask)
        .filter(GenerationTask.id == task_id, GenerationTask.user_id == user.id)
        .first()
    )
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task.status == TaskStatus.cancelled:
        return TaskActionOut(
            task=task,
            message="任务已取消。",
            celery_revoke="already_cancelled",
            comfy_stop="not_requested",
        )
    if task.status not in (TaskStatus.pending, TaskStatus.processing):
        raise HTTPException(409, "只有排队或执行中的任务可以取消")

    celery_task_id = task.celery_task_id
    prompt_id = task.comfy_prompt_id
    updated = (
        db.query(GenerationTask)
        .filter(
            GenerationTask.id == task_id,
            GenerationTask.user_id == user.id,
            GenerationTask.status.in_((TaskStatus.pending, TaskStatus.processing)),
        )
        .update(
            {
                GenerationTask.status: TaskStatus.cancelled,
                GenerationTask.error: None,
                GenerationTask.error_code: "cancelled",
                GenerationTask.updated_at: datetime.utcnow(),
            },
            synchronize_session=False,
        )
    )
    if not updated:
        db.rollback()
        db.refresh(task)
        if task.status == TaskStatus.cancelled:
            return TaskActionOut(
                task=task,
                message="任务已取消。",
                celery_revoke="already_cancelled",
                comfy_stop="not_requested",
            )
        raise HTTPException(409, "任务状态已变化，请刷新后重试")

    db.refresh(task)
    add_task_event(db, task, "cancel_requested", "已记录取消请求。")
    db.commit()

    celery_revoke = "not_needed"
    if celery_task_id:
        try:
            celery_app.control.revoke(celery_task_id, terminate=False)
            celery_revoke = "requested"
        except Exception:
            celery_revoke = "failed"
            add_task_event(
                db,
                task,
                "worker_revoke_failed",
                "任务已标记取消，但无法通知 Celery worker。",
                error_code="worker_revoke_failed",
            )

    comfy_stop = "not_requested"
    if prompt_id:
        try:
            comfy_stop = stop_comfy_prompt(ComfyClient(), prompt_id)
            add_task_event(
                db,
                task,
                "comfy_stop_requested",
                {
                    "interrupted": "已请求中断正在执行的 ComfyUI prompt。",
                    "removed_from_queue": "已从 ComfyUI 队列移除 prompt。",
                    "not_in_queue": "ComfyUI 队列中未找到该 prompt。",
                }[comfy_stop],
            )
        except Exception:
            comfy_stop = "failed"
            add_task_event(
                db,
                task,
                "comfy_stop_failed",
                "任务已标记取消，但无法确认 ComfyUI 是否停止。",
                error_code="comfy_cancel_failed",
            )

    db.commit()
    db.refresh(task)
    message = (
        "任务已取消。"
        if celery_revoke != "failed" and comfy_stop != "failed"
        else "任务已标记取消，但后台停止确认失败。"
    )
    return TaskActionOut(
        task=task,
        message=message,
        celery_revoke=celery_revoke,
        comfy_stop=comfy_stop,
    )
