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
python tests/test_mcp_agent.py
python tests/smoke_test.py
```

`test_mcp_agent.py` 通过模拟 ComfyUI 验证 MCP 工具调用，不需要启动真实 ComfyUI。冒烟测试同样使用模拟服务，不需要 Redis。

## 变更建议

- 保持改动聚焦，遵循现有 Python、FastAPI 和原生 HTML/CSS/JavaScript 写法。
- 不要提交 `.env`、API Key、模型权重、生成图片、数据库或本地缓存。
- API 行为变化时同步更新 `docs/API接口文档.md`；用户可见功能变化时更新 `README.md`。
- 新增自动化行为时，补充可重复运行的测试；避免测试依赖 GPU、外部模型服务或真实 ComfyUI。

## Pull Request

1. 从最新的 `main` 创建功能分支，使用简洁的英文提交标题。
2. 在 PR 描述中说明问题背景、改动内容和验证方式；如涉及 UI，请附截图。
3. 确认 CI 通过，并处理审查意见后再请求合并。

## 许可证

提交贡献即表示你同意该贡献按仓库根目录 `LICENSE` 中的 MIT License 发布。
