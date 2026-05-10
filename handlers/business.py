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
    "telegram":  ("telegram", "телеграм", "телеграмм", "телеграме", "телеграмме", "тг", "tg"),
    "tiktok":    ("tiktok", "тикток", "тиктока", "тиктоке"),
    "rutube":    ("rutube", "рутуб", "рутуба", "рутубе"),
    "vk":        ("vk", "вк", "вконтакте", "вконтакта"),
}


# Кодовая фраза, по которой бот реагирует на первое сообщение нового
# контакта. Это явный ответ на наш скрипт-приветствие («Из какого вы
# города?») — лиды по нашей воронке всегда представляются как
# «Я из <город>». Любые другие первые сообщения игнорируются.
#
# Регекс требует:
#  1. После «из » — кириллический символ (а не латиница). Режет «Я из
#     Telegram-бота» / «Я из Instagram» — там после пробела латиница.
#  2. Слово после «из » не должно быть в списке «не-городов» (бота,
#     канала, инсты, ютуба, ваш/ого и т.п.) — режет случаи когда клиент
#     пишет по-русски «Я из бота» / «Я из вашего канала».
#
# Раньше регекс был [а-яa-z] с IGNORECASE — это пропускало латиницу
# (T в "Telegram") как валидный город. Регрессия с Олгой ("Я из
# Telegram-бота" → бот ответил автоскриптом) ровно поэтому.
_LEAD_INTRO_RE = re.compile(
    # «Я из <Слово>» — захватываем только когда слово начинается с
    # ЗАГЛАВНОЙ кириллической буквы. Города и страны клиенты обычно
    # пишут с большой («Москвы», «СПб», «Лас-Вегаса»), а глаголы и
    # шум — с маленькой («видела», «инсты»). Без этой капитализации
    # в _NON_CITY_WORDS пришлось бы держать бесконечный список глаголов.
    #
    # Допускаем «я из» (lowercase) и «Я из» (capitalized) для самой
    # фразы — клиенты часто пишут с маленькой.
    r"(?<![А-Яа-яЁёA-Za-z])[Яя]\s+из\s+([А-ЯЁ][А-Яа-яЁё]*)",
)
_NON_CITY_WORDS = {
    "бот", "бота", "боте", "ботом", "ботов", "ботик",
    "канал", "канала", "канале", "каналом",
    "телеграм", "телеграма", "тг", "тгшки",
    "инст", "инста", "инсты", "инсте", "инстаграм", "инстаграма", "инсту",
    "ютуб", "ютуба", "ютьюб", "ютьюба", "ютьюбе",
    "тикток", "тиктока",
    "вк", "вконтакте",
    "видос", "видоса", "сайт", "сайта", "реклама", "рекламы",
    "вашего", "твоего",
}


def has_lead_intro(text: str | None) -> bool:
    """True ТОЛЬКО если в тексте есть фраза «Я из <что-то>».

    Варианты:
      1. «Я из <Cyrillic-город>» (не из стоп-листа) — обычный лид «Я из Москвы»
      2. «Я из <канал/площадка>» — клиент явно представляется через канал
         («Я из Telegram-бота», «Я из инсты»)

    Без якоря «Я из …» сообщение НЕ считается лидом, даже если в нём
    упомянут tiktok / instagram / youtube (это может быть просто ссылка
    или разговор).

    Причина: PR #60 расширил has_lead_intro до «detect_source(text) != None»
    без проверки на «Я из …». Это сломало обычную переписку: контакт
    делится TikTok-ссылкой → в URL есть «tiktok» → detect_source находит
    его → бот выстреливает автоответом «Добрый день, благодарю за
    доверие.» в живой разговор. Жёсткий якорь «Я из» снимает эту
    регрессию, не теряя кейс Алёны (там «Я из Telegram-бота» — якорь есть).
    """
    if not text:
        return False
    norm = text.replace("ё", "е").replace("Ё", "Е")

    # Якорь — фраза «я из <что-то>». Без неё дальше не идём.
    intro_anchor = re.search(
        r"(?<![А-Яа-яЁёA-Za-z])[Яя]\s+из\s+\S",
        norm,
    )
    if not intro_anchor:
        return False

    # 1) «Я из <город кириллицей>», не из стоп-листа
    m_city = _LEAD_INTRO_RE.search(norm)
    if m_city:
        word = m_city.group(1).lower()
        if word not in _NON_CITY_WORDS:
            return True

    # 2) «Я из <канал>» — латиница, дефис, и т.п. Различаем по detect_source.
    if detect_source(text) is not None:
        return True

    return False


def detect_source(text: str | None) -> str | None:
    """Ищет в тексте упоминание известной площадки. Возвращает код или None."""
    if not text:
        return None
    norm = text.lower().replace("ё", "е")

    # Особое правило: упоминание «бот» (в любой форме — бот, бота, боте, ботом)
    # означает, что клиент пришёл через наш лид-бот в Telegram, а исходный
    # источник трафика для лид-бота — Instagram. Поэтому фразы вида
    # «я из Telegram-бота», «нашла вас через бота» → источник Instagram.
    # Telegram-канал и просто «тг»/«телеграм» без «бот» остаются как telegram.
    if re.search(r"(?<![а-яa-z0-9])бот[а-я]{0,4}(?![а-яa-z0-9])", norm):
        return "instagram"
    if re.search(r"(?<![a-zа-я0-9])bots?(?![a-zа-я0-9])", norm):
        return "instagram"

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

    is_new = False
    lead_payload: dict | None = None
    with get_session() as session:
        lead = session.execute(
            select(Lead).where(Lead.telegram_user_id == user.id)
        ).scalars().first()

        if lead is None:
            # Новый человек, которого ещё нет в базе. Заводим только если
            # в сообщении есть кодовая фраза «Я из …» — это явный ответ
            # на наш скрипт-приветствие. Без неё ничего не делаем: бот
            # не должен отвечать на случайные обращения вне скрипта
            # (например, «в бот хосте увидел, могу чем-то помочь?»),
            # их подхватит владелец вручную.
            if not has_lead_intro(text):
                log.info("business: skip non-lead message tg_id=%s", user.id)
                return
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
            is_new = True
            log.info("business: created lead id=%s tg_id=%s source=%s", lead.id, user.id, lead.source)
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
            session.refresh(lead)

        lead_id = lead.id
        if is_new:
            from web import _lead_dict
            lead_payload = _lead_dict(lead)

    if is_new and lead_payload is not None:
        try:
            from tg_notify import notify_external_lead
            notify_external_lead(lead_payload, header="📥 <b>Новый лид из Telegram Business</b>")
        except Exception as e:
            log.warning("business notify failed: %s", e)

        # Авто-ответ клиенту от имени владельца. Текст берём из env
        # (BUSINESS_AUTO_REPLY); если переменная пустая — не отвечаем.
        # Меняется через /setautoreply в админ-командах.
        # Чтобы это работало, в Telegram Business у бота должно быть
        # разрешение «Ответы на сообщения». Если нет — send упадёт с
        # TelegramBadRequest и мы просто залогируем без падения хендлера.
        # Текст автоответа берём из отдельного файла (см. handlers/admin.py
        # /setautoreply). Через .env не идём — systemd коверкает backslash-
        # последовательности, оставляя в значении буквы `n` вместо переносов.
        from handlers.admin import _read_auto_reply
        auto_reply = _read_auto_reply()
        if auto_reply and message.business_connection_id:
            try:
                await message.bot.send_message(
                    chat_id=user.id,
                    business_connection_id=message.business_connection_id,
                    text=auto_reply,
                )
                log.info("business: auto-reply sent to lead id=%s", lead_id)
            except Exception as e:
                log.warning("business auto-reply failed for lead %s: %s", lead_id, e)

    try:
        from sheets import sync_lead
        sync_lead(lead_id)
    except Exception as e:
        log.warning("sheets sync skipped: %s", e)
