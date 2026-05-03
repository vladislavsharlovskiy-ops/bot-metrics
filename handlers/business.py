"""
Хендлер Telegram Business: ловим сообщения клиентов в личке владельца и
автоматически заводим/обновляем лид. Бот не отвечает — отвечает владелец сам.

Чтобы это заработало:
  1. У бота включён Business Mode в @BotFather.
  2. Бот добавлен в Settings → Telegram для бизнеса → Чат-боты.
"""
from __future__ import annotations

import logging
import re

from aiogram import Router
from aiogram.types import Message
from sqlalchemy import select

from config import OWNER_ID
from db import get_session
from models import Lead, StageHistory
from stages import LEAD_NEW

log = logging.getLogger("business")
router = Router(name="business")


# Ключевики источников. Считаем match'ем любое упоминание в тексте — клиент
# может написать «я из инсты» или «узнала про вас в ютубе», и то и другое
# одинаково ценно как сигнал.
SOURCE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "instagram": ("instagram", "инстаграм", "инстаграмм", "инсту", "инсты", "инста", "insta", "ig"),
    "youtube":   ("youtube", "ютуб", "ютуба", "ютьюб", "ютубе", "yt"),
    "telegram":  ("telegram", "телеграм", "телеграмм", "тг", "tg"),
    "tiktok":    ("tiktok", "тикток", "тиктока", "тиктоке"),
    "rutube":    ("rutube", "рутуб", "рутуба", "рутубе"),
    "vk":        ("vk", "вк", "вконтакте", "вконтакта"),
}


def detect_source(text: str | None) -> str | None:
    """Ищет в тексте упоминание известной площадки. Возвращает код или None."""
    if not text:
        return None
    norm = text.lower().replace("ё", "е")
    for code, keywords in SOURCE_KEYWORDS.items():
        for kw in keywords:
            # Границы слова, чтобы «вк» не матчился внутри «вконец» и т.п.
            pattern = r"(?<![a-zа-я0-9])" + re.escape(kw) + r"(?![a-zа-я0-9])"
            if re.search(pattern, norm):
                return code
    return None


def _format_username(username: str | None) -> str | None:
    if not username:
        return None
    return "@" + username.lstrip("@")


@router.business_message()
async def on_business_message(message: Message) -> None:
    user = message.from_user
    if user is None or user.is_bot:
        return
    # Сообщения от самого владельца в его же business-чате — это его ответы клиенту,
    # а не входящая заявка. Не заводим лид на самого себя.
    if user.id == OWNER_ID:
        return

    text = (message.text or message.caption or "").strip()
    detected = detect_source(text)
    name = (user.full_name or "").strip() or None
    username = _format_username(user.username)

    with get_session() as session:
        lead = session.execute(
            select(Lead).where(Lead.telegram_user_id == user.id)
        ).scalars().first()

        if lead is None:
            lead = Lead(
                name=name,
                username=username,
                telegram_user_id=user.id,
                source=detected or "unknown",
                request=text or None,
                stage=LEAD_NEW,
            )
            session.add(lead)
            session.flush()
            session.add(StageHistory(lead_id=lead.id, stage=LEAD_NEW))
            session.commit()
            session.refresh(lead)
            lead_id = lead.id
            log.info("business: created lead id=%s tg_id=%s source=%s", lead_id, user.id, lead.source)
        else:
            if name and lead.name != name:
                lead.name = name
            if username and lead.username != username:
                lead.username = username
            if lead.source == "unknown" and detected:
                lead.source = detected
            # Если первое сообщение было без текста (медиа), а вот теперь
            # клиент написал — фиксируем как request.
            if not lead.request and text:
                lead.request = text
            session.commit()
            lead_id = lead.id

    try:
        from sheets import sync_lead
        sync_lead(lead_id)
    except Exception as e:
        log.warning("sheets sync skipped: %s", e)
