"""Celery 生成任务：状态流转 + 调用 ComfyUI + 落盘 + 写图库记录。"""
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path

from PIL import Image

from ..config import settings
from ..database import SessionLocal
from ..models import GenerationTask, ImageAsset, TaskStatus
from ..services.comfy_client import ComfyClient, ComfyError
from ..services.video_workflow import VideoWorkflowError, build_video_workflow
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
    """执行一次生成：processing -> 提交 ComfyUI -> 轮询 -> 下载 -> completed/failed。"""
    db = SessionLocal()
    try:
        task = db.get(GenerationTask, task_id)
        if task is None:
            return {"status": "not_found", "task_id": task_id}

        task.status = TaskStatus.processing
        task.error = None
        db.commit()

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

        client = ComfyClient()
        prompt_id = client.submit_prompt(workflow)
        task.comfy_prompt_id = prompt_id
        db.commit()

        media_meta = client.wait_for_media(prompt_id)
        for meta in media_meta:
            data = client.download_image(
                meta["filename"], meta.get("subfolder", ""), meta.get("type", "output")
            )
            kind = meta.get("kind", "image")
            _, rel, width, height = _save_media_bytes(task_id, meta["filename"], data, kind)
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

        task.status = TaskStatus.completed
        db.commit()
        return {"status": "completed", "task_id": task_id, "prompt_id": prompt_id}

    except Exception as exc:
        db.rollback()
        task = db.get(GenerationTask, task_id)
        if task is not None:
            task.status = TaskStatus.failed
            task.error = str(exc)[:1000]
            task.updated_at = datetime.utcnow()
            db.commit()
        # 短暂瞬时错误（连接/超时）允许重试
        retryable = isinstance(exc, (ComfyError, VideoWorkflowError))
        if retryable and self.request.retries < self.max_retries:
            raise self.retry(exc=exc) from exc
        return {"status": "failed", "task_id": task_id, "error": str(exc)[:500]}
    finally:
        db.close()
