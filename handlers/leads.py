from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, or_, select

from db import get_session
from keyboards import (
    BTN_CLIENTS,
    BTN_IGNORING,
    BTN_LEADS,
    BTN_NEW,
    confirm_delete_kb,
    confirm_lost_kb,
    edit_field_kb,
    edit_source_kb,
    lead_card_kb,
    leads_list_kb,
    skip_kb,
    sources_kb,
)
from models import Client, Lead, Payment, StageHistory
from sheets import sync_all, sync_lead
from stages import (
    ACTIVE_CODES,
    BY_CODE,
    CLIENT_CODES,
    IGNORING,
    IGNORING_CODES,
    LEAD_NEW,
    LOST,
    SOURCE_TITLES,
    next_stage,
)

router = Router()
PAGE_SIZE = 10


class NewLead(StatesGroup):
    source = State()
    name = State()
    username = State()
    request = State()


class EditNote(StatesGroup):
    waiting = State()


class LostReason(StatesGroup):
    waiting = State()


class EditField(StatesGroup):
    waiting = State()


# Поля, которые можно редактировать через текстовый ввод
EDITABLE_TEXT_FIELDS = {
    "name":     "имя",
    "username": "логин",
    "request":  "запрос",
}


def _format_lead(lead: Lead) -> str:
    """
    Карточка лида в HTML. Все поля от пользователя (name, username, request,
    notes, lost_reason) экранируем через html.escape — иначе если в тексте
    есть '<', '>' или '&' (например, клиент написал что-то с тегом или
    эмодзи-замещалкой), Telegram отвечает «can't parse entities», edit_text
    падает, бот не успевает call.answer(), и в клиенте навсегда висит
    «Загрузка...».
    """
    from html import escape

    stage = BY_CODE.get(lead.stage)
    stage_title = stage.title if stage else lead.stage
    src = SOURCE_TITLES.get(lead.source, lead.source)
    parts = [
        f"<b>Лид #{lead.id}</b>",
        f"Этап: <b>{escape(stage_title)}</b>",
        f"Источник: {escape(src)}",
        f"Имя: {escape(lead.name) if lead.name else '—'}",
        f"Логин: {escape(lead.username) if lead.username else '—'}",
    ]
    if lead.request:
        parts.append(f"Запрос: {escape(lead.request)}")
    if lead.notes:
        parts.append(f"Заметка: {escape(lead.notes)}")
    if lead.stage == LOST and lead.lost_reason:
        parts.append(f"Причина отвала: {escape(lead.lost_reason)}")
    parts.append(f"Создан: {lead.created_at:%d.%m.%Y %H:%M}")
    return "\n".join(parts)


# ───────── /new wizard ─────────

@router.message(Command("new"))
@router.message(F.text == BTN_NEW)
async def cmd_new(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(NewLead.source)
    await message.answer("Откуда пришла заявка?", reply_markup=sources_kb())


@router.callback_query(F.data == "new:cancel")
async def new_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text("Создание лида отменено.")
    await call.answer()


@router.callback_query(NewLead.source, F.data.startswith("src:"))
async def new_source(call: CallbackQuery, state: FSMContext) -> None:
    code = call.data.split(":", 1)[1]
    if code not in SOURCE_TITLES:
        await call.answer("Неизвестный источник", show_alert=True)
        return
    await state.update_data(source=code)
    await state.set_state(NewLead.name)
    await call.message.edit_text(
        f"Источник: <b>{SOURCE_TITLES[code]}</b>\n\nИмя клиента?",
        reply_markup=skip_kb("name"),
    )
    await call.answer()


@router.callback_query(NewLead.name, F.data == "skip:name")
async def new_skip_name(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(name=None)
    await state.set_state(NewLead.username)
    await call.message.edit_text("Telegram-логин клиента (например, @ivan)?", reply_markup=skip_kb("username"))
    await call.answer()


@router.message(NewLead.name)
async def new_name(message: Message, state: FSMContext) -> None:
    await state.update_data(name=message.text.strip())
    await state.set_state(NewLead.username)
    await message.answer("Telegram-логин клиента (например, @ivan)?", reply_markup=skip_kb("username"))


@router.callback_query(NewLead.username, F.data == "skip:username")
async def new_skip_username(call: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(username=None)
    await state.set_state(NewLead.request)
    await call.message.edit_text("Кратко запрос/боль клиента?", reply_markup=skip_kb("request"))
    await call.answer()


@router.message(NewLead.username)
async def new_username(message: Message, state: FSMContext) -> None:
    await state.update_data(username=message.text.strip())
    await state.set_state(NewLead.request)
    await message.answer("Кратко запрос/боль клиента?", reply_markup=skip_kb("request"))


@router.callback_query(NewLead.request, F.data == "skip:request")
async def new_skip_request(call: CallbackQuery, state: FSMContext) -> None:
    lead = await _save_lead(state, request=None)
    await state.clear()
    await call.message.edit_text(_format_lead(lead), reply_markup=lead_card_kb(lead.id, lead.stage))
    await call.answer("Лид создан")


@router.message(NewLead.request)
async def new_request(message: Message, state: FSMContext) -> None:
    lead = await _save_lead(state, request=message.text.strip())
    await state.clear()
    await message.answer(_format_lead(lead), reply_markup=lead_card_kb(lead.id, lead.stage))


async def _save_lead(state: FSMContext, request: str | None) -> Lead:
    data = await state.get_data()
    with get_session() as session:
        lead = Lead(
            name=data.get("name"),
            username=data.get("username"),
            source=data["source"],
            request=request,
            stage=LEAD_NEW,
        )
        session.add(lead)
        session.flush()
        session.add(StageHistory(lead_id=lead.id, stage=LEAD_NEW))
        session.commit()
        session.refresh(lead)
    sync_lead(lead.id)
    return lead


# ───────── /leads list ─────────

@router.message(Command("leads"))
@router.message(F.text == BTN_LEADS)
async def cmd_leads(message: Message) -> None:
    await _show_leads_page(message_or_call=message, page=1)


@router.callback_query(F.data.startswith("leads:"))
async def cb_leads(call: CallbackQuery) -> None:
    page = int(call.data.split(":", 1)[1])
    await _show_leads_page(message_or_call=call, page=page)


async def _show_leads_page(message_or_call: Message | CallbackQuery, page: int) -> None:
    offset = (page - 1) * PAGE_SIZE
    with get_session() as session:
        rows = session.execute(
            select(Lead)
            .where(Lead.stage.in_(ACTIVE_CODES))
            .order_by(Lead.updated_at.desc())
            .offset(offset)
            .limit(PAGE_SIZE + 1)
        ).scalars().all()

    has_next = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]

    if not rows and page == 1:
        text = "Активных лидов пока нет.\nСоздайте первого через /new."
        if isinstance(message_or_call, CallbackQuery):
            await message_or_call.message.edit_text(text)
            await message_or_call.answer()
        else:
            await message_or_call.answer(text)
        return

    items = [
        (
            lead.id,
            f"#{lead.id} · {SOURCE_TITLES.get(lead.source, lead.source)} · "
            f"{lead.name or lead.username or '—'} · {BY_CODE[lead.stage].short}",
        )
        for lead in rows
    ]
    text = f"Активные лиды (стр. {page}):"
    kb = leads_list_kb(items, page, has_next)
    if isinstance(message_or_call, CallbackQuery):
        await message_or_call.message.edit_text(text, reply_markup=kb)
        await message_or_call.answer()
    else:
        await message_or_call.answer(text, reply_markup=kb)


# ───────── /clients (оплатившие лиды + выручка по каждому) ─────────

def _money_short(amount: float) -> str:
    if not amount:
        return "—"
    return f"{int(round(amount)):,} ₽".replace(",", " ")


@router.message(Command("clients"))
@router.message(F.text == BTN_CLIENTS)
async def cmd_clients(message: Message) -> None:
    """Список оплативших лидов с суммой выручки от каждого."""
    with get_session() as session:
        # Берём лидов в стадиях клиентов + считаем выручку через Payment
        # (через лид или через привязанного к лиду клиента)
        leads = session.execute(
            select(Lead)
            .where(Lead.stage.in_(CLIENT_CODES))
            .order_by(Lead.updated_at.desc())
            .limit(PAGE_SIZE * 5)
        ).scalars().all()

        if not leads:
            await message.answer("Клиентов пока нет.\nКогда лид оплатит — появится здесь.")
            return

        # Собираем выручку: все Payment где payment_type IN ('first', 'repeat'),
        # привязанные к лиду напрямую или к клиенту, чей lead_id == lead.id
        lead_ids = [l.id for l in leads]
        client_ids_by_lead: dict[int, list[int]] = {lid: [] for lid in lead_ids}
        client_rows = session.execute(
            select(Client.id, Client.lead_id).where(Client.lead_id.in_(lead_ids))
        ).all()
        for cid, lid in client_rows:
            if lid in client_ids_by_lead:
                client_ids_by_lead[lid].append(cid)

        revenue_by_lead: dict[int, float] = {lid: 0.0 for lid in lead_ids}
        # 1) Платежи, привязанные к лиду напрямую (Payment.lead_id IS NOT NULL)
        direct = session.execute(
            select(Payment.lead_id, func.coalesce(func.sum(Payment.amount), 0))
            .where(Payment.lead_id.in_(lead_ids))
            .where(Payment.payment_type.in_(["first", "repeat"]))
            .group_by(Payment.lead_id)
        ).all()
        for lid, total in direct:
            revenue_by_lead[lid] = revenue_by_lead.get(lid, 0) + float(total or 0)
        # 2) Платежи через клиента — но ТОЛЬКО без прямой привязки к лиду,
        # иначе платёж посчитается дважды (через lead_id и через client→lead_id).
        all_client_ids = [c for cids in client_ids_by_lead.values() for c in cids]
        if all_client_ids:
            via_client = session.execute(
                select(Payment.client_id, func.coalesce(func.sum(Payment.amount), 0))
                .where(Payment.client_id.in_(all_client_ids))
                .where(Payment.lead_id.is_(None))  # ← ключевая защита от дубля
                .where(Payment.payment_type.in_(["first", "repeat"]))
                .group_by(Payment.client_id)
            ).all()
            client_to_total = {cid: float(t or 0) for cid, t in via_client}
            for lid, cids in client_ids_by_lead.items():
                for cid in cids:
                    revenue_by_lead[lid] = revenue_by_lead.get(lid, 0) + client_to_total.get(cid, 0)

    total_revenue = sum(revenue_by_lead.values())
    items = []
    for lead in leads:
        rev = revenue_by_lead.get(lead.id, 0)
        src = SOURCE_TITLES.get(lead.source, lead.source)
        name = lead.name or lead.username or "—"
        stage = BY_CODE[lead.stage].short
        # Telegram-кнопка ограничена ~64 символами текста
        label = f"#{lead.id} · {src} · {name} · {stage} · {_money_short(rev)}"
        items.append((lead.id, label[:64]))

    text = (
        f"💚 <b>Клиенты:</b> {len(leads)}\n"
        f"Выручка по списку: <b>{_money_short(total_revenue)}</b>"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=leads_list_kb(items, 1, False))


# ───────── /lead <id> and search ─────────

@router.message(Command("lead"))
async def cmd_lead(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    if not arg:
        await message.answer("Используйте: /lead <id> или /find <имя_или_логин>")
        return
    if not arg.isdigit():
        await message.answer("ID должен быть числом. Для поиска по имени используйте /find")
        return
    await _send_lead_card(message, int(arg))


@router.message(Command("find"))
async def cmd_find(message: Message, command: CommandObject) -> None:
    q = (command.args or "").strip()
    if not q:
        await message.answer("Используйте: /find <часть имени или логина>")
        return
    pattern = f"%{q}%"
    with get_session() as session:
        rows = session.execute(
            select(Lead)
            .where(or_(Lead.name.ilike(pattern), Lead.username.ilike(pattern)))
            .order_by(Lead.updated_at.desc())
            .limit(PAGE_SIZE)
        ).scalars().all()
    if not rows:
        await message.answer("Ничего не найдено.")
        return
    items = [
        (lead.id, f"#{lead.id} · {lead.name or lead.username or '—'} · {BY_CODE[lead.stage].short}")
        for lead in rows
    ]
    await message.answer(f"Найдено {len(rows)}:", reply_markup=leads_list_kb(items, 1, False))


@router.callback_query(F.data.startswith("open:"))
async def cb_open_lead(call: CallbackQuery) -> None:
    # Сразу гасим «Загрузка...» в клиенте — если ниже что-то упадёт
    # (несмотря на try/except), пользователь не зависнет в спиннере.
    await call.answer()
    lead_id = int(call.data.split(":", 1)[1])
    with get_session() as session:
        lead = session.get(Lead, lead_id)
    if lead is None:
        await call.message.answer(f"Лид #{lead_id} не найден.")
        return
    try:
        await call.message.edit_text(
            _format_lead(lead),
            reply_markup=lead_card_kb(lead.id, lead.stage),
        )
    except Exception as e:
        # «message is not modified», «can't parse entities» и т.п. — не фатал.
        # Шлём карточку отдельным сообщением, чтоб пользователь её всё-таки
        # увидел, и логируем причину.
        import logging
        logging.getLogger("leads").warning("cb_open_lead edit_text failed: %s", e)
        await call.message.answer(
            _format_lead(lead),
            reply_markup=lead_card_kb(lead.id, lead.stage),
        )


async def _send_lead_card(message: Message, lead_id: int) -> None:
    with get_session() as session:
        lead = session.get(Lead, lead_id)
    if lead is None:
        await message.answer(f"Лид #{lead_id} не найден.")
        return
    await message.answer(_format_lead(lead), reply_markup=lead_card_kb(lead.id, lead.stage))


# ───────── advance / lost / note ─────────

@router.callback_query(F.data.startswith("adv:"))
async def cb_advance(call: CallbackQuery) -> None:
    lead_id = int(call.data.split(":", 1)[1])
    with get_session() as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            await call.answer("Лид не найден", show_alert=True)
            return
        nxt = next_stage(lead.stage)
        if nxt is None:
            await call.answer("Этап уже финальный", show_alert=True)
            return
        lead.stage = nxt.code
        session.add(StageHistory(lead_id=lead.id, stage=nxt.code))
        session.commit()
        session.refresh(lead)
    sync_lead(lead.id)
    await call.message.edit_text(_format_lead(lead), reply_markup=lead_card_kb(lead.id, lead.stage))
    await call.answer(f"→ {nxt.short}")


@router.callback_query(F.data.startswith("lost:"))
async def cb_lost_ask(call: CallbackQuery, state: FSMContext) -> None:
    lead_id = int(call.data.split(":", 1)[1])
    await state.set_state(LostReason.waiting)
    await state.update_data(lead_id=lead_id)
    await call.message.edit_text(
        f"Причина отвала лида #{lead_id}? Напишите текст или нажмите «Без причины».",
        reply_markup=confirm_lost_kb(lead_id),
    )
    await call.answer()


@router.callback_query(LostReason.waiting, F.data.startswith("lost_no:"))
async def cb_lost_no_reason(call: CallbackQuery, state: FSMContext) -> None:
    lead_id = int(call.data.split(":", 1)[1])
    await _mark_lost(lead_id, reason=None)
    await state.clear()
    with get_session() as session:
        lead = session.get(Lead, lead_id)
    await call.message.edit_text(_format_lead(lead), reply_markup=lead_card_kb(lead.id, lead.stage))
    await call.answer("Помечен как отвал")


@router.message(LostReason.waiting)
async def msg_lost_reason(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    lead_id = data.get("lead_id")
    if lead_id is None:
        await state.clear()
        return
    await _mark_lost(lead_id, reason=message.text.strip())
    await state.clear()
    with get_session() as session:
        lead = session.get(Lead, lead_id)
    await message.answer(_format_lead(lead), reply_markup=lead_card_kb(lead.id, lead.stage))


async def _mark_lost(lead_id: int, reason: str | None) -> None:
    with get_session() as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            return
        lead.stage = LOST
        lead.lost_reason = reason
        session.add(StageHistory(lead_id=lead.id, stage=LOST))
        session.commit()
    sync_lead(lead_id)


@router.callback_query(F.data.startswith("note:"))
async def cb_note_ask(call: CallbackQuery, state: FSMContext) -> None:
    lead_id = int(call.data.split(":", 1)[1])
    await state.set_state(EditNote.waiting)
    await state.update_data(lead_id=lead_id)
    await call.message.edit_text(f"Пришлите новую заметку для лида #{lead_id} (или /cancel).")
    await call.answer()


@router.message(EditNote.waiting, Command("cancel"))
async def msg_note_cancel(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    if "lead_id" in data:
        await _send_lead_card(message, data["lead_id"])


@router.message(EditNote.waiting)
async def msg_note_save(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    lead_id = data.get("lead_id")
    if lead_id is None:
        await state.clear()
        return
    with get_session() as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            await state.clear()
            await message.answer("Лид не найден.")
            return
        lead.notes = message.text.strip()
        session.commit()
        session.refresh(lead)
    sync_lead(lead.id)
    await state.clear()
    await message.answer(_format_lead(lead), reply_markup=lead_card_kb(lead.id, lead.stage))


# ───────── ignoring ─────────

@router.message(Command("ignoring"))
@router.message(F.text == BTN_IGNORING)
async def cmd_ignoring(message: Message) -> None:
    with get_session() as session:
        rows = session.execute(
            select(Lead)
            .where(Lead.stage.in_(IGNORING_CODES))
            .order_by(Lead.updated_at.desc())
            .limit(PAGE_SIZE * 5)  # игноров обычно немного, можно показать побольше
        ).scalars().all()
    if not rows:
        await message.answer("Игнорящих лидов нет — все на связи 🙌")
        return
    items = [
        (lead.id, f"#{lead.id} · {SOURCE_TITLES.get(lead.source, lead.source)} · "
                  f"{lead.name or lead.username or '—'}")
        for lead in rows
    ]
    text = f"🤐 <b>Игнорят:</b> {len(rows)}\n<i>Зайди в карточку → «Снять игнор», когда выйдут на связь.</i>"
    await message.answer(text, reply_markup=leads_list_kb(items, 1, False))


@router.callback_query(F.data.startswith("ignore:"))
async def cb_ignore(call: CallbackQuery) -> None:
    lead_id = int(call.data.split(":", 1)[1])
    with get_session() as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            await call.answer("Лид не найден", show_alert=True)
            return
        if lead.stage == IGNORING:
            await call.answer("Уже в «игнорит»", show_alert=True)
            return
        lead.stage = IGNORING
        session.add(StageHistory(lead_id=lead.id, stage=IGNORING))
        session.commit()
        session.refresh(lead)
    sync_lead(lead.id)
    await call.message.edit_text(_format_lead(lead), reply_markup=lead_card_kb(lead.id, lead.stage))
    await call.answer("Помечен как «игнорит»")


@router.callback_query(F.data.startswith("unignore:"))
async def cb_unignore(call: CallbackQuery) -> None:
    lead_id = int(call.data.split(":", 1)[1])
    with get_session() as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            await call.answer("Лид не найден", show_alert=True)
            return
        if lead.stage != IGNORING:
            await call.answer("Лид не в «игнорит»", show_alert=True)
            return
        # Возвращаем на последний этап до перехода в IGNORING
        prev = session.execute(
            select(StageHistory)
            .where(StageHistory.lead_id == lead_id)
            .where(StageHistory.stage != IGNORING)
            .order_by(StageHistory.changed_at.desc())
            .limit(1)
        ).scalars().first()
        target_stage = prev.stage if prev else LEAD_NEW
        lead.stage = target_stage
        session.add(StageHistory(lead_id=lead.id, stage=target_stage))
        session.commit()
        session.refresh(lead)
    sync_lead(lead.id)
    target_title = BY_CODE[target_stage].title if target_stage in BY_CODE else target_stage
    await call.message.edit_text(_format_lead(lead), reply_markup=lead_card_kb(lead.id, lead.stage))
    await call.answer(f"Возвращён → {target_title}")


# ───────── edit lead fields ─────────

@router.callback_query(F.data.startswith("edit:"))
async def cb_edit(call: CallbackQuery) -> None:
    lead_id = int(call.data.split(":", 1)[1])
    with get_session() as session:
        lead = session.get(Lead, lead_id)
    if lead is None:
        await call.answer("Лид не найден", show_alert=True)
        return
    await call.message.edit_text(
        f"✏️ Редактирование лида #{lead_id}\nЧто меняем?",
        reply_markup=edit_field_kb(lead_id),
    )
    await call.answer()


@router.callback_query(F.data.startswith("editf:"))
async def cb_edit_field(call: CallbackQuery, state: FSMContext) -> None:
    _, lead_id_s, field = call.data.split(":", 2)
    lead_id = int(lead_id_s)
    if field == "source":
        await call.message.edit_text(
            f"Новый источник для лида #{lead_id}?",
            reply_markup=edit_source_kb(lead_id),
        )
        await call.answer()
        return
    if field not in EDITABLE_TEXT_FIELDS:
        await call.answer("Неизвестное поле", show_alert=True)
        return
    label = EDITABLE_TEXT_FIELDS[field]
    await state.set_state(EditField.waiting)
    await state.update_data(lead_id=lead_id, field=field)
    await call.message.edit_text(
        f"Пришлите новое значение для поля «{label}» (или /cancel для отмены).\n"
        f"<i>Чтобы очистить поле — пришлите «-».</i>"
    )
    await call.answer()


@router.callback_query(F.data.startswith("editsrc:"))
async def cb_edit_source_save(call: CallbackQuery) -> None:
    _, lead_id_s, code = call.data.split(":", 2)
    lead_id = int(lead_id_s)
    if code not in SOURCE_TITLES:
        await call.answer("Неизвестный источник", show_alert=True)
        return
    with get_session() as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            await call.answer("Лид не найден", show_alert=True)
            return
        lead.source = code
        session.commit()
        session.refresh(lead)
    sync_lead(lead.id)
    await call.message.edit_text(_format_lead(lead), reply_markup=lead_card_kb(lead.id, lead.stage))
    await call.answer(f"Источник → {SOURCE_TITLES[code]}")


@router.message(EditField.waiting, Command("cancel"))
async def msg_edit_cancel(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    if "lead_id" in data:
        await _send_lead_card(message, data["lead_id"])


@router.message(EditField.waiting)
async def msg_edit_save(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    lead_id = data.get("lead_id")
    field = data.get("field")
    if lead_id is None or field not in EDITABLE_TEXT_FIELDS:
        await state.clear()
        return
    raw = message.text.strip()
    new_value: str | None = None if raw == "-" else raw
    with get_session() as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            await state.clear()
            await message.answer("Лид не найден.")
            return
        setattr(lead, field, new_value)
        session.commit()
        session.refresh(lead)
    sync_lead(lead.id)
    await state.clear()
    await message.answer(_format_lead(lead), reply_markup=lead_card_kb(lead.id, lead.stage))


# ───────── delete lead ─────────

@router.callback_query(F.data.startswith("del:"))
async def cb_delete_ask(call: CallbackQuery) -> None:
    lead_id = int(call.data.split(":", 1)[1])
    with get_session() as session:
        lead = session.get(Lead, lead_id)
    if lead is None:
        await call.answer("Лид уже удалён", show_alert=True)
        return
    title = lead.name or lead.username or f"#{lead.id}"
    await call.message.edit_text(
        f"⚠️ Удалить лид <b>{title}</b> навсегда?\n\n"
        f"Все данные и история этапов будут стёрты. Это действие необратимо.",
        reply_markup=confirm_delete_kb(lead_id),
    )
    await call.answer()


@router.callback_query(F.data.startswith("del_yes:"))
async def cb_delete_confirm(call: CallbackQuery) -> None:
    lead_id = int(call.data.split(":", 1)[1])
    with get_session() as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            await call.answer("Лид уже удалён", show_alert=True)
            return
        title = lead.name or lead.username or f"#{lead.id}"
        session.delete(lead)
        session.commit()
    await call.message.edit_text(f"🗑 Лид <b>{title}</b> удалён.")
    await call.answer("Удалено")
