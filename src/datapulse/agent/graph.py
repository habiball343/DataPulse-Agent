"""基于 LangGraph 的问数 Agent。

流程（状态图）：

    retrieve ──► generate_sql ──► execute ──┬─(成功)──────────────► summarize ──► END
                     ▲                      │
                     └──(失败且未超上限)──────┤
                                            └─(失败且超上限)──────► give_up ───► END

  retrieve      调用 MCP 工具 search_schema，拿到剪枝后的表结构、指标口径和 Few-Shot 示例
  generate_sql  让大模型写 SQL；若之前失败过，会带上失败的 SQL 和报错（执行反馈自修复）
  execute       调用 MCP 工具 run_readonly_query（Server 侧做 SQLGlot 安全检查 + 只读执行）
  summarize     规则推荐图表 + 大模型基于真实结果写结论
  give_up       超过重试上限，返回失败原因，不编造答案
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from datapulse.agent.prompts import build_sql_messages, build_summary_messages, extract_sql
from datapulse.charts import suggest_chart
from datapulse.llm import ChatModel
from datapulse.tool_client import MCPToolClient, ToolCallError


class Attempt(TypedDict):
    sql: str
    error: str | None


class AgentState(TypedDict, total=False):
    question: str
    context: dict[str, Any]
    sql: str
    attempts: Annotated[list[Attempt], operator.add]  # 每次执行都追加一条，形成完整的尝试轨迹
    error: str | None
    result: dict[str, Any]
    answer: str
    chart: dict[str, Any] | None
    success: bool


def build_agent(tools: MCPToolClient, llm: ChatModel, max_repair_rounds: int = 3):
    """构建并编译状态图。tools 需已进入 async with 上下文。"""

    async def retrieve(state: AgentState) -> AgentState:
        context = await tools.call("search_schema", {"question": state["question"]})
        return {"context": context}

    async def generate_sql(state: AgentState) -> AgentState:
        failed = [a for a in state.get("attempts", []) if a["error"]]
        messages = build_sql_messages(state["question"], state["context"], failed_attempts=failed)
        reply = await llm.complete(messages)
        return {"sql": extract_sql(reply)}

    async def execute(state: AgentState) -> AgentState:
        sql = state.get("sql", "")
        if not sql:
            err = "模型没有输出可识别的 SQL 代码块"
            return {"error": err, "attempts": [{"sql": "", "error": err}]}
        try:
            result = await tools.call("run_readonly_query", {"sql": sql})
        except ToolCallError as e:
            return {"error": str(e), "attempts": [{"sql": sql, "error": str(e)}]}
        return {"error": None, "result": result, "attempts": [{"sql": sql, "error": None}]}

    def route_after_execute(state: AgentState) -> str:
        if not state.get("error"):
            return "summarize"
        if len(state.get("attempts", [])) <= max_repair_rounds:
            return "generate_sql"
        return "give_up"

    async def summarize(state: AgentState) -> AgentState:
        result = state["result"]
        columns, rows = result["columns"], result["rows"]
        if not rows:
            return {"answer": "没有查到符合条件的数据，可以换个时间范围或筛选条件再试试。", "chart": None, "success": True}
        chart = suggest_chart(columns, rows, title=state["question"][:30])
        answer = await llm.complete(build_summary_messages(state["question"], columns, rows, result["row_count"]))
        if result.get("truncated"):
            answer += f"\n\n（结果较多，仅返回前 {result['row_count']} 行）"
        return {"answer": answer.strip(), "chart": chart, "success": True}

    async def give_up(state: AgentState) -> AgentState:
        n = len(state.get("attempts", []))
        return {
            "answer": f"抱歉，尝试了 {n} 次仍未生成可执行的查询。最后一次的错误：{state.get('error')}。"
            "可以把问题描述得更具体一些（时间范围、统计口径、维度）。",
            "chart": None,
            "success": False,
        }

    graph = StateGraph(AgentState)
    graph.add_node("retrieve", retrieve)
    graph.add_node("generate_sql", generate_sql)
    graph.add_node("execute", execute)
    graph.add_node("summarize", summarize)
    graph.add_node("give_up", give_up)

    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "generate_sql")
    graph.add_edge("generate_sql", "execute")
    graph.add_conditional_edges("execute", route_after_execute, ["summarize", "generate_sql", "give_up"])
    graph.add_edge("summarize", END)
    graph.add_edge("give_up", END)
    return graph.compile()


async def ask(question: str, tools: MCPToolClient, llm: ChatModel, max_repair_rounds: int = 3) -> AgentState:
    """一次性问答的便捷入口。"""
    agent = build_agent(tools, llm, max_repair_rounds=max_repair_rounds)
    return await agent.ainvoke({"question": question, "attempts": []})
