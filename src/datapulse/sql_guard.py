"""SQL 安全沙箱：基于 SQLGlot 抽象语法树（AST）的静态检查。

为什么不用正则？
  正则只能匹配字面文本，很容易被绕过（注释、大小写、换行、字符串里的关键字……），
  也容易误杀（列名里包含 update_time）。解析成语法树后，判断的是“这条语句到底在做什么”。

检查规则：
  1. 只能有一条语句（拒绝 `SELECT ...; DROP TABLE ...` 这种拼接）
  2. 顶层必须是查询（SELECT / UNION / WITH ... SELECT）
  3. 语法树中任何位置都不能出现写操作或管理命令（INSERT / UPDATE / DELETE / DROP / PRAGMA / ATTACH ...）
  4. 只能访问白名单内的业务表（拒绝 sqlite_master 等系统表）
  5. 禁止调用危险函数（如 load_extension）
  6. 自动注入 / 收紧最外层 LIMIT，防止一次拉取海量数据

这是第一道防线。第二道防线在 db.py：数据库连接本身以只读模式打开。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError


class SQLGuardError(ValueError):
    """SQL 未通过安全检查。错误信息会原样回传给模型，帮助它修正。"""


_FORBIDDEN_NODE_NAMES = (
    "Insert", "Update", "Delete", "Drop", "Create", "Alter", "Merge", "TruncateTable",
    "Command", "Pragma", "Attach", "Detach", "Transaction", "Commit", "Rollback",
    "Set", "Use", "Copy", "Grant", "Analyze",
)
_FORBIDDEN_NODES = tuple(getattr(exp, n) for n in _FORBIDDEN_NODE_NAMES if hasattr(exp, n))
_FORBIDDEN_FUNCTIONS = {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"}

_FENCE_RE = re.compile(r"^```(?:sql)?\s*|\s*```$", re.IGNORECASE)


@dataclass(frozen=True)
class GuardedSQL:
    sql: str  # 改写后真正执行的 SQL
    tables: tuple[str, ...]  # 涉及的业务表
    limit_applied: int | None  # 守卫注入或收紧后的 LIMIT；None 表示原 SQL 已合规


def _strip(sql: str) -> str:
    sql = _FENCE_RE.sub("", sql.strip())
    return sql.strip().rstrip(";").strip()


def _outer_limit(query: exp.Query) -> int | None:
    limit = query.args.get("limit")
    if limit is None:
        return None
    value = limit.expression
    if isinstance(value, exp.Literal) and value.is_int:
        return int(value.this)
    raise SQLGuardError("LIMIT 只能是整数常量")


def guard_sql(
    sql: str,
    allowed_tables: Iterable[str] | None = None,
    max_rows: int = 200,
    dialect: str = "sqlite",
) -> GuardedSQL:
    """检查并改写 SQL。通过时返回 GuardedSQL，不通过时抛出 SQLGuardError。"""
    cleaned = _strip(sql)
    if not cleaned:
        raise SQLGuardError("SQL 为空")

    try:
        statements = [s for s in sqlglot.parse(cleaned, read=dialect) if s is not None]
    except ParseError as e:
        raise SQLGuardError(f"SQL 语法解析失败：{e}") from e

    if len(statements) != 1:
        raise SQLGuardError(f"只允许执行一条语句，实际检测到 {len(statements)} 条")
    tree = statements[0]

    if not isinstance(tree, exp.Query):
        raise SQLGuardError(f"只允许 SELECT 查询，检测到 {tree.key.upper()} 语句")

    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise SQLGuardError(f"检测到禁止的操作：{node.key.upper()}")
        if isinstance(node, exp.Anonymous) and str(node.this).lower() in _FORBIDDEN_FUNCTIONS:
            raise SQLGuardError(f"禁止调用函数：{node.this}")

    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    tables = sorted({t.name for t in tree.find_all(exp.Table) if t.name and t.name.lower() not in cte_names})
    if allowed_tables is not None:
        allowed = {t.lower() for t in allowed_tables}
        illegal = [t for t in tables if t.lower() not in allowed]
        if illegal:
            raise SQLGuardError(
                f"访问了不存在或未授权的表：{', '.join(illegal)}；可用的表：{', '.join(sorted(allowed))}"
            )

    limit_applied: int | None = None
    current = _outer_limit(tree)
    if current is None or current > max_rows:
        tree = tree.limit(max_rows, copy=False)
        limit_applied = max_rows

    return GuardedSQL(sql=tree.sql(dialect=dialect), tables=tuple(tables), limit_applied=limit_applied)
