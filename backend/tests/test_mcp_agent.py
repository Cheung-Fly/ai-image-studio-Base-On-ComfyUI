"""MCP Agent 端到端测试：模拟 ComfyUI + 真实 MCP stdio Server + 脚本化假 LLM。

运行（backend/ 目录下）：python tests/test_mcp_agent.py
"""

import asyncio
import base64
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mcp import StdioServerParameters

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
CALLS: list[str] = []
POST_BODIES: list[tuple[str, dict]] = []


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body: bytes, ctype="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        CALLS.append("GET " + self.path)
        if self.path.startswith("/system_stats"):
            self._send(json.dumps({"system": {"comfyui_version": "0.34"}, "devices": []}).encode())
        elif self.path.startswith("/queue"):
            self._send(json.dumps({"queue_running": [], "queue_pending": []}).encode())
        elif self.path.startswith("/history/"):
            pid = self.path.rsplit("/", 1)[1]
            entry = {
                "status": {"completed": True, "status_str": "success"},
                "outputs": {
                    "9": {"images": [{"filename": "a.png", "subfolder": "", "type": "output"}]}
                },
            }
            self._send(json.dumps({pid: entry}).encode())
        elif self.path.startswith("/view"):
            self._send(PNG, "image/png")
        else:
            self._send(b"{}")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(n)
        POST_BODIES.append((self.path, json.loads(raw_body) if raw_body else {}))
        CALLS.append("POST " + self.path)
        self._send(json.dumps({"prompt_id": "p1"}).encode() if self.path == "/prompt" else b"")


def main() -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tmp = tempfile.mkdtemp()
    backend = Path(__file__).resolve().parent.parent
    env = {
        **os.environ,
        "COMFYUI_BASE_URL": f"http://127.0.0.1:{srv.server_port}",
        "IMAGE_STORAGE_DIR": tmp,
        "DATABASE_URL": f"sqlite:///{(Path(tmp) / 'agent.db').as_posix()}",
        "WORKFLOW_DIR": str(backend / ".." / "workflows"),
        "COMFY_POLL_INTERVAL": "0.1",
    }
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "app.mcp_server"], cwd=str(backend), env=env
    )

    script = [
        {
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "comfy_status", "arguments": "{}"},
                }
            ]
        },
        {
            "tool_calls": [
                {
                    "id": "2",
                    "type": "function",
                    "function": {
                        "name": "comfy_generate_image",
                        "arguments": json.dumps({"prompt": "a corgi in space"}),
                    },
                },
                {
                    "id": "3",
                    "type": "function",
                    "function": {"name": "comfy_interrupt", "arguments": "{}"},
                },
                {
                    "id": "4",
                    "type": "function",
                    "function": {
                        "name": "comfy_delete_queued",
                        "arguments": json.dumps({"prompt_ids": ["waiting-123"]}),
                    },
                },
            ]
        },
        {
            "tool_calls": [
                {
                    "id": "5",
                    "type": "function",
                    "function": {
                        "name": "comfy_remember_preferences",
                        "arguments": json.dumps({"style": "blue watercolor"}),
                    },
                },
                {
                    "id": "6",
                    "type": "function",
                    "function": {"name": "comfy_get_preferences", "arguments": "{}"},
                },
            ]
        },
        {"content": "已记住"},
    ]
    seen_tools: list[str] = []
    progress: list[dict] = []
    confirmation_requests: list[tuple[str, dict]] = []
    system_prompts: list[str] = []

    def fake_llm(messages, tools, provider):
        if not seen_tools:
            seen_tools.extend(t["function"]["name"] for t in tools)
        if not system_prompts:
            system_prompts.append(messages[0]["content"])
        return script.pop(0)

    def approve_action(tool, arguments):
        confirmation_requests.append((tool, arguments))
        return True

    from app.services.mcp_agent import run_mcp_agent

    out = asyncio.run(
        run_mcp_agent(
            "画只柯基，并记住我喜欢蓝色水彩风格",
            llm=fake_llm,
            params=params,
            on_progress=progress.append,
            confirm_action=approve_action,
            memory={"style": "soft watercolor", "aspect_ratio": "16:9 (Widescreen)"},
        )
    )

    expected_tools = {
        "comfy_generate_image",
        "comfy_interrupt",
        "comfy_delete_queued",
        "comfy_workflow_details",
        "comfy_describe_node",
        "comfy_list_input_files",
        "comfy_get_preferences",
        "comfy_remember_preferences",
        "comfy_forget_preferences",
    }
    assert expected_tools.issubset(seen_tools), seen_tools
    assert (
        "soft watercolor" in system_prompts[0] and "当前请求" in system_prompts[0]
    ), system_prompts
    assert out["answer"] == "已记住", out
    assert [s["tool"] for s in out["steps"]] == [
        "comfy_status",
        "comfy_generate_image",
        "comfy_interrupt",
        "comfy_delete_queued",
        "comfy_remember_preferences",
        "comfy_get_preferences",
    ]
    assert len(progress) == 12 and progress[-1]["current_tool"] == "comfy_get_preferences", progress
    assert progress[0]["running"] is True and progress[-1]["running"] is False, progress
    assert confirmation_requests == [
        ("comfy_interrupt", {}),
        ("comfy_delete_queued", {"prompt_ids": ["waiting-123"]}),
    ], confirmation_requests
    gen = json.loads(out["steps"][1]["result"])
    assert gen["ok"] and gen["prompt_id"] == "p1", gen
    assert Path(gen["files"][0]["path"]).read_bytes() == PNG
    assert json.loads(out["steps"][2]["result"])["ok"]
    assert json.loads(out["steps"][3]["result"])["deleted"] == ["waiting-123"]
    assert json.loads(out["steps"][4]["result"])["preferences"]["style"] == "blue watercolor"
    saved = json.loads(out["steps"][5]["result"])["preferences"]
    assert saved["style"] == "blue watercolor", saved
    assert "POST /interrupt" in CALLS and "POST /prompt" in CALLS and "POST /queue" in CALLS, CALLS
    assert ("/queue", {"delete": ["waiting-123"]}) in POST_BODIES, POST_BODIES

    CALLS.clear()
    POST_BODIES.clear()
    denied_script = [
        {
            "tool_calls": [
                {
                    "id": "7",
                    "type": "function",
                    "function": {"name": "comfy_clear_queue", "arguments": "{}"},
                }
            ]
        },
        {"content": "已取消清队列操作。"},
    ]

    def deny_llm(messages, tools, provider):
        return denied_script.pop(0)

    denied = asyncio.run(
        run_mcp_agent(
            "清空等待队列",
            llm=deny_llm,
            params=params,
            confirm_action=lambda tool, arguments: False,
        )
    )
    assert json.loads(denied["steps"][0]["result"])["cancelled"]
    assert "POST /queue" not in CALLS, CALLS
    print("MCP AGENT TEST PASSED")


if __name__ == "__main__":
    main()
