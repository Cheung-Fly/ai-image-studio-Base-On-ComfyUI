"""LLM 对话接口（后端代理）。"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..database import SessionLocal, get_db
from ..models import CustomModelConfig, User
from ..schemas import ChatRequest, ChatResponse
from ..security import decrypt_secret
from ..services.llm_client import LLMError, chat_with_config, stream_chat_with_config
from ..services.llm_client import chat as llm_chat
from ..services.llm_client import stream_chat as llm_stream_chat
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
        if req.provider == "custom":
            cfg = db.get(CustomModelConfig, req.custom_config_id) if req.custom_config_id else None
            if cfg is None or cfg.user_id != user.id:
                raise HTTPException(400, "自定义模型配置无效")
            try:
                api_key = decrypt_secret(cfg.api_key_encrypted)
            except ValueError as exc:
                raise LLMError("自定义模型 API Key 解密失败") from exc
            reply = chat_with_config(
                req.message,
                cfg.base_url,
                api_key,
                cfg.model,
                history,
                req.image_base64,
            )
        elif req.use_rag:
            from ..services import rag

            rag.ensure_index()
            contexts = rag.retrieve(req.message)
            prompt = rag.build_rag_prompt(req.message, contexts)
            reply = llm_chat(prompt, None, provider=req.provider)
        else:
            reply = llm_chat(
                req.message,
                history,
                provider=req.provider,
                image_base64=req.image_base64,
            )
    except LLMError as exc:
        # 完整错误记入服务端日志，仅向前端返回脱敏信息，避免泄露内部地址/key/模型细节
        logger.error("LLM 调用失败: %s", exc)
        raise HTTPException(502, "LLM 服务暂不可用，请稍后重试") from exc

    # 落库：仅内置模型（openai/llama）持久化消息；自定义模型对话仅存前端，不落库
    if req.provider in ("openai", "llama"):
        persist_message(db, user.id, req.provider, "user", req.message)
        persist_message(db, user.id, req.provider, "assistant", reply)
    return ChatResponse(reply=reply)


@router.post("/chat/stream")
def stream_chat_endpoint(
    req: ChatRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """以 SSE 返回逐段生成的文本；流式错误通过 error 事件报告。"""
    check_rate_limit(user)
    check_and_increment_chat_quota(user, db)

    history = [{"role": m.role, "content": m.content} for m in req.history]
    if req.provider == "custom":
        cfg = db.get(CustomModelConfig, req.custom_config_id) if req.custom_config_id else None
        if cfg is None or cfg.user_id != user.id:
            raise HTTPException(400, "自定义模型配置无效")
        try:
            api_key = decrypt_secret(cfg.api_key_encrypted)
        except ValueError as exc:
            error = LLMError("自定义模型 API Key 解密失败")
            logger.error("LLM 调用失败: %s", error)
            raise HTTPException(502, "LLM 服务暂不可用，请稍后重试") from exc
        chunks = stream_chat_with_config(
            req.message,
            cfg.base_url,
            api_key,
            cfg.model,
            history,
            req.image_base64,
        )
    elif req.use_rag:
        from ..services import rag

        rag.ensure_index()
        contexts = rag.retrieve(req.message)
        prompt = rag.build_rag_prompt(req.message, contexts)
        chunks = llm_stream_chat(prompt, None, provider=req.provider)
    else:
        chunks = llm_stream_chat(
            req.message,
            history,
            provider=req.provider,
            image_base64=req.image_base64,
        )

    def event_stream():
        reply_parts: list[str] = []
        try:
            for chunk in chunks:
                reply_parts.append(chunk)
                yield f"data: {json.dumps({'delta': chunk}, ensure_ascii=False)}\n\n"
        except LLMError as exc:
            logger.error("LLM 流式调用失败: %s", exc)
            error = json.dumps(
                {"detail": "LLM 服务暂不可用，请稍后重试"},
                ensure_ascii=False,
            )
            yield f"event: error\ndata: {error}\n\n"
            return

        reply = "".join(reply_parts)
        if req.provider in ("openai", "llama"):
            persist_db = SessionLocal()
            try:
                persist_message(persist_db, user.id, req.provider, "user", req.message)
                persist_message(persist_db, user.id, req.provider, "assistant", reply)
            finally:
                persist_db.close()
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
