from __future__ import annotations

from dataclasses import dataclass

LEAD_NEW = "lead_new"
QUALIFIED = "qualified"
BREAKDOWN_SENT = "breakdown_sent"
AGREED = "agreed"
PAID = "paid"
CONSULTED = "consulted"
PACKAGE_BOUGHT = "package_bought"
LOST = "lost"
IGNORING = "ignoring"


@dataclass(frozen=True)
class Stage:
    code: str
    title: str
    short: str


FUNNEL: list[Stage] = [
    Stage(LEAD_NEW,        "Заявка пришла",            "Заявка"),
    Stage(QUALIFIED,       "Квал. заявка",             "Квал"),
    Stage(BREAKDOWN_SENT,  "Разбор отправлен",         "Разбор"),
    Stage(AGREED,          "Согласие на консультацию", "Согласие"),
    Stage(PAID,            "Оплата консультации",      "Оплата"),
    Stage(CONSULTED,       "Консультация проведена",   "Консультация"),
    Stage(PACKAGE_BOUGHT,  "Куплен пакет / почасово",  "Пакет"),
]

LOST_STAGE = Stage(LOST, "Лид отвалился", "Отвал")
IGNORING_STAGE = Stage(IGNORING, "Игнорит", "Игнор")

BY_CODE: dict[str, Stage] = {s.code: s for s in [*FUNNEL, LOST_STAGE, IGNORING_STAGE]}

# Активные = лиды, которые ещё не оплатили. После оплаты лид становится клиентом.
ACTIVE_CODES = {LEAD_NEW, QUALIFIED, BREAKDOWN_SENT, AGREED}
# Клиенты = тот, кто уже оплатил (хоть консультацию, хоть пакет).
CLIENT_CODES = {PAID, CONSULTED, PACKAGE_BOUGHT}
IGNORING_CODES = {IGNORING}


def next_stage(current: str) -> Stage | None:
    for i, s in enumerate(FUNNEL):
        if s.code == current and i + 1 < len(FUNNEL):
            return FUNNEL[i + 1]
    return None


SOURCES = [
    ("instagram", "Instagram"),
    ("youtube",   "YouTube"),
    ("telegram",  "Telegram-канал"),
    ("tiktok",    "TikTok"),
    ("rutube",    "Rutube"),
    ("vk",        "VK"),
    ("unknown",   "Не определён"),
]


# ───────── Воронка повторных продаж ─────────

REPEAT_REQUEST   = "repeat_request"
REPEAT_SCHEDULED = "repeat_scheduled"
REPEAT_PAID      = "repeat_paid"
REPEAT_DONE      = "repeat_done"

REPEAT_FUNNEL: list[Stage] = [
    Stage(REPEAT_REQUEST,   "Запрос на повтор",     "Запрос"),
    Stage(REPEAT_SCHEDULED, "Согласовано время",    "Время"),
    Stage(REPEAT_PAID,      "Оплачено",             "Оплата"),
    Stage(REPEAT_DONE,      "Сессия проведена",     "Проведена"),
]

REPEAT_BY_CODE: dict[str, Stage] = {s.code: s for s in REPEAT_FUNNEL}


def repeat_next_stage(current: str) -> Stage | None:
    for i, s in enumerate(REPEAT_FUNNEL):
        if s.code == current and i + 1 < len(REPEAT_FUNNEL):
            return REPEAT_FUNNEL[i + 1]
    return None

SOURCE_TITLES = dict(SOURCES)
