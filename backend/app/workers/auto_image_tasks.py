"""RunningHub worker tasks for automatic storyboard reference images."""

import json
import logging
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path

from ..config import settings
from ..database import SessionLocal
from ..models import (
    AutoImageAsset,
    AutoImageBatch,
    CustomModelConfig,
    GenerationTask,
    ImageAsset,
    RunningHubConfig,
    TaskStatus,
)
from ..schemas import DirectorStoryboardOut
from ..security import decrypt_secret
from ..services.auto_image_agent import plan_series_assets
from ..services.auto_image_events import (
    add_auto_image_event,
    add_auto_image_task_event,
)
from ..services.llm_client import LLMError
from ..services.runninghub import (
    RunningHubClient,
    RunningHubError,
    workflow_parameter_node_info,
)
from ..services.task_reliability import TaskCancelled, add_task_event
from ..workers.tasks import _cleanup_saved_files, _save_media_bytes
from .celery_app import celery_app

logger = logging.getLogger(__name__)


def _safe_plan_error(
    exc: Exception, custom_config: dict[str, str] | None
) -> str:
    message = str(exc)
    if custom_config:
        base_url = custom_config.get("base_url", "")
        for secret in (
            custom_config.get("api_key"),
            base_url,
            base_url.rstrip("/"),
        ):
            if secret:
                message = message.replace(secret, "[redacted]")
    if message.startswith("LLM 返回 ") and ": " in message:
        message = f"{message.split(': ', 1)[0]}: 响应正文已省略"
    elif message.startswith("LLM 响应格式异常:"):
        message = "LLM 响应格式异常，响应正文已省略"
    elif message.startswith("LLM 响应缺少内容字段:"):
        message = "LLM 响应缺少内容字段，响应正文已省略"
    return message[:1500]


@celery_app.task(
    name="app.workers.auto_image_tasks.plan_auto_image_batch",
    bind=True,
    acks_late=True,
    time_limit=20 * 60,
    soft_time_limit=18 * 60,
)
def plan_auto_image_batch(
    self,
    batch_id: int,
    provider: str,
    custom_config_id: int | None = None,
):
    """Create the cross-series asset list asynchronously and persist progress."""
    db = SessionLocal()
    started_at = time.monotonic()
    custom_config: dict[str, str] | None = None
    try:
        batch = db.get(AutoImageBatch, batch_id)
        if batch is None:
            logger.error("auto_image plan batch not found batch_id=%s", batch_id)
            return {"status": "not_found", "batch_id": batch_id}
        if batch.status != "planning":
            return {"status": batch.status, "batch_id": batch_id}

        storyboard_entries = json.loads(batch.storyboards_json)
        storyboards = [
            (
                entry["sheet_name"],
                DirectorStoryboardOut.model_validate(entry["storyboard"]),
            )
            for entry in storyboard_entries
        ]
        shot_count = sum(len(storyboard.shots) for _, storyboard in storyboards)
        add_auto_image_event(
            db,
            batch.id,
            "asset_plan_started",
            f"开始规划跨集资产：{len(storyboards)} 集、{shot_count} 个镜头。",
        )
        db.commit()

        if provider == "custom":
            config = (
                db.query(CustomModelConfig)
                .filter(
                    CustomModelConfig.id == custom_config_id,
                    CustomModelConfig.user_id == batch.user_id,
                )
                .first()
            )
            if config is None:
                raise LLMError("自定义模型配置已不存在，请重新选择模型。")
            try:
                api_key = decrypt_secret(config.api_key_encrypted)
            except ValueError as exc:
                raise LLMError("自定义模型 API Key 解密失败。") from exc
            custom_config = {
                "base_url": config.base_url,
                "api_key": api_key,
                "model": config.model,
            }
        elif provider not in {"openai", "llama"}:
            raise LLMError("不支持的资产规划模型")

        add_auto_image_event(
            db,
            batch.id,
            "llm_request_started",
            f"正在请求资产规划模型（{provider}）；单次请求超时上限为 300 秒。",
        )
        db.commit()
        llm_started_at = time.monotonic()
        try:
            plan = plan_series_assets(storyboards, provider, custom_config)
        except Exception as exc:
            duration_ms = round((time.monotonic() - llm_started_at) * 1000)
            safe_message = _safe_plan_error(exc, custom_config)
            logger.error(
                "auto_image llm request failed batch_id=%s provider=%s "
                "duration_ms=%s error_type=%s error=%s",
                batch.id,
                provider,
                duration_ms,
                type(exc).__name__,
                safe_message,
            )
            add_auto_image_event(
                db,
                batch.id,
                "llm_request_failed",
                f"模型请求失败（{type(exc).__name__}）：{safe_message}",
                level="error",
                duration_ms=duration_ms,
            )
            db.commit()
            raise

        add_auto_image_event(
            db,
            batch.id,
            "llm_request_completed",
            "模型响应已通过格式和资产校验，正在保存去重后的资产清单。",
            duration_ms=round((time.monotonic() - llm_started_at) * 1000),
        )
        db.add_all(
            [
                AutoImageAsset(
                    batch_id=batch.id,
                    asset_key=item["asset_key"],
                    category=item["category"],
                    name=item["name"],
                    episode_indices_json=json.dumps(
                        item["episodes"], ensure_ascii=False
                    ),
                    prompt=item["prompt"],
                )
                for item in plan
            ]
        )
        batch.status = "draft"
        batch.updated_at = datetime.utcnow()
        duration_ms = round((time.monotonic() - started_at) * 1000)
        add_auto_image_event(
            db,
            batch.id,
            "asset_plan_completed",
            f"资产清单已保存：{len(plan)} 项跨集去重资产。",
            duration_ms=duration_ms,
        )
        db.commit()
        return {
            "status": "completed",
            "batch_id": batch.id,
            "asset_count": len(plan),
            "duration_ms": duration_ms,
        }
    except Exception as exc:
        db.rollback()
        batch = db.get(AutoImageBatch, batch_id)
        if batch is not None and batch.status == "planning":
            safe_message = _safe_plan_error(exc, custom_config)
            logger.error(
                "auto_image asset planning failed batch_id=%s error_type=%s error=%s",
                batch_id,
                type(exc).__name__,
                safe_message,
            )
            batch.status = "planning_failed"
            batch.updated_at = datetime.utcnow()
            add_auto_image_event(
                db,
                batch.id,
                "asset_plan_failed",
                f"资产清单规划失败（{type(exc).__name__}）：{safe_message}",
                level="error",
                duration_ms=round((time.monotonic() - started_at) * 1000),
            )
            db.commit()
        return {"status": "failed", "batch_id": batch_id}
    finally:
        db.close()


@celery_app.task(
    name="app.workers.auto_image_tasks.run_runninghub_image_task",
    bind=True,
    acks_late=True,
    time_limit=6 * 60 * 60,
    soft_time_limit=5 * 60 * 60,
)
def run_runninghub_image_task(self, task_id: int):
    """Generate one asset using the RunningHub workflow stored with its plan item."""
    db = SessionLocal()
    started_at = time.monotonic()
    downloaded_paths: list[Path] = []

    def check_cancelled() -> None:
        with SessionLocal() as status_db:
            current = status_db.get(GenerationTask, task_id)
        if current is None or current.status == TaskStatus.cancelled:
            raise TaskCancelled()

    try:
        task = db.get(GenerationTask, task_id)
        if task is None:
            return {"status": "not_found", "task_id": task_id}
        if task.status in {TaskStatus.cancelled, TaskStatus.completed}:
            return {"status": task.status.value, "task_id": task_id}
        item = (
            db.query(AutoImageAsset)
            .filter(AutoImageAsset.task_id == task_id)
            .first()
        )
        if item is None:
            raise RunningHubError("自动图片资产记录不存在")
        config = db.get(RunningHubConfig, 1)
        if config is None:
            raise RunningHubError("请先配置 RunningHub API Key")
        try:
            api_key = decrypt_secret(config.api_key_encrypted)
        except ValueError as exc:
            raise RunningHubError("RunningHub API Key 解密失败") from exc

        generation_config = json.loads(item.generation_config_json or "{}")
        selection = generation_config.get("runninghub") or {}
        workflow_id = str(selection.get("workflow_id") or "")
        prompt_node_id = str(selection.get("prompt_node_id") or "")
        prompt_field = str(selection.get("prompt_field") or "")
        if not workflow_id or not prompt_node_id or not prompt_field:
            raise RunningHubError("RunningHub 工作流配置不完整")

        task.status = TaskStatus.processing
        task.error = None
        task.error_code = None
        task.updated_at = datetime.utcnow()
        add_task_event(db, task, "started", "开始通过 RunningHub 生成设定图。")
        add_auto_image_event(
            db, item.batch_id, "image_generation_started", "已开始通过 RunningHub 生成设定图。"
        )
        db.commit()
        check_cancelled()

        node_info_list = [
            {
                "nodeId": prompt_node_id,
                "fieldName": prompt_field,
                "fieldValue": task.prompt,
            }
        ]
        negative_node_id = selection.get("negative_prompt_node_id")
        negative_field = selection.get("negative_prompt_field")
        if task.negative_prompt and negative_node_id and negative_field:
            node_info_list.append(
                {
                    "nodeId": str(negative_node_id),
                    "fieldName": str(negative_field),
                    "fieldValue": task.negative_prompt,
                }
            )

        settings.image_dir.mkdir(parents=True, exist_ok=True)
        temp_dir = Path(
            tempfile.mkdtemp(
                prefix=f"auto-image-{task_id}-",
                dir=settings.image_dir,
            )
        )
        try:
            client = RunningHubClient(api_key)
            try:
                workflow = client.get_workflow(workflow_id)
                node_info_list.extend(
                    workflow_parameter_node_info(
                        workflow,
                        task.aspect_ratio,
                        task.megapixels,
                        seed=task.seed,
                        steps=task.steps,
                        refine_steps=task.refine_steps,
                    )
                )

                def report_runninghub_status(status: str, elapsed: float) -> None:
                    add_auto_image_event(
                        db,
                        item.batch_id,
                        "runninghub_status",
                        f"RunningHub 任务状态：{status}，已等待 {round(elapsed)} 秒。",
                        duration_ms=round(elapsed * 1000),
                    )
                    db.commit()

                def persist_runninghub_task_id(remote_task_id: str | None) -> None:
                    current_task = db.get(GenerationTask, task_id)
                    if current_task is None:
                        raise RunningHubError("RunningHub 本地任务记录已不存在")
                    current_task.runninghub_task_id = remote_task_id
                    current_task.updated_at = datetime.utcnow()
                    db.commit()

                result = client.generate(
                    workflow_id,
                    node_info_list,
                    temp_dir,
                    cancel_check=check_cancelled,
                    status_callback=report_runninghub_status,
                    existing_task_id=task.runninghub_task_id,
                    task_id_callback=persist_runninghub_task_id,
                )
                add_auto_image_event(
                    db,
                    item.batch_id,
                    "runninghub_workflow_completed",
                    "RunningHub 工作流已完成，正在下载并保存图片。",
                    duration_ms=round((time.monotonic() - started_at) * 1000),
                )
                db.commit()
            finally:
                client.close()
            image_files = [
                Path(file["path"])
                for file in result["files"]
                if file["kind"] == "image"
            ]
            if not image_files:
                raise RunningHubError("RunningHub 工作流未返回图片")
            for path in image_files:
                check_cancelled()
                data = path.read_bytes()
                saved_path, relative_path, width, height = _save_media_bytes(
                    task_id,
                    path.name,
                    data,
                    "image",
                )
                downloaded_paths.append(saved_path)
                db.add(
                    ImageAsset(
                        task_id=task_id,
                        filename=Path(relative_path).name,
                        filepath=relative_path,
                        media_type="image",
                        width=width,
                        height=height,
                        created_at=datetime.utcnow(),
                    )
                )
            check_cancelled()
            task = db.get(GenerationTask, task_id)
            if task is None or task.status == TaskStatus.cancelled:
                raise TaskCancelled()
            task.status = TaskStatus.completed
            task.runninghub_task_id = None
            task.error = None
            task.error_code = None
            task.updated_at = datetime.utcnow()
            add_task_event(db, task, "completed", "设定图生成完成。")
            add_auto_image_event(
                db,
                item.batch_id,
                "image_generation_completed",
                "设定图已生成并保存。",
                duration_ms=round((time.monotonic() - started_at) * 1000),
            )
            db.commit()
            return {"status": "completed", "task_id": task_id}
        finally:
            try:
                shutil.rmtree(temp_dir)
            except OSError:
                logger.exception(
                    "清理 RunningHub 自动图片临时目录失败 task_id=%s",
                    task_id,
                )

    except TaskCancelled:
        db.rollback()
        _cleanup_saved_files(task_id, downloaded_paths)
        return {"status": "cancelled", "task_id": task_id}
    except Exception as exc:
        db.rollback()
        _cleanup_saved_files(task_id, downloaded_paths)
        task = db.get(GenerationTask, task_id)
        if task is None:
            logger.exception("RunningHub 自动图片任务记录不存在 task_id=%s", task_id)
            return {"status": "failed", "task_id": task_id}
        if task.status == TaskStatus.cancelled:
            return {"status": "cancelled", "task_id": task_id}
        if isinstance(exc, RunningHubError):
            message = str(exc)
            code = "runninghub_failed"
        else:
            logger.exception("RunningHub 自动图片任务失败 task_id=%s", task_id)
            message = "RunningHub 设定图生成失败，请检查工作流与服务日志。"
            code = "runninghub_failed"
        task.status = TaskStatus.failed
        task.error = message[:1000]
        task.error_code = code
        task.updated_at = datetime.utcnow()
        add_task_event(db, task, "failed", task.error, error_code=code)
        add_auto_image_task_event(
            db,
            task_id,
            "image_generation_failed",
            f"RunningHub 设定图生成失败（{code}）：{task.error}",
            level="error",
            duration_ms=round((time.monotonic() - started_at) * 1000),
        )
        db.commit()
        return {
            "status": "failed",
            "task_id": task_id,
            "error_code": code,
            "error": task.error,
        }
    finally:
        db.close()
