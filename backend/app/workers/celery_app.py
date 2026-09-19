"""Celery 应用实例。

启动 worker（在 backend/ 目录下）：
    celery -A app.workers.celery_app worker --loglevel=info --pool=solo
Windows 上 Celery 5 的 prefork 池不可用，必须 --pool=solo 或 --pool=threads。
"""
from celery import Celery

from ..config import settings

celery_app = Celery(
    "aistudio",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    task_time_limit=settings.comfy_timeout + 120,
    task_soft_time_limit=settings.comfy_timeout + 60,
    timezone="Asia/Shanghai",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    task_default_queue="aistudio",
)
