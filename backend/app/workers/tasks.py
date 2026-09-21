"""Celery 生成任务：状态流转 + 调用 ComfyUI + 落盘 + 写图库记录。"""
import logging
import uuid
from datetime import datetime
from pathlib import Path

from PIL import Image

from ..config import settings
from ..database import SessionLocal
from ..models import GenerationTask, ImageAsset, TaskStatus
from ..services.comfy_client import ComfyClient, ComfyError
from ..services.workflow import build_workflow
from .celery_app import celery_app

logger = logging.getLogger(__name__)


def _save_image_bytes(task_id: int, filename: str, data: bytes) -> tuple[Path, str, int, int]:
    """保存图片到 image_storage_dir/<task_id>/，返回 (绝对路径, 相对路径, 宽, 高)。"""
    subdir = settings.image_dir / str(task_id)
    subdir.mkdir(parents=True, exist_ok=True)
    # 保留 ComfyUI 原始文件名，重名时加随机后缀
    safe = Path(filename).name
    target = subdir / safe
    if target.exists():
        target = subdir / f"{target.stem}-{uuid.uuid4().hex[:6]}{target.suffix}"
    target.write_bytes(data)

    width = height = None
    try:
        with Image.open(target) as im:
            width, height = im.size
    except (OSError, Image.UnidentifiedImageError) as exc:
        # 图片损坏/格式无法识别时不阻塞主流程，但记录日志便于排查（不能静默 pass）
        logger.warning("读取图片尺寸失败 %s: %s", target.name, exc)

    rel = target.relative_to(settings.image_dir).as_posix()
    return target, rel, width, height


@celery_app.task(
    name="app.workers.tasks.run_generation",
    bind=True,
    max_retries=2,
    default_retry_delay=5,
    acks_late=True,
)
def run_generation(self, task_id: int):
    """执行一次文生图：processing -> 提交 ComfyUI -> 轮询 -> 下载图片 -> completed/failed。"""
    db = SessionLocal()
    try:
        task = db.get(GenerationTask, task_id)
        if task is None:
            return {"status": "not_found", "task_id": task_id}

        task.status = TaskStatus.processing
        task.error = None
        db.commit()

        workflow = build_workflow(
            prompt=task.prompt,
            negative_prompt=task.negative_prompt,
            aspect_ratio=task.aspect_ratio,
            megapixels=task.megapixels,
            seed=task.seed,
        )

        client = ComfyClient()
        prompt_id = client.submit_prompt(workflow)
        task.comfy_prompt_id = prompt_id
        db.commit()

        images_meta = client.wait_for_images(prompt_id)
        for meta in images_meta:
            data = client.download_image(
                meta["filename"], meta.get("subfolder", ""), meta.get("type", "output")
            )
            _, rel, width, height = _save_image_bytes(task_id, meta["filename"], data)
            db.add(
                ImageAsset(
                    task_id=task_id,
                    filename=Path(rel).name,
                    filepath=rel,
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
        if isinstance(exc, ComfyError) and self.request.retries < self.max_retries:
            raise self.retry(exc=exc)
        return {"status": "failed", "task_id": task_id, "error": str(exc)[:500]}
    finally:
        db.close()
