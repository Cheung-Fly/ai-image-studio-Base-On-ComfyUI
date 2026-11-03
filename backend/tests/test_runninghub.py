"""Mocked RunningHub Workflow API checks; run with ``python tests/test_runninghub.py``."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import httpx
from mcp import StdioServerParameters
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.runninghub import get_runninghub_config, save_runninghub_config
from app.database import Base
from app.models import RunningHubConfig, User
from app.schemas import RunningHubConfigIn
from app.security import decrypt_secret
from app.services.mcp_agent import run_mcp_agent
from app.services.runninghub import RunningHubClient, RunningHubError

PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
)


class RunningHubClientTests(unittest.TestCase):
    def test_runninghub_config_is_encrypted_and_redacted(self):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            request = RunningHubConfigIn(
                api_key="test-key",
                workflow_id="workflow-1",
                prompt_node_id="4",
                prompt_field="text",
            )
            saved = save_runninghub_config(request, User(id=1), db)
            row = db.get(RunningHubConfig, 1)
            self.assertEqual(saved, {"configured": True, "workflow_id": "workflow-1"})
            self.assertNotEqual(row.api_key_encrypted, "test-key")
            self.assertEqual(decrypt_secret(row.api_key_encrypted), "test-key")
            self.assertNotIn("api_key", get_runninghub_config(User(id=1), db))
        engine.dispose()

    def test_discovers_text_fields_without_returning_workflow_values(self):
        def handler(request):
            self.assertEqual(request.headers["host"], "www.runninghub.ai")
            self.assertEqual(json.loads(request.content)["apiKey"], "test-key")
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "prompt": {
                            "4": {
                                "class_type": "CLIPTextEncode",
                                "_meta": {"title": "Positive prompt"},
                                "inputs": {"text": "old prompt", "clip": ["1", 0]},
                            },
                            "8": {
                                "class_type": "Note",
                                "inputs": {"label": "not a prompt"},
                            },
                        }
                    },
                },
            )

        transport = httpx.MockTransport(handler)
        http = httpx.Client(transport=transport)
        client = RunningHubClient("test-key", http_client=http)
        try:
            fields = client.get_workflow_text_fields("workflow-1")
        finally:
            http.close()

        self.assertEqual(
            fields,
            [
                {
                    "node_id": "4",
                    "node_type": "CLIPTextEncode",
                    "node_title": "Positive prompt",
                    "field_name": "text",
                },
                {
                    "node_id": "8",
                    "node_type": "Note",
                    "node_title": "Note",
                    "field_name": "label",
                },
            ],
        )

    def test_generates_and_downloads_output(self):
        status_calls = 0
        create_body = {}

        def handler(request):
            nonlocal status_calls
            if request.url.path == "/task/openapi/create":
                self.assertEqual(request.headers["host"], "www.runninghub.ai")
                create_body.update(json.loads(request.content))
                return httpx.Response(200, json={"code": 0, "data": {"taskId": "task-1"}})
            if request.url.path == "/task/openapi/status":
                status_calls += 1
                status = "RUNNING" if status_calls == 1 else "SUCCESS"
                return httpx.Response(200, json={"code": 0, "data": status})
            if request.url.path == "/task/openapi/outputs":
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": [
                            {
                                "fileUrl": "https://rh-images.xiaoyaoyou.com/output.png",
                                "fileType": "image",
                            }
                        ],
                    },
                )
            if request.url.host == "rh-images.xiaoyaoyou.com":
                return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        http = httpx.Client(transport=httpx.MockTransport(handler))
        client = RunningHubClient("test-key", http_client=http, poll_interval=0)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                result = client.generate(
                    "workflow-1",
                    [
                        {
                            "nodeId": "4",
                            "fieldName": "text",
                            "fieldValue": "a red fox",
                        }
                    ],
                    Path(temp_dir),
                )
                output_path = Path(result["files"][0]["path"])
                self.assertEqual(output_path.read_bytes(), PNG)
                self.assertEqual(result["task_id"], "task-1")
                self.assertEqual(result["files"][0]["kind"], "image")
                self.assertEqual(output_path.parent, Path(temp_dir))
            self.assertEqual(
                create_body["nodeInfoList"],
                [{"nodeId": "4", "fieldName": "text", "fieldValue": "a red fox"}],
            )
            self.assertEqual(create_body["apiKey"], "test-key")
        finally:
            http.close()

    def test_rejects_untrusted_output_hosts(self):
        client = RunningHubClient("test-key", http_client=httpx.Client())
        try:
            with self.assertRaises(RunningHubError):
                client._download_output(
                    {"fileUrl": "http://127.0.0.1/image.png", "fileType": "image"},
                    Path(tempfile.gettempdir()),
                )
        finally:
            client.http.close()

    def test_agent_selects_runninghub_mcp_tools(self):
        backend_dir = Path(__file__).resolve().parent.parent
        seen = {}

        def fake_llm(messages, tools, provider):
            seen["prompt"] = messages[0]["content"]
            seen["tools"] = [tool["function"]["name"] for tool in tools]
            seen["provider"] = provider
            return {"content": "RunningHub 已就绪。"}

        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                **os.environ,
                "DATABASE_URL": f"sqlite:///{(Path(temp_dir) / 'runninghub.db').as_posix()}",
                "IMAGE_STORAGE_DIR": str(Path(temp_dir) / "images"),
                "PYTHONIOENCODING": "utf-8",
            }
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "app.mcp_server.runninghub"],
                cwd=str(backend_dir),
                env=env,
            )
            result = asyncio.run(
                run_mcp_agent(
                    "生成一张太空柯基",
                    provider="custom",
                    llm=fake_llm,
                    params=params,
                    generation_backend="runninghub",
                )
            )

        self.assertEqual(result["answer"], "RunningHub 已就绪。")
        self.assertEqual(seen["tools"], ["runninghub_generate"])
        self.assertIn("RunningHub", seen["prompt"])
        self.assertIn("不要调用或声称操作本地 ComfyUI", seen["prompt"])
        self.assertEqual(seen["provider"], "custom")


if __name__ == "__main__":
    unittest.main()
