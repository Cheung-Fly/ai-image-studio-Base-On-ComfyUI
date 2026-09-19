"""全局配置：环境变量优先，未设置时使用项目内默认值。"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ 目录（config.py 位于 backend/app/config.py）
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ComfyUI 服务地址（API 模式）
    comfyui_base_url: str = "http://127.0.0.1:8188"
    # 数据库连接串
    database_url: str = f"sqlite:///{(BASE_DIR / 'data' / 'aistudio.db').as_posix()}"
    # 图片存储目录（相对 backend/）
    image_storage_dir: str = "data/images"
    # Celery broker / result backend
    redis_url: str = "redis://127.0.0.1:6379/0"
    # 工作流模板目录（相对 backend/）
    workflow_dir: str = "../workflows"
    # 工作流模板文件名（Krea2 文生图 + 潜空间放大）
    workflow_file: str = "Krea2-极清生图流+SeedVR2-int8图像放大.json"
    # 单次生成超时（秒）
    comfy_timeout: int = 900
    # ComfyUI 输出轮询间隔（秒）
    comfy_poll_interval: float = 2.0

    @property
    def image_dir(self) -> Path:
        p = Path(self.image_storage_dir)
        return p if p.is_absolute() else BASE_DIR / p

    @property
    def workflow_path(self) -> Path:
        p = Path(self.workflow_dir) / self.workflow_file
        return p if p.is_absolute() else BASE_DIR / p


settings = Settings()
