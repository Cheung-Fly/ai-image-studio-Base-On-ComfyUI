"""LLM 对话客户端：多 provider 支持（OpenAI 兼容 + llama.cpp）。

设计目标：后端代理 LLM 调用，运行时按 provider 切换，前端只跟本服务通信。
- provider="openai"：走 OpenAI 兼容协议（OpenAI / DeepSeek / 通义 / vLLM / Ollama 等）。
- provider="llama"：走本地 llama.cpp server（需显式 max_tokens、兼容 choices[0].text）。
- 未配置对应 provider 的 api_key 时，chat() 返回占位回复（保证前端链路可先跑通）。

RAG / 提示词增强可复用 chat() 或 stream_chat() 的 provider 调用能力。
"""
import json
from collections.abc import Iterator
from typing import Any

import httpx

from ..config import settings

SUPPORTED_PROVIDERS = {"openai", "llama"}


class LLMError(Exception):
    """LLM 调用失败。"""

def _build_messages(
    message: str,
    history: list[dict[str, str]] | None = None,
    image_base64: str | None = None,
) -> list[dict[str, Any]]:
    """把前端传来的 history + 当前消息拼成 messages 列表。

    当 image_base64 非空时，当前用户消息用 OpenAI 多模态 content 数组格式
    （image_url + text），否则用纯文本字符串。
    """
    messages: list[dict[str, Any]] = []
    for item in history or []:
        role = item.get("role")
        content = item.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    if image_base64:
        messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_base64}},
                    {"type": "text", "text": message},
                ],
            }
        )
    else:
        messages.append({"role": "user", "content": message})
    return messages



def _resolve(provider: str) -> dict[str, Any]:
    """根据 provider 名返回对应的 base_url/api_key/model 配置。"""
    if provider == "llama":
        return {
            "base_url": settings.llama_base_url,
            "api_key": settings.llama_api_key,
            "model": settings.llama_model,
        }
    # 默认 openai
    return {
        "base_url": settings.openai_base_url,
        "api_key": settings.openai_api_key,
        "model": settings.openai_model,
    }


def chat(
    message: str,
    history: list[dict[str, str]] | None = None,
    provider: str = "",
    image_base64: str | None = None,
) -> str:
    """调用指定 provider 的 LLM，返回回复文本。"""
    provider = provider or settings.default_llm_provider
    if provider not in SUPPORTED_PROVIDERS:
        raise LLMError(f"不支持的 provider: {provider}（可选: {sorted(SUPPORTED_PROVIDERS)}）")

    cfg = _resolve(provider)
    base_url = cfg["base_url"].rstrip("/")
    api_key = cfg["api_key"]

    # 未配置 API key：返回占位回复，保证前端链路可先跑通
    if not api_key:
        return (
            f"[LLM 占位回复] 当前 provider「{provider}」尚未配置 API Key，"
            f"已收到你的消息：{message!r}。配置后即可调用真实模型。"
        )

    messages = _build_messages(message, history, image_base64=image_base64)

    payload: dict[str, Any] = {
        "model": cfg["model"],
        "messages": messages,
    }

    # llama.cpp server 适配：部分版本要求显式 max_tokens，否则报错
    if provider == "llama":
        payload["max_tokens"] = settings.llama_max_tokens

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    url = base_url + "/chat/completions"
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=300.0)
    except httpx.HTTPError as exc:
        raise LLMError(f"无法连接 LLM 服务（{url}）: {exc}") from exc

    if resp.status_code != 200:
        raise LLMError(f"LLM 返回 {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError) as exc:
        raise LLMError(f"LLM 响应格式异常: {data}") from exc

    # 标准 OpenAI 结构：choices[0].message.content
    msg = choice.get("message") or {}
    content = msg.get("content")
    if content is not None:
        return content

    # llama.cpp server 兼容：部分版本把文本放在 choices[0].text
    text = choice.get("text")
    if text is not None:
        return text

    raise LLMError(f"LLM 响应缺少内容字段: {data}")


def chat_with_config(
    message: str,
    base_url: str,
    api_key: str,
    model: str,
    history: list[dict[str, str]] | None = None,
    image_base64: str | None = None,
) -> str:
    """用运行时传入的自定义配置调用 OpenAI 兼容 LLM（用于用户自带模型）。

    与 chat() 的区别：base_url/api_key/model 来自请求而非环境变量。
    """
    base_url = base_url.rstrip("/")
    if not api_key:
        raise LLMError("自定义模型未提供 API Key")

    messages = _build_messages(message, history, image_base64=image_base64)
    payload: dict[str, Any] = {"model": model, "messages": messages}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    url = base_url + "/chat/completions"
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=300.0)
    except httpx.HTTPError as exc:
        raise LLMError(f"无法连接 LLM 服务（{url}）: {exc}") from exc

    if resp.status_code != 200:
        raise LLMError(f"LLM 返回 {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError) as exc:
        raise LLMError(f"LLM 响应格式异常: {data}") from exc

    msg = choice.get("message") or {}
    content = msg.get("content")
    if content is not None:
        return content
    text = choice.get("text")
    if text is not None:
        return text
    raise LLMError(f"LLM 响应缺少内容字段: {data}")


def _choice_content(choice: dict[str, Any]) -> str:
    message = choice.get("message") or {}
    delta = choice.get("delta") or {}
    content = message.get("content")
    if content is None:
        content = delta.get("content")
    if content is None:
        content = choice.get("text")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item["text"]
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return ""


def _stream_completion(
    url: str,
    payload: dict[str, Any],
    api_key: str,
) -> Iterator[str]:
    """读取 OpenAI 兼容 SSE 响应；也接受忽略 stream 参数的非流式服务。"""
    stream_payload = {**payload, "stream": True}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        with httpx.stream(
            "POST",
            url,
            json=stream_payload,
            headers=headers,
            timeout=300.0,
        ) as resp:
            if resp.status_code != 200:
                body = resp.read().decode("utf-8", errors="replace")
                raise LLMError(f"LLM 返回 {resp.status_code}: {body[:500]}")

            content_type = resp.headers.get("content-type", "").lower()
            if "text/event-stream" not in content_type:
                try:
                    data = resp.json()
                    choice = data["choices"][0]
                except (KeyError, IndexError, ValueError, TypeError) as exc:
                    raise LLMError(f"LLM 响应格式异常: {resp.text[:300]}") from exc
                content = _choice_content(choice)
                if content:
                    yield content
                return

            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    choices = chunk.get("choices") or []
                    choice = choices[0] if choices else {}
                except (json.JSONDecodeError, TypeError, AttributeError) as exc:
                    raise LLMError("LLM 流式响应格式异常") from exc
                content = _choice_content(choice)
                if content:
                    yield content
    except httpx.HTTPError as exc:
        raise LLMError(f"无法连接 LLM 服务（{url}）: {exc}") from exc


def stream_chat(
    message: str,
    history: list[dict[str, str]] | None = None,
    provider: str = "",
    image_base64: str | None = None,
) -> Iterator[str]:
    """以文本片段形式调用内置 provider。"""
    provider = provider or settings.default_llm_provider
    if provider not in SUPPORTED_PROVIDERS:
        raise LLMError(f"不支持的 provider: {provider}（可选: {sorted(SUPPORTED_PROVIDERS)}）")

    cfg = _resolve(provider)
    if not cfg["api_key"]:
        yield (
            f"[LLM 占位回复] 当前 provider「{provider}」尚未配置 API Key，"
            f"已收到你的消息：{message!r}。配置后即可调用真实模型。"
        )
        return

    payload: dict[str, Any] = {
        "model": cfg["model"],
        "messages": _build_messages(message, history, image_base64=image_base64),
    }
    if provider == "llama":
        payload["max_tokens"] = settings.llama_max_tokens
    yield from _stream_completion(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        payload,
        cfg["api_key"],
    )


def stream_chat_with_config(
    message: str,
    base_url: str,
    api_key: str,
    model: str,
    history: list[dict[str, str]] | None = None,
    image_base64: str | None = None,
) -> Iterator[str]:
    """以流式方式调用运行时传入的自定义 OpenAI 兼容配置。"""
    if not api_key:
        raise LLMError("自定义模型未提供 API Key")
    payload: dict[str, Any] = {
        "model": model,
        "messages": _build_messages(message, history, image_base64=image_base64),
    }
    yield from _stream_completion(
        base_url.rstrip("/") + "/chat/completions",
        payload,
        api_key,
    )


def chat_with_tools(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    provider: str = "",
) -> dict[str, Any]:
    """OpenAI function calling：返回 assistant message（含 content 或 tool_calls）。

    本地 llama.cpp 需以 --jinja 启动 llama-server 才支持 tools。
    """
    provider = provider or settings.default_llm_provider
    if provider not in SUPPORTED_PROVIDERS:
        raise LLMError(f"不支持的 provider: {provider}（可选: {sorted(SUPPORTED_PROVIDERS)}）")
    cfg = _resolve(provider)
    if not cfg["api_key"]:
        raise LLMError(f"provider「{provider}」未配置 API Key，无法运行 MCP Agent")

    payload: dict[str, Any] = {
        "model": cfg["model"],
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
    }
    if provider == "llama":
        payload["max_tokens"] = settings.llama_max_tokens
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=300.0)
    except httpx.HTTPError as exc:
        raise LLMError(f"无法连接 LLM 服务（{url}）: {exc}") from exc
    if resp.status_code != 200:
        raise LLMError(f"LLM 返回 {resp.status_code}: {resp.text[:500]}")
    try:
        return resp.json()["choices"][0]["message"]
    except (KeyError, IndexError, ValueError) as exc:
        raise LLMError(f"LLM 响应格式异常: {resp.text[:300]}") from exc
