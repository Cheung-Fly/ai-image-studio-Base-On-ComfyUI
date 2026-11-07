"""Mocked RunningHub Workflow API checks; run with ``python tests/test_runninghub.py``."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from mcp import StdioServerParameters
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.runninghub import get_runninghub_config, save_runninghub_config
from app.config import settings
from app.database import Base
from app.models import RunningHubConfig, User
from app.schemas import RunningHubConfigIn
from app.security import decrypt_secret
from app.services.mcp_agent import run_mcp_agent
from app.services.runninghub import (
    RunningHubClient,
    RunningHubError,
    workflow_media_fields,
    workflow_parameter_node_info,
)

PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
)


class RunningHubClientTests(unittest.TestCase):
    def test_runninghub_config_is_encrypted_and_redacted(self):
        test_key = "runninghub-test-key-" + ("x" * 40)
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        with patch.object(settings, "encryption_key", test_key), Session(engine) as db:
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
        requests = 0

        def handler(request):
            nonlocal requests
            requests += 1
            self.assertEqual(request.headers["host"], "www.runninghub.ai")
            self.assertEqual(request.headers["authorization"], "test-key")
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

        http = httpx.Client(transport=httpx.MockTransport(handler))
        client = RunningHubClient("test-key", http_client=http)
        try:
            fields = client.get_workflow_text_fields("workflow-1")
            client.get_workflow("workflow-1")
        finally:
            http.close()

        self.assertEqual(requests, 1)
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

    def test_discovers_compatible_media_loader_inputs(self):
        fields = workflow_media_fields(
            {
                "10": {
                    "class_type": "VHS_LoadVideo",
                    "inputs": {"video": "old.mp4"},
                },
                "2": {
                    "class_type": "LoadImage",
                    "inputs": {"image": "old.png"},
                },
                "7": {
                    "class_type": "LoadAudio",
                    "inputs": {"audio": "old.wav"},
                },
            }
        )
        self.assertEqual(
            fields,
            {
                "image": [{"node_id": "2", "field_name": "image"}],
                "video": [{"node_id": "10", "field_name": "video"}],
                "audio": [{"node_id": "7", "field_name": "audio"}],
            },
        )

    def test_maps_aspect_resolution_and_video_duration_settings(self):
        mapped = workflow_parameter_node_info(
            {
                "3": {
                    "class_type": "VideoSampler",
                    "inputs": {
                        "width": 640,
                        "height": 360,
                        "duration": 5,
                        "frames": 121,
                        "seed": 1,
                    },
                },
                "4": {
                    "class_type": "Sampler",
                    "inputs": {"steps": 20},
                },
            },
            "16:9 (Widescreen)",
            1.0,
            seed=-1,
            steps=8,
            duration_seconds=5,
        )
        self.assertEqual(
            mapped,
            [
                {"nodeId": "3", "fieldName": "width", "fieldValue": "1334"},
                {"nodeId": "3", "fieldName": "height", "fieldValue": "750"},
                {"nodeId": "3", "fieldName": "duration", "fieldValue": "5"},
                {"nodeId": "3", "fieldName": "frames", "fieldValue": "120"},
                {"nodeId": "3", "fieldName": "seed", "fieldValue": "-1"},
                {"nodeId": "4", "fieldName": "steps", "fieldValue": "8"},
            ],
        )

    def test_uploads_reference_media_with_authorization_header(self):
        uploads = 0

        def handler(request):
            nonlocal uploads
            uploads += 1
            self.assertEqual(
                request.url,
                httpx.URL(
                    "https://www.runninghub.cn/openapi/v2/media/upload/binary"
                ),
            )
            self.assertEqual(request.headers["authorization"], "test-key")
            self.assertIn(b'name="file"; filename="ref.png"', request.content)
            self.assertIn(PNG, request.content)
            return httpx.Response(
                200,
                json={"code": 0, "data": {"fileName": "input/ref.png"}},
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ref.png"
            path.write_bytes(PNG)
            http = httpx.Client(transport=httpx.MockTransport(handler))
            client = RunningHubClient("test-key", http_client=http)
            try:
                self.assertEqual(client.upload_media(path), "input/ref.png")
                self.assertEqual(client.upload_media(path), "input/ref.png")
            finally:
                http.close()
        self.assertEqual(uploads, 1)

    def test_generates_and_downloads_output(self):
        query_calls = 0
        create_body = {}
        saved_task_ids = []

        def handler(request):
            nonlocal query_calls
            if request.url.path == "/task/openapi/create":
                self.assertEqual(request.headers["host"], "www.runninghub.ai")
                self.assertEqual(request.headers["authorization"], "test-key")
                create_body.update(json.loads(request.content))
                return httpx.Response(200, json={"code": 0, "data": {"taskId": "task-1"}})
            if request.url.path == "/openapi/v2/query":
                self.assertEqual(request.headers["authorization"], "test-key")
                self.assertEqual(json.loads(request.content), {"taskId": "task-1"})
                query_calls += 1
                status = "RUNNING" if query_calls == 1 else "SUCCESS"
                return httpx.Response(
                    200,
                    json={
                        "taskId": "task-1",
                        "status": status,
                        "results": (
                            []
                            if status == "RUNNING"
                            else [
                                {
                                    "url": "https://rh-images-tos.xiaoyaoyou.com/output.png",
                                    "outputType": "image",
                                }
                            ]
                        ),
                    },
                )
            if request.url.host == "rh-images-tos.xiaoyaoyou.com":
                return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        http = httpx.Client(transport=httpx.MockTransport(handler))
        client = RunningHubClient("test-key", http_client=http, poll_interval=0)
        status_updates = []
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
                    status_callback=lambda status, elapsed: status_updates.append(
                        (status, elapsed)
                    ),
                    task_id_callback=saved_task_ids.append,
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
            self.assertEqual(saved_task_ids, ["task-1"])
            self.assertEqual(
                [status for status, _ in status_updates],
                ["RUNNING", "SUCCESS"],
            )
        finally:
            http.close()

    def test_resumes_existing_remote_task_without_creating_a_duplicate(self):
        def handler(request):
            if request.url.path == "/openapi/v2/query":
                return httpx.Response(
                    200,
                    json={
                        "taskId": "task-existing",
                        "status": "SUCCESS",
                        "results": [
                            {
                                "url": "https://rh-images-1252422369.cos.ap-beijing.myqcloud.com/output.png",
                                "outputType": "image",
                            }
                        ],
                    },
                )
            if request.url.host == "rh-images-1252422369.cos.ap-beijing.myqcloud.com":
                return httpx.Response(
                    200,
                    headers={"content-type": "image/png"},
                    content=PNG,
                )
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        http = httpx.Client(transport=httpx.MockTransport(handler))
        client = RunningHubClient("test-key", http_client=http, poll_interval=0)
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                result = client.generate(
                    "workflow-1",
                    [],
                    Path(temp_dir),
                    existing_task_id="task-existing",
                )
                self.assertEqual(Path(result["files"][0]["path"]).read_bytes(), PNG)
            self.assertEqual(result["task_id"], "task-existing")
        finally:
            http.close()

    def test_terminal_failure_clears_persisted_task_id(self):
        def handler(request):
            if request.url.path == "/task/openapi/create":
                return httpx.Response(
                    200,
                    json={"code": 0, "data": {"taskId": "task-1"}},
                )
            if request.url.path == "/openapi/v2/query":
                return httpx.Response(
                    200,
                    json={
                        "taskId": "task-1",
                        "status": "FAILED",
                        "errorCode": "WORKFLOW_ERROR",
                        "errorMessage": "workflow failed",
                    },
                )
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")

        http = httpx.Client(transport=httpx.MockTransport(handler))
        client = RunningHubClient("test-key", http_client=http, poll_interval=0)
        saved_task_ids = []
        try:
            with (
                tempfile.TemporaryDirectory() as temp_dir,
                self.assertRaisesRegex(RunningHubError, "workflow failed"),
            ):
                client.generate(
                    "workflow-1",
                    [],
                    Path(temp_dir),
                    task_id_callback=saved_task_ids.append,
                )
            self.assertEqual(saved_task_ids, ["task-1", None])
        finally:
            http.close()

    def test_create_timeout_warns_that_remote_task_may_exist(self):
        def handler(request):
            raise httpx.ReadTimeout("response timed out", request=request)

        http = httpx.Client(transport=httpx.MockTransport(handler))
        client = RunningHubClient("test-key", http_client=http)
        try:
            with self.assertRaisesRegex(RunningHubError, "远端任务可能已创建"):
                client.create_task("workflow-1", [])
        finally:
            http.close()

    def test_retries_transient_status_network_errors(self):
        query_calls = 0

        def handler(request):
            nonlocal query_calls
            self.assertEqual(request.url.path, "/openapi/v2/query")
            query_calls += 1
            if query_calls == 1:
                raise httpx.ConnectError("temporary network failure", request=request)
            return httpx.Response(
                200,
                json={"taskId": "task-1", "status": "SUCCESS", "results": []},
            )

        http = httpx.Client(transport=httpx.MockTransport(handler))
        client = RunningHubClient("test-key", http_client=http, poll_interval=0)
        try:
            client.wait_for_completion("task-1")
            self.assertEqual(query_calls, 2)
        finally:
            http.close()

    def test_increases_poll_interval_up_to_configured_ceiling(self):
        query_calls = 0

        def handler(request):
            nonlocal query_calls
            query_calls += 1
            status = "SUCCESS" if query_calls == 5 else "RUNNING"
            return httpx.Response(
                200,
                json={"taskId": "task-1", "status": status, "results": []},
            )

        http = httpx.Client(transport=httpx.MockTransport(handler))
        client = RunningHubClient(
            "test-key",
            http_client=http,
            poll_interval=3,
            max_poll_interval=15,
        )
        try:
            with patch("app.services.runninghub.time.sleep") as sleep:
                client.wait_for_completion("task-1")
            self.assertEqual(
                [call.args[0] for call in sleep.call_args_list],
                [3, 6, 12, 15],
            )
        finally:
            http.close()

    def test_bounds_retries_for_persistent_status_network_errors(self):
        query_calls = 0

        def handler(request):
            nonlocal query_calls
            query_calls += 1
            raise httpx.ConnectError("network unavailable", request=request)

        http = httpx.Client(transport=httpx.MockTransport(handler))
        client = RunningHubClient("test-key", http_client=http, poll_interval=0)
        try:
            with self.assertRaises(RunningHubError):
                client.wait_for_completion("task-1")
            self.assertEqual(query_calls, 4)
        finally:
            http.close()

    def test_accepts_known_runninghub_output_hosts(self):
        for host in (
            "rh-images.xiaoyaoyou.com",
            "rh-images-tos.xiaoyaoyou.com",
            "rh-images-1252422369.cos.ap-beijing.myqcloud.com",
        ):
            with self.subTest(host=host):
                url = f"https://{host}/output.png"
                self.assertEqual(RunningHubClient._validated_output_url(url), url)

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
