"""MCP Agent：LLM 通过 MCP 协议连接 ComfyUI MCP Server，自主决策并调用工具控制 ComfyUI。

流程（ReAct / function calling）：
  列出 MCP 工具 → 转成 OpenAI tools → LLM 决定调用 → 经 MCP 执行 → 结果回灌 → 直到给出最终答复。
"""

import asyncio
import json
import os
import sys
from collections.abc import Callable
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ..config import BASE_DIR
from .llm_client import chat_with_tools

MAX_STEPS = 8
DESTRUCTIVE_TOOLS = {
    "comfy_interrupt",
    "comfy_clear_queue",
    "comfy_delete_queued",
    "comfy_free_memory",
    "comfy_delete_input_file",
}
SYSTEM_PROMPT = (
    "你是 ComfyUI 控制助手，通过工具直接操作本机 ComfyUI：查询状态/模型/队列、生成图片、"
    "管理工作流和素材。需要生图时先把用户描述扩写成详细英文提示词再调用 "
    "comfy_generate_image。中断、清队列、删除队列任务/素材、释放显存属于有影响的操作，"
    "必须等待界面向用户确认后才会执行。工具返回 JSON（ok=false 表示失败）。"
    "完成后用中文简要汇报结果和生成文件路径。不要编造工具结果。"
)


def _server_params() -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp_server"],
        cwd=str(BASE_DIR),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def _to_openai_tools(mcp_tools: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.inputSchema or {"type": "object", "properties": {}},
            },
        }
        for t in mcp_tools
    ]


def _result_text(result: Any) -> str:
    parts = [c.text for c in result.content if getattr(c, "text", None)]
    return "\n".join(parts) if parts else "(无输出)"


async def run_mcp_agent(
    user_request: str,
    provider: str = "",
    llm: Callable[..., dict[str, Any]] = chat_with_tools,
    max_steps: int = MAX_STEPS,
    params: StdioServerParameters | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    confirm_action: Callable[[str, dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """运行 Agent，返回 {answer, steps:[{tool, arguments, result}]}。"""
    steps: list[dict[str, Any]] = []
    async with (
        stdio_client(params or _server_params()) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = _to_openai_tools((await session.list_tools()).tools)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_request},
        ]
        for _ in range(max_steps):
            msg = await asyncio.to_thread(llm, messages, tools, provider)
            calls = msg.get("tool_calls") or []
            if not calls:
                return {"answer": msg.get("content") or "", "steps": steps}
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.get("content") or "",
                    "tool_calls": calls,
                }
            )
            for call in calls:
                fn = call["function"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                if on_progress:
                    on_progress(
                        {"steps": steps.copy(), "current_tool": fn["name"], "running": True}
                    )
                if fn["name"] in DESTRUCTIVE_TOOLS and not (
                    confirm_action and confirm_action(fn["name"], args)
                ):
                    text = json.dumps(
                        {
                            "ok": False,
                            "cancelled": True,
                            "error": "用户未确认该操作，操作未执行。",
                        },
                        ensure_ascii=False,
                    )
                else:
                    try:
                        text = _result_text(await session.call_tool(fn["name"], args))
                    except Exception as exc:  # 工具异常回灌给 LLM，由其决定如何处理
                        text = json.dumps(
                            {"ok": False, "error": str(exc)[:300]}, ensure_ascii=False
                        )
                step = {"tool": fn["name"], "arguments": args, "result": text[:2000]}
                steps.append(step)
                if on_progress:
                    on_progress(
                        {"steps": steps.copy(), "current_tool": fn["name"], "running": False}
                    )
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": text})
        return {"answer": "已达到最大步数，未完成。", "steps": steps}
