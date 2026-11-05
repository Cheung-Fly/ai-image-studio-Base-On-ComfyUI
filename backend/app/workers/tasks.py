"""Celery 生成任务：状态流转 + 调用 ComfyUI + 落盘 + 写图库记录。"""
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from time import monotonic

from PIL import Image

from ..config import settings
from ..database import SessionLocal
from ..models import GenerationTask, ImageAsset, TaskStatus
from ..services.comfy_client import ComfyClient
from ..services.task_reliability import (
    TaskCancelled,
    add_task_event,
    classify_task_error,
    stop_comfy_prompt,
)
from ..services.auto_image_events import add_auto_image_task_event
from ..services.video_workflow import build_video_workflow
from ..services.workflow import build_workflow
from .celery_app import celery_app

logger = logging.getLogger(__name__)


def _save_media_bytes(
    task_id: int, filename: str, data: bytes, kind: str = "image"
) -> tuple[Path, str, int, int]:
    """保存图片/视频到 image_storage_dir/<task_id>/，返回 (绝对路径, 相对路径, 宽, 高)。"""
    subdir = settings.image_dir / str(task_id)
    subdir.mkdir(parents=True, exist_ok=True)
    safe = Path(filename).name
    target = subdir / safe
    if target.exists():
        target = subdir / f"{target.stem}-{uuid.uuid4().hex[:6]}{target.suffix}"
    target.write_bytes(data)

    width = height = None
    if kind == "image":
        try:
            with Image.open(target) as im:
                width, height = im.size
        except (OSError, Image.UnidentifiedImageError) as exc:
            logger.warning("读取图片尺寸失败 %s: %s", target.name, exc)

    rel = target.relative_to(settings.image_dir).as_posix()
    return target, rel, width, height


def _split_csv(s: str) -> list[str]:
    """把逗号分隔的素材字段拆成列表（过滤空项）。"""
    return [x for x in (s or "").split(",") if x.strip()]


def _cleanup_saved_files(task_id: int, paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.exception(
                "生成任务结束后清理媒体文件失败 task_id=%s file=%s",
                task_id,
                path.name,
            )


def _parse_loras(raw: str) -> list[dict]:
    """把 tasks.loras 字段（JSON 数组字符串）解析为 [{name, strength}] 列表。"""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    result = []
    for item in data:
        if isinstance(item, dict) and item.get("name"):
            result.append(
                {"name": str(item["name"]), "strength": float(item.get("strength", 0.7))}
            )
    return result


@celery_app.task(
    name="app.workers.tasks.run_generation",
    bind=True,
    max_retries=2,
    default_retry_delay=5,
    acks_late=True,
)
def run_generation(self, task_id: int):
    """执行一次生成，并持久化状态变化、失败分类与可观察事件。"""
    db = SessionLocal()
    started_at = monotonic()
    prompt_id = None
    submission_attempted = False
    saved_files: list[Path] = []

    def check_cancelled() -> None:
        with SessionLocal() as status_db:
            current = status_db.get(GenerationTask, task_id)
            cancelled = current is not None and current.status == TaskStatus.cancelled
        if cancelled:
            raise TaskCancelled()

    try:
        task = db.get(GenerationTask, task_id)
        if task is None:
            return {"status": "not_found", "task_id": task_id}
        if task.status in (TaskStatus.cancelled, TaskStatus.completed):
            return {"status": task.status.value, "task_id": task_id}

        updated = (
            db.query(GenerationTask)
            .filter(
                GenerationTask.id == task_id,
                GenerationTask.status.in_((TaskStatus.pending, TaskStatus.processing)),
            )
            .update(
                {
                    GenerationTask.status: TaskStatus.processing,
                    GenerationTask.error: None,
                    GenerationTask.error_code: None,
                    GenerationTask.updated_at: datetime.utcnow(),
                },
                synchronize_session=False,
            )
        )
        if not updated:
            db.rollback()
            task = db.get(GenerationTask, task_id)
            return {
                "status": task.status.value if task else "not_found",
                "task_id": task_id,
            }
        db.refresh(task)
        add_task_event(
            db,
            task,
            "started",
            "开始生成。",
        )
        add_auto_image_task_event(
            db,
            task_id,
            "image_generation_started",
            "已开始通过 ComfyUI 生成设定图。",
        )
        db.commit()

        client = ComfyClient()
        prompt_id = task.comfy_prompt_id
        if not prompt_id:
            # 构建工作流：图片 / 视频两路
            if task.task_type == "video":
                workflow = build_video_workflow(
                    prompt=task.prompt,
                    duration=task.duration or 4.0,
                    aspect_ratio=task.aspect_ratio,
                    megapixels=task.megapixels,
                    seed=task.seed,
                    steps=task.steps if task.steps is not None else 20,
                    loras=_parse_loras(task.loras),
                    ref_images=_split_csv(task.ref_images),
                    ref_videos=_split_csv(task.ref_videos),
                    ref_audios=_split_csv(task.ref_audios),
                )
            else:
                workflow = build_workflow(
                    prompt=task.prompt,
                    negative_prompt=task.negative_prompt,
                    aspect_ratio=task.aspect_ratio,
                    megapixels=task.megapixels,
                    seed=task.seed,
                    workflow_file=task.workflow,
                    model_preset=task.model_preset,
                    steps=task.steps if task.steps is not None else 8,
                    refine_steps=task.refine_steps if task.refine_steps is not None else 4,
                    loras=_parse_loras(task.loras),
                )

            check_cancelled()
            submission_attempted = True
            prompt_id = client.submit_prompt(workflow)
            task = db.get(GenerationTask, task_id)
            if task is None:
                return {"status": "not_found", "task_id": task_id}
            task.comfy_prompt_id = prompt_id
            add_task_event(
                db,
                task,
                "submitted",
                "工作流已提交到 ComfyUI。",
            )
            add_auto_image_task_event(
                db,
                task_id,
                "comfyui_workflow_submitted",
                "工作流已提交到 ComfyUI，正在等待输出。",
            )
            db.commit()
            db.refresh(task)
            submission_attempted = False
            if task.status == TaskStatus.cancelled:
                try:
                    stop_comfy_prompt(client, prompt_id)
                except Exception:
                    logger.exception("取消已提交的 ComfyUI 任务失败 task_id=%s", task_id)
                    add_task_event(
                        db,
                        task,
                        "cancel_control_failed",
                        "任务已取消，但无法确认 ComfyUI 是否停止。",
                        error_code="comfy_cancel_failed",
                    )
                    db.commit()
                return {"status": "cancelled", "task_id": task_id}
        else:
            add_task_event(
                db,
                task,
                "resumed",
                "恢复等待已提交的 ComfyUI prompt，避免重复生成。",
            )
            db.commit()

        media_meta = client.wait_for_media(prompt_id, cancel_check=check_cancelled)
        add_auto_image_task_event(
            db,
            task_id,
            "comfyui_workflow_completed",
            "ComfyUI 已返回输出，正在下载并保存图片。",
        )
        db.commit()
        for meta in media_meta:
            check_cancelled()
            data = client.download_image(
                meta["filename"], meta.get("subfolder", ""), meta.get("type", "output")
            )
            kind = meta.get("kind", "image")
            path, rel, width, height = _save_media_bytes(
                task_id, meta["filename"], data, kind
            )
            saved_files.append(path)
            db.add(
                ImageAsset(
                    task_id=task_id,
                    filename=Path(rel).name,
                    filepath=rel,
                    media_type=kind if kind in ("image", "video") else "image",
                    width=width,
                    height=height,
                    created_at=datetime.utcnow(),
                )
            )

        check_cancelled()
        db.expire_all()
        task = db.get(GenerationTask, task_id)
        if task is None or task.status == TaskStatus.cancelled:
            raise TaskCancelled()
        updated = (
            db.query(GenerationTask)
            .filter(
                GenerationTask.id == task_id,
                GenerationTask.status == TaskStatus.processing,
            )
            .update(
                {
                    GenerationTask.status: TaskStatus.completed,
                    GenerationTask.error: None,
                    GenerationTask.error_code: None,
                    GenerationTask.updated_at: datetime.utcnow(),
                },
                synchronize_session=False,
            )
        )
        if not updated:
            db.rollback()
            raise TaskCancelled()
        db.refresh(task)
        add_task_event(
            db,
            task,
            "completed",
            "生成完成。",
        )
        add_auto_image_task_event(
            db,
            task_id,
            "image_generation_completed",
            "设定图已生成并保存。",
            duration_ms=round((monotonic() - started_at) * 1000),
        )
        db.commit()
        return {"status": "completed", "task_id": task_id, "prompt_id": prompt_id}

    except TaskCancelled:
        db.rollback()
        _cleanup_saved_files(task_id, saved_files)
        current = db.get(GenerationTask, task_id)
        return {
            "status": current.status.value if current else "not_found",
            "task_id": task_id,
        }

    except Exception as exc:
        db.rollback()
        task = db.get(GenerationTask, task_id)
        _cleanup_saved_files(task_id, saved_files)
        if task is None:
            logger.exception("生成任务失败且任务记录不存在 task_id=%s", task_id)
            return {"status": "failed", "task_id": task_id}
        if task.status == TaskStatus.cancelled:
            return {"status": "cancelled", "task_id": task_id}

        error = classify_task_error(exc)
        prompt_persisted = bool(
            task.comfy_prompt_id
            and (not prompt_id or task.comfy_prompt_id == prompt_id)
        )
        retryable = (
            (
                error.retryable
                or (error.code == "comfy_timeout" and prompt_persisted)
            )
            and not (
                submission_attempted
                and not prompt_persisted
                and error.code == "comfy_timeout"
            )
            and self.request.retries < self.max_retries
        )
        updates = {
            GenerationTask.error: error.message,
            GenerationTask.error_code: error.code,
            GenerationTask.updated_at: datetime.utcnow(),
        }
        if retryable:
            updates[GenerationTask.status] = TaskStatus.pending
            updates[GenerationTask.retry_count] = GenerationTask.retry_count + 1
        else:
            updates[GenerationTask.status] = TaskStatus.failed

        updated = (
            db.query(GenerationTask)
            .filter(
                GenerationTask.id == task_id,
                GenerationTask.status == TaskStatus.processing,
            )
            .update(updates, synchronize_session=False)
        )
        if not updated:
            db.rollback()
            current = db.get(GenerationTask, task_id)
            return {
                "status": current.status.value if current else "not_found",
                "task_id": task_id,
            }

        db.refresh(task)
        if retryable:
            add_task_event(
                db,
                task,
                "retry_scheduled",
                "ComfyUI 暂时无响应，任务将在短暂等待后恢复。",
                error_code=error.code,
            )
            add_auto_image_task_event(
                db,
                task_id,
                "image_generation_retry",
                "ComfyUI 暂时无响应，任务将自动重试。",
                level="warning",
                duration_ms=round((monotonic() - started_at) * 1000),
            )
            db.commit()
            logger.warning(
                "生成任务将自动重试 task_id=%s error_code=%s retry=%s",
                task_id,
                error.code,
                self.request.retries + 1,
            )
            raise self.retry(exc=exc) from exc

        add_task_event(
            db,
            task,
            "failed",
            error.message,
            error_code=error.code,
        )
        add_auto_image_task_event(
            db,
            task_id,
            "image_generation_failed",
            f"ComfyUI 设定图生成失败（{error.code}）：{error.message}",
            level="error",
            duration_ms=round((monotonic() - started_at) * 1000),
        )
        db.commit()
        logger.exception("生成任务失败 task_id=%s error_code=%s", task_id, error.code)
        return {
            "status": "failed",
            "task_id": task_id,
            "error_code": error.code,
            "error": error.message,
        }
    finally:
        db.close()


@celery_app.task(name="app.workers.tasks.run_agent_pipeline", bind=True)
def run_agent_pipeline(self, user_request: str, user_id: int):
    """兼容旧版固定流水线：提示词优化、生图、审图。"""
    from ..services.agents import run_pipeline

    try:
        return run_pipeline(user_request, user_id)
    except Exception as exc:
        logger.error("Agent 流水线失败: %s", exc)
        return {"status": "failed", "error": str(exc)[:500]}


@celery_app.task(name="app.workers.tasks.run_mcp_agent", bind=True)
def run_mcp_agent_task(
    self,
    user_request: str,
    provider: str = "",
    user_id: int | None = None,
    custom_config_id: int | None = None,
    history: list[dict[str, str]] | None = None,
    use_rag: bool = False,
    quality_review_enabled: bool = False,
    generation_backend: str = "comfyui",
):
    """MCP Agent 任务：LLM 经 MCP 协议控制本地 ComfyUI 或 RunningHub。"""
    import asyncio
    import time
    import uuid
    from functools import partial

    from redis import Redis

    from ..config import settings
    from ..database import SessionLocal
    from ..services.agent_memory import get_preferences
    from ..services.llm_client import chat_with_tools
    from ..services.mcp_agent import run_mcp_agent
    from ..services.quality_review import review_generated_media

    try:
        db = SessionLocal()
        try:
            memory = get_preferences(db)
            custom_config = None
            if provider == "custom":
                from ..models import CustomModelConfig
                from ..security import decrypt_secret

                config = (
                    db.query(CustomModelConfig)
                    .filter(
                        CustomModelConfig.id == custom_config_id,
                        CustomModelConfig.user_id == user_id,
                    )
                    .first()
                )
                if config is None:
                    return {"status": "failed", "error": "自定义模型配置无效"}
                custom_config = {
                    "base_url": config.base_url,
                    "api_key": decrypt_secret(config.api_key_encrypted),
                    "model": config.model,
                }
        finally:
            db.close()

        knowledge_context = None
        if use_rag:
            from ..services import rag

            rag.ensure_index()
            knowledge_context = rag.retrieve(user_request)

        def report_progress(progress):
            self.update_state(state="PROGRESS", meta=progress)

        def confirm_action(tool: str, arguments: dict) -> bool:
            confirmation_id = uuid.uuid4().hex
            task_id = self.request.id
            key = f"aistudio:mcp-agent-confirm:{task_id}:{confirmation_id}"
            expires_at = int(time.time() + 300)
            self.update_state(
                state="CONFIRMATION_REQUIRED",
                meta={
                    "confirmation_id": confirmation_id,
                    "tool": tool,
                    "arguments": arguments,
                    "expires_at": expires_at,
                },
            )
            client = Redis.from_url(settings.redis_url, decode_responses=True)
            deadline = time.monotonic() + 300
            try:
                while time.monotonic() < deadline:
                    answer = client.get(key)
                    if answer is not None:
                        return answer == "yes"
                    time.sleep(0.5)
                return False
            finally:
                client.delete(key)
                client.close()

        return asyncio.run(
            run_mcp_agent(
                user_request,
                provider,
                llm=partial(chat_with_tools, custom_config=custom_config),
                on_progress=report_progress,
                confirm_action=confirm_action,
                memory=memory,
                history=history,
                knowledge_context=knowledge_context,
                quality_review_enabled=quality_review_enabled,
                quality_reviewer=partial(
                    review_generated_media,
                    provider=provider,
                    custom_config=custom_config,
                ),
                generation_backend=generation_backend,
            )
        )
    except Exception as exc:
        logger.error("MCP Agent 失败: %s", exc)
        return {"status": "failed", "error": str(exc)[:500]}
