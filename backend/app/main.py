"""FastAPI 入口。"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# 提前导入，确保 worker 任务在 API 进程内也完成注册（eager 模式/调试用）
from app.workers import tasks as _worker_tasks  # noqa: F401

from .api import (
    agent,
    auth,
    chat,
    conversations,
    custom_models,
    generate,
    images,
    loras,
    me,
    model_presets,
    rag_docs,
    tasks,
    upload,
    video,
    workflows,
)
from .config import settings
from .database import init_db

# 前端静态目录（相对 backend/ 的 ../frontend）
FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"


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

app.include_router(agent.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(generate.router, prefix="/api")
app.include_router(tasks.router, prefix="/api")
app.include_router(images.router, prefix="/api")
app.include_router(chat.router, prefix="/api")
app.include_router(conversations.router, prefix="/api")
app.include_router(custom_models.router, prefix="/api")
app.include_router(workflows.router, prefix="/api")
app.include_router(model_presets.router, prefix="/api")
app.include_router(rag_docs.router, prefix="/api")
app.include_router(me.router, prefix="/api")
app.include_router(upload.router, prefix="/api")
app.include_router(video.router, prefix="/api")
app.include_router(loras.router, prefix="/api")


@app.get("/health", tags=["system"])
def health():
    return {"status": "ok", "comfyui_base_url": settings.comfyui_base_url}


# ---- 前端静态托管（开发期：直接从 8000 端口打开应用）----
@app.get("/", include_in_schema=False)
def index():
    idx = FRONTEND_DIR / "index.html"
    if idx.exists():
        return FileResponse(idx)
    return {"detail": "前端文件不存在"}


if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
