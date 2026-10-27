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
                "outputs": {"9": {"images": [{"filename": "a.png", "subfolder": "", "type": "output"}]}},
            }
            self._send(json.dumps({pid: entry}).encode())
        elif self.path.startswith("/view"):
            self._send(PNG, "image/png")
        else:
            self._send(b"{}")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
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
        "WORKFLOW_DIR": str(backend / ".." / "workflows"),
        "COMFY_POLL_INTERVAL": "0.1",
    }
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "app.mcp_server"], cwd=str(backend), env=env
    )

    script = [
        {"tool_calls": [{"id": "1", "type": "function", "function": {"name": "comfy_status", "arguments": "{}"}}]},
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
                {"id": "3", "type": "function", "function": {"name": "comfy_interrupt", "arguments": "{}"}},
            ]
        },
        {"content": "完成"},
    ]
    seen_tools: list[str] = []

    def fake_llm(messages, tools, provider):
        if not seen_tools:
            seen_tools.extend(t["function"]["name"] for t in tools)
        return script.pop(0)

    from app.services.mcp_agent import run_mcp_agent

    out = asyncio.run(run_mcp_agent("画只柯基", llm=fake_llm, params=params))

    assert "comfy_generate_image" in seen_tools and "comfy_interrupt" in seen_tools, seen_tools
    assert out["answer"] == "完成", out
    assert [s["tool"] for s in out["steps"]] == ["comfy_status", "comfy_generate_image", "comfy_interrupt"]
    gen = json.loads(out["steps"][1]["result"])
    assert gen["ok"] and gen["prompt_id"] == "p1", gen
    assert Path(gen["files"][0]["path"]).read_bytes() == PNG
    assert json.loads(out["steps"][2]["result"])["ok"]
    assert "POST /interrupt" in CALLS and "POST /prompt" in CALLS, CALLS
    print("MCP AGENT TEST PASSED")


if __name__ == "__main__":
    main()
