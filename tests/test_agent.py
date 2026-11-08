from datapulse.agent import ask
from datapulse.agent.prompts import extract_sql
from datapulse.llm import FakeChatModel
from datapulse.tool_client import MCPToolClient

GOOD_SQL = """```sql
SELECT v.fleet AS "车队",
       ROUND(SUM(CASE WHEN w.actual_arrival_time <= w.plan_arrival_time THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) AS "准点率(%)"
FROM waybills w JOIN vehicles v ON v.vehicle_id = w.vehicle_id
WHERE w.status = '已签收'
GROUP BY v.fleet
```"""
BAD_SQL = "```sql\nSELECT fleet, AVG(on_time_flag) FROM waybills GROUP BY fleet\n```"


async def test_happy_path(server):
    llm = FakeChatModel([GOOD_SQL, "自有车队准点率更高。"])
    async with MCPToolClient(server) as tools:
        state = await ask("自有车队和外协车队的准点率", tools, llm)
    assert state["success"] is True
    assert len(state["attempts"]) == 1
    assert state["result"]["row_count"] == 2
    assert state["answer"] == "自有车队准点率更高。"
    assert state["chart"]["series"][0]["type"] == "bar"
    # 提示词中带上了检索到的表结构、指标口径和参考示例
    prompt = llm.calls[0][1]["content"]
    assert "表 waybills" in prompt and "准点率" in prompt and "参考示例" in prompt


async def test_execution_guided_repair(server):
    llm = FakeChatModel([BAD_SQL, GOOD_SQL, "结论"])
    async with MCPToolClient(server) as tools:
        state = await ask("自有车队和外协车队的准点率", tools, llm)
    assert state["success"] is True
    assert [a["error"] is None for a in state["attempts"]] == [False, True]
    # 第二次生成时，提示词里必须带上第一次的 SQL 和数据库报错
    repair_prompt = llm.calls[1]
    assert "on_time_flag" in repair_prompt[-2]["content"]
    assert "no such column" in repair_prompt[-1]["content"]


async def test_guard_violation_is_fed_back(server):
    llm = FakeChatModel(["```sql\nDELETE FROM waybills\n```", GOOD_SQL, "结论"])
    async with MCPToolClient(server) as tools:
        state = await ask("准点率", tools, llm)
    assert state["success"] is True
    assert "安全检查未通过" in state["attempts"][0]["error"]


async def test_gives_up_after_max_rounds(server):
    llm = FakeChatModel([BAD_SQL] * 10)
    async with MCPToolClient(server) as tools:
        state = await ask("准点率", tools, llm, max_repair_rounds=3)
    assert state["success"] is False
    assert len(state["attempts"]) == 4  # 首次 + 3 轮修复
    assert len(llm.calls) == 4  # 失败后不再调用模型总结，不编造答案


async def test_empty_result_skips_llm_summary(server):
    llm = FakeChatModel(["```sql\nSELECT * FROM waybills WHERE status = '不存在的状态'\n```"])
    async with MCPToolClient(server) as tools:
        state = await ask("x", tools, llm)
    assert state["success"] is True
    assert "没有查到" in state["answer"]
    assert len(llm.calls) == 1


def test_extract_sql():
    assert extract_sql("好的：\n```sql\nSELECT 1\n```") == "SELECT 1"
    assert extract_sql("```\nSELECT 2\n```") == "SELECT 2"
    assert extract_sql("SQL 如下 SELECT 3 FROM t") == "SELECT 3 FROM t"
    assert extract_sql("我不知道") == ""
