import asyncio
import json
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from app.api import agent
from app.schemas import AgentGenerateRequest
from app.services import llm_client, mcp_agent, quality_review


class FakeToolResult:
    def __init__(self, value):
        self.content = [SimpleNamespace(text=json.dumps(value))]


class FakeSession:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        return FakeToolResult(self.results.pop(0))


def _generated_file(filename):
    return {
        "ok": True,
        "files": [{"filename": filename, "kind": "image"}],
    }


def test_review_loop_refines_prompt_and_stops_when_score_passes():
    session = FakeSession([_generated_file("first.png"), _generated_file("second.png")])
    reviews = []
    progress = []

    def reviewer(request, prompt, files, attempt):
        reviews.append((request, prompt, files, attempt))
        if attempt == 1:
            return {
                "score": 6,
                "issues": ["主体不突出"],
                "revised_prompt": "强调主体和构图",
            }
        return {"score": 9, "issues": [], "revised_prompt": ""}

    result = json.loads(
        asyncio.run(
            mcp_agent._run_reviewed_generation(
                session,
                "comfy_generate_image",
                {"prompt": "初始提示词"},
                "用户原始需求",
                reviewer,
                progress.append,
                [],
            )
        )
    )

    assert [call[1]["prompt"] for call in session.calls] == [
        "初始提示词",
        "强调主体和构图",
    ]
    assert [review[3] for review in reviews] == [1, 2]
    assert result["files"][0]["filename"] == "second.png"
    assert result["quality_review"]["status"] == "passed"
    assert [item["score"] for item in result["quality_review"]["attempts"]] == [6, 9]
    assert len(progress) == 3


def test_review_loop_caps_total_generations_at_three():
    session = FakeSession(
        [_generated_file(f"attempt-{index}.png") for index in range(1, 4)]
    )

    def reviewer(_request, _prompt, _files, _attempt):
        return {"score": 5, "issues": ["不符合要求"], "revised_prompt": "改进提示词"}

    result = json.loads(
        asyncio.run(
            mcp_agent._run_reviewed_generation(
                session,
                "comfy_generate_image",
                {"prompt": "初始提示词"},
                "用户原始需求",
                reviewer,
                None,
                [],
            )
        )
    )

    assert len(session.calls) == 3
    assert result["files"][0]["filename"] == "attempt-3.png"
    assert result["quality_review"]["status"] == "retry_limit"
    assert result["quality_review"]["passed"] is False
    assert len(result["quality_review"]["attempts"]) == 3


def test_review_failure_is_reported_and_keeps_generated_files():
    session = FakeSession([_generated_file("unreviewed.png")])

    def reviewer(*_args):
        raise quality_review.QualityReviewError("模型不支持图片输入")

    result = json.loads(
        asyncio.run(
            mcp_agent._run_reviewed_generation(
                session,
                "comfy_generate_image",
                {"prompt": "初始提示词"},
                "用户原始需求",
                reviewer,
                None,
                [],
            )
        )
    )

    assert result["files"][0]["filename"] == "unreviewed.png"
    assert result["quality_review"]["status"] == "error"
    assert result["quality_review"]["passed"] is False
    assert "不支持图片输入" in result["quality_review"]["error"]


def test_video_review_samples_five_evenly_spaced_frames(monkeypatch):
    module = SimpleNamespace(
        count_frames_and_secs=lambda _path: (100, 10.0),
        get_ffmpeg_exe=lambda: "ffmpeg",
    )
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", module)
    buffer = BytesIO()
    Image.new("RGB", (4, 4), "red").save(buffer, format="PNG")
    frame_bytes = buffer.getvalue()
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=frame_bytes)

    monkeypatch.setattr(quality_review.subprocess, "run", fake_run)

    frames = quality_review._sample_video_data_urls(Path("clip.mp4"))

    assert len(frames) == 5
    assert [command[command.index("-ss") + 1] for command in commands] == [
        "0.000",
        "2.475",
        "4.950",
        "7.425",
        "9.900",
    ]
    assert all(frame.startswith("data:image/jpeg;base64,") for frame in frames)


def test_llm_message_supports_multiple_review_images():
    messages = llm_client._build_messages(
        "检查所有画面",
        image_base64=[
            "data:image/jpeg;base64,one",
            "data:image/jpeg;base64,two",
        ],
    )

    content = messages[0]["content"]
    assert [item["image_url"]["url"] for item in content[:2]] == [
        "data:image/jpeg;base64,one",
        "data:image/jpeg;base64,two",
    ]
    assert content[2] == {"type": "text", "text": "检查所有画面"}


def test_review_generated_image_uses_selected_custom_vision_model(monkeypatch, tmp_path):
    monkeypatch.setattr(quality_review.settings, "image_storage_dir", str(tmp_path))
    media_dir = tmp_path / "mcp"
    media_dir.mkdir()
    image_path = media_dir / "result.png"
    Image.new("RGB", (16, 8), "blue").save(image_path)
    custom_chat = Mock(
        return_value=json.dumps(
            {"score": 9, "issues": [], "revised_prompt": ""}
        )
    )
    monkeypatch.setattr(quality_review, "chat_with_config", custom_chat)

    result = quality_review.review_generated_media(
        "原始需求",
        "生成提示词",
        [{"filename": image_path.name, "kind": "image"}],
        attempt=1,
        provider="custom",
        custom_config={
            "base_url": "https://llm.example/v1",
            "api_key": "test-key",
            "model": "vision-model",
        },
    )

    assert result["passed"] is True
    custom_chat.assert_called_once()
    assert custom_chat.call_args.args[1:] == (
        "https://llm.example/v1",
        "test-key",
        "vision-model",
    )
    assert custom_chat.call_args.kwargs["image_base64"][0].startswith(
        "data:image/jpeg;base64,"
    )


def test_quality_review_is_opt_in_for_agent_requests():
    assert AgentGenerateRequest(request="生成一张图").quality_review_enabled is False
    assert (
        AgentGenerateRequest(request="生成一张图", quality_review_enabled=True)
        .quality_review_enabled
        is True
    )


def test_agent_api_forwards_quality_review_option(monkeypatch):
    task = SimpleNamespace(id="queued-task")
    delay = Mock(return_value=task)
    monkeypatch.setattr(agent.run_mcp_agent_task, "delay", delay)
    monkeypatch.setattr(agent, "check_rate_limit", lambda _user: None)
    monkeypatch.setattr(agent, "check_and_increment_chat_quota", lambda _user, _db: None)

    response = agent.agent_mcp(
        AgentGenerateRequest(request="生成一张图", quality_review_enabled=True),
        SimpleNamespace(id=7),
        object(),
    )

    assert response["task_id"] == "queued-task"
    assert delay.call_args.args[-1] is True


def test_review_prompt_parser_rejects_missing_revision_on_failure():
    with pytest.raises(quality_review.QualityReviewError):
        quality_review._parse_review(
            json.dumps({"score": 7, "issues": ["主体偏小"], "revised_prompt": ""})
        )
