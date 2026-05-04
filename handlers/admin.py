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

import json
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy import select

from config import OWNER_ID
from db import get_session
from models import Payment

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

    # Self-sync: копируем свежий deploy.sh, backup.sh, deploy_listener.py
    # из репы в /opt/bot-metrics/bin/. Без этого новые правки в deploy.sh
    # (sudoers re-install, cron update, и т.п.) не подхватываются — старая
    # копия в bin/ исполняется как и при первой установке.
    # Каталог bin/ owned by bot:bot, права на запись есть.
    _sync_bin_from_repo()

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


_REPO_BIN_PAIRS = [
    ("/opt/bot-metrics/repo/deploy/deploy.sh",          "/opt/bot-metrics/bin/deploy.sh"),
    ("/opt/bot-metrics/repo/deploy/backup.sh",          "/opt/bot-metrics/bin/backup.sh"),
    ("/opt/bot-metrics/repo/deploy/deploy_listener.py", "/opt/bot-metrics/bin/deploy_listener.py"),
]


def _sync_bin_from_repo() -> list[str]:
    """Копирует bin-скрипты из репы в /opt/bot-metrics/bin/. Возвращает список синканного."""
    synced: list[str] = []
    for src, dst in _REPO_BIN_PAIRS:
        if not os.path.exists(src):
            continue
        try:
            shutil.copy2(src, dst)
            os.chmod(dst, 0o755)
            synced.append(os.path.basename(dst))
        except Exception as e:
            log.warning("bin sync %s -> %s failed: %s", src, dst, e)
    return synced


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
        "🪝 <b>Авто-деплой при push в main</b>\n\n"
        f"URL: <code>{url}</code>\n\n"
        "<b>Способ A — GitHub Actions (рекомендую)</b>\n"
        "1. https://github.com/vladislavsharlovskiy-ops/bot-metrics/settings/secrets/actions\n"
        "2. <b>New repository secret</b>\n"
        "3. Name: <code>DEPLOY_URL</code>\n"
        "4. Value: вставить URL целиком (со скобками <code>https://…/__deploy/…</code>)\n"
        "5. Готово — workflow <code>.github/workflows/deploy.yml</code> сам "
        "будет дёргать его на каждом push в main.\n\n"
        "<b>Способ B — GitHub Webhook (альтернатива)</b>\n"
        "1. https://github.com/vladislavsharlovskiy-ops/bot-metrics/settings/hooks\n"
        "2. <b>Add webhook</b>\n"
        "3. Payload URL: вставить URL\n"
        "4. Content type: <code>application/json</code>\n"
        "5. Events: Just the push event → <b>Add webhook</b>\n\n"
        "Любой из способов — после настройки <code>/redeploy</code> больше не нужен.",
        parse_mode="HTML",
    )


# ─── /addpayment ───────────────────────────────────────────────────

@router.message(Command("addpayment"))
async def cmd_add_payment(message: Message, command: CommandObject) -> None:
    """
    Дозаписать платёж вручную (если webhook упал и оплата не дошла до БД).
    Создаёт Payment с payment_type='unclassified' — потом классифицируется
    через /fixpay (тот же интерактивный flow, что для платежей-сирот от
    webhook'а).

    Форматы:
      1) JSON из письма Prodamus:
         /addpayment {"date":"…","order_id":"…","sum":"5000.00",…}
      2) Pipe-separated (минимум amount|name):
         /addpayment 5000|Бугаева Дарья|+79247330820|email@x.ru|44400242
    """
    if not _is_owner(message):
        return
    arg = (command.args or "").strip()
    if not arg:
        await message.answer(
            "Использование (один из вариантов):\n\n"
            "1) JSON из письма Prodamus:\n"
            "<code>/addpayment {\"sum\":\"5000.00\",\"order_id\":\"44400242\","
            "\"order_num\":\"Имя Клиента\",\"customer_phone\":\"+79...\","
            "\"customer_email\":\"x@y.ru\",\"date\":\"2026-05-04T14:13:21+03:00\"}</code>\n\n"
            "2) Pipe-separated:\n"
            "<code>/addpayment 5000|Имя Клиента|+79...|x@y.ru|44400242</code>",
            parse_mode="HTML",
        )
        return

    parsed = _parse_payment_arg(arg)
    if isinstance(parsed, str):
        await message.answer(f"⚠ {parsed}")
        return

    with get_session() as session:
        # Проверяем дубликат по prodamus_id
        if parsed["prodamus_id"]:
            existing = session.execute(
                select(Payment).where(Payment.prodamus_id == parsed["prodamus_id"])
            ).scalars().first()
            if existing:
                await message.answer(
                    f"⚠ Платёж с prodamus_id={parsed['prodamus_id']} уже есть в БД "
                    f"(#{existing.id}, {existing.amount:.0f} {existing.currency}). "
                    f"Если потерян — удали через дашборд, потом /addpayment."
                )
                return

        payment = Payment(
            prodamus_id=parsed["prodamus_id"] or f"manual-{int(datetime.now().timestamp())}",
            amount=parsed["amount"],
            currency=parsed["currency"],
            paid_at=parsed["paid_at"],
            customer_name=parsed["customer_name"],
            customer_phone=parsed["customer_phone"],
            customer_email=parsed["customer_email"],
            product=parsed.get("product"),
            payment_type="unclassified",
            raw_json=json.dumps({"manual": True, "added_via": "/addpayment", "input": arg[:1000]}),
        )
        session.add(payment)
        session.commit()
        session.refresh(payment)
        pid = payment.id
        amount = payment.amount
        currency = payment.currency
        name = payment.customer_name or "—"

    await message.answer(
        f"✅ Платёж <b>#{pid}</b> создан (unclassified)\n"
        f"💰 {amount:.0f} {currency}\n"
        f"👤 {name}\n\n"
        f"Запусти <code>/fixpay</code> — там предложит привязать к лиду "
        f"или создать нового клиента.",
        parse_mode="HTML",
    )


def _parse_payment_arg(arg: str) -> dict | str:
    """
    Возвращает dict с нормализованными полями платежа или строку с ошибкой.
    """
    # Попытка №1: JSON из письма
    if arg.startswith("{") and arg.endswith("}"):
        try:
            data = json.loads(arg)
            try:
                from prodamus import extract_payment
                p = extract_payment(data)
            except Exception as e:
                return f"Не удалось распарсить JSON через extract_payment: {e}"
            return {
                "prodamus_id": p["prodamus_id"] or None,
                "amount": p["amount"],
                "currency": p["currency"],
                "paid_at": _parse_dt(p["paid_at"]) or datetime.now(),
                "customer_name": p["customer_name"],
                "customer_phone": p["customer_phone"],
                "customer_email": p["customer_email"],
                "product": p.get("product"),
            }
        except json.JSONDecodeError as e:
            return f"Не валидный JSON: {e}"

    # Попытка №2: pipe-separated
    parts = [x.strip() for x in arg.split("|")]
    if len(parts) < 2:
        return "Нужно как минимум amount|name. См. /addpayment без аргументов для примеров."
    try:
        amount = float(parts[0].replace(",", ".").replace(" ", ""))
    except ValueError:
        return f"amount должен быть числом, получил: {parts[0]!r}"
    if amount <= 0:
        return f"amount должен быть > 0, получил: {amount}"
    name = parts[1] or None
    phone = parts[2] if len(parts) > 2 else None
    email = parts[3] if len(parts) > 3 else None
    prodamus_id = parts[4] if len(parts) > 4 else None
    return {
        "prodamus_id": prodamus_id or None,
        "amount": amount,
        "currency": "RUB",
        "paid_at": datetime.now(),
        "customer_name": name,
        "customer_phone": phone or None,
        "customer_email": email or None,
        "product": None,
    }


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # Prodamus шлёт ISO с TZ: "2026-05-04T14:13:21+03:00"
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            return None


# ─── /forcehttps ───────────────────────────────────────────────────

FIX_HTTPS_SCRIPT = "/opt/bot-metrics/repo/tools/fix-https-redirect.sh"


@router.message(Command("forcehttps"))
async def cmd_force_https(message: Message) -> None:
    """
    Принудительно включает редирект http→https и HSTS для дашборд-домена.
    Лечит ситуацию, когда certbot не дописал redirect (на повторных запусках
    или после того, как secure-dashboard.sh затёр SSL-конфиг).
    """
    if not _is_owner(message):
        return
    if not os.path.exists(FIX_HTTPS_SCRIPT):
        await message.answer(
            f"⚠ Не найден {FIX_HTTPS_SCRIPT} — сделай /redeploy сначала."
        )
        return
    await message.answer("🔄 Включаю http→https редирект и HSTS…")
    try:
        result = subprocess.run(
            ["sudo", "/bin/bash", FIX_HTTPS_SCRIPT],
            capture_output=True, text=True, timeout=120,
        )
        tail = (result.stdout + result.stderr)[-1500:]
        if result.returncode == 0:
            await message.answer(
                "✅ Готово.\n\n"
                f"<pre>{tail}</pre>\n\n"
                "Теперь:\n"
                "1. Открой https://dashboard.sharlovsky.pro/ в режиме инкогнито\n"
                "2. Должен быть зелёный замочек 🔒\n"
                "3. После этого можешь и в обычном окне — браузер запомнит HSTS",
                parse_mode="HTML",
            )
        else:
            await message.answer(
                f"⚠ Скрипт упал (код {result.returncode}):\n<pre>{tail}</pre>",
                parse_mode="HTML",
            )
    except subprocess.TimeoutExpired:
        await message.answer("⚠ Скрипт работает дольше 2 минут — что-то не так.")
    except Exception as e:
        await message.answer(f"⚠ Ошибка: {e}")


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
        "<code>/addpayment &lt;…&gt;</code> — дозаписать платёж вручную "
        "(если webhook потерял оплату). Потом /fixpay для классификации\n"
        "<code>/forcehttps</code> — принудительно включить http→https редирект "
        "и HSTS (если в браузере «Не защищено» с валидным сертификатом)\n"
        "<code>/deployurl</code> — показать URL для GitHub-вебхука "
        "(один раз настроишь — авто-деплой при push в main, /redeploy больше не нужен)",
        parse_mode="HTML",
    )
