#!/usr/bin/env python3
"""
Печатает сводку по SQLite-базе bot.db: счётчики, диапазоны дат, последние
записи. Удобно сравнить «что в живой базе» vs «что в бэкапе».

Использование:
    python3 tools/db_inspect.py /opt/bot-metrics/data/bot.db
    python3 tools/db_inspect.py base.db donor.db   # печатает обе подряд
"""
from __future__ import annotations

import os
import sqlite3
import sys


TABLES = ("leads", "stage_history", "clients", "payments", "repeat_sessions")


def _date_col(cur, table: str) -> str | None:
    cols = {row[1] for row in cur.execute(f"PRAGMA table_info({table})").fetchall()}
    for c in ("paid_at", "created_at", "changed_at"):
        if c in cols:
            return c
    return None


def inspect(path: str) -> None:
    if not os.path.exists(path):
        print(f"\n!!! файл не найден: {path}\n")
        return

    size = os.path.getsize(path)
    print()
    print("=" * 72)
    print(f"  {path}")
    print(f"  размер: {size} байт ({size / 1024:.1f} KB)")
    print("=" * 72)

    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    existing_tables = {
        row[0] for row in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }

    for table in TABLES:
        if table not in existing_tables:
            print(f"\n  {table}: НЕТ В БАЗЕ")
            continue
        count = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        line = f"\n  {table}: {count}"
        dc = _date_col(cur, table)
        if dc and count:
            r = cur.execute(
                f"SELECT MIN({dc}) AS mn, MAX({dc}) AS mx FROM {table}"
            ).fetchone()
            line += f"   ({dc}: {r['mn']} … {r['mx']})"
        print(line)

    if "leads" in existing_tables:
        rows = cur.execute(
            "SELECT id, created_at, name, source, stage, telegram_user_id "
            "FROM leads ORDER BY id DESC LIMIT 10"
        ).fetchall()
        if rows:
            print("\n  Последние 10 лидов:")
            for r in rows:
                tg = r["telegram_user_id"] or "—"
                name = r["name"] or "—"
                print(
                    f"    #{r['id']:>4} | {r['created_at']} | "
                    f"{name[:24]:<24} | {r['source']:<10} | "
                    f"{r['stage']:<14} | tg={tg}"
                )

    if "payments" in existing_tables:
        cnt = cur.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        if cnt:
            rows = cur.execute(
                "SELECT id, paid_at, amount, customer_name, customer_phone, "
                "       payment_type, lead_id "
                "FROM payments ORDER BY paid_at DESC LIMIT 10"
            ).fetchall()
            print("\n  Последние 10 платежей:")
            for r in rows:
                who = r["customer_name"] or r["customer_phone"] or "—"
                print(
                    f"    #{r['id']:>4} | {r['paid_at']} | "
                    f"{r['amount']:>9.0f} | {who[:24]:<24} | "
                    f"{r['payment_type']:<13} | lead={r['lead_id']}"
                )

    con.close()


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    for p in sys.argv[1:]:
        inspect(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
