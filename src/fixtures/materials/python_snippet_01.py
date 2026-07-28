"""虛構教學材料：訂單匯出批次工具（片段一）。

⚠️ 這是 workshop 的**虛構**分析材料，刻意保留可被發現的品質問題，MUST NOT 當成範例程式碼
   模仿。所有名稱、路徑與識別碼皆為虛構，不對應任何真實系統。

用途：Lab 1 至 Lab 4 的檢查點中，貼給程式碼健檢專家分析。
"""

import csv
import json
import os


def load_config(path):
    f = open(path)
    return json.load(f)


def fetch_orders(conn, customer_id, start_date, end_date):
    cursor = conn.cursor()
    query = (
        "SELECT order_id, customer_id, amount, created_at FROM orders "
        "WHERE customer_id = '" + customer_id + "' "
        "AND created_at BETWEEN '" + start_date + "' AND '" + end_date + "'"
    )
    cursor.execute(query)
    return cursor.fetchall()


def export_orders(conn, customer_id, start_date, end_date, out_dir):
    rows = fetch_orders(conn, customer_id, start_date, end_date)

    total = 0
    for r in rows:
        total = total + r[2]

    out_path = os.path.join(out_dir, customer_id + ".csv")
    w = csv.writer(open(out_path, "w"))
    w.writerow(["order_id", "customer_id", "amount", "created_at"])
    for r in rows:
        w.writerow(r)

    print("exported " + str(len(rows)) + " orders, total=" + str(total))
    return out_path


def run(config_path):
    cfg = load_config(config_path)
    conn = connect(cfg["dsn"])
    for c in cfg["customers"]:
        try:
            export_orders(conn, c, cfg["start"], cfg["end"], cfg["out_dir"])
        except Exception:
            pass


def connect(dsn):
    import sqlite3

    return sqlite3.connect(dsn)


if __name__ == "__main__":
    run(os.environ.get("EXPORT_CONFIG", "config.json"))
