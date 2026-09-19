"""Pydantic 请求 / 响应模型。"""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class GenerateRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4000, description="正向提示词")
    negative_prompt: str = Field("", max_length=4000, description="负向提示词")
    width: int = Field(1024, ge=256, le=2048)
    height: int = Field(1024, ge=256, le=2048)
    steps: int = Field(24, ge=1, le=150)
    cfg: float = Field(3.5, ge=0.0, le=30.0)
    seed: int = Field(0, ge=-1, description="-1 表示随机")


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
    width: int
    height: int
    steps: int
    cfg: float
    seed: int
    status: str
    error: Optional[str] = None
    comfy_prompt_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    images: List[ImageOut] = []

    model_config = {"from_attributes": True}
