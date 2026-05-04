"""
Утренняя сводка по «зависшим» лидам — каждый день в 9:00 МСК.

В сводку попадают лиды на этапах «Разбор отправлен» и «Игнорят», у которых
уже 2+ дня нет движения (Lead.updated_at). Когда владелец меняет статус
лида — updated_at обновляется и лид перестаёт показываться, пока снова
не «зависнет» на 2 дня.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from config import OWNER_ID
from db import get_session
from models import Lead
from stages import BREAKDOWN_SENT, IGNORING, SOURCE_TITLES

router = Router()
log = logging.getLogger("digest")

DIGEST_HOUR = 9
DIGEST_MINUTE = 0
STUCK_DAYS = 2
DIGEST_STAGES = {BREAKDOWN_SENT, IGNORING}
MSK = ZoneInfo("Europe/Moscow")


def _stuck_leads() -> list[Lead]:
    threshold = datetime.now() - timedelta(days=STUCK_DAYS)
    with get_session() as session:
        return list(
            session.execute(
                select(Lead)
                .where(Lead.stage.in_(DIGEST_STAGES))
                .where(Lead.updated_at < threshold)
                .order_by(Lead.updated_at.asc())
            ).scalars().all()
        )


def _days_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "дня"
    return "дней"


def _lead_block(lead: Lead, days_idle: int) -> str:
    src = SOURCE_TITLES.get(lead.source, lead.source)
    name = lead.name or lead.username or f"#{lead.id}"
    stage_tag = "🤐 В игноре" if lead.stage == IGNORING else "📨 Разбор отправлен"
    text = f"<b>{name}</b> · {src}\n{stage_tag}\n"
    if lead.username:
        text += f"Логин: {lead.username}\n"
    text += f"⏳ Без движения: <b>{days_idle} {_days_word(days_idle)}</b>\n"
    if lead.request:
        preview = lead.request[:160].strip()
        if len(lead.request) > 160:
            preview += "…"
        text += f"📝 {preview}"
    return text


def _lead_kb(lead: Lead) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="📂 Открыть",   callback_data=f"open:{lead.id}"),
            InlineKeyboardButton(text="→ Согласие",  callback_data=f"adv:{lead.id}"),
            InlineKeyboardButton(text="❌ Отвал",     callback_data=f"lost:{lead.id}"),
        ]]
    )


async def send_digest(bot: Bot) -> None:
    leads = _stuck_leads()
    if not leads:
        await bot.send_message(
            OWNER_ID,
            "🌅 <b>Доброе утро!</b>\n\nЗависших лидов на дожим сегодня нет ✨",
        )
        return

    await bot.send_message(
        OWNER_ID,
        f"🌅 <b>Доброе утро!</b>\n\n"
        f"Лидов на дожим: <b>{len(leads)}</b>\n"
        f"<i>(Разбор отправлен или В игноре, {STUCK_DAYS}+ {_days_word(STUCK_DAYS)} без движения)</i>",
    )

    now = datetime.now()
    for lead in leads:
        days = max(1, (now - lead.updated_at).days)
        try:
            await bot.send_message(OWNER_ID, _lead_block(lead, days), reply_markup=_lead_kb(lead))
        except Exception as e:
            log.warning("digest send failed for lead %s: %s", lead.id, e)
        await asyncio.sleep(0.2)  # лёгкий троттлинг


@router.message(Command("digest"))
async def cmd_digest(message: Message, bot: Bot) -> None:
    """Ручной запуск сводки — на проверку или когда хочется свежую сводку днём."""
    await send_digest(bot)


def _seconds_until_next_run() -> float:
    now = datetime.now(MSK)
    target = now.replace(hour=DIGEST_HOUR, minute=DIGEST_MINUTE, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def digest_loop(bot: Bot) -> None:
    """Бесконечный цикл: спим до ближайших 9:00 МСК, отправляем, повторяем."""
    while True:
        wait = _seconds_until_next_run()
        log.info("Next digest in %.0f minutes (at %02d:%02d МСК)", wait / 60, DIGEST_HOUR, DIGEST_MINUTE)
        try:
            await asyncio.sleep(wait)
            await send_digest(bot)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("digest loop error: %s", e)
        await asyncio.sleep(60)  # защита от двойного срабатывания
