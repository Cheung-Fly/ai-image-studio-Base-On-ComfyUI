"""MCP tools for the configured RunningHub workflow backend."""

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..config import settings
from ..database import SessionLocal, init_db
from ..models import RunningHubConfig
from ..security import decrypt_secret
from ..services.runninghub import RunningHubClient, RunningHubError

mcp = FastMCP("runninghub")


def _ok(**data: Any) -> str:
    return json.dumps({"ok": True, **data}, ensure_ascii=False)


def _err(exc: Exception) -> str:
    return json.dumps({"ok": False, "error": str(exc)[:500]}, ensure_ascii=False)


def _load_config() -> dict[str, str | None]:
    db = SessionLocal()
    try:
        config = db.get(RunningHubConfig, 1)
        if config is None:
            raise RunningHubError("请先在 Agent 的 RunningHub 设置中配置 API Key 和工作流")
        return {
            "api_key": decrypt_secret(config.api_key_encrypted),
            "workflow_id": config.workflow_id,
            "prompt_node_id": config.prompt_node_id,
            "prompt_field": config.prompt_field,
            "negative_prompt_node_id": config.negative_prompt_node_id,
            "negative_prompt_field": config.negative_prompt_field,
        }
    finally:
        db.close()


@mcp.tool()
def runninghub_generate(prompt: str, negative_prompt: str = "") -> str:
    """使用设置页中配置的 RunningHub 工作流生成图片或视频，并下载结果到作品库。"""
    if not prompt.strip() or len(prompt) > 8000:
        return _err(ValueError("prompt 必须为 1 到 8000 个字符"))
    if len(negative_prompt) > 4000:
        return _err(ValueError("negative_prompt 不能超过 4000 个字符"))
    try:
        config = _load_config()
        negative_node_id = config["negative_prompt_node_id"]
        negative_field = config["negative_prompt_field"]
        if negative_prompt and not negative_node_id:
            raise RunningHubError("当前工作流未映射负向提示词输入，请先在设置中完成映射")
        node_info_list = [
            {
                "nodeId": str(config["prompt_node_id"]),
                "fieldName": str(config["prompt_field"]),
                "fieldValue": prompt,
            }
        ]
        if negative_prompt and negative_node_id and negative_field:
            node_info_list.append(
                {
                    "nodeId": negative_node_id,
                    "fieldName": negative_field,
                    "fieldValue": negative_prompt,
                }
            )
        client = RunningHubClient(str(config["api_key"]))
        try:
            result = client.generate(
                str(config["workflow_id"]),
                node_info_list,
                settings.image_dir / "mcp",
            )
        finally:
            client.close()
        return _ok(**result)
    except (RunningHubError, OSError, ValueError) as exc:
        return _err(exc)


def main() -> None:
    init_db()
    mcp.run()


if __name__ == "__main__":
    main()
