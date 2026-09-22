"""模型组合预设列表接口（供前端下拉渲染）。"""

from fastapi import APIRouter, Depends

from ..models import User
from ..schemas import ModelPresetOut
from ..services.model_preset import list_presets
from .auth import get_current_user

router = APIRouter(tags=["model-presets"])


@router.get("/model-presets", response_model=list[ModelPresetOut])
def get_model_presets(user: User = Depends(get_current_user)):
    """列出可用模型组合预设（需登录）。"""
    return [ModelPresetOut(**p) for p in list_presets()]
