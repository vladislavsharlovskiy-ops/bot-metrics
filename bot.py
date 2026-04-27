from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, TelegramObject, Update

from config import ALLOWED_USERS, BOT_TOKEN, OPEN_ACCESS, OWNER_ID
from db import init_db
from handlers import leads, notifications, payments, reports

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
    BotCommand(command="help",     description="❓ Помощь"),
]


async def main() -> None:
    init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.update.outer_middleware(OwnerOnlyMiddleware())
    dp.include_router(leads.router)
    dp.include_router(reports.router)
    dp.include_router(notifications.router)
    dp.include_router(payments.router)
    await bot.set_my_commands(BOT_COMMANDS)
    log.info("Bot started")
    asyncio.create_task(notifications.digest_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
