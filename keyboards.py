from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from stages import BY_CODE, FUNNEL, IGNORING, LOST, SOURCES, next_stage


# Текст кнопок главного меню — используются и для отрисовки, и для роутинга.
BTN_NEW       = "➕ Новый лид"
BTN_LEADS     = "📋 Активные"
BTN_IGNORING  = "🤐 Игнорят"
BTN_TODAY     = "📊 Сегодня"
BTN_WEEK      = "📅 Неделя"
BTN_MONTH     = "📆 Месяц"
BTN_CHANNELS  = "🚦 Каналы"
BTN_FUNNEL    = "🎯 Воронка"
BTN_DASHBOARD = "🌐 Дашборд"


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_NEW), KeyboardButton(text=BTN_LEADS), KeyboardButton(text=BTN_IGNORING)],
            [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_WEEK), KeyboardButton(text=BTN_MONTH)],
            [KeyboardButton(text=BTN_CHANNELS), KeyboardButton(text=BTN_FUNNEL)],
            [KeyboardButton(text=BTN_DASHBOARD)],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def sources_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=title, callback_data=f"src:{code}")]
        for code, title in SOURCES
    ]
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="new:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def skip_kb(field: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="Пропустить", callback_data=f"skip:{field}"),
            InlineKeyboardButton(text="Отмена", callback_data="new:cancel"),
        ]]
    )


def lead_card_kb(lead_id: int, stage: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if stage == IGNORING:
        rows.append([InlineKeyboardButton(
            text="▶️ Снять «игнорит»",
            callback_data=f"unignore:{lead_id}",
        )])
    else:
        nxt = next_stage(stage)
        if nxt:
            rows.append([InlineKeyboardButton(
                text=f"→ {nxt.title}",
                callback_data=f"adv:{lead_id}",
            )])
    rows.append([
        InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"edit:{lead_id}"),
        InlineKeyboardButton(text="📝 Заметка",      callback_data=f"note:{lead_id}"),
    ])
    side: list[InlineKeyboardButton] = []
    if stage not in (LOST, IGNORING):
        side.append(InlineKeyboardButton(text="🤐 Игнорит",      callback_data=f"ignore:{lead_id}"))
    if stage != LOST:
        side.append(InlineKeyboardButton(text="❌ Отвалился",    callback_data=f"lost:{lead_id}"))
    if side:
        rows.append(side)
    rows.append([
        InlineKeyboardButton(text="📋 К списку", callback_data="leads:1"),
        InlineKeyboardButton(text="🗑 Удалить",  callback_data=f"del:{lead_id}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def edit_field_kb(lead_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Имя",     callback_data=f"editf:{lead_id}:name")],
            [InlineKeyboardButton(text="Логин",   callback_data=f"editf:{lead_id}:username")],
            [InlineKeyboardButton(text="Источник",callback_data=f"editf:{lead_id}:source")],
            [InlineKeyboardButton(text="Запрос",  callback_data=f"editf:{lead_id}:request")],
            [InlineKeyboardButton(text="Отмена",  callback_data=f"open:{lead_id}")],
        ]
    )


def edit_source_kb(lead_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=title, callback_data=f"editsrc:{lead_id}:{code}")]
        for code, title in SOURCES
    ]
    rows.append([InlineKeyboardButton(text="Отмена", callback_data=f"open:{lead_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_delete_kb(lead_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да, удалить навсегда", callback_data=f"del_yes:{lead_id}"),
            InlineKeyboardButton(text="Отмена", callback_data=f"open:{lead_id}"),
        ]]
    )


def leads_list_kb(items: list[tuple[int, str]], page: int, has_next: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"open:{lead_id}")]
        for lead_id, label in items
    ]
    nav: list[InlineKeyboardButton] = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"leads:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"leads:{page + 1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_lost_kb(lead_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="Без причины", callback_data=f"lost_no:{lead_id}"),
            InlineKeyboardButton(text="Отмена", callback_data=f"open:{lead_id}"),
        ]]
    )
