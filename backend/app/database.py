"""SQLAlchemy 引擎 / 会话工厂。"""
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings

# SQLite 相对路径转绝对路径，确保在任意工作目录下数据都落在 backend/data
_database_url = settings.database_url
if _database_url.startswith("sqlite:///./"):
    _path = settings.image_dir.parent / _database_url.replace("sqlite:///./", "")
    _path.parent.mkdir(parents=True, exist_ok=True)
    _database_url = f"sqlite:///{_path.as_posix()}"

# 确保 SQLite 数据库文件的父目录存在（SQLite 不会自动创建父目录）
if _database_url.startswith("sqlite:///"):
    _db_path = Path(_database_url.replace("sqlite:///", ""))
    # 仅当路径为绝对路径（含盘符）时才创建父目录，跳过纯文件名等异常情形
    if _db_path.parent and str(_db_path.parent) != ".":
        _db_path.parent.mkdir(parents=True, exist_ok=True)

_connect_args = {"check_same_thread": False} if _database_url.startswith("sqlite") else {}

engine = create_engine(_database_url, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def init_db() -> None:
    """建表（幂等）+ 轻量列迁移。"""
    from . import models  # noqa: F401  确保模型注册

    Base.metadata.create_all(bind=engine)

    # 轻量迁移：为已存在的 SQLite 表补齐新增列（create_all 不会 ALTER 已有表）
    _migrate_missing_columns()


def get_db():
    """FastAPI 依赖：请求级数据库会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _migrate_missing_columns() -> None:
    """SQLite 轻量列迁移：检测并补齐缺列（幂等、无第三方迁移库）。

    仅在 SQLite 下执行；PostgreSQL 等生产库请改用 Alembic。
    """
    if not _database_url.startswith("sqlite"):
        return
    from sqlalchemy import inspect, text

    # (表名, 列名, DDL 片段) —— 新增列时在此登记即可
    migrations = [
        ("tasks", "workflow", "VARCHAR(255) NOT NULL DEFAULT ''"),
        ("tasks", "model_preset", "VARCHAR(255) NOT NULL DEFAULT ''"),
        ("tasks", "task_type", "VARCHAR(16) NOT NULL DEFAULT 'image'"),
        ("tasks", "duration", "FLOAT"),
        ("tasks", "ref_images", "TEXT DEFAULT ''"),
        ("tasks", "ref_videos", "TEXT DEFAULT ''"),
        ("tasks", "ref_audios", "TEXT DEFAULT ''"),
        ("tasks", "steps", "INTEGER NOT NULL DEFAULT 20"),
        ("tasks", "refine_steps", "INTEGER NOT NULL DEFAULT 4"),
        ("tasks", "loras", "TEXT NOT NULL DEFAULT '[]'"),
        ("images", "media_type", "VARCHAR(16) NOT NULL DEFAULT 'image'"),
    ]
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, column, ddl in migrations:
            cols = {c["name"] for c in inspector.get_columns(table)}
            if column not in cols:
                conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ddl}'))
