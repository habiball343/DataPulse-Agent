from datapulse.charts import suggest_chart


def test_time_series_becomes_line():
    opt = suggest_chart(["月份", "运单数"], [["2026-01", 10], ["2026-02", 12], ["2026-03", 9]])
    assert opt["series"][0]["type"] == "line"
    assert opt["xAxis"]["data"] == ["2026-01", "2026-02", "2026-03"]


def test_category_becomes_bar_and_dual_axis():
    rows = [["家电", 120, 1_200_000.0], ["家具", 80, 900_000.0]]
    opt = suggest_chart(["行业", "运单数", "收入(元)"], rows)
    assert opt["series"][0]["type"] == "bar"
    assert opt["series"][1]["yAxisIndex"] == 1  # 量纲差异大，第二个序列放右轴
    assert isinstance(opt["yAxis"], list)


def test_many_categories_become_horizontal_bar():
    rows = [[f"客户{i}", i * 10] for i in range(12)]
    opt = suggest_chart(["客户", "欠款(元)"], rows)
    assert opt["xAxis"]["type"] == "value"
    assert opt["yAxis"]["type"] == "category"


def test_share_becomes_pie():
    opt = suggest_chart(["异常类型", "占比(%)"], [["货损", 50.0], ["延误", 30.0], ["丢件", 20.0]])
    assert opt["series"][0]["type"] == "pie"


def test_no_chart_for_single_value_or_detail_rows():
    assert suggest_chart(["毛利率(%)"], [[18.5]]) is None
    assert suggest_chart(["运单号", "客户"], [["WB1", "A"], ["WB2", "B"]]) is None
