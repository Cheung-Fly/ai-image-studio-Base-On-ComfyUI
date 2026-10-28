# 贡献指南

感谢你愿意为 AI Image Studio 提交改进。本项目面向个人使用场景，欢迎围绕 ComfyUI 控制、Agent 助手、工作流、前端体验、文档和可靠性提交贡献。

## 开发环境

- Python 3.13
- Redis（运行 FastAPI/Celery 全链路时需要）
- ComfyUI（真实生成联调时需要；自动化测试使用模拟服务）

按根目录的 `start_local.md` 配置环境。后端 Python 命令应在 `backend/` 目录执行。

## 提交前检查

```powershell
cd backend
python -m compileall -q app
python -m tests.test_mcp_agent
python -m tests.test_agent_memory
python -m tests.test_chat_stream
ruff check app/api/agent.py app/api/chat.py app/mcp_server app/services/comfy_client.py app/services/llm_client.py app/services/mcp_agent.py app/services/agent_memory.py app/workers/tasks.py app/schemas.py app/models.py tests/test_mcp_agent.py tests/test_agent_memory.py tests/test_chat_stream.py
```

以上命令与当前 CI 的后端检查保持一致。MCP Agent、偏好记忆和流式聊天测试不要求真实 GPU 或 ComfyUI；这些外部服务需要真实联调时再单独启动。CI 定义见 `.github/workflows/ci.yml`。

## 变更建议

- 保持改动聚焦，遵循现有 Python、FastAPI 和原生 HTML/CSS/JavaScript 写法。
- 不要提交 `.env`、API Key、模型权重、生成图片、数据库或本地缓存。
- 用户可见变更在 `docs/CHANGES.md` 的 `Unreleased` 下记录；API 合约变化同步更新 `docs/API接口文档.md`，架构边界或关键流程变化同步更新 `docs/ARCHITECTURE.md`，用户可见功能或安装方式变化同步更新 `README.md`。
- 新增自动化行为时，补充可重复运行的测试；避免测试依赖 GPU、外部模型服务或真实 ComfyUI。
- 遵循 `docs/ARCHITECTURE.md` 中的组件边界和工程流程；不要在 API 路由中复制工作流执行或外部服务集成逻辑。

## Pull Request

1. 从最新的 `main` 创建功能分支，使用简洁的英文提交标题。
2. 在 PR 描述中说明问题背景、改动内容、文档更新和验证方式；如涉及 UI，请附截图。
3. 确认 CI 通过，并处理审查意见后再请求合并。
4. 发布时先将变更合并到 `main`，再按 `docs/CHANGES.md` 维护版本记录并为合并提交打对应 Git tag；不要给未合并分支打正式版本标签。

## 许可证

提交贡献即表示你同意该贡献按仓库根目录 `LICENSE` 中的 MIT License 发布。
