"""MCP Agent：LLM 通过 MCP 协议连接 ComfyUI MCP Server，自主决策并调用工具控制 ComfyUI。

流程（ReAct / function calling）：
  列出 MCP 工具 → 转成 OpenAI tools → LLM 决定调用 → 经 MCP 执行 → 结果回灌 → 直到给出最终答复。
"""
import asyncio
import json
import os
import sys
from typing import Any, Callable

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ..config import BASE_DIR
from .llm_client import chat_with_tools

MAX_STEPS = 8
SYSTEM_PROMPT = (
    "你是 ComfyUI 控制助手，通过工具直接操作本机 ComfyUI：查询状态/模型/队列、生成图片、"
    "中断或清空任务、释放显存。需要生图时先把用户描述扩写成详细英文提示词再调用 "
    "comfy_generate_image。工具返回 JSON（ok=false 表示失败）。完成后用中文简要汇报结果，"
    "并给出生成文件路径。不要编造工具结果。"
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
) -> dict[str, Any]:
    """运行 Agent，返回 {answer, steps:[{tool, arguments, result}]}。"""
    steps: list[dict[str, Any]] = []
    async with stdio_client(params or _server_params()) as (read, write):
        async with ClientSession(read, write) as session:
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
                    try:
                        text = _result_text(await session.call_tool(fn["name"], args))
                    except Exception as exc:  # 工具异常回灌给 LLM，由其决定如何处理
                        text = json.dumps({"ok": False, "error": str(exc)[:300]}, ensure_ascii=False)
                    steps.append({"tool": fn["name"], "arguments": args, "result": text[:2000]})
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": text})
            return {"answer": "已达到最大步数，未完成。", "steps": steps}
