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


# AGREED («Согласие на консультацию») убран из FUNNEL display-списка по
# запросу пользователя: «согласие — это и есть оплата». Stage-код остался
# определённым для обратной совместимости с существующими лидами в БД и
# для cumulative-учёта (см. ALL_FUNNEL_CODES_ORDERED ниже).
FUNNEL: list[Stage] = [
    Stage(LEAD_NEW,        "Заявка пришла",            "Заявка"),
    Stage(QUALIFIED,       "Квал. заявка",             "Квал"),
    Stage(BREAKDOWN_SENT,  "Разбор отправлен",         "Разбор"),
    Stage(PAID,            "Оплата консультации",      "Оплата"),
    Stage(CONSULTED,       "Консультация проведена",   "Консультация"),
    Stage(PACKAGE_BOUGHT,  "Куплен пакет / почасово",  "Пакет"),
]

# Полный порядок этапов с AGREED — для cumulative counting в воронке
# (count «лиды на этом этапе ИЛИ позже»). AGREED включён, чтобы лиды,
# застрявшие на этом этапе, попадали в счёт более ранних этапов.
ALL_FUNNEL_CODES_ORDERED: list[str] = [
    LEAD_NEW, QUALIFIED, BREAKDOWN_SENT, AGREED,
    PAID, CONSULTED, PACKAGE_BOUGHT,
]


def codes_at_or_after(stage_code: str) -> list[str]:
    """Все этапы воронки, начиная с указанного (для cumulative counting)."""
    try:
        idx = ALL_FUNNEL_CODES_ORDERED.index(stage_code)
        return ALL_FUNNEL_CODES_ORDERED[idx:]
    except ValueError:
        return [stage_code]


# Stage-объект AGREED оставлен в BY_CODE — иначе для существующих лидов на
# этом этапе UI ломается (например, /lead в боте показывает stage_title).
_AGREED_STAGE = Stage(AGREED, "Согласие на консультацию", "Согласие")

LOST_STAGE = Stage(LOST, "Лид отвалился", "Отвал")
IGNORING_STAGE = Stage(IGNORING, "Игнорит", "Игнор")

BY_CODE: dict[str, Stage] = {s.code: s for s in [*FUNNEL, _AGREED_STAGE, LOST_STAGE, IGNORING_STAGE]}

# Активные = лиды, которые ещё не оплатили. AGREED оставлен для обратной
# совместимости с существующими лидами на этом этапе.
ACTIVE_CODES = {LEAD_NEW, QUALIFIED, BREAKDOWN_SENT, AGREED}
# Клиенты = тот, кто уже оплатил (хоть консультацию, хоть пакет).
CLIENT_CODES = {PAID, CONSULTED, PACKAGE_BOUGHT}
IGNORING_CODES = {IGNORING}


def next_stage(current: str) -> Stage | None:
    """Следующий этап в воронке. Special-case AGREED → PAID, потому что
    AGREED убран из FUNNEL list, но существующие лиды на нём должны
    нормально продвигаться по /advance."""
    if current == AGREED:
        return BY_CODE[PAID]
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
