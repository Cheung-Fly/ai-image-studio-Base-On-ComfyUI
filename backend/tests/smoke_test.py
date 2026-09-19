"""端到端冒烟测试：无需真实 ComfyUI / Redis。

原理：
- 在 import 应用前设置环境变量，把 ComfyUI 指向一个内置的模拟服务（线程内 HTTP server）；
- 开启 Celery eager 模式，任务提交后同步执行完毕；
- 用 fastapi TestClient 走完整链路：提交 -> 状态机 -> 出图落盘 -> 图库 -> 下载。

运行（backend/ 目录下）：
    pip install httpx
    python tests/smoke_test.py
预期输出：SMOKE TEST PASSED（8/8）
"""
import base64
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------- 0. 模拟 ComfyUI 服务 ----------

# 1x1 透明 PNG
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class MockComfyHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静默
        pass

    def _json(self, code: int, payload: dict):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _bytes(self, data: bytes, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path == "/prompt":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            # 简单校验工作流结构
            if "prompt" not in body:
                self._json(400, {"error": "missing prompt"})
                return
            self._json(200, {"prompt_id": "mock-1234"})
        else:
            self._json(404, {"error": "not found"})

    def do_GET(self):
        if self.path.startswith("/history/"):
            pid = self.path.rsplit("/", 1)[-1]
            self._json(
                200,
                {
                    pid: {
                        "status": {"completed": True, "status_str": "success"},
                        "outputs": {
                            "9": {
                                "images": [
                                    {"filename": "mock.png", "subfolder": "", "type": "output"}
                                ]
                            }
                        },
                    }
                },
            )
        elif self.path.startswith("/view"):
            self._bytes(_PNG_BYTES, "image/png")
        elif self.path == "/system_stats":
            self._json(200, {"system": {"comfyui_version": "mock"}})
        elif self.path == "/object_info/CheckpointLoaderSimple":
            self._json(
                200,
                {"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["mock.ckpt"]]}}}},
            )
        else:
            self._json(404, {"error": "not found"})


def start_mock_comfy() -> str:
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockComfyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{server.server_address[1]}"


# ---------- 1. 环境准备（必须在 import app 之前） ----------
_work_dir = Path(tempfile.mkdtemp(prefix="aistudio_smoke_"))
_mock_url = start_mock_comfy()

os.environ["COMFYUI_BASE_URL"] = _mock_url
os.environ["DATABASE_URL"] = f"sqlite:///{(_work_dir / 'test.db').as_posix()}"
os.environ["IMAGE_STORAGE_DIR"] = (_work_dir / "images").as_posix()
os.environ["WORKFLOW_DIR"] = str(Path(__file__).resolve().parent.parent.parent / "workflows")
os.environ["CELERY_TASK_ALWAYS_EAGER"] = "1"
os.environ["CELERY_TASK_EAGER_PROPAGATES"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services.workflow import build_workflow  # noqa: E402
from app.workers.celery_app import celery_app  # noqa: E402

# Celery 不自动读取 CELERY_TASK_ALWAYS_EAGER 环境变量，需显式开启 eager 模式
celery_app.conf.update(task_always_eager=True, task_eager_propagates=True)
assert celery_app.conf.task_always_eager is True, "Celery eager 模式未生效"

# ---------- 2. 执行断言 ----------
PASSED = 0
FAILED = 0
_REASONS = []


def check(name: str, ok: bool, detail: str = ""):
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        _REASONS.append(f"{name}: {detail}")
        print(f"  [FAIL] {name} -> {detail}")


with TestClient(app) as client:
    # 1. 健康检查
    r = client.get("/health")
    body = r.json()
    check("health 返回 ok 且含 comfyui 地址", r.status_code == 200 and body["status"] == "ok" and "comfyui_base_url" in body)

    # 2. 提交任务（eager 同步完成）
    r = client.post(
        "/api/generate",
        json={
            "prompt": "a cute corgi astronaut, digital art",
            "negative_prompt": "blurry",
            "width": 512,
            "height": 512,
            "steps": 20,
            "cfg": 4.0,
            "seed": 42,
        },
    )
    task = r.json()
    check("提交任务返回 202 与 task_id", r.status_code == 202 and task["id"] > 0)
    if task.get("status") != "completed":
        print(f"    [诊断] 提交返回 status={task.get('status')} error={task.get('error')} prompt_id={task.get('comfy_prompt_id')}")
    check("任务同步完成（status=completed）", task.get("status") == "completed", str(task.get("error")))

    # 3. 任务详情含图片
    tid = task["id"]
    r = client.get(f"/api/tasks/{tid}")
    detail = r.json()
    if r.status_code != 200 or len(detail.get("images", [])) != 1:
        print(f"    [诊断] 详情 status={detail.get('status')} error={detail.get('error')} images={len(detail.get('images', []))}")
    check("任务详情包含 1 张产物图片", r.status_code == 200 and len(detail.get("images", [])) == 1)

    # 4. 图库列表
    r = client.get("/api/images")
    images = r.json()
    check("图库列表非空", r.status_code == 200 and len(images) >= 1)

    # 5. 下载图片
    img_id = images[0]["id"]
    r = client.get(f"/api/images/{img_id}/file")
    check("下载图片返回 200 且为合法 PNG", r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n" and len(r.content) > 0)

    # 6. 状态过滤
    r = client.get("/api/tasks", params={"status": "completed"})
    check("按 status=completed 过滤生效", r.status_code == 200 and all(t["status"] == "completed" for t in r.json()))

    # 7. 不存在任务返回 404
    r = client.get("/api/tasks/999999")
    check("不存在任务返回 404", r.status_code == 404)

    # 8. 工作流参数注入正确
    wf = build_workflow(
        prompt="hello world",
        negative_prompt="blurry",
        width=768,
        height=512,
        steps=12,
        cfg=2.0,
        seed=7,
    )
    check(
        "工作流注入 prompt/negative_prompt/seed 正确",
        wf["25"]["inputs"]["text"] == "hello world"
        and wf["23"]["inputs"]["text"] == "blurry"
        and wf["20"]["inputs"]["seed"] == 7,
    )

print()
if FAILED == 0:
    print(f"SMOKE TEST PASSED（{PASSED}/8）")
    sys.exit(0)
else:
    print(f"SMOKE TEST FAILED（{PASSED}/8）")
    for reason in _REASONS:
        print(f"  - {reason}")
    sys.exit(1)
