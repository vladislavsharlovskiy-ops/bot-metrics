#!/usr/bin/env python3
"""
Сливает данные из DONOR.db в BASE.db и пишет результат в OUT.db.

ИСПОЛЬЗОВАНИЕ:
    python3 tools/db_merge.py BASE DONOR OUT [--dry-run]

ЛОГИКА:
    1. Копируем BASE → OUT (через .backup, корректно для WAL).
    2. Идём по DONOR и доливаем в OUT всё, чего там нет.
    3. Источники не модифицируются — итог пишется в OUT, после этого
       пользователь сам подменяет живую bot.db, если результат устроил.

ДЕДУПЛИКАЦИЯ:
    leads          — по telegram_user_id, иначе по (name, source, created_at)
    stage_history  — копируется только для НОВЫХ лидов (донорских, что вставили);
                     для дубликатов считаем, что в BASE уже своя история
    clients        — по последним 10 цифрам телефона, иначе по lower(email)
    payments       — по prodamus_id (UNIQUE-индекс в схеме)
    repeat_sessions — копируется только для НОВЫХ клиентов (донорских)

ID-маппинг:
    Все вставки используют автоинкремент BASE.id, поэтому донорские id могут
    смениться. Скрипт ведёт map'ы donor_id→base_id для leads/clients/payments
    и проставляет правильные FK при вставке зависимых записей.

ПРИМЕР:
    # Сначала — посмотреть, что мы получим (без записи на диск):
    python3 tools/db_merge.py /opt/bot-metrics/data/bot.db \\
        /opt/bot-metrics/data/backups/bot-before-import-20260504-114506.db \\
        /opt/bot-metrics/data/bot-merged.db --dry-run

    # Если устроило — без --dry-run:
    python3 tools/db_merge.py /opt/bot-metrics/data/bot.db \\
        /opt/bot-metrics/data/backups/bot-before-import-20260504-114506.db \\
        /opt/bot-metrics/data/bot-merged.db

    # Проверь результат:
    python3 tools/db_inspect.py /opt/bot-metrics/data/bot-merged.db

    # И только потом подмени живую базу (после остановки сервисов):
    sudo systemctl stop bot-metrics-bot bot-metrics-web
    sudo -u bot mv /opt/bot-metrics/data/bot.db \\
                  /opt/bot-metrics/data/bot.db.before-merge
    sudo -u bot mv /opt/bot-metrics/data/bot-merged.db \\
                  /opt/bot-metrics/data/bot.db
    sudo systemctl start bot-metrics-bot bot-metrics-web
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from typing import Any


def _phone_digits(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = "".join(c for c in phone if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else (digits or None)


def _table_columns(cur: sqlite3.Cursor, table: str) -> list[str]:
    return [row[1] for row in cur.execute(f"PRAGMA table_info({table})").fetchall()]


def _filtered_dict(row: sqlite3.Row, allowed: set[str]) -> dict[str, Any]:
    """Берём из row только те поля, что есть в allowed (без id)."""
    out: dict[str, Any] = {}
    for k in row.keys():
        if k == "id":
            continue
        if k in allowed:
            out[k] = row[k]
    return out


def _insert(cur: sqlite3.Cursor, table: str, data: dict[str, Any]) -> int:
    cols = list(data.keys())
    placeholders = ",".join(["?"] * len(cols))
    cur.execute(
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
        [data[c] for c in cols],
    )
    return cur.lastrowid


def _open_out(base_path: str, out_path: str, dry_run: bool) -> sqlite3.Connection:
    """Открывает OUT (на диске) или :memory: (для dry-run), копируя в него BASE."""
    src = sqlite3.connect(f"file:{base_path}?mode=ro", uri=True)
    if dry_run:
        conn = sqlite3.connect(":memory:")
    else:
        if os.path.exists(out_path):
            os.remove(out_path)
        conn = sqlite3.connect(out_path)
    src.backup(conn)
    src.close()
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def merge(base_path: str, donor_path: str, out_path: str, dry_run: bool) -> None:
    if not os.path.exists(base_path):
        sys.exit(f"BASE не существует: {base_path}")
    if not os.path.exists(donor_path):
        sys.exit(f"DONOR не существует: {donor_path}")
    if os.path.abspath(out_path) == os.path.abspath(base_path):
        sys.exit("OUT не должен совпадать с BASE — это испортило бы живую базу.")
    if os.path.abspath(out_path) == os.path.abspath(donor_path):
        sys.exit("OUT не должен совпадать с DONOR.")

    print(f"BASE : {base_path}")
    print(f"DONOR: {donor_path}")
    print(f"OUT  : {out_path}{' (dry-run, in-memory)' if dry_run else ''}")
    print()

    conn = _open_out(base_path, out_path, dry_run)
    cur = conn.cursor()

    donor = sqlite3.connect(f"file:{donor_path}?mode=ro", uri=True)
    donor.row_factory = sqlite3.Row
    dcur = donor.cursor()

    # Какие колонки есть в BASE (на случай, если DONOR старее/новее)
    cols_leads = set(_table_columns(cur, "leads"))
    cols_history = set(_table_columns(cur, "stage_history"))
    cols_clients = set(_table_columns(cur, "clients"))
    cols_payments = set(_table_columns(cur, "payments"))
    cols_repeat = set(_table_columns(cur, "repeat_sessions"))

    # ─── LEADS ──────────────────────────────────────────────────────────
    base_lead_by_tg: dict[int, int] = {}
    base_lead_by_natural: dict[tuple, int] = {}
    for r in cur.execute(
        "SELECT id, telegram_user_id, name, source, created_at FROM leads"
    ).fetchall():
        if r["telegram_user_id"]:
            base_lead_by_tg[r["telegram_user_id"]] = r["id"]
        base_lead_by_natural[(r["name"], r["source"], r["created_at"])] = r["id"]

    lead_id_map: dict[int, int] = {}
    inserted_lead_donor_ids: set[int] = set()

    for r in dcur.execute("SELECT * FROM leads").fetchall():
        donor_id = r["id"]
        tg = r["telegram_user_id"]
        if tg and tg in base_lead_by_tg:
            lead_id_map[donor_id] = base_lead_by_tg[tg]
            continue
        nk = (r["name"], r["source"], r["created_at"])
        if nk in base_lead_by_natural:
            lead_id_map[donor_id] = base_lead_by_natural[nk]
            continue
        new_id = _insert(cur, "leads", _filtered_dict(r, cols_leads))
        lead_id_map[donor_id] = new_id
        inserted_lead_donor_ids.add(donor_id)
        if tg:
            base_lead_by_tg[tg] = new_id
        base_lead_by_natural[nk] = new_id

    leads_inserted = len(inserted_lead_donor_ids)
    leads_skipped = len(lead_id_map) - leads_inserted

    # ─── STAGE_HISTORY ──────────────────────────────────────────────────
    history_inserted = 0
    for r in dcur.execute("SELECT * FROM stage_history").fetchall():
        donor_lead_id = r["lead_id"]
        if donor_lead_id not in inserted_lead_donor_ids:
            continue
        d = _filtered_dict(r, cols_history)
        d["lead_id"] = lead_id_map[donor_lead_id]
        _insert(cur, "stage_history", d)
        history_inserted += 1

    # ─── CLIENTS ────────────────────────────────────────────────────────
    base_client_by_phone: dict[str, int] = {}
    base_client_by_email: dict[str, int] = {}
    for r in cur.execute("SELECT id, phone, email FROM clients").fetchall():
        ph = _phone_digits(r["phone"])
        if ph:
            base_client_by_phone[ph] = r["id"]
        if r["email"]:
            base_client_by_email[r["email"].lower()] = r["id"]

    client_id_map: dict[int, int] = {}
    inserted_client_donor_ids: set[int] = set()

    for r in dcur.execute("SELECT * FROM clients").fetchall():
        donor_id = r["id"]
        match: int | None = None
        ph = _phone_digits(r["phone"])
        if ph and ph in base_client_by_phone:
            match = base_client_by_phone[ph]
        if match is None and r["email"]:
            em = r["email"].lower()
            if em in base_client_by_email:
                match = base_client_by_email[em]
        if match is not None:
            client_id_map[donor_id] = match
            continue
        d = _filtered_dict(r, cols_clients)
        if d.get("lead_id") is not None:
            d["lead_id"] = lead_id_map.get(d["lead_id"])
        new_id = _insert(cur, "clients", d)
        client_id_map[donor_id] = new_id
        inserted_client_donor_ids.add(donor_id)
        if ph:
            base_client_by_phone[ph] = new_id
        if r["email"]:
            base_client_by_email[r["email"].lower()] = new_id

    clients_inserted = len(inserted_client_donor_ids)
    clients_skipped = len(client_id_map) - clients_inserted

    # ─── PAYMENTS ───────────────────────────────────────────────────────
    base_payment_by_prodamus: dict[str, int] = {}
    for r in cur.execute("SELECT id, prodamus_id FROM payments").fetchall():
        base_payment_by_prodamus[r["prodamus_id"]] = r["id"]

    payment_id_map: dict[int, int] = {}
    payments_inserted = 0
    payments_skipped = 0

    for r in dcur.execute("SELECT * FROM payments").fetchall():
        pid = r["prodamus_id"]
        if pid in base_payment_by_prodamus:
            payment_id_map[r["id"]] = base_payment_by_prodamus[pid]
            payments_skipped += 1
            continue
        d = _filtered_dict(r, cols_payments)
        if d.get("client_id") is not None:
            d["client_id"] = client_id_map.get(d["client_id"])
        if d.get("lead_id") is not None:
            d["lead_id"] = lead_id_map.get(d["lead_id"])
        new_id = _insert(cur, "payments", d)
        payment_id_map[r["id"]] = new_id
        base_payment_by_prodamus[pid] = new_id
        payments_inserted += 1

    # ─── REPEAT_SESSIONS ────────────────────────────────────────────────
    repeat_inserted = 0
    for r in dcur.execute("SELECT * FROM repeat_sessions").fetchall():
        if r["client_id"] not in inserted_client_donor_ids:
            # У существующего клиента уже могут быть свои repeat_sessions —
            # без надёжного natural-key не лезем, чтобы не дублить.
            continue
        d = _filtered_dict(r, cols_repeat)
        d["client_id"] = client_id_map[r["client_id"]]
        if d.get("payment_id") is not None:
            d["payment_id"] = payment_id_map.get(d["payment_id"])
        _insert(cur, "repeat_sessions", d)
        repeat_inserted += 1

    # ─── ИТОГ ───────────────────────────────────────────────────────────
    if dry_run:
        conn.rollback()
    else:
        conn.commit()

    print("РЕЗУЛЬТАТ:")
    print(f"  leads:           вставлено {leads_inserted:>4}, дубликатов {leads_skipped}")
    print(f"  stage_history:   вставлено {history_inserted:>4}")
    print(f"  clients:         вставлено {clients_inserted:>4}, дубликатов {clients_skipped}")
    print(f"  payments:        вставлено {payments_inserted:>4}, дубликатов {payments_skipped}")
    print(f"  repeat_sessions: вставлено {repeat_inserted:>4}")

    print("\nПроверь итог:")
    print(f"  SELECT COUNT(*) FROM leads, clients, payments в {out_path}")
    if dry_run:
        print("\n[dry-run] изменения не сохранены, файл OUT не записан.")
    else:
        print(f"\nГотово. Файл: {out_path}")
        print("Дальше — db_inspect.py для проверки и ручная подмена bot.db.")

    conn.close()
    donor.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__,
    )
    parser.add_argument("base", help="основная база (живая bot.db)")
    parser.add_argument("donor", help="донор (бэкап)")
    parser.add_argument("out", help="куда сохранить итог")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="не писать на диск, прокрутить в памяти и показать счётчики",
    )
    args = parser.parse_args()
    merge(args.base, args.donor, args.out, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
