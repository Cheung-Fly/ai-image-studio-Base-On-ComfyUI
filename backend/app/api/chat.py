"""LLM 对话接口（后端代理）。"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import User
from ..schemas import ChatRequest, ChatResponse
from ..services.llm_client import LLMError
from ..services.llm_client import chat as llm_chat
from ..throttle import check_and_increment_chat_quota, check_rate_limit
from .auth import get_current_user
from .conversations import persist_message

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
def chat_endpoint(
    req: ChatRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """对话：转发到 LLM，返回回复文本（需登录，受限流与额度控制，消息落库）。"""
    check_rate_limit(user)
    check_and_increment_chat_quota(user, db)

    history = [{"role": m.role, "content": m.content} for m in req.history]
    try:
        reply = llm_chat(req.message, history, provider=req.provider)
    except LLMError as exc:
        # 完整错误记入服务端日志，仅向前端返回脱敏信息，避免泄露内部地址/key/模型细节
        logger.error("LLM 调用失败: %s", exc)
        raise HTTPException(502, "LLM 服务暂不可用，请稍后重试") from exc

    # 落库：用户消息 + 助手回复
    persist_message(db, user.id, req.provider, "user", req.message)
    persist_message(db, user.id, req.provider, "assistant", reply)
    return ChatResponse(reply=reply)
