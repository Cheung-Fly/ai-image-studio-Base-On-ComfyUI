# AI 生图工坊 · API 接口文档

> 基于 FastAPI + Celery + SQLite + ComfyUI 的 AIGC 生图/生视频平台后端接口。
>
> - **版本**：0.1.0
> - **Base URL**：`http://127.0.0.1:8000`
> - **接口前缀**：`/api`
> - **交互式文档**：启动后端后访问 `http://127.0.0.1:8000/docs`（Swagger UI）或 `/redoc`

---

## 目录

1. [通用约定](#1-通用约定)
2. [认证接口](#2-认证接口)
3. [生图接口](#3-生图接口)
4. [视频生成接口](#4-视频生成接口)
5. [任务查询接口](#5-任务查询接口)
6. [图库 / 图片下载接口](#6-图库--图片下载接口)
7. [素材上传接口](#7-素材上传接口)
8. [对话助手接口](#8-对话助手接口)
9. [工作流 / 模型预设接口](#9-工作流--模型预设接口)
10. [个人资料接口](#10-个人资料接口)
11. [系统接口](#11-系统接口)
12. [错误码与通用错误响应](#12-错误码与通用错误响应)

---

## 1. 通用约定

### 1.1 鉴权

除「注册」「登录」「健康检查」外，**所有接口都需要鉴权**。在请求头携带：

```
Authorization: Bearer <access_token>
```

`access_token` 由注册或登录接口返回。未携带或 token 无效时返回 `401 Unauthorized`。

### 1.2 请求体

除上传接口使用 `multipart/form-data`、下载接口返回二进制外，其余接口均使用 `application/json`。

### 1.3 分页参数

列表接口统一支持 `limit`（默认 50，最大 200）与 `offset`（默认 0）。

### 1.4 任务状态枚举

任务生成采用**异步模型**：提交后立即返回 `task_id`，实际生成由 Celery worker 异步执行，需轮询任务详情获取最新状态。

| status | 含义 |
|---|---|
| `pending` | 已入队，等待 worker 处理 |
| `processing` | worker 正在生成 |
| `completed` | 生成成功 |
| `failed` | 生成失败（`error` 字段含原因） |

---

## 2. 认证接口

### 2.1 注册

`POST /api/auth/register`

**请求体：**

| 字段 | 类型 | 必填 | 约束 | 说明 |
|---|---|---|---|---|
| username | string | 是 | 3–64 位，仅字母/数字/下划线 | 用户名 |
| password | string | 是 | 8–128 位 | 密码 |

**响应 `201`：**

```json
{
  "access_token": "<jwt-token>",
  "token_type": "bearer",
  "username": "alice"
}
```

**错误：** `409` 用户名已存在。

### 2.2 登录

`POST /api/auth/login`

**请求体：** 同注册（`username` 允许 1–64 位）。

**响应 `200`：** 同注册，返回 `access_token`。

**错误：** `401` 用户名或密码错误。

---

## 3. 生图接口

### 3.1 提交文生图任务

`POST /api/generate`

**请求体：**

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| prompt | string | 是 | — | 正向提示词（1–4000 字符） |
| negative_prompt | string | 否 | `""` | 负向提示词 |
| aspect_ratio | string | 否 | `"1:1 (Square)"` | 宽高比，见下方枚举 |
| megapixels | float | 否 | `1.0` | 目标百万像素（0.1–16.0） |
| seed | int | 否 | `0` | 随机种子，`-1` 表示随机 |
| workflow | string | 否 | `""` | 工作流文件名（空则用默认） |
| model_preset | string | 否 | `""` | 模型组合预设名（空则用工作流内置模型） |

**`aspect_ratio` 可选值：**

```
1:1 (Square)              1:1 方形
2:3 (Portrait Photo)      2:3 竖版照片
3:2 (Photo)               3:2 横版照片
3:4 (Portrait Standard)   3:4 竖版标准
4:3 (Standard)            4:3 标准
9:16 (Portrait Widescreen) 9:16 竖版宽屏
16:9 (Widescreen)         16:9 宽屏
21:9 (Ultrawide)          21:9 超宽屏
```

**响应 `202`：** 返回任务对象（见 [5.2 任务详情响应结构](#52-任务详情响应结构)），此时 `status` 通常为 `pending`。

**请求示例：**

```json
{
  "prompt": "a cute corgi astronaut floating in space, digital art",
  "negative_prompt": "blurry, low quality",
  "aspect_ratio": "16:9 (Widescreen)",
  "megapixels": 1.0,
  "seed": -1,
  "workflow": "",
  "model_preset": ""
}
```

---

## 4. 视频生成接口

### 4.1 提交视频生成任务

`POST /api/generate-video`

> 视频由 MiniMax H3 Ref2VA 工作流生成，支持参考图/视频/音频素材与原生音频输出。

**请求体：**

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| prompt | string | 是 | — | 提示词（1–8000 字符，可用 `[image N]`/`[video N]`/`[audio N]` 引用素材） |
| duration | float | 否 | `4.0` | 视频时长（秒），2.0–15.0 |
| aspect_ratio | string | 否 | `"16:9 (Widescreen)"` | 宽高比（同生图枚举） |
| megapixels | float | 否 | `1.0` | 目标百万像素（0.1–16.0） |
| seed | int | 否 | `0` | 随机种子，`-1` 表示随机 |
| steps | int | 否 | `20` | 基本调度器采样步数（1–100） |
| lora | bool | 否 | `true` | 是否开启加速 Lora |
| ref_images | string[] | 否 | `[]` | 参考图文件名列表（上传接口返回的文件名），最多 9 个 |
| ref_videos | string[] | 否 | `[]` | 参考视频文件名列表，最多 3 个 |
| ref_audios | string[] | 否 | `[]` | 参考音频文件名列表，最多 3 个 |

**参数语义说明：**

- **`lora`（加速 Lora 开关）**：`true` 时走固定 Sigma 加速调度（Turbo Lora），`false` 时走基本调度器（BasicScheduler）。**只有 `lora=false` 时 `steps` 才生效。**
- **素材引用**：素材文件名来自 [素材上传接口](#7-素材上传接口)，在 `prompt` 中用 `[image 1]`、`[video 1]`、`[audio 1]` 按顺序引用（图片→视频→音频依次编号）。

**响应 `202`：** 返回任务对象（`task_type` 为 `"video"`）。

**请求示例：**

```json
{
  "prompt": "一个红衣朝圣者拾级而上，镜头缓慢前推 [image 1]",
  "duration": 4.0,
  "aspect_ratio": "16:9 (Widescreen)",
  "megapixels": 1.0,
  "seed": -1,
  "steps": 20,
  "lora": true,
  "ref_images": ["ref_a.png"],
  "ref_videos": [],
  "ref_audios": []
}
```

---

## 5. 任务查询接口

### 5.1 任务列表

`GET /api/tasks`

**查询参数：**

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| status | string | 无 | 按状态过滤：`pending` / `processing` / `completed` / `failed` |
| limit | int | 50 | 返回条数（上限 200） |
| offset | int | 0 | 偏移量 |

**响应 `200`：** 任务对象数组（按 id 倒序）。

### 5.2 任务详情响应结构

```json
{
  "id": 12,
  "task_type": "image",
  "prompt": "a cute corgi astronaut...",
  "negative_prompt": "",
  "workflow": "",
  "model_preset": "",
  "aspect_ratio": "16:9 (Widescreen)",
  "megapixels": 1.0,
  "seed": 42,
  "status": "completed",
  "error": null,
  "comfy_prompt_id": "abc123...",
  "created_at": "2026-09-22T06:30:00",
  "updated_at": "2026-09-22T06:31:00",
  "images": [
    {
      "id": 5,
      "task_id": 12,
      "filename": "MinimaxH3_00001_.mp4",
      "file_url": "/api/images/5/file",
      "width": null,
      "height": null,
      "created_at": "2026-09-22T06:31:00"
    }
  ]
}
```

> 注：`task_type` 为 `image` / `video`；视频任务的产物也存放在 `images` 数组中（字段名沿用历史命名，实际可能为视频文件）。

### 5.3 查询单个任务

`GET /api/tasks/{task_id}`

- 仅能查询**本人**任务；否则返回 `404`。

---

## 6. 图库 / 图片下载接口

### 6.1 图库列表

`GET /api/images`

**查询参数：** `limit`（默认 50，上限 200）、`offset`（默认 0）。

**响应 `200`：** 图片对象数组（见 [5.2](#52-任务详情响应结构) 中 `images` 元素结构）。

### 6.2 下载图片 / 视频文件

`GET /api/images/{image_id}/file`

- 返回二进制文件流（`Content-Type` 为 `image/png`，视频任务则为实际媒体类型）。
- 仅限本人资产；文件不存在返回 `404`。
- 前端因 `<img>`/`<video>` 无法携带 `Authorization` 头，可用 `fetch` 携带 token 拉取 blob 后展示。

---

## 7. 素材上传接口

`POST /api/upload`

> 上传参考图/视频/音频，落盘到 ComfyUI input 目录，返回文件名供工作流引用。

**请求：** `multipart/form-data`，字段名为 `file`。

**允许的媒体类型与限制：**

| 类型 | 扩展名 | 大小上限 |
|---|---|---|
| image | png / jpg / jpeg / webp | 20 MB |
| video | mp4 / mov / webm / mkv | 500 MB |
| audio | mp3 / wav / flac / aac / m4a / ogg | 100 MB |

**响应 `201`：**

```json
{
  "filename": "ref_a_3f2a9c1d.png",
  "media_type": "image",
  "size": 123456
}
```

- 文件名会加随机后缀防冲突；前端需保存此 `filename`，后续在生图/视频请求中引用。

---

## 8. 对话助手接口

### 8.1 发送对话消息

`POST /api/chat`

**请求体：**

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| message | string | 是 | — | 当前用户消息（1–8000 字符） |
| history | object[] | 否 | `[]` | 历史消息（最多 20 条），元素为 `{role, content}` |
| provider | string | 否 | `"openai"` | LLM 服务：`openai` / `llama` |
| image_base64 | string | 否 | `null` | 可选：图片 data URL（`data:image/...;base64,...`），启用多模态看图 |
| use_rag | boolean | 否 | `false` | 是否启用知识库检索增强（RAG）：`true` 时自动检索项目文档并增强回答 |

**响应 `200`：**

```json
{ "reply": "助手回复文本" }
```

> 受限流与每日额度控制；消息自动落库。

### 8.2 拉取对话历史

`GET /api/conversations/{provider}`

- `provider` 为 `openai` 或 `llama`，其余返回 `400`。

**响应 `200`：**

```json
{
  "provider": "openai",
  "messages": [
    { "role": "user", "content": "你好" },
    { "role": "assistant", "content": "你好！" }
  ]
}
```

### 8.3 清空对话历史

`DELETE /api/conversations/{provider}`

- 响应 `204 No Content`。

---

## 9. 工作流 / 模型预设接口

### 9.1 可用工作流列表

`GET /api/workflows`

**响应 `200`：**

```json
[
  { "file": "MiniMaxH3_ref2va_video.json", "is_default": false },
  { "file": "krea2.json", "is_default": true }
]
```

### 9.2 模型组合预设列表

`GET /api/model-presets`

**响应 `200`：**

```json
[
  { "name": "krea2-moody", "display_name": "Krea2 Moody 风格" }
]
```

---

## 10. 个人资料接口

`GET /api/me`

**响应 `200`：**

```json
{
  "username": "alice",
  "created_at": "2026-09-20T10:00:00",
  "task_count": 12,
  "image_count": 8
}
```

---

## 11. 系统接口

### 11.1 健康检查

`GET /health`（无需鉴权）

**响应 `200`：**

```json
{
  "status": "ok",
  "comfyui_base_url": "http://127.0.0.1:8188"
}
```

---

## 12. 错误码与通用错误响应

| 状态码 | 含义 | 场景 |
|---|---|---|
| 400 | 请求参数错误 | 非法宽高比、非法状态、不支持的 provider/文件类型、空文件 |
| 401 | 未认证 / token 无效 | 缺少 `Authorization` 头或 token 过期 |
| 404 | 资源不存在 | 任务/图片不存在、图片文件丢失 |
| 409 | 冲突 | 用户名已存在 |
| 413 | 文件过大 | 上传超出大小上限 |
| 422 | 校验失败 | 请求体字段不满足 Pydantic 约束 |
| 502 | 上游服务错误 | LLM 服务不可用 |

**通用错误响应格式：**

```json
{ "detail": "错误描述" }
```

---

## 附：快速上手（curl 示例）

```bash
# 1. 注册并获取 token
curl -X POST http://127.0.0.1:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"password123"}'

# 2. 提交文生图任务（用上一步返回的 token）
curl -X POST http://127.0.0.1:8000/api/generate \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"a cute corgi astronaut","aspect_ratio":"16:9 (Widescreen)","seed":-1}'

# 3. 轮询任务状态
curl http://127.0.0.1:8000/api/tasks/1 \
  -H "Authorization: Bearer <token>"

# 4. 下载产物（image_id 见任务详情 images[0].id）
curl -OJ http://127.0.0.1:8000/api/images/1/file \
  -H "Authorization: Bearer <token>"
```
