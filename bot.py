from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, TelegramObject, Update

from config import ALLOWED_USERS, BOT_TOKEN, OPEN_ACCESS, OWNER_ID
from db import init_db
from handlers import admin, business, leads, notifications, payments, reports

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")


class OwnerOnlyMiddleware(BaseMiddleware):
    """Drop any update that is not from the configured OWNER_ID."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if OPEN_ACCESS:
            return await handler(event, data)
        # Business-апдейты приходят от клиентов владельца (не из ALLOWED_USERS),
        # но это легитимный канал — фильтр здесь обходится, отдельный роутер
        # сам решает, что с ними делать.
        if isinstance(event, Update) and (
            event.business_message is not None
            or event.edited_business_message is not None
            or event.business_connection is not None
            or event.deleted_business_messages is not None
        ):
            return await handler(event, data)
        if user is None or user.id not in ALLOWED_USERS:
            log.warning("Blocked update from user_id=%s", getattr(user, "id", None))
            # Подсказываем человеку, как получить доступ
            if user is not None and getattr(event, "message", None):
                try:
                    await event.message.answer(
                        f"Привет! Это закрытый бот учёта лидов.\n\n"
                        f"Ваш Telegram ID: <code>{user.id}</code>\n\n"
                        f"Перешлите этот номер владельцу — он добавит вас в доступ.",
                        parse_mode="HTML",
                    )
                except Exception as e:
                    log.warning("Couldn't send block notice: %s", e)
            return None
        return await handler(event, data)


BOT_COMMANDS = [
    BotCommand(command="new",      description="➕ Новый лид"),
    BotCommand(command="leads",    description="📋 Активные лиды"),
    BotCommand(command="ignoring", description="🤐 Лиды в «игнорит»"),
    BotCommand(command="today",    description="📊 Сегодня"),
    BotCommand(command="week",     description="📊 Неделя"),
    BotCommand(command="month",    description="📊 Месяц"),
    BotCommand(command="channels", description="📈 По каналам"),
    BotCommand(command="funnel",   description="🎯 Текущая воронка"),
    BotCommand(command="find",     description="🔍 Поиск по имени/логину"),
    BotCommand(command="digest",   description="🌅 Сводка лидов на дожим"),
    BotCommand(command="revenue",  description="💰 Выручка (первичка/повторка)"),
    BotCommand(command="months",   description="📅 По месяцам (динамика)"),
    BotCommand(command="clients",  description="👥 Топ клиентов по LTV"),
    BotCommand(command="dashboard",description="🌐 Открыть дашборд"),
    BotCommand(command="admin",    description="🛠 Админ-команды (бэкап, редеплой, …)"),
    BotCommand(command="help",     description="❓ Помощь"),
]


def _ensure_poll_deploy_cron() -> None:
    """Idempotent self-installer for poll-deploy crontab entry.

    Запускается на каждом старте бота. Проверяет crontab юзера bot, и если
    строки `*/2 * * * * /opt/bot-metrics/bin/poll-deploy.sh` нет — добавляет.
    deploy.sh пытается ставить эту же строку, но иногда это не срабатывает
    (sudo -u bot crontab из root-контекста + cron permissions могут давать
    silent fail). Авто-чек на старте bota — пуленепробиваемая страховка.

    Не падает если cron вообще недоступен в окружении, бот всё равно стартует.
    """
    poll_path = "/opt/bot-metrics/bin/poll-deploy.sh"
    cron_line = f"*/2 * * * * {poll_path}"
    if not os.path.isfile(poll_path):
        log.info("poll-deploy: %s missing, skip cron check", poll_path)
        return
    try:
        cur = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=5,
        )
        # rc=1 + пустой stdout = нет crontab вообще, это норм для свежего юзера
        existing = cur.stdout if cur.returncode in (0, 1) else ""
    except Exception as e:  # noqa: BLE001
        log.warning("poll-deploy: crontab -l failed: %s", e)
        return

    if "poll-deploy.sh" in existing:
        log.info("poll-deploy: cron entry уже стоит, пропускаем")
        return

    new_crontab = (existing.rstrip() + ("\n" if existing.strip() else "")) + cron_line + "\n"
    try:
        r = subprocess.run(
            ["crontab", "-"], input=new_crontab,
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            log.info("poll-deploy: cron entry установлен")
        else:
            log.warning("poll-deploy: crontab install rc=%d stderr=%s", r.returncode, r.stderr[:200])
    except Exception as e:  # noqa: BLE001
        log.warning("poll-deploy: crontab install failed: %s", e)


async def main() -> None:
    init_db()
    _ensure_poll_deploy_cron()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.update.outer_middleware(OwnerOnlyMiddleware())
    dp.include_router(leads.router)
    dp.include_router(reports.router)
    dp.include_router(notifications.router)
    dp.include_router(payments.router)
    dp.include_router(business.router)
    dp.include_router(admin.router)
    await bot.set_my_commands(BOT_COMMANDS)
    log.info("Bot started")
    asyncio.create_task(notifications.digest_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
