"""对话历史接口：按 provider 拉取/清空历史。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Conversation, Message, User
from ..schemas import ConversationOut
from .auth import get_current_user

router = APIRouter(tags=["conversations"])

SUPPORTED_PROVIDERS = {"openai", "llama"}


def _get_or_create_conversation(db: Session, user_id: int, provider: str) -> Conversation:
    conv = (
        db.query(Conversation)
        .filter(Conversation.user_id == user_id, Conversation.provider == provider)
        .first()
    )
    if conv is None:
        conv = Conversation(user_id=user_id, provider=provider)
        db.add(conv)
        db.commit()
        db.refresh(conv)
    return conv


@router.get("/conversations/{provider}", response_model=ConversationOut)
def get_conversation(
    provider: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """拉取当前用户在指定 provider 下的对话历史（含全部消息，按时间顺序）。"""
    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(400, f"不支持的 provider: {provider}")
    conv = (
        db.query(Conversation)
        .filter(Conversation.user_id == user.id, Conversation.provider == provider)
        .first()
    )
    if conv is None:
        return ConversationOut(provider=provider, messages=[])
    return ConversationOut(
        provider=provider,
        messages=[{"role": m.role, "content": m.content} for m in conv.messages],
    )


@router.delete("/conversations/{provider}", status_code=204)
def clear_conversation(
    provider: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """清空当前用户在指定 provider 下的对话历史。"""
    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(400, f"不支持的 provider: {provider}")
    conv = (
        db.query(Conversation)
        .filter(Conversation.user_id == user.id, Conversation.provider == provider)
        .first()
    )
    if conv is not None:
        db.delete(conv)
        db.commit()
    return


def persist_message(db: Session, user_id: int, provider: str, role: str, content: str) -> None:
    """落库一条消息（供 chat 接口复用）。"""
    conv = _get_or_create_conversation(db, user_id, provider)
    db.add(Message(conversation_id=conv.id, role=role, content=content))
    db.commit()
