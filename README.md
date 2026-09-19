# AI 生图工坊（ai-image-studio）

基于 **ComfyUI API** 的全栈 AIGC 生图平台：网页 / 接口提交提示词与参数 → FastAPI 入队 → Celery 调度 ComfyUI 出图 → 图片落盘并入库，支持任务状态查询与图库管理。

> 就业导向项目：覆盖 ComfyUI 工作流产品化、任务队列、API 集成、工程化部署。

## 技术栈



| 层            | 技术                                      |
| ------------ | --------------------------------------- |
| 后端           | FastAPI + SQLAlchemy 2 + Pydantic v2    |
| 任务队列         | Celery 5 + Redis                        |
| 生成引擎         | ComfyUI（API 模式，`http://127.0.0.1:8188`） |
| 存储           | SQLite（数据）+ 本地文件（图片）                    |
| 前端（第 2 周里程碑） | Vue 3 + Vite + Pinia                    |

## 本机环境（已确认）



* Python 3.13 虚拟环境：`backend/.venv`（依赖已安装）

* Redis：已在 `127.0.0.1:6379` 运行

* ComfyUI 0.34：已在 `127.0.0.1:8188` 运行，GPU 为 RTX 4060 Laptop（8GB）

* **模型情况（重要）**：本机没有传统 SD checkpoint，文生图走 Krea 2 组合加载，工作流 `workflows/txt2img_api.json` 已按本机模型配好：


  * 扩散模型：`Krea2-Moody-Mix-premium_int8_convrot.safetensors`（UNETLoader）

  * 文本编码器：`qwen3vl_4b_fp8_scaled.safetensors`（CLIPLoader，type=`krea2`）

  * VAE：`qwen_image_vae.safetensors`（VAELoader）

  * 潜空间：`EmptySD3LatentImage`（Krea2 是 SD3 系 latent，不能用 EmptyLatentImage）

## 目录结构



```
ai-image-studio/

├── backend/

│   ├── app/

│   │   ├── main.py              # FastAPI 入口（/health、路由注册、建表）

│   │   ├── config.py            # 环境变量配置

│   │   ├── database.py          # SQLAlchemy 引擎/会话

│   │   ├── models.py            # 任务/图片 ORM

│   │   ├── schemas.py           # 请求/响应模型

│   │   ├── api/                 # generate / tasks / images 路由

│   │   ├── services/

│   │   │   ├── comfy\_client.py  # ComfyUI HTTP 客户端（提交/轮询/下载）

│   │   │   └── workflow.py      # 工作流模板加载 + {{参数}} 注入

│   │   └── workers/

│   │       ├── celery\_app.py    # Celery 实例

│   │       └── tasks.py         # 生成任务执行体（状态机）

│   ├── tests/smoke\_test.py      # 无需 ComfyUI/Redis 的端到端冒烟测试

│   ├── requirements.txt

│   └── Dockerfile

├── workflows/

│   └── txt2img\_api.json         # 文生图工作流（ComfyUI API 格式）

├── docker-compose.yml

└── README.md
```

任务状态机：`pending → processing → completed / failed`（连接类失败自动重试 2 次）。



***

## 本地跑通全流程（Windows）

### 第 0 步：确认前置服务

Redis 与 ComfyUI 已在运行时可跳过。手动确认：



```
\# ComfyUI 健康检查，返回 JSON 即正常

Invoke-WebRequest http://127.0.0.1:8188/system\_stats -UseBasicParsing

\# Redis（若 6379 没监听，先启动 redis-server）

redis-server
```

### 第 1 步：激活虚拟环境（依赖已装好）



```
cd D:\学习规划\ai-image-studio\backend

.\\.venv\Scripts\Activate.ps1
```

> 如果以后要重装：
>
> `py -3.13 -m venv .venv`
>
>  → 激活 → 
>
> `pip install -r requirements.txt`

### 第 2 步：先跑冒烟测试（不依赖 ComfyUI/Redis，验证代码链路）



```
python tests\smoke\_test.py

\# 预期最后一行：SMOKE TEST PASSED（8/8）
```

### 第 3 步：启动 API 服务（终端窗口 A，保持开着）



```
cd D:\学习规划\ai-image-studio\backend

.\\.venv\Scripts\Activate.ps1

uvicorn app.main:app --reload --port 8000
```

看到 `Application startup complete.` 即成功。

### 第 4 步：启动 Celery worker（终端窗口 B，保持开着）



```
cd D:\学习规划\ai-image-studio\backend

.\\.venv\Scripts\Activate.ps1

celery -A app.workers.celery\_app worker --loglevel=info --pool=solo
```

> **Windows 必须加&#x20;**
>
> `--pool=solo`
>
> （Celery 5 的 prefork 池在 Windows 不可用）。
> 想要并发可改用 
>
> `--pool=threads --concurrency=2`
>
> ；8GB 显存建议 solo 串行出图。
> 看到 
>
> `celery@... ready.`
>
>  即成功。

### 第 5 步：提交一个真实生图任务

最简单：浏览器打开 Swagger 文档 [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) →

找到 `POST /api/generate` → Try it out → 用下面的 body → Execute。



```
{

&#x20; "prompt": "a cute corgi astronaut floating in space, digital art, highly detailed",

&#x20; "negative\_prompt": "blurry, low quality, watermark",

&#x20; "width": 1024,

&#x20; "height": 1024,

&#x20; "steps": 24,

&#x20; "cfg": 3.5,

&#x20; "seed": -1

}
```

或用 PowerShell 命令行：



```
\$body = @{

&#x20; prompt = "a cute corgi astronaut floating in space, digital art, highly detailed"

&#x20; negative\_prompt = "blurry, low quality, watermark"

&#x20; width = 1024; height = 1024; steps = 24; cfg = 3.5; seed = -1

} | ConvertTo-Json

Invoke-RestMethod -Uri http://127.0.0.1:8000/api/generate -Method Post -ContentType "application/json" -Body \$body
```

> **首次出图较慢**
>
> ：ComfyUI 要加载 13GB 扩散模型 + 4B 文本编码器，可能 1–3 分钟；之后出图约 30–90 秒。worker 窗口能实时看到进度。

### 第 6 步：查看结果



```
\# 任务详情（把 1 换成返回的任务 id），completed 后 images 里有 file\_url

Invoke-RestMethod http://127.0.0.1:8000/api/tasks/1

\# 图库列表

Invoke-RestMethod http://127.0.0.1:8000/api/images
```

图片文件同时保存在：



* 平台图库：`backend/data/images/<任务id>/`

* ComfyUI 原始输出：`D:\AI_MOVIE\ComfyUI_windows_portable\ComfyUI\output\`（前缀 aistudio）

浏览器直接打开下载链接即可看图：

`http://127.0.0.1:8000/api/images/1/file`



***

## API 一览



| 方法   | 路径                                  | 说明                        |
| ---- | ----------------------------------- | ------------------------- |
| GET  | `/health`                           | 健康检查（含 ComfyUI 地址）        |
| POST | `/api/generate`                     | 提交文生图任务，返回任务对象（含 task id） |
| GET  | `/api/tasks?status=&limit=&offset=` | 任务列表（状态过滤 / 分页）           |
| GET  | `/api/tasks/{id}`                   | 任务详情（含产物图片）               |
| GET  | `/api/images`                       | 图库列表                      |
| GET  | `/api/images/{id}/file`             | 下载图片                      |

Swagger：[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

## 参数建议（Krea 2）



* 分辨率：原生约 100 万像素，优先 `1024x1024` / `832x1216` / `1216x832`，不建议 512

* steps：24–40；cfg：3.0–4.0；sampler 已固定 euler + simple

* seed：`-1` 随机，固定数字可复现

## 环境变量（均有默认值，一般不用改）



| 变量                  | 默认值                                  | 说明            |
| ------------------- | ------------------------------------ | ------------- |
| `COMFYUI_BASE_URL`  | `http://127.0.0.1:8188`              | ComfyUI 地址    |
| `DATABASE_URL`      | `sqlite:///backend/data/aistudio.db` | 数据库           |
| `IMAGE_STORAGE_DIR` | `data/images`                        | 图片存储目录        |
| `REDIS_URL`         | `redis://127.0.0.1:6379/0`           | Celery broker |
| `WORKFLOW_DIR`      | `../workflows`                       | 工作流模板目录       |

## 常见问题



1. **worker 启动报 billiard/prefork 错误** → Windows 加 `--pool=solo`。

2. **任务一直 pending** → worker 没启动或没连上 Redis，看 worker 窗口日志。

3. **任务 failed：无法连接 ComfyUI** → 确认 ComfyUI 已启动且端口 8188。

4. **任务 failed：节点 / 模型错误** → 说明工作流与本机模型不匹配，看 worker 日志里 ComfyUI 返回的完整报错，检查 `workflows/txt2img_api.json` 中的模型文件名。

5. **端口被占用** → uvicorn 改 `--port 8001`，或释放占用进程。

## Docker 部署（进阶，后续里程碑）

ComfyUI 运行在宿主机（GPU 直通），Redis / API / Worker 容器化：



```
docker compose up -d --build
```

容器通过 `host.docker.internal` 访问宿主机 ComfyUI，数据挂载在 `./data/`。