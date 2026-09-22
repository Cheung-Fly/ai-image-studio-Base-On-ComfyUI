"""ORM 模型：生成任务 + 图片资产。"""
import enum
from datetime import datetime

from sqlalchemy import Column, DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from .database import Base


class TaskStatus(str, enum.Enum):
    pending = "pending"          # 已入队，等待 worker 处理
    processing = "processing"    # worker 正在生成
    completed = "completed"      # 生成成功
    failed = "failed"            # 生成失败


class User(Base):
    """用户账号（公网多人使用时启用鉴权）。"""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(String(256), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class UsageRecord(Base):
    """每日用量记录（用于额度控制）。"""

    __tablename__ = "usage_records"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    date = Column(String(10), nullable=False, index=True)  # YYYY-MM-DD
    chat_count = Column(Integer, default=0)
    image_count = Column(Integer, default=0)


class GenerationTask(Base):
    """一次生成请求的任务记录（文生图 / 视频生成共用）。"""

    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    task_type = Column(String(16), default="image", nullable=False)  # image / video
    prompt = Column(Text, nullable=False)
    negative_prompt = Column(Text, default="")
    workflow = Column(String(255), default="", nullable=False)  # 所用工作流文件名（空=默认）
    model_preset = Column(String(255), default="", nullable=False)  # 所用模型组合预设名（空=默认）
    aspect_ratio = Column(String(32), default="1:1 (Square)")
    megapixels = Column(Float, default=1.0)
    seed = Column(Integer, default=0)
    duration = Column(Float, nullable=True)  # 视频时长（秒，仅视频任务）
    # 采样步数（视频=调度器步数，生图=阶段一主生成步数）
    steps = Column(Integer, default=20, nullable=False)
    # 生图：放大精修步数（阶段二，仅生图任务）
    refine_steps = Column(Integer, default=4, nullable=False)
    # LoRA 列表（JSON 数组 [{name,strength}]，生图/视频共用）
    loras = Column(Text, default="[]", nullable=False)
    ref_images = Column(Text, default="")   # 视频素材：参考图文件名（逗号分隔）
    ref_videos = Column(Text, default="")   # 视频素材：参考视频文件名（逗号分隔）
    ref_audios = Column(Text, default="")   # 视频素材：参考音频文件名（逗号分隔）
    status = Column(Enum(TaskStatus, native_enum=False), default=TaskStatus.pending, index=True)
    error = Column(Text, nullable=True)
    comfy_prompt_id = Column(String(64), nullable=True)  # ComfyUI 返回的 prompt_id
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    images = relationship(
        "ImageAsset",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="ImageAsset.id",
    )


class ImageAsset(Base):
    """生成结果资产（图片 / 视频共用）。"""

    __tablename__ = "images"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), index=True)
    filename = Column(String(255), nullable=False)        # 磁盘文件名
    filepath = Column(String(512), nullable=False)        # 相对 image_storage_dir 的路径
    media_type = Column(String(16), default="image", nullable=False)  # image / video
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    task = relationship("GenerationTask", back_populates="images")

    @property
    def file_url(self) -> str:
        """资产下载地址（供响应模型 file_url 字段使用）。"""
        return f"/api/images/{self.id}/file"


class Conversation(Base):
    """对话会话：每个用户 × 每个 provider 一条（对应前端每个模型窗口）。"""

    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    provider = Column(String(32), nullable=False, index=True)  # openai / llama
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    messages = relationship(
        "Message",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.id",
    )


class Message(Base):
    """单条对话消息。"""

    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False, index=True)
    role = Column(String(16), nullable=False)  # user / assistant
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    conversation = relationship("Conversation", back_populates="messages")
