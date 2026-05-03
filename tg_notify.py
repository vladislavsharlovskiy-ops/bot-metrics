"""
Push-уведомления в Telegram из не-aiogram-кода (Flask-эндпоинты, webhook'и).

Не использует aiogram, чтобы не тащить event loop в Flask. Просто HTTPS-запрос
на Telegram Bot API через urllib.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Optional

log = logging.getLogger("tg_notify")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
_owner_raw = os.environ.get("OWNER_ID", "").strip()
OWNER_ID = int(_owner_raw) if _owner_raw.isdigit() else 0
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def tg_send(text: str, reply_markup: Optional[dict] = None, chat_id: Optional[int] = None) -> bool:
    """
    Шлёт сообщение в Telegram. Не падает, если не сложилось — логирует и
    возвращает False, чтобы вызывающий код мог продолжить работу.
    """
    if not BOT_TOKEN:
        log.warning("tg_send skipped: BOT_TOKEN is not set")
        return False
    target = chat_id if chat_id is not None else OWNER_ID
    if not target:
        log.warning("tg_send skipped: no chat_id and OWNER_ID is empty")
        return False
    payload = {"chat_id": target, "text": text, "parse_mode": "HTML"}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    data = urllib.parse.urlencode(payload).encode()
    try:
        req = urllib.request.Request(f"{TG_API}/sendMessage", data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
        return True
    except Exception as e:
        log.warning("tg_send failed: %s", e)
        return False


def _format_created_at(iso_value: Optional[str]) -> Optional[str]:
    if not iso_value:
        return None
    try:
        return datetime.fromisoformat(iso_value).strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return iso_value


def _external_lead_keyboard(lead_id: int) -> dict:
    """
    Кнопки под карточкой лида. Те же callback_data, что использует
    aiogram-роутер (handlers/leads.py).
    """
    return {
        "inline_keyboard": [
            [
                {"text": "✏️ Редактировать", "callback_data": f"edit:{lead_id}"},
                {"text": "📝 Заметка",        "callback_data": f"note:{lead_id}"},
            ],
            [
                {"text": "📋 К списку",       "callback_data": "leads:1"},
                {"text": "🗑 Удалить",         "callback_data": f"del:{lead_id}"},
            ],
        ]
    }


def notify_external_lead(lead: dict, header: str = "🤖 <b>Автоматически из лид-бота</b>") -> bool:
    """
    Уведомление владельцу о новом лиде. Заголовок настраивается, чтобы
    отличать источник (внешний лид-бот, Telegram Business и т.п.).
    Возвращает True если отправлено, False если нет (не бросает исключений).
    """
    try:
        lead_id = lead["id"]
        lines = [
            header,
            "",
            f"<b>Лид #{lead_id}</b>",
            f"Этап: <b>{lead.get('stage_title') or '—'}</b>",
            f"Источник: {lead.get('source_title') or lead.get('source') or '—'}",
            f"Имя: {lead.get('name') or '—'}",
            f"Логин: {lead.get('username') or '—'}",
        ]
        if lead.get("request"):
            lines.append(f"Запрос: {lead['request']}")
        created = _format_created_at(lead.get("created_at"))
        if created:
            lines.append(f"Создан: {created}")
        text = "\n".join(lines)
        return tg_send(text, reply_markup=_external_lead_keyboard(lead_id))
    except Exception as e:
        log.warning("notify_external_lead failed to build message: %s", e)
        return False
