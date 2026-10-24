"""生成演示用的物流 TMS 数据库（SQLite）。

数据全部为程序随机生成的虚构数据，仅用于演示 Text-to-SQL，
业务结构参考了干线零担物流的常见报表口径：线路毛利、车队准点率、客户月结账单。
随机种子固定，同一份代码生成的数据完全一致，方便写测试和做评测。
"""

from __future__ import annotations

import random
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

SCHEMA_DDL = """
CREATE TABLE customers (
    customer_id      TEXT PRIMARY KEY,
    customer_name    TEXT NOT NULL,
    industry         TEXT NOT NULL,
    city             TEXT NOT NULL,
    settlement_type  TEXT NOT NULL,
    credit_limit     REAL NOT NULL
);

CREATE TABLE routes (
    route_id       TEXT PRIMARY KEY,
    origin_city    TEXT NOT NULL,
    dest_city      TEXT NOT NULL,
    line_type      TEXT NOT NULL,
    distance_km    INTEGER NOT NULL
);

CREATE TABLE vehicles (
    vehicle_id     TEXT PRIMARY KEY,
    plate_no       TEXT NOT NULL,
    vehicle_type   TEXT NOT NULL,
    fleet          TEXT NOT NULL
);

CREATE TABLE waybills (
    waybill_id           TEXT PRIMARY KEY,
    customer_id          TEXT NOT NULL REFERENCES customers(customer_id),
    route_id             TEXT NOT NULL REFERENCES routes(route_id),
    vehicle_id           TEXT NOT NULL REFERENCES vehicles(vehicle_id),
    ship_date            TEXT NOT NULL,
    plan_arrival_time    TEXT NOT NULL,
    actual_arrival_time  TEXT,
    weight_kg            REAL NOT NULL,
    volume_m3            REAL NOT NULL,
    charge_weight_kg     REAL NOT NULL,
    freight_revenue      REAL NOT NULL,
    transport_cost       REAL NOT NULL,
    status               TEXT NOT NULL
);

CREATE TABLE exceptions (
    exception_id     TEXT PRIMARY KEY,
    waybill_id       TEXT NOT NULL REFERENCES waybills(waybill_id),
    exception_type   TEXT NOT NULL,
    report_date      TEXT NOT NULL,
    claim_amount     REAL NOT NULL,
    status           TEXT NOT NULL
);

CREATE TABLE monthly_bills (
    bill_id        TEXT PRIMARY KEY,
    customer_id    TEXT NOT NULL REFERENCES customers(customer_id),
    bill_month     TEXT NOT NULL,
    amount_due     REAL NOT NULL,
    amount_paid    REAL NOT NULL,
    due_date       TEXT NOT NULL,
    status         TEXT NOT NULL
);

CREATE INDEX idx_waybills_ship_date ON waybills(ship_date);
CREATE INDEX idx_waybills_customer ON waybills(customer_id);
CREATE INDEX idx_waybills_route ON waybills(route_id);
"""

DATA_START = date(2026, 1, 1)
DATA_END = date(2026, 10, 31)
VOLUME_FACTOR_KG_PER_M3 = 250  # 轻抛货折算系数：1 方折 250 公斤

_ORIGINS = ["佛山", "广州", "东莞"]
_DESTS = {
    "上海": 1450, "杭州": 1280, "苏州": 1420, "南京": 1500, "武汉": 1000, "长沙": 690,
    "成都": 1650, "重庆": 1350, "郑州": 1500, "北京": 2150, "西安": 1700, "南宁": 600,
}
_DISTRICTS = ["顺德", "南海", "东莞", "中山", "江门", "番禺"]
_BRANDS = ["华晟", "粤辉", "恒泰", "联创", "金鹏", "嘉信", "宏达", "博远", "鼎盛", "新锐", "立丰", "汇通"]
_INDUSTRIES = {
    "家电": "电器", "家具": "家居", "陶瓷": "陶瓷", "五金": "五金制品", "电子": "电子科技", "服装": "服饰",
}
_VEHICLE_TYPES = ["4.2米厢车", "9.6米厢车", "13米高栏", "17.5米平板"]


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def build_demo_db(path: str | Path, seed: int = 20260101, n_waybills: int = 3600) -> Path:
    """生成演示库。若文件已存在会被覆盖。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    rng = random.Random(seed)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_DDL)

    # ---- 客户 ----
    customers = []
    industries = list(_INDUSTRIES)
    for i in range(24):
        industry = industries[i % len(industries)]
        district = _DISTRICTS[(i + i // 6) % len(_DISTRICTS)]
        brand = _BRANDS[(i * 5) % len(_BRANDS)]
        name = f"{district}{brand}{_INDUSTRIES[industry]}有限公司"
        settlement = "月结" if i % 6 != 5 else "现结"
        credit = float(rng.choice([50_000, 100_000, 200_000, 300_000, 500_000]))
        # 保证客户画像有轻重之分：前几个是大客户
        customers.append((f"C{i + 1:03d}", name, industry, district, settlement, credit))
    conn.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?)", customers)
    customer_weights = [max(1.0, 12 - i * 0.5) for i in range(len(customers))]

    # ---- 线路 ----
    routes = []
    rid = 1
    for origin in _ORIGINS:
        for dest, dist in _DESTS.items():
            if rng.random() < 0.55 or origin == "佛山":
                offset = {"佛山": 0, "广州": -20, "东莞": 40}[origin]
                line_type = "整车" if dist > 1400 and rng.random() < 0.35 else "零担专线"
                routes.append((f"R{rid:02d}", origin, dest, line_type, dist + offset))
                rid += 1
    conn.executemany("INSERT INTO routes VALUES (?,?,?,?,?)", routes)

    # ---- 车辆 ----
    vehicles = []
    for i in range(30):
        fleet = "自有车队" if i < 14 else "外协车队"
        vtype = _VEHICLE_TYPES[i % len(_VEHICLE_TYPES)]
        plate = f"粤E{rng.choice('ABCDFGHJK')}{rng.randint(10000, 99999)}"
        vehicles.append((f"V{i + 1:03d}", plate, vtype, fleet))
    conn.executemany("INSERT INTO vehicles VALUES (?,?,?,?)", vehicles)

    # ---- 运单 ----
    waybills = []
    total_days = (DATA_END - DATA_START).days
    route_by_id = {r[0]: r for r in routes}
    vehicle_by_id = {v[0]: v for v in vehicles}
    # 某些线路天然更赚钱 / 更亏钱，让“线路毛利”分析有看头
    route_margin_bias = {r[0]: rng.uniform(-0.06, 0.08) for r in routes}
    for i in range(n_waybills):
        cust = rng.choices(customers, weights=customer_weights)[0]
        route = rng.choice(routes)
        vehicle = rng.choice(vehicles)
        # 旺季（9-10 月）单量更多
        day = int(total_days * (rng.random() ** 0.85))
        ship = datetime.combine(DATA_START + timedelta(days=day), datetime.min.time()) + timedelta(
            hours=rng.randint(8, 21), minutes=rng.choice([0, 15, 30, 45])
        )
        dist = route_by_id[route[0]][4]
        plan_hours = dist / 55 + 8
        plan = ship + timedelta(hours=round(plan_hours))

        weight = round(rng.uniform(80, 6000) if route[3] == "零担专线" else rng.uniform(8000, 28000), 1)
        light_cargo = cust[2] in ("家具", "家电", "服装")
        density = rng.uniform(90, 240) if light_cargo else rng.uniform(220, 600)
        volume = round(weight / density, 2)
        charge_weight = round(max(weight, volume * VOLUME_FACTOR_KG_PER_M3), 1)

        unit_price = 0.00016 * dist + 0.12  # 元 / 公斤
        if route[3] == "整车":
            unit_price *= 0.72
        revenue = round(charge_weight * unit_price * rng.uniform(0.92, 1.08) + 50, 2)
        outsourced = vehicle_by_id[vehicle[0]][3] == "外协车队"
        cost_ratio = rng.uniform(0.70, 0.86) + (0.06 if outsourced else 0.0) - route_margin_bias[route[0]]
        cost = round(revenue * cost_ratio, 2)

        days_ago = (DATA_END - ship.date()).days
        if days_ago <= 2 and rng.random() < 0.8:
            status, actual = "在途", None
        else:
            delay_p = 0.18 if outsourced else 0.08
            if ship.month in (9, 10):
                delay_p += 0.05
            if rng.random() < delay_p:
                delay = timedelta(hours=rng.uniform(1, 30))
            else:
                delay = -timedelta(hours=rng.uniform(0, 5))
            actual = plan + delay
            status = "异常" if rng.random() < 0.035 else "已签收"
        waybills.append((
            f"WB{ship:%y%m}{i + 1:05d}", cust[0], route[0], vehicle[0], _fmt(ship), _fmt(plan),
            _fmt(actual) if actual else None, weight, volume, charge_weight, revenue, cost, status,
        ))
    waybills.sort(key=lambda w: w[4])
    conn.executemany("INSERT INTO waybills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", waybills)

    # ---- 异常与理赔 ----
    exceptions = []
    eid = 1
    for wb in waybills:
        if wb[12] == "异常" or (wb[12] == "已签收" and rng.random() < 0.02):
            etype = rng.choices(["货损", "延误", "丢件", "错发"], weights=[5, 3, 1, 1])[0]
            report = datetime.strptime(wb[6] or wb[5], "%Y-%m-%d %H:%M:%S") + timedelta(hours=rng.randint(2, 48))
            claim = round(rng.uniform(200, 9000) if etype != "延误" else rng.uniform(100, 1500), 2)
            estatus = "处理中" if (DATA_END - report.date()).days < 20 and rng.random() < 0.6 else "已结案"
            exceptions.append((f"EX{eid:04d}", wb[0], etype, report.strftime("%Y-%m-%d"), claim, estatus))
            eid += 1
    conn.executemany("INSERT INTO exceptions VALUES (?,?,?,?,?,?)", exceptions)

    # ---- 客户月结账单（由运单汇总得出）----
    bills = []
    rows = conn.execute(
        """
        SELECT w.customer_id, strftime('%Y-%m', w.ship_date) AS m, ROUND(SUM(w.freight_revenue), 2)
        FROM waybills w JOIN customers c ON c.customer_id = w.customer_id
        WHERE c.settlement_type = '月结'
        GROUP BY w.customer_id, m ORDER BY m, w.customer_id
        """
    ).fetchall()
    bid = 1
    for cust_id, month, due in rows:
        y, mth = map(int, month.split("-"))
        due_date = date(y + (mth == 12), mth % 12 + 1, 25)
        if month <= "2026-07":
            paid_ratio = 1.0 if rng.random() < 0.92 else rng.uniform(0.5, 0.95)
        elif month <= "2026-09":
            paid_ratio = rng.choice([1.0, 1.0, 1.0, rng.uniform(0.3, 0.9), 0.0])
        else:
            paid_ratio = rng.choice([0.0, 0.0, rng.uniform(0.2, 0.8)])
        paid = round(due * paid_ratio, 2)
        status = "已结清" if paid >= due else ("未回款" if paid == 0 else "部分回款")
        bills.append((f"B{bid:04d}", cust_id, month, due, paid, due_date.isoformat(), status))
        bid += 1
    conn.executemany("INSERT INTO monthly_bills VALUES (?,?,?,?,?,?,?)", bills)

    conn.commit()
    conn.close()
    return path


def ensure_demo_db(path: str | Path) -> Path:
    """数据库不存在时自动生成，已存在则直接复用。"""
    path = Path(path)
    if not path.exists():
        build_demo_db(path)
    return path


if __name__ == "__main__":
    out = build_demo_db("data/tms_demo.db")
    print(f"演示库已生成：{out}")
