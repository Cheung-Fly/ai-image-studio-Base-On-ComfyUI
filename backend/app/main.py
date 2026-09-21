"""FastAPI 入口。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# 提前导入，确保 worker 任务在 API 进程内也完成注册（eager 模式/调试用）
from app.workers import tasks as _worker_tasks  # noqa: F401

from .api import auth, chat, conversations, generate, images, tasks
from .config import settings
from .database import init_db


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="AI Image Studio API",
    description="基于 ComfyUI API 的全栈 AIGC 生图平台（FastAPI + Celery + SQLite）",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins.split(","),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api")
app.include_router(generate.router, prefix="/api")
app.include_router(tasks.router, prefix="/api")
app.include_router(images.router, prefix="/api")
app.include_router(chat.router, prefix="/api")
app.include_router(conversations.router, prefix="/api")


@app.get("/health", tags=["system"])
def health():
    return {"status": "ok", "comfyui_base_url": settings.comfyui_base_url}
