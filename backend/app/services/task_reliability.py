"""共享生成任务的持久状态、事件和错误分类。"""
import uuid
from dataclasses import dataclass
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from ..models import GenerationTask, TaskEvent, TaskStatus
from .comfy_client import ComfyClient, ComfyError
from .workflow import WorkflowError

MAX_MANUAL_RETRIES = 3
RETRYABLE_TASK_ERROR_CODES = {"comfy_unavailable", "comfy_timeout", "queue_unavailable"}


class TaskCancelled(Exception):
    """worker 检测到任务已取消。"""


class TaskDispatchError(Exception):
    """任务已记录，但无法投递到 Celery 队列。"""


@dataclass(frozen=True)
class TaskErrorInfo:
    code: str
    message: str
    retryable: bool = False


def classify_task_error(exc: Exception) -> TaskErrorInfo:
    """映射底层异常到稳定、可展示且不泄露内部细节的错误类别。"""
    if isinstance(exc, WorkflowError):
        return TaskErrorInfo("workflow_invalid", "工作流配置无效，请检查模板、模型与素材。")

    if isinstance(exc, ComfyError):
        cause = exc
        while cause.__cause__ is not None:
            cause = cause.__cause__
        if isinstance(cause, (httpx.ConnectError, httpx.ConnectTimeout)):
            return TaskErrorInfo(
                "comfy_unavailable",
                "无法连接 ComfyUI，请检查服务状态与地址。",
                retryable=True,
            )
        if isinstance(cause, httpx.TimeoutException):
            return TaskErrorInfo("comfy_timeout", "等待 ComfyUI 响应超时。")
        if "超时" in str(exc) or "timeout" in str(exc).casefold():
            return TaskErrorInfo("comfy_timeout", "等待 ComfyUI 响应超时。")
        if any(f"返回 {status}" in str(exc) for status in (429, 500, 502, 503, 504)):
            return TaskErrorInfo(
                "comfy_unavailable",
                "ComfyUI 暂时不可用。",
                retryable=True,
            )
        return TaskErrorInfo("comfy_failed", "ComfyUI 执行失败，请检查工作流与生成素材。")

    if isinstance(exc, OSError):
        return TaskErrorInfo("storage_error", "保存生成结果失败，请检查存储空间与目录权限。")

    return TaskErrorInfo("internal_error", "生成任务失败，请查看服务日志。")


def add_task_event(
    db: Session,
    task: GenerationTask,
    event_type: str,
    message: str,
    *,
    error_code: str | None = None,
    attempt: int | None = None,
) -> TaskEvent:
    status = task.status.value if isinstance(task.status, TaskStatus) else str(task.status)
    event = TaskEvent(
        task_id=task.id,
        event_type=event_type,
        status=status,
        message=message,
        error_code=error_code,
        attempt=(
            attempt
            if attempt is not None
            else (task.retry_count or 0) + (task.manual_retry_count or 0) + 1
        ),
        created_at=datetime.utcnow(),
    )
    db.add(event)
    return event


def dispatch_generation(db: Session, task: GenerationTask) -> None:
    """持久化队列事件和 Celery ID 后再投递，保证可追踪与可撤销。"""
    if task.status == TaskStatus.cancelled:
        return

    task_id = uuid.uuid4().hex
    task.celery_task_id = task_id
    task.status = TaskStatus.pending
    add_task_event(
        db,
        task,
        "queued",
        "任务已加入生成队列。",
    )
    db.commit()

    try:
        from ..workers.tasks import run_generation

        run_generation.apply_async(args=[task.id], task_id=task_id)
    except Exception as exc:
        db.rollback()
        current = db.get(GenerationTask, task.id)
        if current is not None:
            current.status = TaskStatus.failed
            current.error_code = "queue_unavailable"
            current.error = "任务无法加入生成队列，请检查后台 worker 与 Redis。"
            current.updated_at = datetime.utcnow()
            add_task_event(
                db,
                current,
                "enqueue_failed",
                current.error,
                error_code=current.error_code,
            )
            db.commit()
        raise TaskDispatchError("任务无法加入生成队列，请检查后台 worker 与 Redis。") from exc

    db.refresh(task)


def stop_comfy_prompt(client: ComfyClient, prompt_id: str) -> str:
    """停止指定 prompt；只有确认其正在执行时才调用全局 interrupt。"""
    queue = client.queue_info()

    def contains_prompt(items) -> bool:
        for item in items or []:
            if isinstance(item, dict):
                item_id = item.get("prompt_id")
            elif isinstance(item, (list, tuple)) and len(item) > 1:
                item_id = item[1]
            else:
                item_id = None
            if item_id == prompt_id:
                return True
        return False

    if contains_prompt(queue.get("queue_running")):
        client.interrupt()
        return "interrupted"
    if contains_prompt(queue.get("queue_pending")):
        client.delete_queued([prompt_id])
        return "removed_from_queue"
    return "not_in_queue"
