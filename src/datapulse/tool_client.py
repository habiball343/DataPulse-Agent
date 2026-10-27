"""Agent 侧的 MCP 客户端封装。

Agent 不直接 import 数据库代码，所有数据访问都通过 MCP 协议调用 Server 的工具：
  - 进程内：直接传入 MCPServer 对象（内存传输，适合 Streamlit / 测试）
  - 跨进程：传入 StdioServerParameters，以子进程方式启动 `datapulse-mcp`
两种方式对 Agent 来说完全一样。
"""

from __future__ import annotations

import json
import re
from typing import Any

from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters

_ERROR_PREFIX = re.compile(r"^Error executing tool \S+:\s*")


def _maybe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


class ToolCallError(RuntimeError):
    """工具返回了错误（例如 SQL 未通过安全检查或执行失败）。"""


class MCPToolClient:
    def __init__(self, target: Any):
        self._target = target
        self._client: Client | None = None

    @classmethod
    def stdio(cls, command: str = "datapulse-mcp", args: list[str] | None = None) -> MCPToolClient:
        return cls(StdioServerParameters(command=command, args=args or []))

    async def __aenter__(self) -> MCPToolClient:
        self._client = Client(self._target)
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            await self._client.__aexit__(*exc)
            self._client = None

    async def list_tools(self) -> list[str]:
        assert self._client is not None, "请在 async with 中使用"
        result = await self._client.list_tools()
        return [t.name for t in result.tools]

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        assert self._client is not None, "请在 async with 中使用"
        result = await self._client.call_tool(name, arguments or {})
        texts = [getattr(c, "text", "") for c in result.content]
        if result.is_error:
            raise ToolCallError(_ERROR_PREFIX.sub("", "".join(texts)))
        parsed = [_maybe_json(t) for t in texts]
        return parsed[0] if len(parsed) == 1 else parsed
