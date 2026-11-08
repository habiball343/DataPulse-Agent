import sqlite3

import pytest

from datapulse.db import QueryError


def test_tables_and_columns(db):
    assert set(db.table_names()) == {"customers", "routes", "vehicles", "waybills", "exceptions", "monthly_bills"}
    cols = {c.name: c for c in db.columns("waybills")}
    assert cols["waybill_id"].primary_key
    assert cols["customer_id"].references == "customers.customer_id"
    assert set(cols["status"].sample_values) == {"已签收", "在途", "异常"}


def test_connection_is_read_only_even_without_guard(db):
    # 第二道防线：即使绕过 SQL 守卫直接执行写语句，数据库层也会拒绝
    with pytest.raises(QueryError):
        db.execute("DELETE FROM waybills")
    with pytest.raises(QueryError):
        db.execute("CREATE TABLE hacked (x INT)")
    assert db.row_count("waybills") > 0


def test_query_error_message(db):
    with pytest.raises(QueryError, match="no such column"):
        db.execute("SELECT not_a_column FROM waybills")


def test_timeout(db_path):
    from datapulse.db import Database

    slow = Database(db_path, timeout_s=0.05)
    with pytest.raises(QueryError, match="超时"):
        # 三表笛卡尔积，足够慢
        slow.execute("SELECT COUNT(*) FROM waybills a, waybills b, waybills c")


def test_demo_data_is_deterministic(tmp_path):
    from datapulse.demo_data import build_demo_db

    a = build_demo_db(tmp_path / "a.db")
    b = build_demo_db(tmp_path / "b.db")
    q = "SELECT COUNT(*), ROUND(SUM(freight_revenue), 2) FROM waybills"
    assert sqlite3.connect(a).execute(q).fetchone() == sqlite3.connect(b).execute(q).fetchone()
