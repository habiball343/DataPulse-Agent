"""Streamlit 前端：对话式问数 + SQL 透明展示 + 自动图表。

运行：streamlit run app/streamlit_app.py
"""

from __future__ import annotations

import asyncio
import logging

import pandas as pd
import streamlit as st
from streamlit_echarts import st_echarts

from datapulse.agent import build_agent
from datapulse.config import get_settings
from datapulse.llm import OpenAICompatibleChat
from datapulse.mcp_server import build_server
from datapulse.tool_client import MCPToolClient

logging.getLogger("mcp").setLevel(logging.WARNING)

st.set_page_config(page_title="DataPulse 智能问数", page_icon="📊", layout="wide")

NODE_LABELS = {
    "retrieve": "检索相关表结构与参考 SQL",
    "generate_sql": "生成 SQL",
    "execute": "安全检查并执行",
    "summarize": "生成结论与图表",
    "give_up": "超过重试上限",
}
SAMPLES = [
    "上个月各线路的毛利排行",
    "自有车队和外协车队每个月的准点率",
    "目前欠款最多的 5 个客户",
    "今年每个月的运单量和运费收入",
    "各类异常的理赔金额占比",
    "哪个车型的毛利率最高",
]


@st.cache_resource(show_spinner="正在加载数据目录与向量索引…")
def get_server():
    return build_server(get_settings())


async def run_question(question: str, status) -> dict:
    settings = get_settings()
    llm = OpenAICompatibleChat.from_settings(settings)
    final: dict = {"question": question, "attempts": []}
    async with MCPToolClient(get_server()) as tools:
        agent = build_agent(tools, llm, max_repair_rounds=settings.max_repair_rounds)
        async for update in agent.astream({"question": question, "attempts": []}, stream_mode="updates"):
            for node, delta in update.items():
                delta = delta or {}
                if node == "execute":
                    attempt = delta["attempts"][-1]
                    if attempt["error"]:
                        status.write(f"❌ 执行失败，准备自动修正：{attempt['error']}")
                    else:
                        status.write("✅ " + NODE_LABELS[node])
                else:
                    status.write("• " + NODE_LABELS.get(node, node))
                for k, v in delta.items():
                    final[k] = final.get(k, []) + v if k == "attempts" else v
    return final


def render_result(state: dict) -> None:
    st.markdown(state.get("answer", ""))
    chart = state.get("chart")
    if chart:
        y_axis = chart.get("yAxis")
        bars = len(y_axis["data"]) if isinstance(y_axis, dict) and y_axis.get("type") == "category" else 0
        st_echarts(options=chart, height=f"{max(380, 24 * bars + 80)}px")  # 横向柱状图按类别数自适应高度
    result = state.get("result")
    if result and result.get("rows"):
        st.dataframe(pd.DataFrame(result["rows"], columns=result["columns"]), use_container_width=True, hide_index=True)
    attempts = state.get("attempts", [])
    if attempts:
        with st.expander(f"查看 SQL（共尝试 {len(attempts)} 次）"):
            for i, a in enumerate(attempts, 1):
                st.caption(f"第 {i} 次 · {'成功' if not a['error'] else '失败'}")
                st.code(a["sql"] or "（未生成 SQL）", language="sql")
                if a["error"]:
                    st.error(a["error"])
            if result:
                st.caption(f"实际执行（经安全改写）· 耗时 {result.get('elapsed_ms')} ms")
                st.code(result["sql"], language="sql")


with st.sidebar:
    st.header("📊 DataPulse")
    st.caption("基于 MCP 协议的智能问数助手 · 演示数据为虚构的物流 TMS 经营数据")
    settings = get_settings()
    st.markdown(f"**模型**：`{settings.llm_model}`  \n**Embedding**：`{settings.embedding_provider}`")
    if not settings.llm_api_key:
        st.warning("未检测到 DATAPULSE_LLM_API_KEY，请先在 .env 中配置。")
    st.divider()
    st.markdown("**试试这些问题**")
    for q in SAMPLES:
        if st.button(q, use_container_width=True):
            st.session_state.pending = q
    if st.button("清空对话", type="secondary"):
        st.session_state.history = []

st.session_state.setdefault("history", [])
for item in st.session_state.history:
    with st.chat_message("user"):
        st.markdown(item["question"])
    with st.chat_message("assistant"):
        render_result(item)

question = st.chat_input("用自然语言提问，例如：9 月份毛利率最低的 5 条线路") or st.session_state.pop("pending", None)
if question:
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        with st.status("分析中…", expanded=True) as status:
            try:
                state = asyncio.run(run_question(question, status))
                status.update(label="完成", state="complete", expanded=False)
            except Exception as e:  # noqa: BLE001 - 前端兜底展示
                status.update(label="出错了", state="error")
                st.exception(e)
                st.stop()
        render_result(state)
    st.session_state.history.append(state)
