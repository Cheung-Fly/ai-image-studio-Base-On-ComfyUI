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
    cancelled = "cancelled"      # 用户已取消


class User(Base):
    """单实例内部资料；当前应用不提供登录或注册。"""

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
    error_code = Column(String(64), nullable=True)
    comfy_prompt_id = Column(String(64), nullable=True)  # ComfyUI 返回的 prompt_id
    celery_task_id = Column(String(255), nullable=True, index=True)
    retry_count = Column(Integer, default=0, nullable=False)
    manual_retry_count = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    images = relationship(
        "ImageAsset",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="ImageAsset.id",
    )
    events = relationship(
        "TaskEvent",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="TaskEvent.id",
    )


class TaskEvent(Base):
    """持久化的任务生命周期事件。"""

    __tablename__ = "task_events"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False, index=True)
    event_type = Column(String(32), nullable=False)
    status = Column(String(16), nullable=False)
    message = Column(Text, nullable=False)
    error_code = Column(String(64), nullable=True)
    attempt = Column(Integer, default=1, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    task = relationship("GenerationTask", back_populates="events")


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


class VideoEpisode(Base):
    """一个导入、审核并串行制作的视频单集。"""

    __tablename__ = "video_episodes"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String(128), nullable=False)
    status = Column(String(16), default="draft", nullable=False, index=True)
    storyboard_json = Column(Text, nullable=False)
    assets_json = Column(Text, nullable=False, default="[]")
    mapping_warning = Column(Text, nullable=True)
    aspect_ratio = Column(String(32), default="16:9 (Widescreen)", nullable=False)
    megapixels = Column(Float, default=1.0, nullable=False)
    generation_backend = Column(String(16), default="comfyui", nullable=False)
    workflow_config_json = Column(Text, nullable=False, default="{}")
    current_shot_index = Column(Integer, default=0, nullable=False)
    current_segment_index = Column(Integer, default=0, nullable=False)
    total_shots = Column(Integer, default=0, nullable=False)
    progress_message = Column(Text, default="等待确认镜头与素材", nullable=False)
    output_filepath = Column(String(512), nullable=True)
    error = Column(Text, nullable=True)
    celery_task_id = Column(String(255), nullable=True, index=True)
    comfy_prompt_id = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    shots = relationship(
        "VideoEpisodeShot",
        back_populates="episode",
        cascade="all, delete-orphan",
        order_by="VideoEpisodeShot.shot_index",
    )


class VideoEpisodeShot(Base):
    """单集制作中的一个分镜及其生成片段。"""

    __tablename__ = "video_episode_shots"

    id = Column(Integer, primary_key=True, index=True)
    episode_id = Column(Integer, ForeignKey("video_episodes.id"), nullable=False, index=True)
    shot_index = Column(Integer, nullable=False)
    shot_no = Column(String(32), nullable=False)
    data_json = Column(Text, nullable=False)
    status = Column(String(16), default="pending", nullable=False)
    segments_json = Column(Text, default="[]", nullable=False)
    error = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    episode = relationship("VideoEpisode", back_populates="shots")


class VideoSeries(Base):
    """一个整剧素材目录及其分集串行制作任务。"""

    __tablename__ = "video_series"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String(128), nullable=False)
    status = Column(String(16), default="draft", nullable=False, index=True)
    total_episodes = Column(Integer, default=0, nullable=False)
    current_episode_index = Column(Integer, default=0, nullable=False)
    progress_message = Column(Text, default="等待检查分集素材", nullable=False)
    error = Column(Text, nullable=True)
    celery_task_id = Column(String(255), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    episodes = relationship(
        "VideoSeriesEpisode",
        back_populates="series",
        cascade="all, delete-orphan",
        order_by="VideoSeriesEpisode.episode_index",
    )


class VideoSeriesEpisode(Base):
    """整剧中的分集顺序与单集制作记录之间的关联。"""

    __tablename__ = "video_series_episodes"

    id = Column(Integer, primary_key=True, index=True)
    series_id = Column(Integer, ForeignKey("video_series.id"), nullable=False, index=True)
    episode_id = Column(
        Integer, ForeignKey("video_episodes.id"), nullable=False, unique=True, index=True
    )
    episode_index = Column(Integer, nullable=False)
    folder_name = Column(String(255), nullable=False)

    series = relationship("VideoSeries", back_populates="episodes")
    episode = relationship("VideoEpisode")


class AutoImageBatch(Base):
    """A storyboard workbook and its cross-episode reference-asset plan."""

    __tablename__ = "auto_image_batches"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String(128), nullable=False)
    source_filename = Column(String(255), nullable=False)
    episode_count = Column(Integer, nullable=False)
    storyboards_json = Column(Text, nullable=False)
    selected_episode_index = Column(Integer, nullable=False, default=1)
    status = Column(String(16), nullable=False, default="draft", index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    assets = relationship(
        "AutoImageAsset",
        back_populates="batch",
        cascade="all, delete-orphan",
        order_by="AutoImageAsset.id",
    )
    events = relationship(
        "AutoImageEvent",
        back_populates="batch",
        cascade="all, delete-orphan",
        order_by="AutoImageEvent.id",
    )


class AutoImageAsset(Base):
    """One reusable character, scene, or prop reference image."""

    __tablename__ = "auto_image_assets"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(
        Integer, ForeignKey("auto_image_batches.id"), nullable=False, index=True
    )
    asset_key = Column(String(64), nullable=False)
    category = Column(String(16), nullable=False)
    name = Column(String(255), nullable=False)
    episode_indices_json = Column(Text, nullable=False)
    prompt = Column(Text, nullable=False)
    generation_config_json = Column(Text, nullable=False, default="{}")
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True, unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    batch = relationship("AutoImageBatch", back_populates="assets")
    task = relationship("GenerationTask")


class AutoImageEvent(Base):
    """Durable, user-visible progress log for an automatic image batch."""

    __tablename__ = "auto_image_events"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(
        Integer, ForeignKey("auto_image_batches.id"), nullable=False, index=True
    )
    event_type = Column(String(48), nullable=False)
    level = Column(String(16), nullable=False, default="info")
    message = Column(Text, nullable=False)
    duration_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    batch = relationship("AutoImageBatch", back_populates="events")


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


class CustomModelConfig(Base):
    """用户自定义模型配置（自带 API Key，加密存储）。"""

    __tablename__ = "custom_model_configs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(64), default="自定义模型", nullable=False)  # 显示名
    base_url = Column(String(255), nullable=False)                  # OpenAI 兼容服务地址
    model = Column(String(128), nullable=False)                     # 模型名
    api_key_encrypted = Column(Text, nullable=False)                # 加密后的 API Key
    created_at = Column(DateTime, default=datetime.utcnow)


class DirectorSkill(Base):
    """A reusable, user-authored set of storyboard guidance."""

    __tablename__ = "director_skills"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(64), nullable=False)
    instructions = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class RunningHubConfig(Base):
    """单实例 RunningHub 工作流配置（API Key 加密存储）。"""

    __tablename__ = "runninghub_config"

    id = Column(Integer, primary_key=True, default=1)
    workflow_id = Column(String(128), nullable=False)
    prompt_node_id = Column(String(128), nullable=False)
    prompt_field = Column(String(128), nullable=False)
    negative_prompt_node_id = Column(String(128), nullable=True)
    negative_prompt_field = Column(String(128), nullable=True)
    api_key_encrypted = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AgentMemory(Base):
    """单实例个人 ComfyUI 偏好记忆（固定 singleton id，不按账号拆分）。"""

    __tablename__ = "agent_memory"

    id = Column(Integer, primary_key=True, default=1)
    preferences = Column(Text, nullable=False, default="{}")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
