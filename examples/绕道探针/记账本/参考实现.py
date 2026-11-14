# -*- coding: utf-8 -*-
"""记账本（Python 参考实现，只用标准库）。

与「记账.jsh」同功能、同输出口径 —— 用来对照「靠数据库能省多少事」。
写法按 Python 的常规习惯（英文标识符 + sqlite3），不刻意写长。
"""

import sqlite3
import sys
from datetime import date

DB = "账目.db"


def connect():
    conn = sqlite3.connect(DB)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS 账目 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            金额 REAL NOT NULL,
            类别 TEXT NOT NULL,
            备注 TEXT NOT NULL DEFAULT '',
            日期 TEXT NOT NULL)"""
    )
    return conn


def add(conn, amount, category, note):
    conn.execute(
        "INSERT INTO 账目(金额, 类别, 备注, 日期) VALUES (?, ?, ?, ?)",
        (amount, category, note, date.today().isoformat()),
    )
    conn.commit()
    print(f"已记：{amount:.2f} 元 / {category} / {note}")


def listing(conn):
    rows = conn.execute("SELECT 日期, 类别, 金额, 备注 FROM 账目 ORDER BY id").fetchall()
    print(f"共 {len(rows)} 笔")
    print(f"{'日期':<12}{'类别':<8}{'金额':>10}  备注")
    for day, cat, amount, note in rows:
        print(f"{day:<12}{cat:<8}{amount:>10.2f}  {note}")


def summary(conn):
    rows = conn.execute(
        "SELECT 类别, SUM(金额) FROM 账目 GROUP BY 类别 ORDER BY MIN(id)"
    ).fetchall()
    print(f"{'类别':<10}{'合计':>10}")
    total = 0.0
    for cat, amount in rows:
        print(f"{cat:<10}{amount:>10.2f}")
        total += amount
    print(f"{'合计':<10}{total:>10.2f}")


def query(conn, category):
    rows = conn.execute(
        "SELECT 日期, 金额, 备注 FROM 账目 WHERE 类别 = ? ORDER BY id", (category,)
    ).fetchall()
    print(f"「{category}」共 {len(rows)} 笔")
    for day, amount, note in rows:
        print(f"  {day}  {amount:.2f}  {note}")


def main(argv):
    conn = connect()
    if not argv:
        listing(conn)
    elif argv[0] == "记" and len(argv) >= 3:
        add(conn, float(argv[1]), argv[2], argv[3] if len(argv) > 3 else "")
    elif argv[0] == "汇总":
        summary(conn)
    elif argv[0] == "查" and len(argv) >= 2:
        query(conn, argv[1])
    elif argv[0] == "列出":
        listing(conn)
    else:
        print("用法：python 参考实现.py [记 <金额> <类别> [备注] | 汇总 | 查 <类别> | 列出]")


if __name__ == "__main__":
    main(sys.argv[1:])
