"""Операции над Lead, общие для бота и дашборда.

Сейчас здесь только cascade-удаление: бот (handlers/leads.py) и дашборд
(web.py) должны удалять лид одинаково — иначе остаются «осиротевшие»
Payment-записи (Payment.lead_id становится NULL через FK ondelete=SET
NULL, но сама строка в БД остаётся), и счётчики на дашборде расходятся
(карточка «Первичных оплат: 5» против нижнего блока «Клиенты: 4»).
"""
from __future__ import annotations

from sqlalchemy import delete as sql_delete, select
from sqlalchemy.orm import Session

from models import Client, Lead, Payment


def delete_lead_cascade(session: Session, lead: Lead) -> None:
    """Удалить лид со всеми связями: Payments, Clients, RepeatSessions.

    1. Payments, привязанные напрямую к лиду — bulk-delete (у Payment
       нет своих cascade-зависимостей, ORM-обход не нужен).
    2. Clients, у которых originating lead = удаляемый — ORM-delete.
       Через relationship cascade='all, delete-orphan' автоматически
       чистятся их оставшиеся Payments и RepeatSessions.
    3. Сам лид — StageHistory чистится через relationship cascade
       в Lead.history.
    """
    session.execute(sql_delete(Payment).where(Payment.lead_id == lead.id))
    clients = session.execute(
        select(Client).where(Client.lead_id == lead.id)
    ).scalars().all()
    for c in clients:
        session.delete(c)
    session.delete(lead)
