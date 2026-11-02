"""命令行入口。

  datapulse init-db                 生成演示数据库
  datapulse ask "上个月各线路毛利"     命令行问数（默认进程内连接 MCP Server）
  datapulse ask "..." --stdio        以子进程方式启动 MCP Server 再提问，演示跨进程解耦
  datapulse serve                   以 stdio 方式启动 MCP Server（供 Claude Desktop 等客户端使用）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from datapulse.config import get_settings


def _cmd_init_db(args: argparse.Namespace) -> None:
    from datapulse.demo_data import build_demo_db

    path = build_demo_db(args.path or get_settings().db_path)
    print(f"演示数据库已生成：{path}")


async def _ask(question: str, use_stdio: bool) -> None:
    from datapulse.agent import ask
    from datapulse.llm import OpenAICompatibleChat
    from datapulse.mcp_server import build_server
    from datapulse.tool_client import MCPToolClient

    settings = get_settings()
    llm = OpenAICompatibleChat.from_settings(settings)
    target = MCPToolClient.stdio(sys.executable, ["-m", "datapulse.mcp_server"]) if use_stdio else MCPToolClient(
        build_server(settings)
    )
    async with target as tools:
        state = await ask(question, tools, llm, max_repair_rounds=settings.max_repair_rounds)

    for i, a in enumerate(state.get("attempts", []), 1):
        mark = "✓" if not a["error"] else "✗"
        print(f"\n[{mark} 第 {i} 次尝试]\n{a['sql']}")
        if a["error"]:
            print(f"  → {a['error']}")
    result = state.get("result")
    if result and result["rows"]:
        print("\n" + " | ".join(result["columns"]))
        for row in result["rows"][:20]:
            print(" | ".join("" if v is None else str(v) for v in row))
        if result["row_count"] > 20:
            print(f"...（共 {result['row_count']} 行）")
    print(f"\n{state.get('answer', '')}")
    if state.get("chart"):
        print(f"\n[图表] {state['chart']['series'][0]['type']}，可在 Streamlit 界面中查看")


def _cmd_ask(args: argparse.Namespace) -> None:
    asyncio.run(_ask(args.question, args.stdio))


def _cmd_serve(_: argparse.Namespace) -> None:
    from datapulse.mcp_server import main as serve

    serve()


def _cmd_schema(args: argparse.Namespace) -> None:
    from datapulse.mcp_server import DataService

    data = DataService(get_settings()).search_schema(args.question)
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(prog="datapulse", description="DataPulse-Agent 智能问数")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-db", help="生成演示数据库")
    p.add_argument("--path", default=None)
    p.set_defaults(func=_cmd_init_db)

    p = sub.add_parser("ask", help="用自然语言提问")
    p.add_argument("question")
    p.add_argument("--stdio", action="store_true", help="以子进程方式连接 MCP Server")
    p.set_defaults(func=_cmd_ask)

    p = sub.add_parser("schema", help="只看 Schema Linking 的检索结果（不调用大模型）")
    p.add_argument("question")
    p.set_defaults(func=_cmd_schema)

    p = sub.add_parser("serve", help="以 stdio 方式启动 MCP Server")
    p.set_defaults(func=_cmd_serve)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
