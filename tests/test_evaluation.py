from pathlib import Path

import yaml

from datapulse.evaluation import is_ordered, results_match
from datapulse.sql_guard import guard_sql

QUESTIONS = Path(__file__).parents[1] / "eval" / "questions.yaml"


def test_match_ignores_extra_columns_and_order():
    gold = [["A", 1.0], ["B", 2.0]]
    pred = [["B", "x", 2.004], ["A", "y", 1.0]]
    assert results_match(gold, pred, ordered=False)
    assert not results_match(gold, pred, ordered=True)


def test_match_requires_same_row_count():
    assert not results_match([["A"]], [["A"], ["B"]])


def test_match_detects_wrong_values():
    assert not results_match([[18.53]], [[19.10]])


def test_is_ordered():
    assert is_ordered("SELECT a FROM t ORDER BY a")
    assert not is_ordered("SELECT a FROM (SELECT a FROM t ORDER BY a)")


def test_gold_sql_is_valid_and_non_empty(db, index):
    """评测集里的标准 SQL 本身必须能通过安全检查并查出数据。"""
    cases = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))
    assert len({c["id"] for c in cases}) == len(cases)
    few_shot_questions = {e["question"] for e in index.few_shots}
    for case in cases:
        assert case["question"] not in few_shot_questions, "评测题不应出现在 Few-Shot 示例中"
        guarded = guard_sql(case["gold"], allowed_tables=index.tables, max_rows=1000)
        assert db.execute(guarded.sql).rows, case["id"]


def test_few_shot_sql_is_valid(db, index):
    for ex in index.few_shots:
        guarded = guard_sql(ex["sql"], allowed_tables=index.tables)
        assert db.execute(guarded.sql).rows, ex["question"]
