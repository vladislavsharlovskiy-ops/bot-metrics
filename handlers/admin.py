"""
Админ-команды для владельца бота (доступны только OWNER_ID).

Цель: делать регулярные операции (бэкап, редеплой, смена URL дашборда,
получение URL для GitHub-вебхука) прямо из чата с ботом, без захода
на сервер по SSH.

Все команды требуют OWNER_ID (фильтр прописан на каждом хендлере, плюс
есть глобальный OwnerOnlyMiddleware в bot.py — но business_message
обходит middleware, поэтому здесь страхуемся на уровне хендлера).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from config import OWNER_ID

log = logging.getLogger("admin")
router = Router(name="admin")

ENV_FILE = Path("/opt/bot-metrics/.env")
BACKUP_SCRIPT = "/opt/bot-metrics/bin/backup.sh"
DEPLOY_SCRIPT = "/opt/bot-metrics/bin/deploy.sh"


def _is_owner(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == OWNER_ID)


# ─── /backup ───────────────────────────────────────────────────────

@router.message(Command("backup"))
async def cmd_backup(message: Message) -> None:
    """Запускает backup.sh: снимок bot.db + отправка файлом в этот же бот."""
    if not _is_owner(message):
        return
    if not os.path.exists(BACKUP_SCRIPT):
        await message.answer(
            f"⚠ Не найден {BACKUP_SCRIPT}. Похоже, скрипт ещё не положен install.sh'ем."
        )
        return
    await message.answer("🔄 Снимаю бэкап и шлю файлом…")
    try:
        result = subprocess.run(
            ["bash", BACKUP_SCRIPT],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            # backup.sh сам шлёт файл в TG, поэтому здесь только отбивка
            await message.answer("✅ Готово. Файл должен прийти отдельным сообщением.")
        else:
            tail = (result.stdout + "\n" + result.stderr)[-1500:]
            await message.answer(
                f"⚠ Бэкап завершился с ошибкой (код {result.returncode}):\n"
                f"<pre>{tail}</pre>",
                parse_mode="HTML",
            )
    except subprocess.TimeoutExpired:
        await message.answer("⚠ Бэкап выполняется дольше 2 минут — что-то не так.")
    except Exception as e:
        log.warning("backup error: %s", e)
        await message.answer(f"⚠ Ошибка: {e}")


# ─── /redeploy ─────────────────────────────────────────────────────

@router.message(Command("redeploy"))
async def cmd_redeploy(message: Message) -> None:
    """
    Запускает deploy.sh: git pull + перезапуск сервисов.
    После этого бот сам себя рестартит, поэтому ответа после «запускаю» уже не
    будет — просто отправь /help через 10-20 сек, чтобы убедиться что поднялся.
    """
    if not _is_owner(message):
        return
    if not os.path.exists(DEPLOY_SCRIPT):
        await message.answer(f"⚠ Не найден {DEPLOY_SCRIPT}.")
        return
    await message.answer(
        "🚀 Подтягиваю свежий код и перезапускаю сервисы.\n"
        "Бот сейчас уйдёт на 10-20 секунд, после этого пиши /help для проверки."
    )
    try:
        # sudo нужен — sudoers.d-bot-metrics разрешает bot'у запускать deploy.sh без пароля
        subprocess.Popen(
            ["sudo", DEPLOY_SCRIPT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    except Exception as e:
        log.warning("redeploy spawn error: %s", e)
        await message.answer(f"⚠ Не удалось запустить деплой: {e}")


# ─── /setdashboardurl ──────────────────────────────────────────────

_URL_RE = re.compile(r"^https?://[^\s]+$")


@router.message(Command("setdashboardurl"))
async def cmd_set_dashboard_url(message: Message, command: CommandObject) -> None:
    """Меняет DASHBOARD_URL в env (и в /opt/bot-metrics/.env, чтоб пережил рестарт)."""
    if not _is_owner(message):
        return
    arg = (command.args or "").strip()
    if not arg or not _URL_RE.match(arg):
        await message.answer(
            "Использование: <code>/setdashboardurl https://example.com/</code>",
            parse_mode="HTML",
        )
        return
    if not arg.endswith("/"):
        arg += "/"

    # 1) В текущий процесс — сразу даёт эффект на /dashboard
    os.environ["DASHBOARD_URL"] = arg

    # 2) В .env — чтобы пережило рестарт (deploy.sh, systemd reload, etc.)
    persisted = _persist_env(ENV_FILE, "DASHBOARD_URL", arg)

    if persisted is True:
        await message.answer(
            f"✅ Адрес дашборда обновлён: {arg}\n\nПроверь: /dashboard"
        )
    else:
        await message.answer(
            f"⚠ В env обновил, но в .env записать не удалось ({persisted}).\n"
            f"После рестарта бота значение откатится. Адрес сейчас: {arg}"
        )


def _persist_env(path: Path, key: str, value: str) -> bool | str:
    """
    Заменяет/добавляет KEY=VALUE в .env-файле. Возвращает True при успехе,
    строку с ошибкой иначе. Не падает — ловит все исключения.
    """
    try:
        if not path.exists():
            return f"{path} not found"
        lines = path.read_text(encoding="utf-8").splitlines()
        out: list[str] = []
        replaced = False
        for line in lines:
            if line.startswith(f"{key}="):
                out.append(f"{key}={value}")
                replaced = True
            else:
                out.append(line)
        if not replaced:
            out.append(f"{key}={value}")
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        return True
    except Exception as e:
        return str(e)


# ─── /setprodamuskey ───────────────────────────────────────────────

@router.message(Command("setprodamuskey"))
async def cmd_set_prodamus_key(message: Message, command: CommandObject) -> None:
    """
    Меняет PRODAMUS_SECRET_KEY в .env и перезапускает web-сервис.
    После этого webhook'и от Prodamus снова валидируются успешно.
    """
    if not _is_owner(message):
        return
    arg = (command.args or "").strip()
    if not arg or len(arg) < 10:
        await message.answer(
            "Использование: <code>/setprodamuskey твой_secret_key</code>\n\n"
            "Где взять: panel.prodamus.ru → Настройки → "
            "Защита от поддельных уведомлений → secret key.\n\n"
            "⚠ После выполнения сообщение лучше удали — ключ виден в истории чата.",
            parse_mode="HTML",
        )
        return

    # 1) В .env — без этого web-процесс не подхватит, т.к. PRODAMUS_SECRET
    #    читается на старте модуля prodamus.py
    persisted = _persist_env(ENV_FILE, "PRODAMUS_SECRET_KEY", arg)
    if persisted is not True:
        await message.answer(f"⚠ Не удалось записать в .env: {persisted}")
        return

    # 2) Перезапуск web-сервиса (бот сам себя НЕ перезапускаем — это другой
    #    юнит). systemctl restart bot-metrics-web разрешён через sudoers.
    await message.answer(
        "🔄 Ключ записан. Перезапускаю web-сервис, чтобы Flask его подхватил…"
    )
    try:
        result = subprocess.run(
            ["sudo", "/bin/systemctl", "restart", "bot-metrics-web"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            await message.answer(
                "✅ Готово. Webhook на /webhook/prodamus теперь будет принимать "
                "платежи. Попроси Prodamus прислать тестовый или дождись следующей "
                "оплаты.\n\n"
                "Если оплата уже была и потерялась — её можно вручную добавить, "
                "но в БД она вернётся только при повторном webhook'е.\n\n"
                "⚠ Удали сообщение с ключом из чата."
            )
        else:
            await message.answer(
                f"⚠ Restart не удался:\n<pre>{result.stderr[:1000]}</pre>",
                parse_mode="HTML",
            )
    except Exception as e:
        await message.answer(f"⚠ Ошибка: {e}")


# ─── /deployurl ────────────────────────────────────────────────────

@router.message(Command("deployurl"))
async def cmd_deploy_url(message: Message) -> None:
    """Печатает URL GitHub-вебхука для настройки авто-деплоя."""
    if not _is_owner(message):
        return
    secret = os.environ.get("DEPLOY_SECRET", "").strip()
    if not secret:
        await message.answer(
            "⚠ В env нет DEPLOY_SECRET. Похоже, install.sh не успел его сгенерить."
        )
        return

    # Берём домен из DASHBOARD_URL, если он не IP — иначе хардкод
    base = os.environ.get("DASHBOARD_URL", "").rstrip("/")
    if not base or re.search(r"://\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", base):
        base = "https://dashboard.sharlovsky.pro"
    url = f"{base}/__deploy/{secret}"

    await message.answer(
        "🪝 <b>GitHub Webhook URL для авто-деплоя</b>\n\n"
        f"<code>{url}</code>\n\n"
        "Куда вставить:\n"
        "1. https://github.com/vladislavsharlovskiy-ops/bot-metrics/settings/hooks\n"
        "2. <b>Add webhook</b>\n"
        "3. <b>Payload URL</b> — вставь URL выше\n"
        "4. <b>Content type</b> — application/json\n"
        "5. <b>Which events</b> — Just the push event\n"
        "6. <b>Active</b> — галочка\n"
        "7. Add webhook\n\n"
        "После этого каждый push в main → авто-деплой на сервер. "
        "Тебе больше не надо ни /redeploy, ни SSH.",
        parse_mode="HTML",
    )


# ─── /admin (помощь) ───────────────────────────────────────────────

@router.message(Command("admin"))
async def cmd_admin_help(message: Message) -> None:
    if not _is_owner(message):
        return
    await message.answer(
        "🛠 <b>Админ-команды</b> (только владелец)\n\n"
        "<code>/backup</code> — бэкап БД прямо сейчас, файл придёт в этот чат\n"
        "<code>/redeploy</code> — подтянуть свежий код с GitHub и перезапустить\n"
        "<code>/setdashboardurl &lt;url&gt;</code> — сменить адрес дашборда\n"
        "<code>/setprodamuskey &lt;key&gt;</code> — задать секретный ключ "
        "Prodamus (если webhook'и валятся с 'bad signature')\n"
        "<code>/deployurl</code> — показать URL для GitHub-вебхука "
        "(один раз настроишь — авто-деплой при push в main, /redeploy больше не нужен)",
        parse_mode="HTML",
    )
