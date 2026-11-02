"""根据查询结果自动推荐图表，输出 ECharts 配置（option）。

规则驱动、不调用大模型：图表类型由数据形状决定，结果稳定可复现。
  - 第一列是月份 / 日期 + 若干数值列        → 折线图
  - 第一列是类别（≤ 30 个）+ 若干数值列      → 柱状图（类别多时横向）
  - 列名含“占比”且只有一个数值列、≤ 8 类     → 饼图
  - 单行结果、无类别列、明细数据等             → 不出图，直接看表格
"""

from __future__ import annotations

import re
from typing import Any

_TIME_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?")


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def suggest_chart(columns: list[str], rows: list[list[Any]], title: str = "") -> dict[str, Any] | None:
    if len(rows) < 2 or len(columns) < 2:
        return None

    numeric = [i for i in range(len(columns)) if all(_is_number(r[i]) or r[i] is None for r in rows)]
    if not numeric or 0 in numeric:
        return None  # 第一列必须是维度（类别或时间）
    numeric = numeric[:3]
    labels = ["" if r[0] is None else str(r[0]) for r in rows]
    is_time = all(_TIME_RE.match(x) for x in labels)
    if not is_time and len(labels) > 30:
        return None

    base: dict[str, Any] = {
        "title": {"text": title, "left": "center", "textStyle": {"fontSize": 14}} if title else {},
        "tooltip": {"trigger": "axis" if len(numeric) else "item"},
        "grid": {"left": 60, "right": 60, "bottom": 50, "top": 60 if len(numeric) > 1 else 40, "containLabel": True},
    }

    # 饼图：占比类、单一数值、类别不多
    if len(numeric) == 1 and "占比" in columns[numeric[0]] and len(rows) <= 8:
        return {
            **{k: v for k, v in base.items() if k != "grid"},
            "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"},
            "series": [{
                "type": "pie",
                "radius": ["35%", "65%"],
                "data": [{"name": n, "value": r[numeric[0]]} for n, r in zip(labels, rows, strict=True)],
            }],
        }

    # 多个数值列量纲差异很大时（如“运单数”与“收入(元)”），第二个序列放到右侧 Y 轴
    def magnitude(i: int) -> float:
        vals = [abs(r[i]) for r in rows if r[i] is not None]
        return max(vals) if vals else 0.0

    dual_axis = len(numeric) == 2 and min(magnitude(numeric[0]), magnitude(numeric[1])) * 20 < max(
        magnitude(numeric[0]), magnitude(numeric[1])
    )
    chart_type = "line" if is_time else "bar"
    horizontal = chart_type == "bar" and len(labels) > 8 and len(numeric) == 1

    series = []
    for k, i in enumerate(numeric):
        s: dict[str, Any] = {
            "name": columns[i],
            "type": chart_type,
            "data": [r[i] for r in rows],
        }
        if chart_type == "line":
            s["smooth"] = True
        if dual_axis and k == 1:
            s["yAxisIndex"] = 1
            s["type"] = "line"
        series.append(s)

    category_axis = {"type": "category", "data": labels, "axisLabel": {"interval": 0, "rotate": 30 if len(labels) > 6 and not horizontal else 0}}
    value_axes: Any = (
        [{"type": "value", "name": columns[numeric[0]]}, {"type": "value", "name": columns[numeric[1]]}]
        if dual_axis
        else {"type": "value"}
    )
    option = {
        **base,
        "legend": {"top": 28 if title else 0} if len(numeric) > 1 else {"show": False},
        "series": series,
    }
    if horizontal:
        category_axis["data"] = labels[::-1]
        category_axis["axisLabel"] = {"interval": 0}
        for s in series:
            s["data"] = s["data"][::-1]
        option.update({"xAxis": {"type": "value"}, "yAxis": category_axis})
    else:
        option.update({"xAxis": category_axis, "yAxis": value_axes})
    return option
