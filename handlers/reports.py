from __future__ import annotations

from datetime import datetime, time, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import func, select

import os
import re

from db import get_session
from keyboards import (
    BTN_CHANNELS,
    BTN_DASHBOARD,
    BTN_FUNNEL,
    BTN_MONTH,
    BTN_TODAY,
    BTN_WEEK,
    main_menu_kb,
)
from models import Client, Lead, Payment, StageHistory
from sheets import is_enabled, sync_all
from stages import (
    AGREED,
    BREAKDOWN_SENT,
    BY_CODE,
    CONSULTED,
    FUNNEL,
    IGNORING,
    LEAD_NEW,
    LOST,
    PACKAGE_BOUGHT,
    PAID,
    QUALIFIED,
    codes_at_or_after,
    SOURCE_TITLES,
    SOURCES,
)

router = Router()


# ───────── period helpers ─────────

def _today_range() -> tuple[datetime, datetime]:
    start = datetime.combine(datetime.now().date(), time.min)
    return start, start + timedelta(days=1)


def _week_range() -> tuple[datetime, datetime]:
    today = datetime.now().date()
    monday = today - timedelta(days=today.weekday())
    start = datetime.combine(monday, time.min)
    return start, start + timedelta(days=7)


def _month_range() -> tuple[datetime, datetime]:
    today = datetime.now().date()
    start = datetime.combine(today.replace(day=1), time.min)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def _period_label(start: datetime, end: datetime) -> str:
    last = end - timedelta(seconds=1)
    if start.date() == last.date():
        return f"{start:%d.%m.%Y}"
    return f"{start:%d.%m} – {last:%d.%m.%Y}"


# ───────── core counters ─────────

# AGREED убран — синхронизировано с дашбордом по запросу пользователя
# («согласие = это и есть оплата, не нужно отдельно»).
STAGES_FOR_REPORT = [
    (LEAD_NEW,        "Заявок"),
    (QUALIFIED,       "Квал"),
    (BREAKDOWN_SENT,  "Разборов отправлено"),
    (PAID,            "Оплат"),
    (CONSULTED,       "Консультаций проведено"),
    (PACKAGE_BOUGHT,  "Куплено пакетов"),
]


def _counts_in_period(start: datetime, end: datetime, source: str | None = None) -> dict[str, int]:
    """
    Cumulative counts: лиды на этапе X ИЛИ позже в [start, end).
    Синхронизировано с web._counts_in_period (см. PR #28+).
    """
    result: dict[str, int] = {}
    with get_session() as session:
        for code, _ in STAGES_FOR_REPORT:
            later_codes = codes_at_or_after(code)
            q = (
                select(func.count(func.distinct(StageHistory.lead_id)))
                .where(StageHistory.stage.in_(later_codes))
                .where(StageHistory.changed_at >= start)
                .where(StageHistory.changed_at < end)
            )
            if source is not None:
                q = q.join(Lead, Lead.id == StageHistory.lead_id).where(Lead.source == source)
            result[code] = session.execute(q).scalar_one()
    return result


def _payments_count(start: datetime, end: datetime) -> int:
    """
    Реальное число платежей (first + repeat) в периоде.
    Синхронизировано с card «Оплат за месяц» на дашборде «Все»: повторки
    от существующих клиентов сюда тоже попадают, в отличие от lead-funnel.
    """
    with get_session() as session:
        return int(session.execute(
            select(func.count(Payment.id))
            .where(Payment.paid_at >= start)
            .where(Payment.paid_at < end)
            .where(Payment.payment_type.in_(["first", "repeat"]))
        ).scalar_one() or 0)


def _repeat_sessions_count(start: datetime, end: datetime) -> int:
    """
    Сколько RepeatSession создано в [start, end). Каждая повторная оплата
    от существующего клиента создаёт сессию → 1 RepeatSession = 1 повторная
    заявка. Синхронизировано с web._repeat_sessions_count.
    """
    from models import RepeatSession  # отдельный import чтобы не ломать порядок
    with get_session() as session:
        return int(session.execute(
            select(func.count(RepeatSession.id))
            .where(RepeatSession.created_at >= start)
            .where(RepeatSession.created_at < end)
        ).scalar_one() or 0)


def _pct(numer: int, denom: int) -> str:
    if denom == 0:
        return "—"
    return f"{numer * 100 / denom:.0f}%"


def _format_period_report(title: str, start: datetime, end: datetime, counts: dict[str, int]) -> str:
    """
    Отчёт за период. «Заявок» = первичные лиды + повторные RepeatSessions
    (синхронизировано с дашбордом, вкладка «Все»). «Оплат» = реальное число
    Payment-записей (первичка + повторка). Конверсии считаются от total leads.
    """
    first_leads = counts.get(LEAD_NEW, 0)
    repeat_sessions = _repeat_sessions_count(start, end)
    total_leads = first_leads + repeat_sessions
    payments = _payments_count(start, end)
    counts_for_display = dict(counts)
    counts_for_display[PAID] = payments

    lines = [f"📊 <b>{title}</b>"]
    for code, label in STAGES_FOR_REPORT:
        n = counts_for_display.get(code, 0)
        if code == LEAD_NEW:
            if repeat_sessions:
                lines.append(
                    f"{label}: <b>{total_leads}</b> "
                    f"({first_leads} первичка + {repeat_sessions} повторка)"
                )
            else:
                lines.append(f"{label}: <b>{total_leads}</b>")
        else:
            lines.append(f"{label}: <b>{n}</b> ({_pct(n, total_leads)} от заявок)")
    return "\n".join(lines)


def _format_channels_block(start: datetime, end: datetime) -> str:
    lines = ["", "<b>По каналам:</b>"]
    for code, title in SOURCES:
        c = _counts_in_period(start, end, source=code)
        leads = c.get(LEAD_NEW, 0)
        paid = c.get(PAID, 0)
        lines.append(f"• {title} — {leads} заявок → {paid} оплат ({_pct(paid, leads)})")
    return "\n".join(lines)


# ───────── command handlers ─────────

async def _send_period(message: Message, label: str, start: datetime, end: datetime) -> None:
    counts = _counts_in_period(start, end)
    text = _format_period_report(f"{label} ({_period_label(start, end)})", start, end, counts)
    text += "\n" + _format_channels_block(start, end)
    await message.answer(text)


@router.message(Command("today"))
@router.message(F.text == BTN_TODAY)
async def cmd_today(message: Message) -> None:
    start, end = _today_range()
    await _send_period(message, "Сегодня", start, end)


@router.message(Command("week"))
@router.message(F.text == BTN_WEEK)
async def cmd_week(message: Message) -> None:
    start, end = _week_range()
    await _send_period(message, "Неделя", start, end)


@router.message(Command("month"))
@router.message(F.text == BTN_MONTH)
async def cmd_month(message: Message) -> None:
    start, end = _month_range()
    await _send_period(message, "Месяц", start, end)


@router.message(Command("channels"))
@router.message(F.text == BTN_CHANNELS)
async def cmd_channels(message: Message) -> None:
    start, end = _month_range()
    lines = [f"📊 <b>Каналы за {_period_label(start, end)}</b>", ""]
    for code, title in SOURCES:
        c = _counts_in_period(start, end, source=code)
        leads = c.get(LEAD_NEW, 0)
        qual = c.get(QUALIFIED, 0)
        paid = c.get(PAID, 0)
        package = c.get(PACKAGE_BOUGHT, 0)
        lines.append(f"<b>{title}</b>")
        lines.append(f"  Заявок: {leads}")
        lines.append(f"  Квал: {qual} ({_pct(qual, leads)})")
        lines.append(f"  Оплат: {paid} ({_pct(paid, leads)})")
        lines.append(f"  Пакетов: {package} ({_pct(package, leads)})")
        lines.append("")
    await message.answer("\n".join(lines).rstrip())


@router.message(Command("funnel"))
@router.message(F.text == BTN_FUNNEL)
async def cmd_funnel(message: Message) -> None:
    """Текущий снимок: сколько лидов СЕЙЧАС находятся на каждом этапе."""
    with get_session() as session:
        rows = session.execute(
            select(Lead.stage, func.count(Lead.id)).group_by(Lead.stage)
        ).all()
    by_stage = {stage: cnt for stage, cnt in rows}
    lines = ["📊 <b>Текущая воронка</b>", ""]
    total_active = 0
    for s in FUNNEL:
        n = by_stage.get(s.code, 0)
        lines.append(f"{s.title}: <b>{n}</b>")
        total_active += n
    lost = by_stage.get(LOST, 0)
    ignoring = by_stage.get(IGNORING, 0)
    lines.append("")
    lines.append(f"Всего в воронке: <b>{total_active}</b>")
    lines.append(f"Игнорят: <b>{ignoring}</b>")
    lines.append(f"Отвалилось: {lost}")
    await message.answer("\n".join(lines))


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Это бот учёта лидов.\n\n"
        "Снизу — кнопки быстрых действий. Слева от поля ввода — кнопка «/», "
        "там полный список команд.\n\n"
        "Найти лида: /find <i>часть имени или логина</i>\n"
        "Открыть конкретный лид: /lead <i>номер</i>",
        reply_markup=main_menu_kb(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await cmd_start(message)


@router.message(Command("sync"))
async def cmd_sync(message: Message) -> None:
    if not is_enabled():
        await message.answer(
            "Google Sheets не подключены. Добавьте SHEETS_WEBHOOK_URL в .env и перезапустите бота."
        )
        return
    await message.answer("Синхронизирую все лиды в Google Sheets…")
    n = sync_all()
    await message.answer(f"Готово. Отправлено лидов: <b>{n}</b>.")


def _money(amount: float, currency: str = "RUB") -> str:
    sym = {"RUB": "₽", "USD": "$", "EUR": "€"}.get(currency, currency)
    return f"{amount:,.0f}".replace(",", " ") + " " + sym


RU_MONTHS = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]


@router.message(Command("months"))
async def cmd_months(message: Message) -> None:
    """Сводка по последним 12 месяцам — для динамики."""
    today = datetime.now().date()
    rows = []
    for i in range(12):
        y, m = today.year, today.month - i
        while m <= 0:
            m += 12
            y -= 1
        start = datetime(y, m, 1)
        end = datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1)
        counts = _counts_in_period(start, end)
        with get_session() as session:
            total_rev = session.execute(
                select(func.coalesce(func.sum(Payment.amount), 0))
                .where(Payment.paid_at >= start)
                .where(Payment.paid_at < end)
                .where(Payment.payment_type.in_(["first", "repeat"]))
            ).scalar_one() or 0
            rev_first = session.execute(
                select(func.coalesce(func.sum(Payment.amount), 0))
                .where(Payment.paid_at >= start)
                .where(Payment.paid_at < end)
                .where(Payment.payment_type == "first")
            ).scalar_one() or 0
        leads = counts.get(LEAD_NEW, 0)
        paid_total = counts.get(PAID, 0) + counts.get(CONSULTED, 0) + counts.get(PACKAGE_BOUGHT, 0)
        if leads or total_rev:
            rows.append({
                "label": f"{RU_MONTHS[m - 1]} {y}",
                "leads": leads,
                "paid": paid_total,
                "conv": (paid_total / leads * 100) if leads else 0,
                "rev_total": float(total_rev),
                "rev_first": float(rev_first),
                "rev_repeat": float(total_rev - rev_first),
            })

    if not rows:
        await message.answer("Пока нет ни одного месяца с активностью.")
        return

    lines = ["📅 <b>Динамика по месяцам</b>", ""]
    for r in rows:
        lines.append(f"<b>{r['label']}</b>")
        lines.append(f"  Заявок: {r['leads']}, оплат: {r['paid']} ({r['conv']:.0f}%)")
        if r["rev_total"]:
            lines.append(
                f"  💰 {_money(r['rev_total'])} "
                f"(первичка {_money(r['rev_first'])} · повторка {_money(r['rev_repeat'])})"
            )
        lines.append("")
    await message.answer("\n".join(lines).rstrip())


@router.message(Command("revenue"))
async def cmd_revenue(message: Message) -> None:
    start, end = _month_range()
    with get_session() as session:
        rows = session.execute(
            select(Payment.payment_type, func.count(Payment.id), func.sum(Payment.amount))
            .where(Payment.paid_at >= start)
            .where(Payment.paid_at < end)
            .where(Payment.payment_type.in_(["first", "repeat"]))
            .group_by(Payment.payment_type)
        ).all()
    by_type = {t: (cnt, total or 0) for t, cnt, total in rows}
    first_cnt, first_sum = by_type.get("first", (0, 0))
    repeat_cnt, repeat_sum = by_type.get("repeat", (0, 0))
    total_cnt, total_sum = first_cnt + repeat_cnt, first_sum + repeat_sum
    avg = total_sum / total_cnt if total_cnt else 0

    txt = [f"💰 <b>Выручка за {_period_label(start, end)}</b>", ""]
    txt.append(f"Всего: <b>{_money(total_sum)}</b> ({total_cnt} оплат)")
    txt.append(f"Средний чек: <b>{_money(avg)}</b>")
    txt.append("")
    txt.append(f"🆕 Первичка: <b>{_money(first_sum)}</b> ({first_cnt} оплат)")
    txt.append(f"🔁 Повторка: <b>{_money(repeat_sum)}</b> ({repeat_cnt} оплат)")
    if total_sum:
        share = repeat_sum * 100 / total_sum
        txt.append(f"   Доля повторок: <b>{share:.0f}%</b>")
    await message.answer("\n".join(txt))


@router.message(Command("clients"))
async def cmd_clients(message: Message) -> None:
    """Топ-15 клиентов по LTV (сумме платежей)."""
    with get_session() as session:
        rows = session.execute(
            select(Client, func.count(Payment.id), func.sum(Payment.amount))
            .join(Payment, Payment.client_id == Client.id)
            .where(Payment.payment_type.in_(["first", "repeat"]))
            .group_by(Client.id)
            .order_by(func.sum(Payment.amount).desc())
            .limit(15)
        ).all()
    if not rows:
        await message.answer("Пока нет клиентов с оплатами.")
        return
    lines = ["👥 <b>Топ клиентов по LTV</b>", ""]
    for client, cnt, total in rows:
        name = client.name or client.phone or f"#{client.id}"
        lines.append(f"<b>{name}</b> — {_money(total or 0)} ({cnt} оп.)")
    await message.answer("\n".join(lines))


DASHBOARD_FALLBACK = "https://dashboard.sharlovsky.pro/"

_IP_URL_RE = re.compile(r"://\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}")


def _resolve_dashboard_url() -> str:
    url = (os.environ.get("DASHBOARD_URL") or "").strip()
    if not url:
        return DASHBOARD_FALLBACK
    # Голый IP в URL (любой — внутренний или публичный) — отдаём fallback
    # с красивым доменом. Покрывает старые .env с http://<IP>/ после миграции.
    if _IP_URL_RE.search(url):
        return DASHBOARD_FALLBACK
    if any(p in url for p in ("localhost", "127.0.0.1")):
        return DASHBOARD_FALLBACK
    return url


@router.message(Command("dashboard"))
@router.message(F.text == BTN_DASHBOARD)
async def cmd_dashboard(message: Message) -> None:
    await message.answer(f"🌐 <b>Дашборд:</b> {_resolve_dashboard_url()}")


@router.message(Command("sheet"))
async def cmd_sheet(message: Message) -> None:
    url = (os.environ.get("SHEET_URL") or "").strip()
    if not url:
        await message.answer("Ссылка на таблицу не задана. Добавьте SHEET_URL в .env.")
        return
    await message.answer(f"📊 Ваша таблица: {url}")
