"""
Обработчики кнопок классификации платежей из webhook'а Prodamus.

Callback data формата:
  pay:flip:<payment_id>          — авто-повторку переклассифицировать в первичку
  pay:first_lead:<pid>:<lead_id> — первичка от существующего лида (создать клиента)
  pay:first_new:<pid>            — первичка от нового клиента → меню источников
  pay:fn_src:<pid>:<source>      — выбран источник, создаём лид + клиента + историю
  pay:repeat_new:<pid>           — повторка, но клиента нет — создать клиента
  pay:ignore:<pid>               — пометить как ignored

Команды:
  /fixpay — найти платёж-сироту (первичка без лида) и дозаписать с источником
"""

from __future__ import annotations

from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from config import OWNER_ID
from db import get_session
from models import Client, Lead, Payment, RepeatSession, StageHistory
from sheets import sync_lead
from stages import LEAD_NEW, PAID, REPEAT_PAID, SOURCES

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


# ───────── first new client → выбор источника ─────────

def _source_keyboard(payment_id: int) -> InlineKeyboardMarkup:
    """Клавиатура с источниками для классификации первички."""
    buttons = []
    row: list[InlineKeyboardButton] = []
    for code, title in SOURCES:
        row.append(InlineKeyboardButton(
            text=title,
            callback_data=f"pay:fn_src:{payment_id}:{code}",
        ))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("pay:first_new:"))
async def cb_first_new(call: CallbackQuery) -> None:
    """Шаг 1: спрашиваем источник, чтобы создать лид с правильным каналом."""
    payment_id = int(call.data.split(":")[2])
    with get_session() as session:
        payment = session.get(Payment, payment_id)
        if not payment:
            await call.answer("Платёж не найден", show_alert=True)
            return

    base_text = call.message.text or call.message.html_text or ""
    await call.message.edit_text(
        base_text + "\n\n👉 <b>Откуда пришёл клиент?</b>",
        parse_mode="HTML",
        reply_markup=_source_keyboard(payment_id),
    )
    await call.answer()


def _record_first_payment(session, payment: Payment, source: str) -> tuple[Lead, Client]:
    """Создаёт лид + клиента + историю этапов для первички. Возвращает (lead, client)."""
    name = payment.customer_name or payment.customer_phone or payment.customer_email or "Клиент Prodamus"
    paid_at = payment.paid_at or datetime.now()

    lead = Lead(
        name=payment.customer_name,
        username=payment.customer_phone or payment.customer_email,
        source=source,
        request=payment.product or "Оплата через Prodamus",
        stage=PAID,
        created_at=paid_at,
    )
    session.add(lead)
    session.flush()

    # История: lead_new (на момент платежа) → paid (тогда же)
    session.add(StageHistory(lead_id=lead.id, stage=LEAD_NEW, changed_at=paid_at))
    session.add(StageHistory(lead_id=lead.id, stage=PAID, changed_at=paid_at))

    client = _create_client_from_payment(session, payment, lead=lead)
    payment.client_id = client.id
    payment.lead_id = lead.id
    payment.payment_type = "first"
    return lead, client


@router.callback_query(F.data.startswith("pay:fn_src:"))
async def cb_first_new_with_source(call: CallbackQuery) -> None:
    """Шаг 2: получили источник, создаём лид/клиента/историю и связываем платёж."""
    parts = call.data.split(":")
    payment_id = int(parts[2])
    source = parts[3]
    source_title = dict(SOURCES).get(source, source)

    with get_session() as session:
        payment = session.get(Payment, payment_id)
        if not payment:
            await call.answer("Платёж не найден", show_alert=True)
            return
        if payment.lead_id:
            await call.answer("Этот платёж уже привязан к лиду", show_alert=True)
            return

        lead, client = _record_first_payment(session, payment, source)
        session.commit()
        sync_lead(lead.id)
        client_name = client.name or client.phone or "клиент"
        lead_id = lead.id

    await call.message.edit_text(
        f"✅ Платёж записан как <b>первичка</b>.\n"
        f"Создан лид #{lead_id} «{client_name}» · источник: <b>{source_title}</b>\n"
        f"Этап: «Оплата консультации». В базу клиентов добавлено.",
        parse_mode="HTML",
    )
    await call.answer("Записано")


# ───────── /fixpay: починить платёж-сироту ─────────

@router.message(Command("fixpay"))
async def cmd_fixpay(message: Message, bot: Bot) -> None:
    """Найти последний платёж-первичку без лида и предложить дозаписать с источником."""
    if message.from_user and message.from_user.id != OWNER_ID:
        return
    with get_session() as session:
        payment = session.execute(
            select(Payment)
            .where(Payment.payment_type == "first")
            .where(Payment.lead_id.is_(None))
            .order_by(Payment.paid_at.desc())
        ).scalars().first()
        if not payment:
            await message.answer("Нет платежей-сирот: все первички привязаны к лидам ✨")
            return
        pid = payment.id
        amount = _money(payment)
        who = payment.customer_name or payment.customer_phone or payment.customer_email or "—"
        product = payment.product or "—"
        paid_at = payment.paid_at.strftime("%d.%m.%Y %H:%M") if payment.paid_at else "—"

    text = (
        f"🛠 <b>Платёж-сирота</b>\n\n"
        f"💰 {amount}\n"
        f"👤 {who}\n"
        f"🛍 {product}\n"
        f"🕒 {paid_at}\n\n"
        f"👉 <b>Откуда пришёл клиент?</b>"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=_source_keyboard(pid))


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
