# AI 生图工坊 · API 接口文档

> 基于 FastAPI + Celery + SQLite + ComfyUI 的个人 AIGC 生图/生视频助手接口。
> 当前为无需登录的单实例使用模式，不代表多租户或 SaaS 支持。
>
> - **版本**：0.1.0
> - **Base URL**：`http://127.0.0.1:8000`
> - **接口前缀**：`/api`
> - **交互式文档**：启动后端后访问 `http://127.0.0.1:8000/docs`（Swagger UI）或 `/redoc`

---

## 目录

1. [通用约定](#1-通用约定)
2. [访问模式](#2-访问模式)
3. [生图接口](#3-生图接口)
4. [视频生成接口](#4-视频生成接口)
5. [任务查询接口](#5-任务查询接口)
6. [图库 / 图片下载接口](#6-图库--图片下载接口)
7. [素材上传接口](#7-素材上传接口)
8. [对话助手接口](#8-对话助手接口)
9. [工作流 / 模型预设接口](#9-工作流--模型预设接口)
10. [个人资料接口](#10-个人资料接口)
11. [Agent 接口](#11-agent-接口)
12. [系统接口](#12-系统接口)
13. [错误码与通用错误响应](#13-错误码与通用错误响应)

---

## 1. 通用约定

### 1.1 访问方式

当前所有业务接口均无需登录或携带认证头；/api/auth/login 与 /api/auth/register 已移除。部署时应通过回环地址、SSH 隧道或可信网络限制访问。

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

## 2. 访问模式

本应用为单实例匿名访问，不创建账号、不签发 token。业务数据使用内部本地资料归属，浏览器不需要保存认证凭据。

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
- 前端通过 `fetch` 拉取媒体并创建 blob URL 预览；接口无需认证头。

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
| provider | string | 否 | `"openai"` | LLM 服务：`openai` / `llama` / `custom` |
| image_base64 | string | 否 | `null` | 可选：图片 data URL（`data:image/...;base64,...`），启用多模态看图 |
| use_rag | boolean | 否 | `false` | 是否启用知识库检索增强（RAG）：`true` 时自动检索项目文档并增强回答 |
| custom_config_id | int | 否 | `null` | `provider=custom` 时选择当前账号的自定义模型配置 |

**响应 `200`：**

```json
{ "reply": "助手回复文本" }
```

> 受限流与每日额度控制。内置 `openai` / `llama` 对话会落库；`custom` 对话仅保留在当前前端会话中。启用 RAG 时，当前请求会使用检索上下文构造 prompt。

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

### 8.4 流式对话

`POST /api/chat/stream`

请求字段与 `POST /api/chat` 相同；支持内置模型、自定义模型、RAG 和多模态图片输入。响应类型为 `text/event-stream`，文本片段以 SSE 事件逐段返回：

```text
data: {"delta":"你好"}

data: {"delta":"，世界"}

data: [DONE]
```

模型在响应开始后发生错误时，返回 `event: error`，其 `data` 为 JSON 格式的通用错误信息。`POST /api/chat` 保持原有 JSON 响应，供未迁移的客户端继续使用。使用 RAG 时，检索基于当前问题；图片和历史消息不会并入 RAG prompt。

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

## 11. Agent 接口

### 11.1 固定流水线生图

`POST /api/agent/generate`

兼容接口：提交“一句话生图”固定流程（提示词优化 → 生图 → 审图），返回 Celery 任务 ID。当前前端不再提供固定流水线表单，创作入口统一使用 MCP Agent。

**请求体：**

| 字段 | 类型 | 必填 | 约束 | 说明 |
|---|---|---|---|---|
| request | string | 是 | 1–2000 字符 | 用户自然语言描述 |

**响应 `202`：**

```json
{
  "task_id": "9e0f7f20-....",
  "request": "帮我画一只在太空的柯基"
}
```

### 11.2 MCP Agent 控制生成后端

`POST /api/agent/mcp`

提交自然语言创作或控制任务，Agent 通过 MCP 调用所选生成后端：本地 ComfyUI 可生成图片/视频、查询状态和队列、管理工作流等；RunningHub 使用已配置的自定义工作流生成媒体。中断、清理队列、删除素材、释放显存及清除偏好等有影响的本地操作需要用户确认。

**请求体：**

| 字段 | 类型 | 必填 | 约束 / 默认值 | 说明 |
|---|---|---|---|---|
| request | string | 是 | 1–2000 字符 | 当前自然语言请求 |
| history | array | 否 | 最多 20 条 | 历史消息；每条含 `role`（`user` 或 `assistant`）和 `content` |
| provider | string | 否 | `""`、`openai`、`llama` 或 `custom`；默认 `""` | 空值使用服务端默认 provider |
| custom_config_id | integer | 条件必填 | `provider=custom` 时必须是当前用户的有效配置 ID | 选择自定义模型 |
| use_rag | boolean | 否 | `false` | 将知识库检索结果作为 Agent 参考资料 |
| generation_backend | string | 否 | `comfyui` 或 `runninghub`；默认 `comfyui` | 选择生成后端，与 LLM provider 分开 |
| quality_review_enabled | boolean | 否 | `false` | 开启图片/视频生成的视觉质量 Harness；当前所选模型需支持图片输入。低于 8/10 时最多自动优化重生成 2 次；视频评审均匀抽取最多 5 帧。RunningHub 重试会再次提交远程任务，可能产生费用 |

**响应 `202`：**

```json
{
  "task_id": "2bf8a8d0-....",
  "request": "先看下状态，再生成一张赛博风格城市夜景"
}
```

### 11.2.1 RunningHub 工作流配置

- `GET /api/runninghub/config`：读取工作流 ID 和提示词节点映射，不返回 API Key 明文。
- `PUT /api/runninghub/config`：保存单个 RunningHub 工作流配置；首次保存必须提供 `api_key`，后续可省略或传空字符串以保留已保存的密钥。
- `POST /api/runninghub/workflow-nodes`：读取给定工作流的文本输入，用于选择正向和可选的负向提示词映射。首次配置时请求需包含 `api_key`。

保存请求示例：

```json
{
  "api_key": "用户的 RunningHub API Key",
  "workflow_id": "workflow-id",
  "prompt_node_id": "6",
  "prompt_field": "text",
  "negative_prompt_node_id": "7",
  "negative_prompt_field": "text"
}
```

API Key 使用应用配置的高熵随机 `ENCRYPTION_KEY` 做版本化认证加密存储，读取接口不会返回密钥。新密钥未配置或短于 32 个字符时，保存接口返回 `503`。旧版密文可用原 `ENCRYPTION_KEY` 解密；迁移或轮换时可临时设置 `LEGACY_ENCRYPTION_KEY`，旧版未显式配置密钥的部署需使用当时的示例默认值。使用 `app.cli.rotate_secrets` 迁移后即可移除旧密钥。RunningHub 成功输出会下载到 MCP Agent 媒体目录，并可通过 `GET /api/agent/files` 和 `GET /api/agent/files/{filename}` 访问。输出下载只允许 HTTPS 及受信任的 RunningHub 文件域名。

### 11.3 查询 Agent 任务结果

`GET /api/agent/result/{task_id}`

**响应 `200`：**

```json
{
  "task_id": "2bf8a8d0-....",
  "state": "SUCCESS",
  "progress": null,
  "result": {
    "answer": "已完成：图片已生成，路径如下 ...",
    "steps": [
      {"tool": "comfy_status", "arguments": {}, "result": "{\"ok\":true,...}"},
      {"tool": "comfy_generate_image", "arguments": {"prompt":"..."}, "result": "{\"ok\":true,...}"}
    ]
  }
}
```

任务执行期间，`state` 可为 `STARTED` 或 `PROGRESS`；`PROGRESS` 时 `progress.current_tool` 和 `progress.running` 表示正在执行的工具，`progress.steps` 包含已完成的 MCP 工具调用记录。完成后 `result` 返回最终回答和全部调用步骤。`result` 内输出文件包含 `filename`、`kind` 和本机路径。

开启 Harness 后，支持的图片/视频生成工具结果还包含 `quality_review`，其中 `status` 为 `passed`、`retry_limit`、`retry_failed` 或 `error`，`attempts` 列出每轮评分和问题；只有达到 8/10 才标记为通过。模型不支持视觉输入或审查失败时会明确显示为未完成，不会默认为通过；RunningHub 重新生成会再次提交远程任务。

### 11.4 预览 MCP Agent 生成文件

`GET /api/agent/files/{filename}`

返回 MCP Agent 生成的图片或视频二进制内容，无需登录。此接口只从 MCP Agent 专用输出目录读取文件。

`GET /api/agent/files` 返回 MCP Agent 已生成媒体的列表，含文件名、类型和生成时间，供结果库恢复历史结果。

### 11.5 确认 MCP Agent 有影响的操作

`POST /api/agent/confirm/{task_id}`

Agent 请求中断生成、清理队列、删除排队任务/输入素材或释放显存时，任务状态变为 `CONFIRMATION_REQUIRED`，`GET /api/agent/result/{task_id}` 的 `confirmation` 字段包含确认信息。需要在 5 分钟内提交：

```json
{
  "confirmation_id": "从任务状态中取得的确认 ID",
  "confirmed": true
}
```

`confirmed` 为 `false` 时操作不会执行；过期或已处理的确认请求返回 `409`。

### 11.6 个人助手偏好记忆

记忆是个人实例级别的单份 ComfyUI 偏好，不按账号拆分。

- `GET /api/agent/memory`：读取全部偏好。
- `PATCH /api/agent/memory`：只更新请求体中提供的字段；支持 `style`、`model_preset`、`workflow`、`aspect_ratio`、`megapixels`、`negative_prompt`、`notes`。
- `DELETE /api/agent/memory`：清除全部偏好并返回空记忆。

例如：

```json
{
  "style": "soft watercolor, warm natural light",
  "model_preset": "",
  "workflow": "",
  "aspect_ratio": "16:9 (Widescreen)",
  "megapixels": 2.0,
  "negative_prompt": "blurry, low quality",
  "notes": "Prefer a clear subject and uncluttered composition"
}
```

工作流和模型预设必须是当前可用项；宽高比和百万像素使用与生图接口相同的约束。MCP Agent 会把已保存偏好作为生成默认值，用户当前请求中的明确参数优先。

### 11.7 导演分镜

`POST /api/director/storyboard` 接收一集或一段剧本，返回可编辑的分镜表。

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| script | string | 是 | — | 剧本文本，去除首尾空格后为 1–50,000 字符 |
| video_title | string | 否 | `""` | 视频名称，最多 128 字符 |
| provider | string | 否 | `"openai"` | 模型提供方：`openai` / `llama` / `custom` |
| custom_config_id | int | 否 | `null` | `provider=custom` 时选择自定义模型配置 |
| skill_ids | int[] | 否 | `[]` | 最多选择 10 个当前用户已保存的 Skill |

成功响应包含 `video_title`、`theme`、`shooting_time`、`deadline`、`props`、`characters` 和 `shots`。每个镜头包含 `shot_no`、`framing`、`camera_movement`、`visual_description`、`duration_seconds`、`dialogue`、`sound_effects`、`prompt`，以及制作、提交和审核字段；镜头数为 1–120。

- `GET /api/director/skills`、`POST /api/director/skills`、`PUT /api/director/skills/{skill_id}` 和 `DELETE /api/director/skills/{skill_id}`：列出和管理当前用户的分镜 Skill；指导内容最多 10,000 字符。
- `POST /api/director/skills/import`：以 `multipart/form-data` 上传 `file` 字段导入 `SKILL.md`，最大 256 KB。文件夹导入只读取唯一的 `SKILL.md`，支持 YAML frontmatter 中的 `name`、`description` 和 Markdown 正文；其他文件不会上传或执行。

`POST /api/director/export` 接收当前编辑后的同一分镜结构，返回 `.xlsx` 工作簿（二进制），可用于下载和后续制作。

### 11.8 自动图片 Agent

- `POST /api/auto-image/plan`：以 `multipart/form-data` 上传整剧 `.xlsx` 分镜表并建立资产清单。字段包括 `file`、`episode_index`（默认 1，作为首个试跑分集）、`provider` 和可选的 `custom_config_id`。接口解析全部工作表后将资产规划提交给 Celery Worker，并立即返回 `planning` 批次；只规划资产，不提交生图任务。
- `GET /api/auto-image/batches?limit=20` 和 `GET /api/auto-image/batches/{batch_id}`：读取最近资产清单、任务状态和按时间排列的 `logs`。规划事件包含工作簿解析、后台排队、LLM 请求成功/失败与耗时、资产清单保存；后续也记录设定图生成阶段和节流后的 RunningHub 状态变化。LLM 规划仍使用 300 秒请求超时，超时详情会保存在批次日志中。
- `GET /api/auto-image/batches/{batch_id}/episodes/{episode_index}/storyboard`：读取指定分集的分镜，镜头数组顺序就是交付顺序。
- `POST /api/auto-image/batches/{batch_id}/start`：只启动指定的一集。请求包含 `episode_index`、`generation_backend`（`comfyui` / `runninghub`）、`workflow_file`、`aspect_ratio`、`megapixels`、`seed`、`steps` 和 `refine_steps`。RunningHub 后端还需提供 `runninghub` 工作流 ID 与正向提示词节点/字段；API Key 使用已保存的加密配置。
- `POST /api/auto-image/batches/{batch_id}/assets/{asset_id}/retry`：重新生成指定失败资产，请求使用与 `start` 相同的生成设置；只接受属于所选分集且仍为失败状态的资产，每项最多手动重试 3 次。批次响应中的资产包含 `error`、`error_code` 和 `retry_available`，可用于展示失败详情和可重试状态。
- RunningHub 自动映射工作流中名为 `width`、`height`、`aspect_ratio`、`resolution`、`megapixels`、`seed`、`steps` 等标准输入，并要求任务返回图片。ComfyUI 后端使用所选图片工作流。
- `GET /api/auto-image/batches/{batch_id}/episodes/{episode_index}.zip`：本集全部设定图完成后下载素材包，包含 `storyboard.json`、`assets/` 和 `assets_manifest.json`，可交给自动视频栏目继续制作。
- `GET /api/auto-image/batches/{batch_id}/series.zip`：将全剧按原分集顺序打包为一个 ZIP 根文件夹，每集包含 `storyboard.json`、按角色/场景/道具分类的 `assets/` 和 `assets_manifest.json`；根目录含 `delivery_manifest.json`，记录集次和镜头顺序。所有必需设定图完成后才可导出；解压根文件夹可直接导入 Agent3。总解压大小最多 4 GB、最多 10,000 个文件，与 Agent3 整剧导入限制一致。

### 11.9 自动视频 Agent

`POST /api/video-agent/episodes` 以 `multipart/form-data` 导入一个 `.zip` 单集素材包：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| file | file | 是 | — | ZIP 内必须包含一份分镜 `.xlsx` 或 `storyboard.json`，可同时包含图片、视频和音频素材 |
| provider | string | 否 | `openai` | 素材匹配模型：`openai` / `llama` / `custom` |
| custom_config_id | int | 否 | `null` | `provider=custom` 时选择自定义模型配置 |

Agent 根据分镜内容和素材文件名提出镜头匹配建议；用户可逐镜调整匹配、时长后确认制作。匹配阶段只向 LLM 发送分镜文本和文件名，不上传素材内容。素材包压缩后最大 1 GB、解压后最大 4 GB，最多 200 个媒体文件；单张图片不超过 20 MB、视频不超过 500 MB、音频不超过 100 MB。

- `GET /api/video-agent/episodes`：列出最近单集任务；`GET /api/video-agent/episodes/{episode_id}`：读取单集、素材建议和逐镜状态。
- `POST /api/video-agent/episodes/{episode_id}/start`：确认镜头参数并串行启动制作。请求体示例：

```json
{
  "aspect_ratio": "16:9 (Widescreen)",
  "megapixels": 1.0,
  "generation_backend": "comfyui",
  "workflow_file": "MiniMaxH3_ref2va_video.json",
  "shots": [
    {"shot_no": "1", "duration_seconds": 6, "asset_ids": ["a0001"]}
  ]
}
```

`generation_backend` 可选 `comfyui` 或 `runninghub`；ComfyUI 当前只接受兼容自动视频管线的模板。RunningHub 请求额外提供 `runninghub` 对象（`workflow_id`、`prompt_node_id`、`prompt_field`，以及可选负向提示词映射），运行时会上传已选素材并按 `LoadImage`、`LoadVideo`、`LoadAudio` 节点顺序映射。工作流可通过标准 `width`、`height`、`duration`、`frames`、`fps` 等输入接收画幅与时长参数；提交前会检查提示词映射和媒体输入容量。

每个镜头可使用最多 9 张图片、3 个视频和 3 个音频；音频参考需要同时至少选择一张图片或一个视频。单镜不足 2 秒时按 2 秒生成并在成片中裁短；超过 15 秒时拆分为不超过 15 秒的连续片段，后续片段参考前一段。所有镜头按分镜顺序执行，结束后统一画幅并合成 MP4。取消 RunningHub 单集会停止本地等待与后续处理；RunningHub 远端已提交的任务可能仍会继续运行。

- `POST /api/video-agent/episodes/{episode_id}/cancel`：取消排队或执行中的单集。
- `POST /api/video-agent/episodes/{episode_id}/resume`：从失败镜头继续，或在镜头均完成但合成失败时重新合成。
- `GET /api/video-agent/episodes/{episode_id}/assets/{asset_id}`：读取已导入素材；`GET /api/video-agent/episodes/{episode_id}/output`：下载完成的 MP4。

整剧目录导入和批量制作使用以下接口：

- `POST /api/video-agent/series`：以 `multipart/form-data` 上传同一个根目录中的多个文件。重复的 `files` 字段携带文件，文件名保留 `整剧名称/第1集/分镜.xlsx` 这样的相对路径；可选字段 `provider` 和 `custom_config_id` 与单集接口相同。每个分集子目录必须且只能包含一份 `.xlsx` 或名为 `storyboard.json` / `分镜.json` 的 JSON 分镜。支持图片、视频和音频素材；最多 100 集、10,000 个文件、总量 4 GB，每集最多 200 个媒体文件。分集按文件夹名称自然排序，例如“第2集”排在“第10集”之前。
- `GET /api/video-agent/series?limit=20`：列出最近整剧任务；`GET /api/video-agent/series/{series_id}`：读取整剧、全部分集、素材建议和逐镜状态。
- `GET /api/video-agent/series/workflows`：列出自动视频管线支持的 ComfyUI 模板。
- `POST /api/video-agent/series/{series_id}/start`：一次提交所有分集的确认设置。每个分集必须恰好出现一次，且 `episode_id` 必须属于该整剧。示例：

```json
{
  "episodes": [
    {
      "episode_id": 41,
      "settings": {
        "aspect_ratio": "16:9 (Widescreen)",
        "megapixels": 1.0,
        "shots": [
          {"shot_no": "1", "duration_seconds": 6, "asset_ids": ["a0001"]}
        ]
      }
    }
  ]
}
```

- `POST /api/video-agent/series/{series_id}/cancel`：取消排队或制作中的整剧及当前 ComfyUI 任务。
- `POST /api/video-agent/series/{series_id}/resume`：从失败分集继续；已完成分集会跳过，后续分集仅在前一集成功后开始。

---

## 12. 系统接口

### 12.1 ComfyUI 网页入口配置

`GET /api/instance`（无需登录）返回浏览器打开 ComfyUI 所需的非敏感连接信息：

```json
{
  "comfyui_web_url": "http://127.0.0.1:8188",
  "default_llm_provider": "openai"
}
```

`COMFYUI_WEB_URL` 面向用户浏览器；它与后端连接 ComfyUI API 的 `COMFYUI_BASE_URL` 分开配置。远程访问时，网页地址必须是浏览器可达的 HTTP 或 HTTPS 地址。
前端允许用户保存浏览器本地覆盖地址，保存在当前浏览器的单实例本地配置中；恢复默认会重新读取 `COMFYUI_WEB_URL`。此覆盖不会写入后端，也不会更改 MCP Agent 使用的 `COMFYUI_BASE_URL`。Agent 通过后端 MCP 工具直接控制 ComfyUI，无需浏览器自动化。
`default_llm_provider` 返回当前默认的 `openai` 或 `llama` provider；用户在 Agent 区选择其他模型后，该次请求使用所选模型。

### 12.2 本地依赖自检

`GET /api/instance/diagnostics`（无需登录）检查数据库、Redis、Celery Worker、ComfyUI API、媒体目录和默认 LLM 配置是否就绪。响应不包含内部服务地址、密钥或上游异常内容；LLM 检查只确认配置是否完整，不会发起真实调用。

```json
{
  "status": "ready",
  "checks": [
    {"id": "database", "label": "数据库", "status": "ok", "message": "数据库连接正常。"},
    {"id": "redis", "label": "Redis 队列", "status": "ok", "message": "Redis 连接正常。"}
  ]
}
```

完整检查项为 `database`、`redis`、`worker`、`comfyui`、`storage` 和 `llm`。核心依赖全部正常时 `status` 为 `ready`，否则为 `degraded`；LLM 配置缺失会单独显示 `warning`，不影响 ComfyUI 基础生成就绪状态。

### 12.3 健康检查

`GET /health`（无需鉴权），仅用于确认 API 进程存活，不检查依赖，也不返回内部服务地址。

**响应 `200`：**

```json
{"status": "ok"}
```

---

## 13. 错误码与通用错误响应

| 状态码 | 含义 | 场景 |
|---|---|---|
| 400 | 请求参数错误 | 非法宽高比、非法状态、不支持的 provider/文件类型、空文件 |
| 404 | 资源不存在 | 任务/图片不存在、图片文件丢失 |
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
# 提交文生图任务，无需注册或登录
curl -X POST http://127.0.0.1:8000/api/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt":"a cute corgi astronaut","aspect_ratio":"16:9 (Widescreen)","seed":-1}'

# 轮询任务状态
curl http://127.0.0.1:8000/api/tasks/1

# 下载生成图片
curl -OJ http://127.0.0.1:8000/api/images/1/file
```