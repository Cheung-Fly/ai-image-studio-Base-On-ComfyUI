"""Pydantic 请求 / 响应模型。"""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


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


class GenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4000, description="正向提示词")
    negative_prompt: str = Field("", max_length=4000, description="负向提示词")
    aspect_ratio: str = Field("1:1 (Square)", description="宽高比（作用于 ComfyUI ResolutionSelector）")
    megapixels: float = Field(1.0, ge=0.1, le=16.0, description="目标百万像素（作用于 ComfyUI ResolutionSelector）")
    seed: int = Field(0, ge=-1, description="-1 表示随机")

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
    file_url: str
    width: Optional[int] = None
    height: Optional[int] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class TaskOut(BaseModel):
    id: int
    prompt: str
    negative_prompt: str
    aspect_ratio: str
    megapixels: float
    seed: int
    status: str
    error: Optional[str] = None
    comfy_prompt_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    images: List[ImageOut] = []

    model_config = {"from_attributes": True}


class ChatMessage(BaseModel):
    """单条对话消息（用于 history）。"""

    role: str = Field(..., pattern="^(user|assistant)$", description="角色：user / assistant")
    content: str = Field("", max_length=8000)


class ChatRequest(BaseModel):
    """LLM 对话请求。"""

    message: str = Field(..., min_length=1, max_length=8000, description="当前用户消息")
    history: List[ChatMessage] = Field([], max_length=20, description="历史对话（最多 20 条）")
    provider: str = Field("openai", pattern="^(openai|llama)$", description="LLM 服务：openai / llama")


class ChatResponse(BaseModel):
    """LLM 对话回复。"""

    reply: str


class RegisterRequest(BaseModel):
    """注册请求。"""

    username: str = Field(..., min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_]+$")
    password: str = Field(..., min_length=8, max_length=128)


class LoginRequest(BaseModel):
    """登录请求。"""

    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class TokenResponse(BaseModel):
    """登录/注册成功返回 token。"""

    access_token: str
    token_type: str = "bearer"
    username: str


class MessageOut(BaseModel):
    """单条消息（历史拉取用）。"""

    role: str
    content: str

    model_config = {"from_attributes": True}


class ConversationOut(BaseModel):
    """会话历史。"""

    provider: str
    messages: List[MessageOut] = []
