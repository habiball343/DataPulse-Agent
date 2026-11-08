import pytest


@pytest.mark.parametrize(
    "question, must_have",
    [
        ("上个月各线路的毛利是多少", {"waybills", "routes"}),
        ("外协车队的准点率", {"waybills", "vehicles"}),
        ("哪些客户欠款最多", {"monthly_bills", "customers"}),
        ("9.6米厢车的平均运输成本", {"waybills", "vehicles"}),
        ("每个客户在各条线路上的运费收入", {"waybills", "customers", "routes"}),
        ("今年每个月的理赔率", {"exceptions", "waybills"}),
        ("陶瓷行业客户的毛利", {"customers", "waybills"}),
    ],
)
def test_schema_linking_finds_required_tables(index, question, must_have):
    linked = index.link(question)
    assert must_have <= set(linked.tables), linked.tables


def test_irrelevant_tables_are_pruned(index):
    linked = index.link("广州到上海的线路有几条")
    assert linked.tables == ["routes"]


def test_metric_definition_is_attached(index):
    linked = index.link("外协车队的准点率")
    assert [m["name"] for m in linked.metrics] == ["准点率"]
    # 指标依赖的字段不能被列剪枝剪掉
    for col in ("actual_arrival_time", "plan_arrival_time", "status"):
        assert col in linked.schema_text


def test_column_pruning_keeps_keys_and_time(index):
    linked = index.link("上个月各线路的毛利是多少")
    text = linked.schema_text
    for col in ("route_id", "ship_date", "freight_revenue", "transport_cost"):
        assert col in text
    assert "volume_m3" not in text
    assert "已按相关性省略" in text


def test_bridge_table_is_added(index):
    # customers 与 routes 没有直接外键，需要经由 waybills 关联
    assert index._shortest_path("customers", "routes") == ["customers", "waybills", "routes"]


def test_enum_values_are_rendered(index):
    linked = index.link("外协车队的准点率")
    assert "外协车队" in linked.schema_text


def test_few_shot_examples_are_relevant(index):
    examples = index.search_examples("欠钱最多的客户是谁", top_k=2)
    assert any("欠款" in e["question"] for e in examples)
