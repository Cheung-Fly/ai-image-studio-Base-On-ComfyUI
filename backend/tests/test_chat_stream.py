"""Verify streamed LLM responses and the SSE chat endpoint."""

import asyncio
import sys
from types import ModuleType
from unittest.mock import Mock, patch

from app.api.chat import stream_chat_endpoint
from app.schemas import ChatRequest
from app.services import llm_client
from app.services.llm_client import LLMError


class FakeResponse:
    def __init__(
        self,
        *,
        status_code=200,
        content_type="text/event-stream",
        lines=(),
        body=None,
        text="",
    ):
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self.lines = lines
        self.body = body
        self.text = text

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_lines(self):
        yield from self.lines

    def read(self):
        return self.text.encode()

    def json(self):
        return self.body


def collect_response(response, call):
    captured = {}

    def fake_stream(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return response

    with patch.object(llm_client.httpx, "stream", fake_stream):
        chunks = list(call())
    return chunks, captured


def test_builtin_stream_and_multimodal_payload():
    response = FakeResponse(
        lines=[
            "data:",
            'data: {"choices":[{"delta":{"content":"你好"}}]}',
            'data: {"choices":[{"delta":{"content":"，世界"}}]}',
            "data: [DONE]",
        ]
    )
    with patch.object(
        llm_client,
        "_resolve",
        return_value={
            "base_url": "https://llm.example",
            "api_key": "test-key",
            "model": "test-model",
        },
    ):
        chunks, request = collect_response(
            response,
            lambda: llm_client.stream_chat(
                "这是什么？",
                [{"role": "assistant", "content": "上一轮"}],
                provider="openai",
                image_base64="data:image/png;base64,abc",
            ),
        )

    assert chunks == ["你好", "，世界"], chunks
    assert request["url"] == "https://llm.example/chat/completions", request
    assert request["json"]["stream"] is True, request
    assert request["json"]["messages"][0]["content"] == "上一轮", request
    assert request["json"]["messages"][1]["content"][0]["image_url"]["url"].startswith(
        "data:image/png"
    ), request


def test_custom_provider_non_streaming_fallback():
    response = FakeResponse(
        content_type="application/json",
        body={"choices": [{"message": {"content": "完整回复"}}]},
    )
    chunks, request = collect_response(
        response,
        lambda: llm_client.stream_chat_with_config(
            "你好",
            "https://custom.example/",
            "test-key",
            "custom-model",
        ),
    )

    assert chunks == ["完整回复"], chunks
    assert request["url"] == "https://custom.example/chat/completions", request
    assert request["json"]["stream"] is True, request
    assert request["json"]["model"] == "custom-model", request


def test_http_error_is_reported():
    response = FakeResponse(status_code=503, text="upstream unavailable")
    try:
        collect_response(
            response,
            lambda: llm_client.stream_chat_with_config(
                "你好",
                "https://custom.example",
                "test-key",
                "custom-model",
            ),
        )
    except LLMError as exc:
        assert "503" in str(exc), exc
    else:
        raise AssertionError("Non-200 LLM response was accepted")


def test_sse_endpoint_streams_and_persists():
    persist_db = Mock()
    request = ChatRequest(message="你好", provider="openai")
    user = Mock(id=7)
    with (
        patch("app.api.chat.check_rate_limit"),
        patch("app.api.chat.check_and_increment_chat_quota"),
        patch("app.api.chat.llm_stream_chat", return_value=iter(["你好", "！"])),
        patch("app.api.chat.SessionLocal", return_value=persist_db),
        patch("app.api.chat.persist_message") as persist,
    ):
        response = stream_chat_endpoint(request, user, Mock())

        async def read_body():
            chunks = [chunk async for chunk in response.body_iterator]
            return "".join(
                chunk.decode() if isinstance(chunk, bytes) else chunk for chunk in chunks
            )

        body = asyncio.run(read_body())

    assert response.media_type == "text/event-stream"
    assert 'data: {"delta": "你好"}' in body, body
    assert 'data: {"delta": "！"}' in body, body
    assert body.endswith("data: [DONE]\n\n"), body
    assert persist.call_count == 2, persist.call_args_list
    assert persist.call_args_list[0].args[1:] == (7, "openai", "user", "你好")
    assert persist.call_args_list[1].args[1:] == (7, "openai", "assistant", "你好！")
    persist_db.close.assert_called_once()


def test_sse_endpoint_streams_rag_prompt():
    rag = ModuleType("app.services.rag")
    rag.ensure_index = Mock()
    rag.retrieve = Mock(return_value=["retrieved context"])
    rag.build_rag_prompt = Mock(return_value="RAG prompt")
    request = ChatRequest(message="基于文档回答", provider="openai", use_rag=True)
    persist_db = Mock()

    with (
        patch.dict(sys.modules, {"app.services.rag": rag}),
        patch("app.api.chat.check_rate_limit"),
        patch("app.api.chat.check_and_increment_chat_quota"),
        patch("app.api.chat.llm_stream_chat", return_value=iter(["答案"])) as stream,
        patch("app.api.chat.SessionLocal", return_value=persist_db),
        patch("app.api.chat.persist_message"),
    ):
        response = stream_chat_endpoint(request, Mock(id=8), Mock())

        async def consume_body():
            return [chunk async for chunk in response.body_iterator]

        asyncio.run(consume_body())

    stream.assert_called_once_with("RAG prompt", None, provider="openai")
    rag.ensure_index.assert_called_once()
    rag.retrieve.assert_called_once_with("基于文档回答")
    rag.build_rag_prompt.assert_called_once_with("基于文档回答", ["retrieved context"])
    persist_db.close.assert_called_once()


def main():
    test_builtin_stream_and_multimodal_payload()
    test_custom_provider_non_streaming_fallback()
    test_http_error_is_reported()
    test_sse_endpoint_streams_and_persists()
    test_sse_endpoint_streams_rag_prompt()
    print("CHAT STREAM TEST PASSED")


if __name__ == "__main__":
    main()
