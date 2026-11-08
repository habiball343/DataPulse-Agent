"""Text-to-SQL 评测：执行准确率（Execution Accuracy）。

只比较 SQL 字面是否一致没有意义（同一个问题有无数种正确写法），
所以分别执行“模型 SQL”和“标准 SQL”，比较结果是否一致：

  - 行数必须相同；
  - 标准答案的每一列，都要能在模型结果中找到数值相同的一列（允许模型多返回辅助列、列顺序不同）；
  - 浮点数按相对误差 0.5% 比较（容忍 ROUND 精度差异）；
  - 标准 SQL 含 ORDER BY 时要求顺序一致，否则按集合比较。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any


def _norm(v: Any) -> Any:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return float(v)
    return "" if v is None else str(v).strip()


def _equal(a: Any, b: Any) -> bool:
    a, b = _norm(a), _norm(b)
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(a, b, rel_tol=5e-3, abs_tol=1e-2)
    return a == b


def _sort_key(v: Any) -> tuple[int, Any]:
    v = _norm(v)
    return (0, v) if isinstance(v, float) else (1, v)


def _columns(rows: list[list[Any]], ordered: bool) -> list[list[Any]]:
    if not rows:
        return []
    cols = [list(c) for c in zip(*rows, strict=True)]
    return cols if ordered else [sorted(c, key=_sort_key) for c in cols]


def results_match(gold_rows: list[list[Any]], pred_rows: list[list[Any]], ordered: bool = False) -> bool:
    if len(gold_rows) != len(pred_rows):
        return False
    if not gold_rows:
        return True
    gold_cols, pred_cols = _columns(gold_rows, ordered), _columns(pred_rows, ordered)
    used: set[int] = set()
    for g in gold_cols:
        hit = next(
            (i for i, p in enumerate(pred_cols) if i not in used and all(_equal(x, y) for x, y in zip(g, p, strict=True))),
            None,
        )
        if hit is None:
            return False
        used.add(hit)
    return True


def is_ordered(sql: str) -> bool:
    """最外层是否有 ORDER BY（粗略判断：最后一个右括号之后出现 ORDER BY）。"""
    tail = sql[sql.rfind(")") + 1 :] if ")" in sql else sql
    return re.search(r"\border\s+by\b", tail, re.IGNORECASE) is not None


@dataclass
class CaseResult:
    id: str
    question: str
    correct: bool
    attempts: int
    pred_sql: str
    error: str | None = None
    tags: tuple[str, ...] = ()
