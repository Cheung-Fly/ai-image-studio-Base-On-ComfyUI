"""Director Agent endpoints for screenplay-to-storyboard work."""

import logging
from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import CustomModelConfig, User
from ..schemas import DirectorStoryboardOut, DirectorStoryboardRequest
from ..security import decrypt_secret
from ..services.director_agent import (
    DirectorOutputError,
    content_disposition,
    export_filename,
    export_storyboard_xlsx,
    generate_storyboard,
)
from ..services.llm_client import LLMError
from ..throttle import check_and_increment_chat_quota, check_rate_limit
from .auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/director", tags=["director"])


@router.post("/storyboard", response_model=DirectorStoryboardOut)
def create_storyboard(
    req: DirectorStoryboardRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Generate a single-episode storyboard with the selected LLM."""
    check_rate_limit(user)

    custom_config = None
    if req.provider == "custom":
        config = (
            db.get(CustomModelConfig, req.custom_config_id)
            if req.custom_config_id
            else None
        )
        if config is None or config.user_id != user.id:
            raise HTTPException(400, "自定义模型配置无效")
        try:
            api_key = decrypt_secret(config.api_key_encrypted)
        except ValueError as exc:
            raise HTTPException(502, "自定义模型 API Key 解密失败") from exc
        custom_config = {
            "base_url": config.base_url,
            "api_key": api_key,
            "model": config.model,
        }

    check_and_increment_chat_quota(user, db)
    try:
        return generate_storyboard(
            req.script,
            req.video_title,
            req.provider,
            custom_config,
        )
    except LLMError as exc:
        logger.error("导演 Agent LLM 调用失败: %s", exc)
        raise HTTPException(502, "导演模型暂不可用，请检查模型配置后重试") from exc
    except DirectorOutputError as exc:
        logger.warning("导演 Agent 输出结构无效: %s", exc)
        raise HTTPException(
            502,
            "模型未返回有效分镜表，请缩短剧本或重试",
        ) from exc


@router.post("/export")
def export_storyboard(
    storyboard: DirectorStoryboardOut,
    _user: User = Depends(get_current_user),
):
    """Download an editable Excel workbook matching the supplied storyboard."""
    filename = export_filename(storyboard.video_title)
    content = export_storyboard_xlsx(storyboard)
    return StreamingResponse(
        BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(filename)},
    )
