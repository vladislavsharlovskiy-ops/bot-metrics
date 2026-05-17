from __future__ import annotations

from dataclasses import dataclass

LEAD_NEW = "lead_new"
QUALIFIED = "qualified"
BREAKDOWN_SENT = "breakdown_sent"
AGREED = "agreed"
PAID = "paid"
CONSULTED = "consulted"
PACKAGE_BOUGHT = "package_bought"
PACKAGE_DECLINED = "package_declined"
LOST = "lost"
IGNORING = "ignoring"


@dataclass(frozen=True)
class Stage:
    code: str
    title: str
    short: str


# QUALIFIED убран из FUNNEL display-списка по запросу пользователя:
# «разбор отправляю всем квалам всегда, отдельный этап Квал не нужен».
# Stage-код остался определённым для обратной совместимости с
# существующими лидами в БД и для cumulative-учёта (ALL_FUNNEL_CODES_ORDERED).
# Существующие QUALIFIED-лиды мигрируются в BREAKDOWN_SENT на старте
# бота (см. db.init_db).
#
# AGREED тоже убран из FUNNEL — «согласие — это и есть оплата».
FUNNEL: list[Stage] = [
    Stage(LEAD_NEW,        "Заявка пришла",            "Заявка"),
    Stage(BREAKDOWN_SENT,  "Разбор отправлен",         "Разбор"),
    Stage(PAID,            "Оплата консультации",      "Оплата"),
    Stage(CONSULTED,       "Консультация проведена",   "Консультация"),
    Stage(PACKAGE_BOUGHT,  "Куплен пакет / почасово",  "Пакет"),
]

# Полный порядок этапов с QUALIFIED+AGREED — для cumulative counting в
# воронке (count «лиды на этом этапе ИЛИ позже»). Старые этапы включены,
# чтобы лиды на них попадали в счёт более ранних этапов.
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


# Stage-объекты QUALIFIED+AGREED оставлены в BY_CODE — иначе для
# существующих лидов на этих этапах UI ломается (например, /lead в боте
# показывает stage_title). После миграции QUALIFIED→BREAKDOWN_SENT
# таких лидов быть не должно, но код страховочный.
_QUALIFIED_STAGE = Stage(QUALIFIED, "Квал. заявка", "Квал")
_AGREED_STAGE = Stage(AGREED, "Согласие на консультацию", "Согласие")

LOST_STAGE = Stage(LOST, "Лид отвалился", "Отвал")
IGNORING_STAGE = Stage(IGNORING, "Игнорит", "Игнор")
# package_declined — клиент после консультации, пакет не купил.
# Не входит в CLIENT_CODES (выпадает из вкладки «Клиенты»),
# не входит в FUNNEL (не отрисовывается в воронке как этап).
# Исторически Payment остаётся, выручка считается. Чтобы вернуть
# в работу, на карточке появляется кнопка «Купил пакет» (advance).
PACKAGE_DECLINED_STAGE = Stage(PACKAGE_DECLINED, "Без пакета", "Без пакета")

BY_CODE: dict[str, Stage] = {
    s.code: s for s in [*FUNNEL, _QUALIFIED_STAGE, _AGREED_STAGE, LOST_STAGE, IGNORING_STAGE, PACKAGE_DECLINED_STAGE]
}

# Активные = лиды, которые ещё не оплатили. QUALIFIED+AGREED оставлены
# для обратной совместимости с существующими лидами.
ACTIVE_CODES = {LEAD_NEW, QUALIFIED, BREAKDOWN_SENT, AGREED}
# Клиенты = тот, кто уже оплатил (хоть консультацию, хоть пакет).
CLIENT_CODES = {PAID, CONSULTED, PACKAGE_BOUGHT}
IGNORING_CODES = {IGNORING}


def next_stage(current: str) -> Stage | None:
    """Следующий этап в воронке. Special-cases:
    - QUALIFIED → BREAKDOWN_SENT: QUALIFIED убран из FUNNEL,
      но старые лиды на нём должны двигаться вперёд.
    - AGREED → PAID: AGREED тоже убран из FUNNEL.
    - PACKAGE_DECLINED → PACKAGE_BOUGHT: если клиент после «Без пакета»
      всё-таки купил пакет, кнопка «Дальше» вернёт во «Клиенты».
    """
    if current == QUALIFIED:
        return BY_CODE[BREAKDOWN_SENT]
    if current == AGREED:
        return BY_CODE[PAID]
    if current == PACKAGE_DECLINED:
        return BY_CODE[PACKAGE_BOUGHT]
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
