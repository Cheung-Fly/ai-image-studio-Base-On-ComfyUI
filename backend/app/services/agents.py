"""多 Agent 编排：一句话生图流水线（提示词优化 → 生图 → 审图）。

固定流程编排（起步方案）：编排器按固定顺序调用三个 Agent，不依赖 LLM function calling。
"""
import base64
import mimetypes
import time
from pathlib import Path

from ..config import settings
from ..database import SessionLocal
from ..models import GenerationTask, TaskStatus
from ..services.llm_client import chat
from ..workers.tasks import run_generation


class PromptAgent:
    """提示词优化 Agent：把用户一句话扩写成专业英文提示词。"""

    def run(self, user_request: str) -> str:
        message = (
            "你是专业的图像提示词工程师。把下面的用户描述扩写成一段英文的详细提示词，"
            "包含主体、风格、光线、构图、质量词。只输出提示词本身，不要任何解释。\n\n"
            f"用户描述：{user_request}"
        )
        return chat(message, provider=settings.default_llm_provider)


class GenerateAgent:
    """生图 Agent：创建任务 + 提交 Celery + 轮询到完成，返回 (task_id, 图片绝对路径列表)。"""

    def run(self, prompt: str, user_id: int) -> tuple[int, list[str]]:
        db = SessionLocal()
        task = GenerationTask(
            user_id=user_id,
            prompt=prompt,
            negative_prompt="",
            aspect_ratio="1:1 (Square)",
            megapixels=1.0,
            seed=-1,
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        task_id = task.id
        db.close()

        run_generation.delay(task_id)

        # 轮询等待生成完成（复用现有 Celery 出图，不重复生成逻辑）
        while True:
            db = SessionLocal()
            t = db.get(GenerationTask, task_id)
            if t is None:
                db.close()
                raise RuntimeError("生成任务不存在")
            if t.status == TaskStatus.completed:
                paths = [str(settings.image_dir / img.filepath) for img in t.images]
                db.close()
                return task_id, paths
            if t.status == TaskStatus.failed:
                err = t.error
                db.close()
                raise RuntimeError(f"生成失败：{err}")
            db.close()
            time.sleep(5)


class ReviewAgent:
    """审图 Agent：用多模态模型看图，返回一句中文评价。"""

    def run(self, image_path: str) -> str:
        b64 = _image_to_base64(image_path)
        message = "用一句中文评价这张图：是否清晰、主体是否突出、有无明显瑕疵。"
        return chat(message, provider=settings.default_llm_provider, image_base64=b64)


def _image_to_base64(image_path: str) -> str:
    """读图片文件，转成 data URL（供多模态看图）。"""
    p = Path(image_path)
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    data = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def run_pipeline(user_request: str, user_id: int) -> dict:
    """编排器：固定流程「提示词优化 → 生图 → 审图」。"""
    result = {"request": user_request}

    # ① 提示词优化
    result["optimized_prompt"] = PromptAgent().run(user_request)

    # ② 生图
    task_id, paths = GenerateAgent().run(result["optimized_prompt"], user_id)
    result["task_id"] = task_id
    result["images"] = paths

    # ③ 审图（对第一张图）
    result["review"] = ReviewAgent().run(paths[0]) if paths else "未生成图片"

    return result
