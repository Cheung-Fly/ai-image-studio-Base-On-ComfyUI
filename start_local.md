# 一键启动本地全链路（Windows PowerShell）

```powershell
# 1) 首次：创建虚拟环境并安装依赖
cd backend
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2) 启动 Redis（若未运行，见 README）
redis-server

# 3) 启动 API
uvicorn app.main:app --reload --port 8000

# 4) 另开终端启动 Celery worker（Windows 必须 --pool=solo 或 threads）
cd backend
.\.venv\Scripts\Activate.ps1
celery -A app.workers.celery_app worker --loglevel=info --pool=solo --concurrency=2
```

验证：

```powershell
curl http://127.0.0.1:8000/health
curl -X POST http://127.0.0.1:8000/api/generate -H "Content-Type: application/json" -d '{"prompt":"a cute corgi astronaut, digital art","width":1024,"height":1024,"steps":24}'
curl http://127.0.0.1:8000/api/tasks/1
```

Swagger 文档：http://127.0.0.1:8000/docs

## 启动 Vue 前端

另开 PowerShell 窗口，在仓库根目录执行：

```powershell
cd frontend
npm ci
npm run dev
```

打开 Vite 显示的地址（默认 `http://127.0.0.1:5173`）。开发服务器会将 `/api` 请求代理到 `http://127.0.0.1:8000`。
