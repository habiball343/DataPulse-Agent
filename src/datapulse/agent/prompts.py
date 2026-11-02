"""提示词模板。约束越具体，模型越少“自由发挥”。"""

from __future__ import annotations

import re
from typing import Any

SQL_SYSTEM = """\
你是一名资深物流经营数据分析师，负责把业务人员的问题翻译成 SQLite 查询语句。

必须遵守：
1. 只写一条 SELECT 语句（可以用 WITH 子句），不得出现任何写操作。
2. 只使用“数据库结构”中列出的表和字段，不要臆造字段；需要关联时按标注的外键 JOIN。
3. 指标严格按“指标口径”计算，不要自创算法。
4. 结果列用中文别名并加双引号，金额、比率用 ROUND(..., 2)，比率列名带“(%)”，例如 AS "毛利率(%)"。
5. 日期字段是文本，按月统计用 strftime('%Y-%m', 字段)。“本月 / 上月 / 近 N 个月 / 今年”等相对时间，
   一律以“数据最新日期”所在月份为本月来推算，不要使用 date('now')。
6. 排名类问题要 ORDER BY；问“前 N / 最高的 N 个”要加 LIMIT N。
7. 只输出一个 ```sql 代码块，不要输出解释。
"""


def _format_examples(examples: list[dict[str, str]]) -> str:
    if not examples:
        return "（无）"
    return "\n\n".join(f"问题：{e['question']}\n```sql\n{e['sql']}\n```" for e in examples)


def build_sql_messages(
    question: str,
    context: dict[str, Any],
    failed_attempts: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    ctx = context.get("context", {})
    metrics = context.get("metrics") or []
    metric_text = "\n".join(f"- {m['name']}：{m['definition']}" for m in metrics) or "（无）"
    user = f"""\
## 数据库结构（{ctx.get("dialect", "SQLite")}）
{context.get("schema", "")}

## 指标口径
{metric_text}

## 参考示例（相似问题的标准写法）
{_format_examples(context.get("examples") or [])}

## 时间信息
数据覆盖范围：{ctx.get("data_start")} 至 {ctx.get("data_end")}；数据最新日期：{ctx.get("data_end")}

## 问题
{question}
"""
    messages = [{"role": "system", "content": SQL_SYSTEM}, {"role": "user", "content": user}]

    # 执行反馈自修复：把之前失败的 SQL 和具体报错一并交给模型，让它“对症下药”
    for attempt in failed_attempts or []:
        messages.append({"role": "assistant", "content": f"```sql\n{attempt['sql']}\n```"})
        messages.append({
            "role": "user",
            "content": (
                f"这条 SQL 执行失败，错误信息：\n{attempt['error']}\n\n"
                "请先判断错误原因（字段名写错？表没 JOIN？语法不被 SQLite 支持？违反了只读限制？），"
                "再输出修正后的完整 SQL。仍然只输出一个 ```sql 代码块。"
            ),
        })
    return messages


SUMMARY_SYSTEM = """\
你是物流公司的经营分析助手。根据查询结果，用简洁的中文回答业务问题。
要求：
1. 先直接回答问题，再补充 1~2 个值得注意的发现（最高 / 最低、明显差异、异常值）。
2. 只能使用结果表中出现的数字，不要估算或编造；数字保留原精度并带单位。
3. 总长度不超过 150 字，不要复述 SQL，不要使用标题。
"""


def build_summary_messages(question: str, columns: list[str], rows: list[list[Any]], total: int) -> list[dict]:
    shown = rows[:30]
    header = "| " + " | ".join(columns) + " |"
    sep = "|" + "---|" * len(columns)
    body = "\n".join("| " + " | ".join("" if v is None else str(v) for v in r) + " |" for r in shown)
    note = f"\n（共 {total} 行，以上为前 {len(shown)} 行）" if total > len(shown) else ""
    user = f"问题：{question}\n\n查询结果：\n{header}\n{sep}\n{body}{note}"
    return [{"role": "system", "content": SUMMARY_SYSTEM}, {"role": "user", "content": user}]


_SQL_BLOCK = re.compile(r"```(?:sql|sqlite)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)


def extract_sql(text: str) -> str:
    """从模型输出中提取 SQL：优先取 ```sql 代码块，否则从第一个 SELECT / WITH 开始截取。"""
    blocks = _SQL_BLOCK.findall(text)
    if blocks:
        return blocks[-1].strip()
    m = re.search(r"\b(WITH|SELECT)\b.*", text, re.IGNORECASE | re.DOTALL)
    return m.group(0).strip() if m else ""
