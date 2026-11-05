"""Pydantic 请求 / 响应模型。"""
import math
from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

# ResolutionSelector 节点支持的宽高比选项（与 ComfyUI 保持一致）
ASPECT_RATIO_OPTIONS = {
    "1:1 (Square)",
    "2:3 (Portrait Photo)",
    "3:2 (Photo)",
    "3:4 (Portrait Standard)",
    "4:3 (Standard)",
    "9:16 (Portrait Widescreen)",
    "16:9 (Widescreen)",
    "21:9 (Ultrawide)",
}


class LoraRef(BaseModel):
    """单个 LoRA 引用（文件名 + 强度）。"""

    name: str = Field(..., min_length=1, max_length=255, description="LoRA 文件名（含扩展名）")
    strength: float = Field(0.7, ge=-5.0, le=5.0, description="LoRA 强度")


class GenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4000, description="正向提示词")
    negative_prompt: str = Field("", max_length=4000, description="负向提示词")
    aspect_ratio: str = Field("1:1 (Square)", description="宽高比（ComfyUI ResolutionSelector）")
    megapixels: float = Field(1.0, ge=0.1, le=16.0, description="目标百万像素")
    seed: int = Field(0, ge=-1, description="-1 表示随机")
    workflow: str = Field("", max_length=255, description="工作流文件名（空=默认）")
    model_preset: str = Field("", max_length=255, description="模型预设名（空=工作流默认）")
    steps: int = Field(8, ge=1, le=100, description="主生成采样步数（阶段一）")
    refine_steps: int = Field(4, ge=1, le=100, description="放大精修步数（阶段二）")
    loras: list[LoraRef] = Field([], description="LoRA 列表（空则直连 UNET）")

    @field_validator("aspect_ratio")
    @classmethod
    def _check_aspect_ratio(cls, v: str) -> str:
        if v not in ASPECT_RATIO_OPTIONS:
            raise ValueError(f"非法宽高比: {v}，可选值: {sorted(ASPECT_RATIO_OPTIONS)}")
        return v


class ImageOut(BaseModel):
    id: int
    task_id: int
    filename: str
    media_type: str = "image"   # image / video
    file_url: str
    width: int | None = None
    height: int | None = None
    created_at: datetime

    model_config = {"from_attributes": True}

class TaskOut(BaseModel):
    id: int
    task_type: str
    prompt: str
    negative_prompt: str
    workflow: str
    model_preset: str
    aspect_ratio: str
    megapixels: float
    seed: int
    status: str
    error: str | None = None
    error_code: str | None = None
    retry_count: int
    manual_retry_count: int
    comfy_prompt_id: str | None = None
    created_at: datetime
    updated_at: datetime
    images: list[ImageOut] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class TaskEventOut(BaseModel):
    id: int
    task_id: int
    event_type: str
    status: str
    message: str
    error_code: str | None = None
    attempt: int
    created_at: datetime

    model_config = {"from_attributes": True}


class TaskActionOut(BaseModel):
    task: TaskOut
    message: str
    celery_revoke: str
    comfy_stop: str


class GenerateVideoRequest(BaseModel):
    """视频生成请求（MiniMax H3 Ref2VA）。"""

    prompt: str = Field(
        ...,
        min_length=1,
        max_length=8000,
        description="提示词（可用 [image N]/[video N]/[audio N] 引用素材）",
    )
    duration: float = Field(4.0, ge=2.0, le=15.0, description="视频时长（秒）")
    aspect_ratio: str = Field("16:9 (Widescreen)", description="宽高比")
    megapixels: float = Field(1.0, ge=0.1, le=16.0, description="目标百万像素")
    seed: int = Field(0, ge=-1, description="-1 表示随机")
    steps: int = Field(20, ge=1, le=100, description="基本调度器采样步数")
    loras: list[LoraRef] = Field([], description="LoRA 列表（空则直连 UNET，不加载 LoRA）")
    ref_images: list[str] = Field([], description="参考图文件名列表（上传接口返回的文件名）")
    ref_videos: list[str] = Field([], description="参考视频文件名列表")
    ref_audios: list[str] = Field([], description="参考音频文件名列表")


class ChatMessage(BaseModel):
    """单条对话消息（用于 history）。"""

    role: str = Field(..., pattern="^(user|assistant)$", description="角色：user / assistant")
    content: str = Field("", max_length=8000)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000, description="当前用户消息")
    history: list[ChatMessage] = Field([], max_length=20, description="历史对话（最多 20 条）")
    provider: str = Field("openai", pattern="^(openai|llama|custom)$", description="LLM 服务")
    image_base64: str | None = Field(None, description="可选：图片的 data URL（多模态看图）")
    use_rag: bool = Field(False, description="是否启用知识库检索增强（RAG）")
    custom_config_id: int | None = Field(
        None, description="自定义模型配置 ID（provider=custom 时使用）"
    )


class ChatResponse(BaseModel):
    """LLM 对话回复。"""

    reply: str


class MessageOut(BaseModel):
    """单条消息（历史拉取用）。"""

    role: str
    content: str

    model_config = {"from_attributes": True}


class ConversationOut(BaseModel):
    """会话历史。"""

    provider: str
    messages: list[MessageOut] = []


class WorkflowOut(BaseModel):
    """可选工作流条目（供前端侧边栏渲染）。"""

    file: str
    is_default: bool


class ModelPresetOut(BaseModel):
    """模型组合预设条目（供前端下拉渲染）。"""

    name: str
    display_name: str


class UserProfileOut(BaseModel):
    """个人主页资料。"""

    username: str
    created_at: datetime
    task_count: int
    image_count: int


class UploadOut(BaseModel):
    """素材上传结果。"""

    filename: str
    media_type: str  # image / video / audio
    size: int


class CustomModelIn(BaseModel):
    """保存自定义模型配置的请求体（含 API Key，后端加密后落库）。"""

    name: str = Field("自定义模型", max_length=64, description="显示名")
    base_url: str = Field(..., min_length=1, max_length=255, description="OpenAI 兼容服务地址")
    model: str = Field(..., min_length=1, max_length=128, description="模型名")
    api_key: str = Field(..., min_length=1, max_length=512, description="API Key")


class RunningHubConfigIn(BaseModel):
    """RunningHub API Key 与自定义工作流提示词节点映射。"""

    api_key: str | None = Field(None, max_length=512, description="留空时保留已保存的 API Key")
    workflow_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    prompt_node_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    prompt_field: str = Field(..., min_length=1, max_length=128)
    negative_prompt_node_id: str | None = Field(
        None, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )
    negative_prompt_field: str | None = Field(None, max_length=128)

    @field_validator("api_key")
    @classmethod
    def _strip_api_key(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def _validate_negative_mapping(self):
        if bool(self.negative_prompt_node_id) != bool(self.negative_prompt_field):
            raise ValueError("负向提示词节点 ID 与字段名必须同时填写或同时留空")
        return self


class RunningHubWorkflowNodesRequest(BaseModel):
    """查询 RunningHub 工作流的可映射文本输入。"""

    api_key: str | None = Field(None, max_length=512)
    workflow_id: str | None = Field(
        None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )

    @field_validator("api_key")
    @classmethod
    def _strip_optional_api_key(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class RunningHubWorkflowSelection(BaseModel):
    """A module-specific workflow selection using the saved RunningHub API key."""

    workflow_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    prompt_node_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    prompt_field: str = Field(..., min_length=1, max_length=128)
    negative_prompt_node_id: str | None = Field(
        None, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
    )
    negative_prompt_field: str | None = Field(None, max_length=128)

    @model_validator(mode="after")
    def _validate_negative_mapping(self):
        if bool(self.negative_prompt_node_id) != bool(self.negative_prompt_field):
            raise ValueError("负向提示词节点 ID 与字段名必须同时填写或同时留空")
        return self


class AgentGenerateRequest(BaseModel):
    """对话式 ComfyUI Agent 请求。"""

    request: str = Field(..., min_length=1, max_length=2000, description="用户的自然语言描述")
    history: list[ChatMessage] = Field([], max_length=20, description="最近对话上下文")
    provider: str = Field(
        "",
        pattern="^(|openai|llama|custom)$",
        description="LLM provider；空值使用服务端默认 provider",
    )
    custom_config_id: int | None = Field(None, description="自定义模型配置 ID")
    use_rag: bool = Field(False, description="是否将知识库检索结果作为 Agent 参考资料")
    quality_review_enabled: bool = Field(
        False, description="是否在生成后运行视觉质量审查 Harness"
    )
    generation_backend: str = Field(
        "comfyui",
        pattern="^(comfyui|runninghub)$",
        description="生成后端，与 LLM provider 分开选择",
    )


class AgentConfirmationRequest(BaseModel):
    """MCP Agent 有影响操作的用户确认。"""

    confirmation_id: str = Field(..., min_length=1, max_length=64)
    confirmed: bool


class DirectorStoryboardRequest(BaseModel):
    """导演 Agent 的单集分镜生成请求。"""

    script: str = Field(..., min_length=1, max_length=50000)
    video_title: str = Field("", max_length=128)
    provider: str = Field("openai", pattern="^(openai|llama|custom)$")
    custom_config_id: int | None = Field(None, gt=0)
    skill_ids: list[int] = Field(default_factory=list, max_length=10)

    model_config = {"extra": "forbid"}

    @field_validator("script")
    @classmethod
    def _strip_director_script(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("请先填写剧本")
        return value

    @field_validator("video_title")
    @classmethod
    def _strip_director_title(cls, value: str) -> str:
        return value.strip()

    @field_validator("skill_ids")
    @classmethod
    def _validate_director_skill_ids(cls, value: list[int]) -> list[int]:
        if any(skill_id <= 0 for skill_id in value):
            raise ValueError("Skill ID 必须为正整数")
        if len(set(value)) != len(value):
            raise ValueError("Skill 不能重复选择")
        return value


class DirectorSkillIn(BaseModel):
    """A reusable storyboard skill supplied by the current user."""

    name: str = Field(..., min_length=1, max_length=64)
    instructions: str = Field(..., min_length=1, max_length=10000)

    @field_validator("name", "instructions")
    @classmethod
    def _strip_director_skill_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Skill 名称和指导内容不能为空")
        return value

    model_config = {"extra": "forbid"}


class DirectorSkillOut(BaseModel):
    """A saved storyboard skill."""

    id: int
    name: str
    instructions: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DirectorStoryboardShot(BaseModel):
    """单个分镜行；字段对应 Excel 样表。"""

    shot_no: str = Field(..., min_length=1, max_length=32)
    framing: str = Field("", max_length=64)
    camera_movement: str = Field("", max_length=128)
    visual_description: str = Field(..., min_length=1, max_length=3000)
    duration_seconds: float = Field(..., gt=0, le=300)
    dialogue: str = Field("", max_length=2000)
    sound_effects: str = Field("", max_length=1000)
    prompt: str = Field("", max_length=32767)
    producer: str = Field("", max_length=128)
    expected_submission_time: str = Field("", max_length=128)
    reviewer: str = Field("", max_length=128)
    actual_submission_time: str = Field("", max_length=128)

    @field_validator("shot_no", mode="before")
    @classmethod
    def _normalize_shot_no(cls, value: object) -> str:
        return str(value).strip() if value is not None else ""

    @field_validator("duration_seconds")
    @classmethod
    def _finite_duration(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("镜头时长必须是有限数值")
        return value

    model_config = {"extra": "ignore"}


class DirectorStoryboardOut(BaseModel):
    """导演 Agent 输出的单集分镜表。"""

    video_title: str = Field(..., min_length=1, max_length=128)
    theme: str = Field("", max_length=1000)
    shooting_time: str = Field("", max_length=128)
    deadline: str = Field("", max_length=128)
    props: str = Field("", max_length=1000)
    characters: str = Field("", max_length=1000)
    shots: list[DirectorStoryboardShot] = Field(..., min_length=1, max_length=120)

    model_config = {"extra": "ignore"}


class VideoEpisodeAssetOut(BaseModel):
    id: str
    filename: str
    media_type: str
    size: int
    file_url: str


class VideoEpisodeShotOut(BaseModel):
    shot_index: int
    shot_no: str
    framing: str
    camera_movement: str
    visual_description: str
    duration_seconds: float
    dialogue: str
    sound_effects: str
    prompt: str
    asset_ids: list[str] = Field(default_factory=list)
    suggested_asset_ids: list[str] = Field(default_factory=list)
    match_confidence: float = 0.0
    match_source: str = "agent"
    status: str
    segment_count: int = 0
    error: str | None = None


class VideoEpisodeOut(BaseModel):
    id: int
    title: str
    status: str
    assets: list[VideoEpisodeAssetOut] = Field(default_factory=list)
    shots: list[VideoEpisodeShotOut] = Field(default_factory=list)
    aspect_ratio: str
    megapixels: float
    generation_backend: str = "comfyui"
    workflow_config: dict = Field(default_factory=dict)
    current_shot_index: int
    total_shots: int
    progress_message: str
    mapping_warning: str | None = None
    error: str | None = None
    output_url: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class VideoEpisodeSummaryOut(BaseModel):
    id: int
    title: str
    status: str
    current_shot_index: int
    total_shots: int
    progress_message: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class VideoEpisodeShotStart(BaseModel):
    shot_no: str = Field(..., min_length=1, max_length=32)
    duration_seconds: float = Field(..., gt=0, le=300)
    asset_ids: list[str] = Field(default_factory=list, max_length=15)

    @field_validator("duration_seconds")
    @classmethod
    def _finite_duration(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("镜头时长必须是有限数值")
        return value


class VideoEpisodeStartRequest(BaseModel):
    aspect_ratio: str = Field("16:9 (Widescreen)")
    megapixels: float = Field(1.0, ge=0.1, le=16.0)
    generation_backend: str = Field("comfyui", pattern="^(comfyui|runninghub)$")
    workflow_file: str = Field("", max_length=255)
    runninghub: RunningHubWorkflowSelection | None = None
    shots: list[VideoEpisodeShotStart] = Field(..., min_length=1, max_length=120)

    @field_validator("aspect_ratio")
    @classmethod
    def _check_episode_aspect_ratio(cls, value: str) -> str:
        if value not in ASPECT_RATIO_OPTIONS:
            raise ValueError(f"非法宽高比: {value}，可选值: {sorted(ASPECT_RATIO_OPTIONS)}")
        return value

    @field_validator("megapixels")
    @classmethod
    def _finite_megapixels(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("百万像素必须是有限数值")
        return value

    @model_validator(mode="after")
    def _check_workflow_selection(self):
        if self.generation_backend == "runninghub" and self.runninghub is None:
            raise ValueError("RunningHub 制作需要配置兼容的视频工作流")
        if self.generation_backend == "comfyui" and self.runninghub is not None:
            raise ValueError("ComfyUI 制作不能携带 RunningHub 工作流配置")
        return self


class AutoImageEpisodeOption(BaseModel):
    episode_index: int
    title: str
    shot_count: int


class AutoImageAssetOut(BaseModel):
    id: int
    asset_key: str
    category: str
    name: str
    episode_indices: list[int]
    prompt: str
    status: str
    error: str | None = None
    task_id: int | None = None
    image_url: str | None = None


class AutoImageEventOut(BaseModel):
    id: int
    event_type: str
    level: str
    message: str
    duration_ms: int | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class AutoImageBatchOut(BaseModel):
    id: int
    title: str
    source_filename: str
    episode_count: int
    selected_episode_index: int
    status: str
    episodes: list[AutoImageEpisodeOption]
    assets: list[AutoImageAssetOut]
    logs: list[AutoImageEventOut]
    completed_assets: int
    total_assets: int
    created_at: datetime
    updated_at: datetime


class AutoImageStartRequest(BaseModel):
    episode_index: int = Field(..., ge=1, le=100)
    generation_backend: str = Field("comfyui", pattern="^(comfyui|runninghub)$")
    workflow_file: str = Field("", max_length=255)
    model_preset: str = Field("", max_length=255)
    aspect_ratio: str = Field("3:2 (Photo)")
    megapixels: float = Field(1.0, ge=0.1, le=16.0)
    seed: int = Field(-1, ge=-1)
    steps: int = Field(8, ge=1, le=100)
    refine_steps: int = Field(4, ge=1, le=100)
    runninghub: RunningHubWorkflowSelection | None = None

    @field_validator("aspect_ratio")
    @classmethod
    def _check_auto_image_aspect_ratio(cls, value: str) -> str:
        if value not in ASPECT_RATIO_OPTIONS:
            raise ValueError(f"非法宽高比: {value}，可选值: {sorted(ASPECT_RATIO_OPTIONS)}")
        return value

    @field_validator("megapixels")
    @classmethod
    def _finite_auto_image_megapixels(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("百万像素必须是有限数值")
        return value

    @model_validator(mode="after")
    def _check_auto_image_workflow_selection(self):
        if self.generation_backend == "runninghub" and self.runninghub is None:
            raise ValueError("RunningHub 生图需要配置工作流和提示词输入")
        if self.generation_backend == "comfyui" and self.runninghub is not None:
            raise ValueError("ComfyUI 生图不能携带 RunningHub 工作流配置")
        return self


class VideoSeriesEpisodeOut(BaseModel):
    folder_name: str
    episode: VideoEpisodeOut


class VideoSeriesOut(BaseModel):
    id: int
    title: str
    status: str
    total_episodes: int
    current_episode_index: int
    progress_message: str
    error: str | None = None
    episodes: list[VideoSeriesEpisodeOut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class VideoSeriesSummaryOut(BaseModel):
    id: int
    title: str
    status: str
    total_episodes: int
    current_episode_index: int
    progress_message: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class VideoSeriesEpisodeStart(BaseModel):
    episode_id: int = Field(..., gt=0)
    settings: VideoEpisodeStartRequest


class VideoSeriesStartRequest(BaseModel):
    episodes: list[VideoSeriesEpisodeStart] = Field(..., min_length=1, max_length=100)


class AgentMemoryUpdate(BaseModel):
    """个人 ComfyUI 偏好记忆的局部更新。"""

    style: str | None = Field(None, max_length=500, description="偏好画风")
    model_preset: str | None = Field(None, max_length=255, description="默认模型预设")
    workflow: str | None = Field(None, max_length=255, description="默认工作流文件名")
    aspect_ratio: str | None = Field(None, description="默认宽高比")
    megapixels: float | None = Field(None, ge=0.1, le=16.0, description="默认百万像素")
    negative_prompt: str | None = Field(None, max_length=4000, description="默认负向提示词")
    notes: str | None = Field(None, max_length=2000, description="其他生成习惯")

    model_config = {"extra": "forbid"}

    @field_validator("aspect_ratio")
    @classmethod
    def _check_memory_aspect_ratio(cls, value: str | None) -> str | None:
        if value is not None and value not in ASPECT_RATIO_OPTIONS:
            raise ValueError(f"非法宽高比: {value}，可选值: {sorted(ASPECT_RATIO_OPTIONS)}")
        return value


class AgentMemoryOut(BaseModel):
    """完整个人偏好记忆响应。"""

    style: str = ""
    model_preset: str = ""
    workflow: str = ""
    aspect_ratio: str | None = None
    megapixels: float | None = None
    negative_prompt: str = ""
    notes: str = ""
    updated_at: datetime | None = None
