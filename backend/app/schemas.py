"""Pydantic 请求 / 响应模型。"""
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
