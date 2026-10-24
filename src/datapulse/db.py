"""数据库访问层：只读连接 + 查询超时 + 表结构探查。"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class QueryError(RuntimeError):
    """SQL 执行失败（语法错误、字段不存在、超时等）。"""


@dataclass
class ColumnInfo:
    name: str
    type: str
    primary_key: bool = False
    references: str | None = None  # 外键，形如 "customers.customer_id"
    sample_values: list[str] = field(default_factory=list)  # 低基数文本列的全部取值


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    elapsed_ms: float

    @property
    def row_count(self) -> int:
        return len(self.rows)


class Database:
    """SQLite 只读访问。

    以 `mode=ro` 打开，并开启 `PRAGMA query_only`：即使 SQL 守卫被绕过，
    数据库层面也无法写入，这是第二道防线。
    """

    def __init__(self, path: str | Path, timeout_s: float = 5.0):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"数据库文件不存在：{self.path}")
        self.timeout_s = timeout_s

    def _connect(self) -> sqlite3.Connection:
        uri = f"file:{self.path.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.execute("PRAGMA query_only = ON")
        return conn

    def execute(self, sql: str) -> QueryResult:
        conn = self._connect()
        deadline = time.monotonic() + self.timeout_s
        # 每执行 N 条虚拟机指令回调一次，超时则中断查询
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        start = time.perf_counter()
        try:
            cur = conn.execute(sql)
            rows = [list(r) for r in cur.fetchall()]
            columns = [d[0] for d in cur.description or []]
        except sqlite3.OperationalError as e:
            if "interrupted" in str(e):
                raise QueryError(f"查询超时（超过 {self.timeout_s} 秒），请缩小查询范围") from e
            raise QueryError(str(e)) from e
        except sqlite3.Error as e:
            raise QueryError(str(e)) from e
        finally:
            conn.close()
        return QueryResult(columns=columns, rows=rows, elapsed_ms=round((time.perf_counter() - start) * 1000, 1))

    # ---------- 表结构探查 ----------

    def table_names(self) -> list[str]:
        sql = "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        return [r[0] for r in self.execute(sql).rows]

    def row_count(self, table: str) -> int:
        self._check_table(table)
        return int(self.execute(f'SELECT COUNT(*) FROM "{table}"').rows[0][0])

    def columns(self, table: str, max_distinct: int = 8) -> list[ColumnInfo]:
        self._check_table(table)
        fks = {row[3]: f"{row[2]}.{row[4]}" for row in self.execute(f'PRAGMA foreign_key_list("{table}")').rows}
        cols = []
        for _, name, ctype, _, _, pk in self.execute(f'PRAGMA table_info("{table}")').rows:
            info = ColumnInfo(name=name, type=ctype or "", primary_key=bool(pk), references=fks.get(name))
            if ctype.upper() == "TEXT" and not pk and name not in fks:
                distinct = self.execute(
                    f'SELECT DISTINCT "{name}" FROM "{table}" WHERE "{name}" IS NOT NULL LIMIT {max_distinct + 1}'
                ).rows
                if len(distinct) <= max_distinct:
                    info.sample_values = sorted(str(r[0]) for r in distinct)
            cols.append(info)
        return cols

    def date_range(self, table: str = "waybills", column: str = "ship_date") -> tuple[str, str] | None:
        try:
            lo, hi = self.execute(f'SELECT MIN("{column}"), MAX("{column}") FROM "{table}"').rows[0]
        except QueryError:
            return None
        return (str(lo)[:10], str(hi)[:10]) if lo and hi else None

    def _check_table(self, table: str) -> None:
        if table not in self.table_names():
            raise QueryError(f"表不存在：{table}")
