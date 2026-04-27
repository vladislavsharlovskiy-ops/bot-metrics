"""
Обработчики кнопок классификации платежей из webhook'а Prodamus.

Callback data формата:
  pay:flip:<payment_id>          — авто-повторку переклассифицировать в первичку
  pay:first_lead:<pid>:<lead_id> — первичка от существующего лида (создать клиента)
  pay:first_new:<pid>            — первичка от нового клиента
  pay:repeat_new:<pid>           — повторка, но клиента нет — создать клиента
  pay:ignore:<pid>               — пометить как ignored
"""

from __future__ import annotations

from datetime import datetime

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select

from db import get_session
from models import Client, Lead, Payment, RepeatSession, StageHistory
from sheets import sync_lead
from stages import PAID, REPEAT_PAID

router = Router()


def _money(p: Payment) -> str:
    sym = {"RUB": "₽", "USD": "$", "EUR": "€"}.get(p.currency, p.currency)
    return f"{p.amount:,.0f}".replace(",", " ") + " " + sym


def _create_client_from_payment(session, payment: Payment, lead: Lead | None = None) -> Client:
    name = payment.customer_name
    if not name and lead:
        name = lead.name
    client = Client(
        name=name,
        phone=payment.customer_phone,
        email=payment.customer_email,
        lead_id=lead.id if lead else None,
        first_payment_at=payment.paid_at,
        last_payment_at=payment.paid_at,
    )
    session.add(client)
    session.flush()
    return client


# ───────── flip (auto-repeat → first) ─────────

@router.callback_query(F.data.startswith("pay:flip:"))
async def cb_flip(call: CallbackQuery) -> None:
    payment_id = int(call.data.split(":")[2])
    with get_session() as session:
        payment = session.get(Payment, payment_id)
        if not payment:
            await call.answer("Платёж не найден", show_alert=True)
            return
        payment.payment_type = "first"
        session.commit()
    await call.message.edit_text(
        (call.message.text or call.message.html_text or "")
        + "\n\n✏️ Переклассифицировано как <b>первичка</b>.",
        parse_mode="HTML",
    )
    await call.answer("Готово")


# ───────── first from existing lead ─────────

@router.callback_query(F.data.startswith("pay:first_lead:"))
async def cb_first_from_lead(call: CallbackQuery) -> None:
    parts = call.data.split(":")
    payment_id, lead_id = int(parts[2]), int(parts[3])
    with get_session() as session:
        payment = session.get(Payment, payment_id)
        lead = session.get(Lead, lead_id)
        if not payment or not lead:
            await call.answer("Платёж или лид не найден", show_alert=True)
            return

        # Создаём клиента на основе лида + платежа
        client = _create_client_from_payment(session, payment, lead=lead)
        payment.client_id = client.id
        payment.lead_id = lead.id
        payment.payment_type = "first"

        # Двигаем лид в "Оплата"
        if lead.stage != PAID:
            lead.stage = PAID
            session.add(StageHistory(lead_id=lead.id, stage=PAID))

        session.commit()
        sync_lead(lead.id)
        client_name = client.name or client.phone or "клиент"

    await call.message.edit_text(
        f"✅ Платёж записан как <b>первичка</b> от лида #{lead_id}.\n"
        f"Лид «{client_name}» переведён в этап «Оплата консультации»,\n"
        f"в базе клиентов создана запись.",
        parse_mode="HTML",
    )
    await call.answer("Записано")


# ───────── first new client ─────────

@router.callback_query(F.data.startswith("pay:first_new:"))
async def cb_first_new(call: CallbackQuery) -> None:
    payment_id = int(call.data.split(":")[2])
    with get_session() as session:
        payment = session.get(Payment, payment_id)
        if not payment:
            await call.answer("Платёж не найден", show_alert=True)
            return
        client = _create_client_from_payment(session, payment)
        payment.client_id = client.id
        payment.payment_type = "first"
        session.commit()
        client_name = client.name or client.phone or "клиент"
    await call.message.edit_text(
        f"✅ Создан новый клиент «{client_name}», платёж записан как <b>первичка</b>.",
        parse_mode="HTML",
    )
    await call.answer("Записано")


# ───────── repeat new ─────────

@router.callback_query(F.data.startswith("pay:repeat_new:"))
async def cb_repeat_new(call: CallbackQuery) -> None:
    payment_id = int(call.data.split(":")[2])
    with get_session() as session:
        payment = session.get(Payment, payment_id)
        if not payment:
            await call.answer("Платёж не найден", show_alert=True)
            return
        client = _create_client_from_payment(session, payment)
        payment.client_id = client.id
        payment.payment_type = "repeat"
        # Создаём запись повторной сессии сразу на этапе "оплачено"
        rs = RepeatSession(
            client_id=client.id,
            stage=REPEAT_PAID,
            payment_id=payment.id,
        )
        session.add(rs)
        session.commit()
        client_name = client.name or client.phone or "клиент"
    await call.message.edit_text(
        f"✅ Создан клиент «{client_name}», платёж записан как <b>повторка</b>.\n"
        f"Запущена сессия в воронке повторок.",
        parse_mode="HTML",
    )
    await call.answer("Записано")


# ───────── ignore ─────────

@router.callback_query(F.data.startswith("pay:ignore:"))
async def cb_ignore(call: CallbackQuery) -> None:
    payment_id = int(call.data.split(":")[2])
    with get_session() as session:
        payment = session.get(Payment, payment_id)
        if not payment:
            await call.answer("Платёж не найден", show_alert=True)
            return
        payment.payment_type = "ignored"
        session.commit()
    await call.message.edit_text(
        (call.message.text or call.message.html_text or "")
        + "\n\n🚫 Платёж помечен как игнорируемый (не учитывается в отчётах).",
        parse_mode="HTML",
    )
    await call.answer("Игнорирую")
