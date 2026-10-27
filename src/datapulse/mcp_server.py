"""DataPulse MCP Server：把“查库能力”封装成标准 MCP 工具。

为什么要单独做成 MCP Server，而不是在 Agent 里直接连数据库？
  - 解耦：Agent 只认识“工具”，不关心背后是 SQLite、MySQL 还是数仓；换库只改 Server。
  - 复用：同一个 Server 可以直接挂到 Claude Desktop、Cursor、Cherry Studio 等任何 MCP 客户端。
  - 安全边界清晰：SQL 守卫、只读连接、行数上限都在 Server 侧强制执行，客户端绕不过去。

对外暴露的工具：
  list_tables         列出业务表及说明
  describe_table      查看单表完整结构（字段、类型、外键、枚举值）
  search_schema       Schema Linking：按问题检索相关表、剪枝后的字段、指标口径和 Few-Shot 示例
  run_readonly_query  经 SQLGlot 安全检查后只读执行 SQL
资源：
  schema://catalog    全库结构说明（Markdown）

启动方式（stdio）：
  datapulse-mcp            # 或 python -m datapulse.mcp_server
"""

from __future__ import annotations

from datetime import date
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from datapulse.config import Settings, get_settings
from datapulse.db import Database, QueryError
from datapulse.demo_data import ensure_demo_db
from datapulse.embeddings import build_embedder
from datapulse.schema_index import SchemaIndex
from datapulse.sql_guard import SQLGuardError, guard_sql

INSTRUCTIONS = """\
这是一个物流 TMS 经营数据的只读查询服务。推荐的使用顺序：
1. 调用 search_schema(question) 获取与问题相关的表结构、指标口径和参考 SQL；
2. 按 SQLite 语法写一条 SELECT 语句，调用 run_readonly_query(sql) 执行；
3. 若返回错误，根据错误信息修改 SQL 后重试。
只允许 SELECT，写操作、多语句、系统表都会被拒绝；结果最多返回 {max_rows} 行。
"""


class DataService:
    """MCP 工具背后的业务实现，单独拆出来便于单元测试。"""

    def __init__(self, settings: Settings, index: SchemaIndex | None = None):
        self.settings = settings
        db_path = ensure_demo_db(settings.db_path)
        self.db = Database(db_path, timeout_s=settings.query_timeout_s)
        self.index = index or SchemaIndex(self.db, build_embedder(settings))
        self.allowed_tables = set(self.index.tables)

    def list_tables(self) -> dict[str, Any]:
        return {
            "tables": [
                {"name": t.name, "description": t.description, "row_count": self.db.row_count(t.name)}
                for t in self.index.tables.values()
            ]
        }

    def describe_table(self, table_name: str) -> dict[str, Any]:
        if table_name not in self.index.tables:
            raise ToolError(f"表不存在：{table_name}；可用的表：{', '.join(sorted(self.allowed_tables))}")
        return {"table": table_name, "schema": self.index.render_table(table_name)}

    def search_schema(self, question: str) -> dict[str, Any]:
        linked = self.index.link(
            question, top_k_tables=self.settings.top_k_tables, top_k_examples=self.settings.top_k_examples
        )
        data = linked.to_dict()
        rng = self.db.date_range()
        data["context"] = {
            "dialect": "SQLite",
            "today": date.today().isoformat(),
            "data_start": rng[0] if rng else None,
            "data_end": rng[1] if rng else None,
            "max_rows": self.settings.max_rows,
        }
        return data

    def run_readonly_query(self, sql: str) -> dict[str, Any]:
        try:
            guarded = guard_sql(sql, allowed_tables=self.allowed_tables, max_rows=self.settings.max_rows)
        except SQLGuardError as e:
            raise ToolError(f"SQL 安全检查未通过：{e}") from e
        try:
            result = self.db.execute(guarded.sql)
        except QueryError as e:
            raise ToolError(f"SQL 执行失败：{e}") from e
        return {
            "sql": guarded.sql,
            "columns": result.columns,
            "rows": result.rows,
            "row_count": result.row_count,
            "truncated": guarded.limit_applied is not None and result.row_count >= guarded.limit_applied,
            "elapsed_ms": result.elapsed_ms,
        }

    def catalog_markdown(self) -> str:
        parts = ["# 数据目录", ""]
        for name in self.index.tables:
            parts += ["```", self.index.render_table(name), "```", ""]
        parts.append("## 指标口径")
        parts += [f"- **{m['name']}**：{m['definition']}" for m in self.index.metrics]
        return "\n".join(parts)


def build_server(settings: Settings | None = None, service: DataService | None = None) -> MCPServer:
    settings = settings or get_settings()
    svc = service or DataService(settings)
    server = MCPServer(
        name="datapulse",
        title="DataPulse 智能问数",
        instructions=INSTRUCTIONS.format(max_rows=settings.max_rows),
        version="0.1.0",
    )

    @server.tool()
    def list_tables() -> dict[str, Any]:
        """列出数据库中所有业务表，包含中文说明和行数。"""
        return svc.list_tables()

    @server.tool()
    def describe_table(table_name: str) -> dict[str, Any]:
        """查看一张表的完整结构：字段名、类型、中文说明、外键关系、枚举取值。"""
        return svc.describe_table(table_name)

    @server.tool()
    def search_schema(question: str) -> dict[str, Any]:
        """Schema Linking：根据自然语言问题，返回相关表结构（已剪枝）、指标口径、相似的参考 SQL，
        以及数据日期范围等上下文。写 SQL 之前应先调用它。"""
        return svc.search_schema(question)

    @server.tool()
    def run_readonly_query(sql: str) -> dict[str, Any]:
        """执行一条只读 SQL（SQLite 语法）。会先经过语法树安全检查：只允许单条 SELECT，
        禁止写操作和系统表，并自动限制返回行数。出错时返回具体原因，可据此修改 SQL 重试。"""
        return svc.run_readonly_query(sql)

    @server.resource("schema://catalog", name="catalog", mime_type="text/markdown")
    def catalog() -> str:
        """全库结构与指标口径说明。"""
        return svc.catalog_markdown()

    return server


def main() -> None:
    build_server().run("stdio")


if __name__ == "__main__":
    main()
