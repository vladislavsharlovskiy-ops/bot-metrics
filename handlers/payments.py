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
from sqlalchemy import or_, select

from config import OWNER_ID
from db import get_session
from models import Client, Lead, Payment, RepeatSession, StageHistory
from sheets import sync_lead
from stages import LEAD_NEW, PAID, REPEAT_PAID, SOURCES


def _phone_digits(phone):
    if not phone:
        return None
    digits = "".join(c for c in phone if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else (digits or None)


def _match_lead_candidates(session, name, phone, email, limit=5):
    """Дублирует логику webhook._match_leads: ищет лидов по словам имени + телефону + email."""
    conds = []
    if name:
        for word in name.split():
            if len(word) >= 3:
                conds.append(Lead.name.ilike(f"%{word}%"))
                conds.append(Lead.username.ilike(f"%{word}%"))
    if phone:
        conds.append(Lead.username.ilike(f"%{phone}%"))
        digits = _phone_digits(phone)
        if digits and len(digits) >= 7:
            conds.append(Lead.username.ilike(f"%{digits[-7:]}%"))
    if email:
        conds.append(Lead.username.ilike(f"%{email}%"))
    if not conds:
        return []
    rows = session.execute(
        select(Lead).where(or_(*conds))
        .order_by(Lead.updated_at.desc())
        .limit(limit)
    ).scalars().all()
    return list(rows)


def _lead_button_label(lead: Lead) -> str:
    name = lead.name or lead.username or f"#{lead.id}"
    src = lead.source or ""
    label = f"🎯 #{lead.id} · {name}"
    if src:
        label += f" · {src}"
    return label[:48]

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

        # 1) клиент: переиспользуем существующего, иначе ищем по лиду, иначе создаём
        client = None
        if payment.client_id:
            client = session.get(Client, payment.client_id)
        if client is None:
            client = session.execute(
                select(Client).where(Client.lead_id == lead.id)
            ).scalars().first()
        if client is None:
            client = _create_client_from_payment(session, payment, lead=lead)

        # привяжем клиента к этому лиду
        if not client.lead_id:
            client.lead_id = lead.id
        if not client.first_payment_at:
            client.first_payment_at = payment.paid_at
        client.last_payment_at = payment.paid_at

        payment.client_id = client.id
        payment.lead_id = lead.id
        payment.payment_type = "first"

        # 2) этап: двигаем в PAID, если ещё не там, и пишем историю на момент платежа
        if lead.stage != PAID:
            lead.stage = PAID
            session.add(StageHistory(
                lead_id=lead.id,
                stage=PAID,
                changed_at=payment.paid_at or datetime.now(),
            ))

        session.commit()
        sync_lead(lead.id)
        client_name = client.name or client.phone or "клиент"

    await call.message.edit_text(
        f"✅ Платёж записан как <b>первичка</b> от лида #{lead_id}.\n"
        f"Лид «{client_name}» переведён в этап «Оплата консультации».",
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
    """Найти последний платёж-сироту, найти кандидатов-лидов, предложить привязку."""
    if message.from_user and message.from_user.id != OWNER_ID:
        return
    with get_session() as session:
        # Сирота = первичка без лида или unclassified без лида
        payment = session.execute(
            select(Payment)
            .where(Payment.payment_type.in_(["first", "unclassified"]))
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

        candidates = _match_lead_candidates(
            session,
            payment.customer_name,
            payment.customer_phone,
            payment.customer_email,
        )

    text_lines = [
        "🛠 <b>Платёж-сирота</b>",
        "",
        f"💰 {amount}",
        f"👤 {who}",
        f"🛍 {product}",
        f"🕒 {paid_at}",
    ]

    rows: list[list[InlineKeyboardButton]] = []
    if candidates:
        text_lines.append("")
        text_lines.append("🔍 <b>Возможные совпадения с лидами:</b>")
        for lead in candidates:
            text_lines.append(f"• #{lead.id} «{lead.name or lead.username}» — {lead.source}")
            rows.append([InlineKeyboardButton(
                text=_lead_button_label(lead),
                callback_data=f"pay:first_lead:{pid}:{lead.id}",
            )])
        text_lines.append("")
        text_lines.append("Если это новый клиент — нажми «Новый клиент».")
    else:
        text_lines.append("")
        text_lines.append("❓ Совпадений с лидами не найдено.")

    rows.append([InlineKeyboardButton(
        text="➕ Новый клиент (выбрать источник)",
        callback_data=f"pay:first_new:{pid}",
    )])
    rows.append([InlineKeyboardButton(
        text="🚫 Игнорировать платёж",
        callback_data=f"pay:ignore:{pid}",
    )])

    await message.answer(
        "\n".join(text_lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


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
