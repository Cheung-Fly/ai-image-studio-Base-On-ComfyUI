"""公开给单实例前端的连接配置。"""
import tempfile
from urllib.parse import urlsplit

import httpx
from celery.exceptions import CeleryError
from fastapi import APIRouter, Depends
from kombu.exceptions import OperationalError
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ..config import settings
from ..database import engine
from ..models import User
from ..workers.celery_app import celery_app
from .auth import get_current_user

router = APIRouter(tags=["instance"])


@router.get("/instance")
def get_instance_config(_user: User = Depends(get_current_user)):
    """返回浏览器打开 ComfyUI 网页所需的非敏感连接地址。"""
    return {
        "comfyui_web_url": settings.comfyui_web_url,
        "default_llm_provider": settings.default_llm_provider,
    }


def _check_database() -> dict[str, str]:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return {"id": "database", "label": "数据库", "status": "error",
                "message": "数据库不可用，请检查 DATABASE_URL 和数据目录权限。"}
    return {"id": "database", "label": "数据库", "status": "ok", "message": "数据库连接正常。"}


def _check_redis() -> dict[str, str]:
    client = None
    try:
        client = Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        client.ping()
    except (RedisError, ValueError):
        return {"id": "redis", "label": "Redis 队列", "status": "error",
                "message": "无法连接 Redis，请检查 Redis 服务和 REDIS_URL。"}
    finally:
        if client is not None:
            client.close()
    return {"id": "redis", "label": "Redis 队列", "status": "ok", "message": "Redis 连接正常。"}


def _check_worker() -> dict[str, str]:
    try:
        workers = celery_app.control.inspect(timeout=1.5).ping()
    except (CeleryError, OperationalError, RedisError, OSError, TimeoutError, ValueError):
        workers = None
    if not workers:
        return {"id": "worker", "label": "Celery Worker", "status": "error",
                "message": "未检测到可用 Worker；请确认 Worker 已启动并连接同一 Redis。"}
    return {"id": "worker", "label": "Celery Worker", "status": "ok",
            "message": f"检测到 {len(workers)} 个 Worker。"}


def _check_comfyui() -> dict[str, str]:
    try:
        response = httpx.get(
            settings.comfyui_base_url.rstrip("/") + "/system_stats",
            timeout=2.0,
            trust_env=False,
        )
        response.raise_for_status()
        response.json()
    except (httpx.HTTPError, httpx.InvalidURL, ValueError):
        return {"id": "comfyui", "label": "ComfyUI", "status": "error",
                "message": "无法访问 ComfyUI API；请检查 COMFYUI_BASE_URL 和 ComfyUI 状态。"}
    return {"id": "comfyui", "label": "ComfyUI", "status": "ok", "message": "ComfyUI API 可访问。"}


def _check_storage() -> dict[str, str]:
    try:
        settings.image_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=settings.image_dir):
            pass
    except OSError:
        return {"id": "storage", "label": "媒体存储", "status": "error",
                "message": "媒体目录不可写，请检查 IMAGE_STORAGE_DIR 和磁盘权限。"}
    return {"id": "storage", "label": "媒体存储", "status": "ok", "message": "媒体目录可写。"}


def _check_llm() -> dict[str, str]:
    if settings.default_llm_provider == "llama":
        base_url, model, api_key = (
            settings.llama_base_url,
            settings.llama_model,
            settings.llama_api_key,
        )
    elif settings.default_llm_provider == "openai":
        base_url, model, api_key = (
            settings.openai_base_url,
            settings.openai_model,
            settings.openai_api_key,
        )
    else:
        base_url, model, api_key = "", "", ""
    try:
        parsed_url = urlsplit(base_url)
        configured = bool(
            parsed_url.scheme in {"http", "https"}
            and parsed_url.netloc
            and model.strip()
            and api_key.strip()
        )
    except ValueError:
        configured = False
    if not configured:
        return {"id": "llm", "label": "默认语言模型", "status": "warning",
                "message": "默认模型地址、名称或 API Key 缺失/无效；本检查不会发起真实调用。"}
    return {"id": "llm", "label": "默认语言模型", "status": "ok",
            "message": (
                f"默认 provider「{settings.default_llm_provider}」配置完整；未执行真实调用。"
            )}


@router.get("/instance/diagnostics")
def get_instance_diagnostics(_user: User = Depends(get_current_user)):
    """检查本地依赖是否就绪，不返回内部地址、凭据或上游错误内容。"""
    checks = [
        _check_database(),
        _check_redis(),
        _check_worker(),
        _check_comfyui(),
        _check_storage(),
        _check_llm(),
    ]
    required_checks = {"database", "redis", "worker", "comfyui", "storage"}
    required_statuses = {
        check["id"]: check["status"]
        for check in checks
        if check["id"] in required_checks
    }
    ready = set(required_statuses) == required_checks and all(
        status == "ok" for status in required_statuses.values()
    )
    return {"status": "ready" if ready else "degraded", "checks": checks}
