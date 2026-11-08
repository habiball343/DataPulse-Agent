"""运行离线评测：python eval/run_eval.py [--limit N] [--no-repair]

需要在 .env 中配置大模型 API Key。输出：
  - 总体执行准确率、首次即正确的比例、经自修复后挽回的题数
  - 按标签分类的准确率
  - 明细写入 eval/results/<时间戳>.json，方便对比不同模型 / 提示词 / 检索策略
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import yaml

from datapulse.agent import ask
from datapulse.config import get_settings
from datapulse.db import Database
from datapulse.evaluation import CaseResult, is_ordered, results_match
from datapulse.llm import OpenAICompatibleChat
from datapulse.mcp_server import build_server
from datapulse.tool_client import MCPToolClient

HERE = Path(__file__).parent


async def main(limit: int | None, repair: bool) -> None:
    logging.getLogger("mcp").setLevel(logging.WARNING)
    settings = get_settings()
    cases = yaml.safe_load((HERE / "questions.yaml").read_text(encoding="utf-8"))[:limit]
    llm = OpenAICompatibleChat.from_settings(settings)
    server = build_server(settings)
    db = Database(settings.db_path)
    max_repair = settings.max_repair_rounds if repair else 0

    results: list[CaseResult] = []
    async with MCPToolClient(server) as tools:
        for case in cases:
            state = await ask(case["question"], tools, llm, max_repair_rounds=max_repair)
            attempts = state.get("attempts", [])
            pred = state.get("result")
            gold_rows = db.execute(case["gold"]).rows
            correct = bool(pred) and results_match(gold_rows, pred["rows"], ordered=is_ordered(case["gold"]))
            results.append(CaseResult(
                id=case["id"], question=case["question"], correct=correct, attempts=len(attempts),
                pred_sql=attempts[-1]["sql"] if attempts else "", error=state.get("error"),
                tags=tuple(case.get("tags", [])),
            ))
            print(f"{'✓' if correct else '✗'} {case['id']} 尝试 {len(attempts)} 次  {case['question']}")

    n = len(results)
    acc = sum(r.correct for r in results) / n
    first = sum(r.correct and r.attempts == 1 for r in results) / n
    rescued = sum(r.correct and r.attempts > 1 for r in results)
    print(f"\n模型：{settings.llm_model}  Embedding：{settings.embedding_provider}  题数：{n}")
    print(f"执行准确率：{acc:.1%}   首次即正确：{first:.1%}   自修复挽回：{rescued} 题")

    by_tag: dict[str, list[bool]] = defaultdict(list)
    for r in results:
        for t in r.tags:
            by_tag[t].append(r.correct)
    for tag, flags in sorted(by_tag.items(), key=lambda kv: -len(kv[1])):
        print(f"  {tag:<8} {sum(flags)}/{len(flags)}")

    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps({
        "model": settings.llm_model,
        "embedding": settings.embedding_provider,
        "repair": repair,
        "accuracy": acc,
        "first_try_accuracy": first,
        "cases": [asdict(r) for r in results],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细已保存：{out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-repair", action="store_true", help="关闭自修复，用于对比消融")
    args = parser.parse_args()
    asyncio.run(main(args.limit, not args.no_repair))
