# AI 生图工坊（ai-image-studio）

基于 **ComfyUI API** 的全栈 AIGC 生图平台：网页 / 接口提交提示词与参数 → FastAPI 入队 → Celery 调度 ComfyUI 出图 → 图片落盘并入库，支持用户认证、任务状态查询、图库管理与多模型 AI 对话助手。

> 就业导向项目：覆盖 ComfyUI 工作流产品化、任务队列、鉴权与限流、LLM 多 provider 集成、工程化部署。

## 技术栈

| 层    | 技术                                                 |
| ---- | -------------------------------------------------- |
| 后端   | FastAPI + SQLAlchemy 2 + Pydantic v2               |
| 任务队列 | Celery 5 + Redis（broker / result backend）          |
| 生成引擎 | ComfyUI（API 模式，`http://127.0.0.1:8188`）            |
| 存储   | SQLite（数据）+ 本地文件（图片）                               |
| 前端   | 原生 HTML / CSS / JS 单页（`frontend/index.html`，无构建步骤） |
| 安全   | 标准库自研：PBKDF2 密码哈希 + HS256 JWT                      |

## 功能一览

1. **用户认证**：注册 / 登录返回 JWT，所有业务接口强制登录，数据按用户隔离。
2. **文生图**：提交提示词 → 入队 → Celery 异步出图 → 落盘入库 → 图库可查可下载。
3. **AI 对话助手**（多模态 + RAG + 自定义模型）：
   - 后端代理 LLM，支持 **OpenAI 兼容协议**（OpenAI / DeepSeek / 通义 / vLLM / Ollama）与**本地 llama.cpp**（Qwen3-VL）双 provider 切换；
   - **多模态看图**：上传图片让模型理解画面；
   - **RAG 知识库问答**：基于项目文档检索增强，支持前端动态增删知识库 md；
   - **自定义模型（BYOK）**：用户自带 Base URL + API Key 连接自己的模型，Key 后端加密存储、前端不接触明文；
   - 各模型对话上下文独立、历史落库、可一键清空。
4. **多 Agent 流水线**：一句话生图（提示词优化 → 生图 → 审图），自研轻量编排 + 复用 Celery。
5. **安全与治理**：每用户每日 LLM 额度 + 每分钟频率限流 + LLM 报错脱敏 + API Key 加密存储。
6. **图库管理**：图库列表、单图下载、任务列表 / 详情查询。

## 本机环境（已确认）

- Python 3.13 虚拟环境：`backend/.venv`（依赖已安装）
- Redis：已在 `127.0.0.1:6379` 运行
- ComfyUI 0.34：已在 `127.0.0.1:8188` 运行，GPU 为 RTX 4060 Laptop（8GB）
- **模型情况（重要）**：本机没有传统 SD checkpoint，文生图走 Krea 2 组合加载，工作流 `workflows/Krea2-极清生图流+SeedVR2-int8图像放大.json` 已按本机模型配好：
  - 扩散模型：`Krea2-Moody-Mix-premium_int8_convrot.safetensors`（UNETLoader）
  - 文本编码器：`qwen3vl_4b_fp8_scaled.safetensors`（CLIPLoader，type=`krea2`）
  - VAE：`qwen_image_vae.safetensors`（VAELoader）
  - 出图流程：主采样（KSamplerAdvanced）→ 潜空间 1.5× 放大 → 二次精修采样（KSampler）

## 目录结构

```
ai-image-studio/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI 入口（/health、路由注册、建表）
│   │   ├── config.py            # 环境变量配置（含 LLM / JWT / 额度）
│   │   ├── database.py          # SQLAlchemy 引擎/会话
│   │   ├── models.py            # User / Task / Image / Conversation / Message / UsageRecord ORM
│   │   ├── schemas.py           # Pydantic 请求/响应模型
│   │   ├── security.py          # PBKDF2 密码哈希 + HS256 JWT（纯标准库）
│   │   ├── throttle.py          # 频率限流 + 每日 LLM 额度
│   │   ├── api/                 # auth / generate / tasks / images / chat / conversations 路由
│   │   ├── services/
│   │   │   ├── comfy_client.py  # ComfyUI HTTP 客户端（提交/轮询/下载）
│   │   │   ├── llm_client.py    # 多 provider LLM 客户端（openai / llama）
│   │   │   └── workflow.py      # 工作流模板加载 + {{参数}} 注入
│   │   └── workers/
│   │       ├── celery_app.py    # Celery 实例（含软/硬超时配置）
│   │       └── tasks.py         # 生成任务执行体（状态机 + 自动重试）
│   ├── tests/smoke_test.py      # 无需 ComfyUI/Redis 的端到端冒烟测试
│   ├── requirements.txt
│   ├── Dockerfile
│   └── ruff.toml
├── workflows/
│   └── Krea2-极清生图流+SeedVR2-int8图像放大.json   # 文生图工作流（ComfyUI API 格式）
├── frontend/
│   └── index.html               # 单文件前端（登录 / 生图 / 对话 / 图库）
├── docs/                        # 全流程原理讲解、技术栈选型对比、多端上线路线图等
├── docker-compose.yml
├── start_local.md               # 一键启动本地全链路
└── README.md
```

任务状态机：`pending → processing → completed / failed`（连接类失败自动重试 2 次）。

---

## 本地跑通全流程（Windows）

### 第 0 步：确认前置服务

Redis 与 ComfyUI 已在运行时可跳过。手动确认：

```powershell
# ComfyUI 健康检查，返回 JSON 即正常
Invoke-WebRequest http://127.0.0.1:8188/system_stats -UseBasicParsing

# Redis（若 6379 没监听，先启动 redis-server）
redis-server
```

### 第 1 步：激活虚拟环境（依赖已装好）

```powershell
cd D:\Study-Plan\ai-image-studio\backend
.\.venv\Scripts\Activate.ps1
```

> 如果以后要重装：  
> `py -3.13 -m venv .venv` → 激活 → `pip install -r requirements.txt`

### 第 2 步：先跑冒烟测试（不依赖 ComfyUI/Redis，验证代码链路）

```powershell
python tests\smoke_test.py

# 预期最后一行：SMOKE TEST PASSED（9/9）
```

### 第 3 步：启动 API 服务（终端窗口 A，保持开着）

```powershell
cd D:\Study-Plan\ai-image-studio\backend
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --port 8000
```

看到 `Application startup complete.` 即成功。

### 第 4 步：启动 Celery worker（终端窗口 B，保持开着）

```powershell
cd D:\Study-Plan\ai-image-studio\backend
.\.venv\Scripts\Activate.ps1
celery -A app.workers.celery_app worker --loglevel=info --pool=solo
```

> **Windows 必须加 `--pool=solo`**（Celery 5 的 prefork 池在 Windows 不可用）。  
> 想要并发可改用 `--pool=threads --concurrency=2`；8GB 显存建议 solo 串行出图。  
> 看到 `celery@... ready.` 即成功。

### 第 5 步：打开前端

浏览器直接打开 `frontend/index.html`（前端硬编码后端地址 `http://127.0.0.1:8000`，需先启动 API）。

先**注册一个账号**（用户名 3-64 位字母/数字/下划线，密码至少 8 位），登录后即可生图、对话、查看图库。

> 前端也可通过简单静态服务访问，例如在 `frontend/` 目录下 `python -m http.server 5500`。

### 第 6 步：提交生图任务（命令行方式）

```powershell
# 先注册拿 token
$login = Invoke-RestMethod -Uri http://127.0.0.1:8000/api/auth/register -Method Post `
  -ContentType "application/json" `
  -Body (@{ username="demo"; password="demo12345" } | ConvertTo-Json)
$token = $login.access_token

# 带 token 提交文生图任务
$body = @{
  prompt = "a cute corgi astronaut floating in space, digital art, highly detailed"
  negative_prompt = "blurry, low quality, watermark"
  aspect_ratio = "1:1 (Square)"
  megapixels = 1.0
  seed = -1
} | ConvertTo-Json
Invoke-RestMethod -Uri http://127.0.0.1:8000/api/generate -Method Post `
  -Headers @{ Authorization = "Bearer $token" } -ContentType "application/json" -Body $body
```

> **首次出图较慢**：ComfyUI 要加载 13GB 扩散模型 + 4B 文本编码器，可能 1–3 分钟；之后出图约 30–90 秒。worker 窗口能实时看到进度。

### 第 7 步：查看结果

```powershell
# 任务详情（把 1 换成返回的任务 id），completed 后 images 里有 file_url
Invoke-RestMethod http://127.0.0.1:8000/api/tasks/1 -Headers @{ Authorization = "Bearer $token" }

# 图库列表
Invoke-RestMethod http://127.0.0.1:8000/api/images -Headers @{ Authorization = "Bearer $token" }
```

图片文件同时保存在：

- 平台图库：`backend/data/images/<任务id>/`
- ComfyUI 原始输出：`D:\AI_MOVIE\ComfyUI_windows_portable\ComfyUI\output\`（前缀 Krea2）

---

## API 一览

| 方法     | 路径                                  | 说明                                | 需登录 |
| ------ | ----------------------------------- | --------------------------------- | --- |
| GET    | `/health`                           | 健康检查（含 ComfyUI 地址）                | 否   |
| POST   | `/api/auth/register`                | 注册，返回 JWT                         | 否   |
| POST   | `/api/auth/login`                   | 登录，返回 JWT                         | 否   |
| POST   | `/api/generate`                     | 提交文生图任务（返回 202 + task id）         | 是   |
| GET    | `/api/tasks?status=&limit=&offset=` | 任务列表（状态过滤 / 分页）                   | 是   |
| GET    | `/api/tasks/{id}`                   | 任务详情（含产物图片）                       | 是   |
| GET    | `/api/images`                       | 图库列表                              | 是   |
| GET    | `/api/images/{id}/file`             | 下载图片                              | 是   |
| POST   | `/api/chat`                         | LLM 对话（openai / llama / custom，支持多模态看图 + RAG） | 是   |
| GET    | `/api/conversations/{provider}`     | 拉取指定 provider 对话历史                | 是   |
| DELETE | `/api/conversations/{provider}`     | 清空指定 provider 对话历史                | 是   |
| POST   | `/api/agent/generate`               | 一句话生图（多 Agent 流水线，返回任务 id）      | 是   |
| GET    | `/api/rag/docs`                     | 列出知识库文档（固定 + 动态）                | 是   |
| POST   | `/api/rag/docs`                     | 上传 md 加入知识库                       | 是   |
| DELETE | `/api/rag/docs/{filename}`          | 删除知识库动态文档                        | 是   |
| GET    | `/api/custom-models`                | 列出自定义模型配置（不含 key 明文）            | 是   |
| POST   | `/api/custom-models`                | 保存自定义模型配置（key 加密存储）              | 是   |
| DELETE | `/api/custom-models/{id}`           | 删除自定义模型配置                        | 是   |

Swagger：<http://127.0.0.1:8000/docs>

## 参数说明（Krea 2 工作流）

| 参数                | 说明                                             |
| ----------------- | ---------------------------------------------- |
| `prompt`          | 正向提示词（1–4000 字）                                |
| `negative_prompt` | 负向提示词（默认空，≤4000 字）                             |
| `aspect_ratio`    | 宽高比，作用于 ResolutionSelector 节点，8 档可选（见下）        |
| `megapixels`      | 目标百万像素（0.1–16.0），作用于 ResolutionSelector，默认 1.0 |
| `seed`            | 种子，`-1` 随机、固定数字可复现                             |

宽高比可选值：`1:1 (Square)` · `2:3 (Portrait Photo)` · `3:2 (Photo)` · `3:4 (Portrait Standard)` · `4:3 (Standard)` · `9:16 (Portrait Widescreen)` · `16:9 (Widescreen)` · `21:9 (Ultrawide)`

采样参数（steps / cfg / sampler / scheduler / denoise）已固化在工作流 JSON 中，不在 API 暴露。

## 环境变量（均有默认值，一般不用改）

| 变量                     | 默认值                                  | 说明                     |
| ---------------------- | ------------------------------------ | ---------------------- |
| `COMFYUI_BASE_URL`     | `http://127.0.0.1:8188`              | ComfyUI 地址             |
| `DATABASE_URL`         | `sqlite:///backend/data/aistudio.db` | 数据库                    |
| `IMAGE_STORAGE_DIR`    | `data/images`                        | 图片存储目录                 |
| `REDIS_URL`            | `redis://127.0.0.1:6379/0`           | Celery broker          |
| `WORKFLOW_DIR`         | `../workflows`                       | 工作流模板目录                |
| `WORKFLOW_FILE`        | `Krea2-极清生图流+SeedVR2-int8图像放大.json`  | 工作流模板文件名               |
| `COMFY_TIMEOUT`        | `900`                                | 单次生成超时（秒）              |
| `COMFY_POLL_INTERVAL`  | `2.0`                                | ComfyUI 输出轮询间隔（秒）      |
| `OPENAI_BASE_URL`      | `https://api.openai.com/v1`          | OpenAI 兼容服务地址          |
| `OPENAI_API_KEY`       | (空)                                  | OpenAI 兼容服务密钥          |
| `OPENAI_MODEL`         | `gpt-4o-mini`                        | OpenAI 兼容模型名           |
| `LLAMA_BASE_URL`       | `http://127.0.0.1:8080/v1`           | 本地 llama.cpp server 地址 |
| `LLAMA_API_KEY`        | (空)                                  | llama.cpp server 密钥    |
| `LLAMA_MODEL`          | `llama-3.1-8b`                       | llama.cpp 模型名          |
| `LLAMA_MAX_TOKENS`     | `1024`                               | llama.cpp 最大生成 token   |
| `DEFAULT_LLM_PROVIDER` | `openai`                             | 默认 LLM provider        |
| `JWT_SECRET`           | `change-me-in-production-please`     | JWT 签名密钥（生产必改）         |
| `JWT_EXPIRE_MINUTES`   | `1440`                               | token 有效期（分钟）          |
| `DAILY_CHAT_QUOTA`     | `100`                                | 每用户每日 LLM 额度（-1 不限）    |
| `CORS_ORIGINS`         | `*`                                  | CORS 允许来源（逗号分隔）        |

> 完整模板见 `.env.example`。生产环境务必覆盖 `JWT_SECRET` 与各 LLM API Key。

## 常见问题

1. **worker 启动报 billiard/prefork 错误** → Windows 加 `--pool=solo`。
2. **任务一直 pending** → worker 没启动或没连上 Redis，看 worker 窗口日志。
3. **任务 failed：无法连接 ComfyUI** → 确认 ComfyUI 已启动且端口 8188。
4. **任务 failed：节点 / 模型错误** → 工作流与本机模型不匹配，看 worker 日志里 ComfyUI 返回的完整报错，检查工作流 JSON 中的模型文件名。
5. **对话返回「LLM 服务暂不可用」** → 未配置对应 provider 的 API Key，或 LLM 服务不可达；配好 Key 后即可调用真实模型（未配 Key 时返回占位回复，保证链路可先跑通）。
6. **端口被占用** → uvicorn 改 `--port 8001`，同时把前端 `index.html` 里的 `API` 常量一并改掉。


## Docker 部署（日常开发测试方式）

**架构**：ComfyUI 与 llama.cpp 运行在宿主机（GPU 直通），Redis / API / Worker 容器化。

```
宿主机（Windows）
  ├─ ComfyUI        :8188   ← 生图引擎，GPU 直通
  └─ llama-server   :8080   ← 本地 LLM，GPU 直通（阶段 1 接入）

Docker 容器
  ├─ aistudio-redis   :6379
  ├─ aistudio-api     :8000   ← FastAPI 后端
  └─ aistudio-worker          ← Celery 出图任务
```

### 启动后端（三个容器）

```powershell
cd D:\Study-Plan\ai-image-studio
docker compose up -d --build
```

- `docker compose ps` 查看容器状态（三个都应 `Up`）；
- `docker compose logs -f api` / `-f worker` 跟踪日志；
- API 代码已挂载热更新（`./backend/app:/app/app` + `--reload`），改后端代码无需重新 build；
- 改了 `docker-compose.yml` 本身，用 `docker compose up -d api`（或 `--force-recreate`）重建。

### 启动宿主机服务（ComfyUI + llama）

**ComfyUI**（生图）：宿主机启动后监听 `127.0.0.1:8188`，Worker 容器经 `host.docker.internal:8188` 回调。

**llama.cpp 本地 LLM**（对话，阶段 1）：

```powershell
cd D:\llama\llama-b11105-bin-win-cuda-13.4-x64
.\llama-server.exe -m .\models\Qwen3-8B-Q4_K_M.gguf --host 0.0.0.0 --port 8080 --n-gpu-layers 45 --ctx-size 4096
```

> 关键：`--host 0.0.0.0`（让容器能访问）、`--n-gpu-layers 45`（GPU 加速）、`--ctx-size 4096`（省显存）。  
> 看到 `listening on http://0.0.0.0:8080` 即成功。**该窗口需保持开启**（前台进程，关窗口即停服务）。  
> 对话走 `provider="llama"`（`DEFAULT_LLM_PROVIDER=llama`），模型 Qwen3-8B Q4_K_M 约 8 token/s。

### 国内拉镜像：配置加速器

国内直连 Docker Hub 会超时，需给 Docker Desktop 配镜像加速器（`registry-mirrors` 字段放在 daemon.json **顶层**）：

```json
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://mirror.ccs.tencentyun.com",
    "https://hub-mirror.c.163.com"
  ]
}
```

配置后重启 Docker Desktop，`docker info | Select-String "Registry Mirrors"` 验证生效。

---

## 工作流 JSON 变更指引

工作流模板位于 `workflows/Krea2-极清生图流+SeedVR2-int8图像放大.json`，通过 `{{占位符}}` 注入参数。**变更工作流时遵循以下约定：**

1. **占位符必须与 `build_workflow()` 对齐**：目前支持 `{{prompt}}`、`{{negative_prompt}}`、`{{aspect_ratio}}`、`{{megapixels}}`、`{{seed}}` 五个占位符，注入逻辑在 `backend/app/services/workflow.py`。
2. **加了新占位符，必须同步三处**：
   - `workflow.py` 的 `build_workflow()` 里把参数加进 `params` 字典；
   - `schemas.py` 的 `GenerateRequest` 里加对应字段；
   - `api/generate.py` 里把字段写进 `GenerationTask`，且 `models.py` 加对应列（若需持久化）。
3. **改了文件名 / 目录**：更新 `.env` 的 `WORKFLOW_FILE`（或 config.py 默认值），无需改代码。
4. **模型文件名变了**（UNETLoader / CLIPLoader / VAELoader 里的 `.safetensors`）→ 直接改 JSON 对应节点的 `inputs`，但要确认本机确实有该模型文件，否则任务会 failed。
5. **改完必跑冒烟测试**：`python tests/smoke_test.py`，其中会校验 `{{prompt}}` / `{{negative_prompt}}` / `{{seed}}` / `{{aspect_ratio}}` / `{{megapixels}}` 是否正确注入。若新增了占位符，建议在冒烟测试里补一条对应断言。

> 提示：若在 ComfyUI 界面里可视化编辑工作流，导出 API 格式 JSON 时，把要动态变化的字段值手动改成 `{{占位符}}` 字符串即可，其余结构保持不变。

## 多用户资源调度说明

当前实现面向「单机单人 / 少量用户」，多用户并发出图时资源调度机制如下：

**现状（已具备）**

- **任务隔离**：每个任务归属 `user_id`，任务 / 图库查询均按用户过滤。
- **队列串行**：所有出图任务进入同一条 Celery 队列（`aistudio`），由 worker 按 FIFO 消费。
- **显存保护**：Windows 下建议 `--pool=solo` 串行出图，天然避免多任务同时抢占 8GB 显存导致 OOM。
- **LLM 侧已限流**：对话有每用户每分钟 20 次频率限制 + 每日额度（默认 100 次），但**生图接口目前没有限流 / 排队配额**。

**多用户压力下的风险与建议**

| 问题         | 表现              | 建议方案                                                                      |
| ---------- | --------------- | ------------------------------------------------------------------------- |
| 生图无配额      | 个别用户狂提交占满队列     | 仿照 `throttle.py` 给 `/api/generate` 加每日额度（`UsageRecord.image_count` 字段已预留） |
| 长队无感知      | 用户看不到排队位置       | 任务列表已支持 `pending` 状态，前端可展示排队序号 / 预计等待                                     |
| 公平性        | 无优先级，纯 FIFO     | 引入 Celery 优先级队列（`x-max-priority`）或按用户加权                                   |
| 显存并发       | 8GB 显存扛不住多任务并行  | 保持 solo 串行；若上多卡 / 大显存，改用 `--pool=threads` 并限制并发                           |
| 内存限流失效     | 多 worker 进程各自计数 | `throttle.py` 已注明：多进程部署需把限流迁到 Redis（Lua 脚本 / `INCR` + 过期）                 |
| SQLite 写竞争 | 高并发写库可能锁库       | 多用户规模上去后换 PostgreSQL，Celery 仍用 Redis                                      |

**若要支撑真正的多用户并发**，建议按优先级推进：① 生图加每日额度与限流；② 前端展示排队状态；③ 限流与额度计数迁到 Redis；④ 数据库换 PostgreSQL；⑤ 按需引入优先级队列与多 worker 水平扩展。
