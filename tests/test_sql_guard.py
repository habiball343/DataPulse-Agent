import pytest

from datapulse.sql_guard import SQLGuardError, guard_sql

TABLES = {"waybills", "customers", "routes"}


def test_select_gets_limit_injected():
    g = guard_sql("SELECT * FROM waybills", TABLES, max_rows=50)
    assert g.sql.endswith("LIMIT 50")
    assert g.tables == ("waybills",)
    assert g.limit_applied == 50


def test_small_limit_is_kept():
    g = guard_sql("SELECT * FROM waybills LIMIT 5", TABLES, max_rows=50)
    assert g.sql.endswith("LIMIT 5")
    assert g.limit_applied is None


def test_large_limit_is_clamped():
    g = guard_sql("SELECT * FROM waybills LIMIT 100000", TABLES, max_rows=50)
    assert g.sql.endswith("LIMIT 50")


def test_code_fence_and_semicolon_are_stripped():
    g = guard_sql("```sql\nSELECT 1 AS x FROM customers;\n```", TABLES)
    assert g.sql.startswith("SELECT 1")


def test_cte_and_union_are_allowed():
    guard_sql("WITH t AS (SELECT route_id FROM waybills) SELECT * FROM t", TABLES)
    guard_sql("SELECT customer_id FROM customers UNION SELECT customer_id FROM waybills", TABLES)


def test_chinese_alias_roundtrip():
    g = guard_sql('SELECT SUM(freight_revenue) AS "运费收入(元)" FROM waybills', TABLES)
    assert '"运费收入(元)"' in g.sql


def test_column_named_like_keyword_is_not_blocked():
    # 正则方案容易误杀：列名里带 update / delete 字样
    guard_sql("SELECT update_time, deleted_flag FROM waybills", TABLES)


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE waybills",
        "DELETE FROM waybills",
        "UPDATE waybills SET status = 'x'",
        "INSERT INTO customers VALUES (1)",
        "CREATE TABLE x (a INT)",
        "ALTER TABLE waybills ADD COLUMN x INT",
        "PRAGMA table_info(waybills)",
        "ATTACH DATABASE 'evil.db' AS evil",
        "VACUUM",
        "REPLACE INTO customers VALUES (1)",
    ],
)
def test_write_and_admin_statements_are_blocked(sql):
    with pytest.raises(SQLGuardError):
        guard_sql(sql, TABLES)


def test_stacked_statements_are_blocked():
    with pytest.raises(SQLGuardError, match="一条语句"):
        guard_sql("SELECT 1 FROM waybills; DROP TABLE waybills", TABLES)


def test_comment_tricks_are_blocked():
    with pytest.raises(SQLGuardError):
        guard_sql("SELECT 1 FROM waybills /* hi */ ; DELETE FROM waybills -- x", TABLES)


def test_unknown_and_system_tables_are_blocked():
    with pytest.raises(SQLGuardError, match="sqlite_master"):
        guard_sql("SELECT * FROM sqlite_master", TABLES)
    with pytest.raises(SQLGuardError, match="salary"):
        guard_sql("SELECT * FROM salary", TABLES)


def test_subquery_tables_are_checked():
    with pytest.raises(SQLGuardError):
        guard_sql("SELECT * FROM waybills WHERE customer_id IN (SELECT name FROM sqlite_master)", TABLES)


def test_dangerous_function_is_blocked():
    with pytest.raises(SQLGuardError, match="load_extension"):
        guard_sql("SELECT load_extension('evil.so') FROM waybills", TABLES)


def test_parse_error_is_reported():
    with pytest.raises(SQLGuardError):
        guard_sql("SELEC * FORM waybills", TABLES)


def test_empty_sql():
    with pytest.raises(SQLGuardError, match="为空"):
        guard_sql("   ", TABLES)
