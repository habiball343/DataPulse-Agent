import pytest

from datapulse.tool_client import MCPToolClient, ToolCallError


async def test_tools_are_exposed(server):
    async with MCPToolClient(server) as client:
        assert set(await client.list_tools()) == {
            "list_tables", "describe_table", "search_schema", "run_readonly_query",
        }


async def test_list_and_describe(server):
    async with MCPToolClient(server) as client:
        tables = (await client.call("list_tables"))["tables"]
        assert {t["name"] for t in tables} >= {"waybills", "customers"}
        assert all(t["row_count"] > 0 for t in tables)
        desc = await client.call("describe_table", {"table_name": "routes"})
        assert "dest_city" in desc["schema"]
        with pytest.raises(ToolCallError, match="表不存在"):
            await client.call("describe_table", {"table_name": "nope"})


async def test_search_schema_returns_context(server):
    async with MCPToolClient(server) as client:
        data = await client.call("search_schema", {"question": "上个月各线路的毛利"})
        assert "waybills" in data["tables"]
        assert data["examples"]
        assert data["context"]["data_end"].startswith("2026-10")


async def test_run_readonly_query(server):
    async with MCPToolClient(server) as client:
        data = await client.call("run_readonly_query", {"sql": "SELECT fleet, COUNT(*) AS n FROM vehicles GROUP BY fleet"})
        assert data["columns"] == ["fleet", "n"]
        assert data["row_count"] == 2
        assert data["sql"].endswith("LIMIT 200")


async def test_dangerous_sql_is_rejected_over_mcp(server):
    async with MCPToolClient(server) as client:
        with pytest.raises(ToolCallError, match="安全检查未通过"):
            await client.call("run_readonly_query", {"sql": "DROP TABLE waybills"})
        with pytest.raises(ToolCallError, match="执行失败"):
            await client.call("run_readonly_query", {"sql": "SELECT nope FROM waybills"})
        # 数据没被破坏
        data = await client.call("run_readonly_query", {"sql": "SELECT COUNT(*) FROM waybills"})
        assert data["rows"][0][0] > 0


async def test_truncation_flag(server):
    async with MCPToolClient(server) as client:
        data = await client.call("run_readonly_query", {"sql": "SELECT waybill_id FROM waybills"})
        assert data["row_count"] == 200
        assert data["truncated"] is True
