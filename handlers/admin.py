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
from sqlalchemy import delete as sql_delete, select

from config import OWNER_ID
from db import get_session
from models import Client, Lead, Payment
from stages import CLIENT_CODES

log = logging.getLogger("admin")
router = Router(name="admin")

ENV_FILE = Path("/opt/bot-metrics/.env")
REPO_DIR = "/opt/bot-metrics/repo"
BACKUP_SCRIPT = "/opt/bot-metrics/bin/backup.sh"
DEPLOY_SCRIPT = "/opt/bot-metrics/bin/deploy.sh"
# Многострочный автоответ хранится в отдельном файле, а не в .env, потому
# что systemd EnvironmentFile экранирует/коверкает backslash-последовательности
# (\n превращается в `n` без переноса). Файл — простой UTF-8, любые символы
# и переносы строк хранятся as-is.
AUTO_REPLY_FILE = Path("/opt/bot-metrics/data/business_auto_reply.txt")


def _read_auto_reply() -> str:
    """Возвращает текст автоответа из файла; при отсутствии файла — fallback
    на переменную окружения (legacy, для обратной совместимости с серверами,
    где /setautoreply ещё не запускался после фикса)."""
    try:
        return AUTO_REPLY_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        legacy = os.environ.get("BUSINESS_AUTO_REPLY", "").strip()
        # Старые версии /setautoreply сохраняли с escape \n — пробуем
        # декодировать на случай, если значение было записано до фикса.
        return legacy.replace("\\n", "\n")
    except OSError as e:  # noqa: BLE001
        log.warning("auto-reply file read failed: %s", e)
        return ""


def _write_auto_reply(value: str) -> str | bool:
    """Сохраняет текст автоответа в файл. Возвращает True или строку с ошибкой."""
    try:
        AUTO_REPLY_FILE.parent.mkdir(parents=True, exist_ok=True)
        AUTO_REPLY_FILE.write_text(value, encoding="utf-8")
        return True
    except Exception as e:  # noqa: BLE001
        return str(e)


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
    synced = _sync_bin_from_repo()
    sync_msg = f"📦 Sync bin: {', '.join(synced) if synced else '(nothing copied)'}"

    await message.answer(
        f"{sync_msg}\n\n"
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
    ("/opt/bot-metrics/repo/deploy/poll-deploy.sh",     "/opt/bot-metrics/bin/poll-deploy.sh"),
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


# ─── /test_deploy ──────────────────────────────────────────────────
#
# Диагностика: куда именно теряется DEPLOY_SECRET. Делает три проверки:
#   1) os.environ.get('DEPLOY_SECRET') — что сейчас в env у бота (то, что
#      печатает /deployurl и что должно совпадать с GitHub-секретом).
#   2) Все строки DEPLOY_SECRET= в .env файле (если их несколько — мы их
#      найдём и поймём, какой systemd считает «настоящим»).
#   3) GET на http://127.0.0.1:9876/__deploy/<secret> — если listener вернёт
#      200, значит секрет матчится у него в памяти. Если 404 — значит
#      listener держит другой секрет (мой re-read фикс не сработал, либо
#      сервис не рестартанули). Если timeout/connection refused — listener
#      не запущен.

@router.message(Command("test_deploy"))
async def cmd_test_deploy(message: Message) -> None:
    if not _is_owner(message):
        return

    env_secret = os.environ.get("DEPLOY_SECRET", "").strip()

    file_secrets: list[str] = []
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                if line.startswith("DEPLOY_SECRET="):
                    file_secrets.append(line.split("=", 1)[1].strip().strip('"').strip("'"))
        file_err = ""
    except Exception as e:  # noqa: BLE001
        file_err = str(e)

    candidates: list[tuple[str, str]] = []
    if env_secret:
        candidates.append(("os.environ", env_secret))
    for i, s in enumerate(file_secrets):
        candidates.append((f".env line #{i+1}", s))

    test_results: list[str] = []
    for label, secret in candidates:
        try:
            r = subprocess.run(
                ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
                 "--max-time", "5",
                 f"http://127.0.0.1:9876/__deploy/{secret}"],
                capture_output=True, text=True, timeout=10,
            )
            code = r.stdout.strip() or "?"
            err = (r.stderr or "").strip()[:80]
        except Exception as e:  # noqa: BLE001
            code = "ERR"
            err = str(e)[:80]
        suffix = f" — <code>{err}</code>" if err else ""
        marker = "✅" if code == "200" else "❌"
        test_results.append(f"  {marker} {label}: HTTP <code>{code}</code>{suffix}")

    def short(s: str) -> str:
        return f"{s[:8]}…{s[-4:]} (len={len(s)})" if len(s) > 12 else f"{s} (len={len(s)})"

    lines = [
        "🔬 <b>Self-test deploy listener</b>",
        "",
        f"<b>1) os.environ.get('DEPLOY_SECRET'):</b>",
        f"  <code>{short(env_secret) if env_secret else '(пусто)'}</code>",
        "",
        f"<b>2) Строки DEPLOY_SECRET= в {ENV_FILE}:</b>",
    ]
    if file_err:
        lines.append(f"  ⚠ ошибка чтения: <code>{file_err}</code>")
    elif not file_secrets:
        lines.append("  (не найдено)")
    else:
        for i, s in enumerate(file_secrets, 1):
            lines.append(f"  #{i}: <code>{short(s)}</code>")
        if len(file_secrets) > 1:
            lines.append(f"  ⚠ <b>дублей: {len(file_secrets)}</b> — systemd возьмёт ПОСЛЕДНЮЮ")

    lines.append("")
    lines.append("<b>3) GET напрямую на listener (127.0.0.1:9876):</b>")
    if not candidates:
        lines.append("  (нечего тестировать — секрет нигде не нашёлся)")
    else:
        lines.extend(test_results)

    # 4) GET через HTTPS-nginx с Host: header (тест что 443-блок видит
    #    /__deploy/ location). НЕ делаем POST — POST на правильный путь
    #    запускает deploy.sh, и /test_deploy сам себе триггерит деплой,
    #    бот рестартует посреди обработки и ответ не приходит.
    https_results: list[str] = []
    https_secret = (file_secrets[-1] if file_secrets else env_secret)
    if https_secret:
        tests = [
            ("GET без /",  ""),
            ("GET с /",    "/"),
        ]
        for label, suffix in tests:
            try:
                r = subprocess.run(
                    ["curl", "-sS", "-k", "-o", "/dev/null", "-w", "%{http_code}",
                     "--max-time", "5",
                     "-H", "Host: dashboard.sharlovsky.pro",
                     f"https://127.0.0.1/__deploy/{https_secret}{suffix}"],
                    capture_output=True, text=True, timeout=10,
                )
                code = r.stdout.strip() or "(empty)"
                stderr = (r.stderr or "").strip().replace("<", "&lt;").replace(">", "&gt;")
                stderr_short = stderr[:120]
            except Exception as e:  # noqa: BLE001
                code = "ERR"
                stderr_short = str(e)[:120]
            marker = "✅" if code == "200" else "❌"
            err_part = f"\n     <i>stderr:</i> <code>{stderr_short}</code>" if stderr_short and code != "200" else ""
            https_results.append(f"  {marker} {label}: HTTP <code>{code}</code>{err_part}")

    lines.append("")
    lines.append("<b>4) Через HTTPS+nginx (как Actions):</b>")
    if https_secret:
        lines.extend(https_results)
    else:
        lines.append("  (нет секрета для теста)")

    # 5) Реальный публичный URL — точная реплика Actions (DNS, любой CF/CDN
    #    в DNS, реальный TLS). Это последний шаг, чтобы понять режет ли
    #    кто-то снаружи, до nginx.
    lines.append("")
    lines.append("<b>5) Через DNS+интернет (как Actions):</b>")
    if https_secret:
        try:
            r = subprocess.run(
                ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}|%{remote_ip}",
                 "--max-time", "10",
                 f"https://dashboard.sharlovsky.pro/__deploy/{https_secret}"],
                capture_output=True, text=True, timeout=15,
            )
            out = (r.stdout or "").strip()
            stderr = (r.stderr or "").strip()
            if "|" in out:
                code, remote_ip = out.split("|", 1)
            else:
                code, remote_ip = out or "(empty)", ""
            marker = "✅" if code == "200" else "❌"
            lines.append(f"  {marker} GET <code>https://dashboard.sharlovsky.pro/__deploy/&lt;secret&gt;</code>")
            lines.append(f"     HTTP <code>{code}</code>, remote IP: <code>{remote_ip or '?'}</code>")
            if stderr and code != "200":
                stderr_short = stderr[:200].replace("<", "&lt;").replace(">", "&gt;")
                lines.append(f"     <i>stderr:</i> <code>{stderr_short}</code>")
        except Exception as e:  # noqa: BLE001
            lines.append(f"  ❌ ERR: <code>{str(e)[:120]}</code>")

    lines.extend([
        "",
        "<i>Расшифровка кодов:</i>",
        "✅ 200 — всё ок",
        "❌ 401 — nginx 443-блок не знает <code>/__deploy/</code>, фолбэк на basic auth",
        "❌ 403 — listener получил запрос, но путь не совпал (trailing /, lf?)",
        "❌ 404 — listener другой секрет, либо location вернул not found",
        "",
        "<b>Если 4 (через 127.0.0.1) = 200, а 5 (через DNS) = 403</b> → "
        "между интернетом и nginx стоит CF/CDN/firewall и он режет запрос. "
        "Решение: в /deployurl выбрать вариант через прямой IP+HTTP.",
    ])

    await message.answer("\n".join(lines), parse_mode="HTML")


# ─── /diag_nginx ───────────────────────────────────────────────────
#
# Дампит /etc/nginx/sites-available/bot-metrics.conf чанками по 3500 chars,
# чтобы не упереться в 4096-лимит TG-сообщения (у /diag это и происходит,
# поэтому 443-блок до владельца не доезжает). Нужен один раз чтобы
# понять, есть ли там location /__deploy/ и куда он проксирует.

@router.message(Command("diag_nginx"))
async def cmd_diag_nginx(message: Message) -> None:
    if not _is_owner(message):
        return
    nginx_conf = Path("/etc/nginx/sites-available/bot-metrics.conf")
    try:
        content = nginx_conf.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        await message.answer(f"⚠ Не могу прочитать {nginx_conf}: <code>{e!s}</code>", parse_mode="HTML")
        return
    chunk_size = 3500
    parts = [content[i:i + chunk_size] for i in range(0, len(content), chunk_size)]
    if not parts:
        await message.answer("⚠ Файл пустой", parse_mode="HTML")
        return
    for i, chunk in enumerate(parts, 1):
        escaped = chunk.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        await message.answer(
            f"<b>📄 nginx-conf {i}/{len(parts)}</b> ({len(chunk)} chars)\n<pre>{escaped}</pre>",
            parse_mode="HTML",
        )


# ─── /addlead ──────────────────────────────────────────────────────
#
# Создать лида руками — нужно когда клиент написал в business-чат, но
# обработчик его пропустил (например, фраза не подошла под has_lead_intro,
# или клиент пришёл по нестандартному скрипту). После /addlead лид
# попадает на дашборд и в обычную воронку.
#
# Формат: pipe-separated, обязательно как минимум name.
#   /addlead Имя Фамилия
#   /addlead Имя Фамилия | instagram
#   /addlead Имя Фамилия | instagram | @username | текст_заявки

@router.message(Command("addlead"))
async def cmd_add_lead(message: Message, command: CommandObject) -> None:
    if not _is_owner(message):
        return
    arg = (command.args or "").strip()
    if not arg:
        await message.answer(
            "Использование:\n"
            "<code>/addlead Имя Фамилия</code>\n"
            "<code>/addlead Имя Фамилия | instagram</code>\n"
            "<code>/addlead Имя Фамилия | instagram | @username | текст заявки</code>\n\n"
            "Источники: instagram, youtube, telegram, tiktok, rutube, vk, unknown",
            parse_mode="HTML",
        )
        return
    parts = [p.strip() for p in arg.split("|")]
    name = parts[0] or None
    source = (parts[1].lower() if len(parts) > 1 and parts[1] else "unknown")
    username = (parts[2] if len(parts) > 2 else None) or None
    if username:
        username = username if username.startswith("@") else f"@{username}"
    request_text = (parts[3] if len(parts) > 3 else None) or None

    from stages import LEAD_NEW, SOURCE_TITLES
    from models import Lead, StageHistory
    if source not in SOURCE_TITLES:
        await message.answer(
            f"⚠ source <code>{source}</code> неизвестен. "
            f"Допустимые: {', '.join(SOURCE_TITLES.keys())}",
            parse_mode="HTML",
        )
        return

    with get_session() as session:
        lead = Lead(
            name=name,
            username=username,
            source=source,
            request=request_text,
            stage=LEAD_NEW,
        )
        session.add(lead)
        session.flush()
        session.add(StageHistory(lead_id=lead.id, stage=LEAD_NEW))
        session.commit()
        session.refresh(lead)
        lead_id = lead.id

    await message.answer(
        f"✅ Лид <b>#{lead_id}</b> создан\n"
        f"👤 {name or '—'}\n"
        f"📥 {SOURCE_TITLES[source]}\n"
        f"📝 {request_text or '—'}\n\n"
        f"Появится на дашборде, можно обрабатывать обычной воронкой.",
        parse_mode="HTML",
    )

    # Best-effort sync в Sheets, как делает business-handler
    try:
        from sheets import sync_lead
        sync_lead(lead_id)
    except Exception as e:  # noqa: BLE001
        log.warning("sheets sync skipped for /addlead %s: %s", lead_id, e)


# ─── /diag_poll ────────────────────────────────────────────────────
#
# Диагностика poll-deploy. Показывает:
#   1) local HEAD в /opt/bot-metrics/repo vs origin/main (живой git fetch)
#   2) crontab юзера bot — есть ли там строка с poll-deploy.sh
#   3) tail /opt/bot-metrics/logs/poll-deploy.log
#   4) есть ли сам /opt/bot-metrics/bin/poll-deploy.sh
# По выводу сразу видно, на каком из этапов сломалось.

@router.message(Command("diag_poll"))
async def cmd_diag_poll(message: Message) -> None:
    if not _is_owner(message):
        return

    lines: list[str] = ["🔬 <b>Poll-deploy status</b>", ""]

    # 1. HEAD сравнение (с принудительным fetch, чтобы видеть актуальное origin)
    try:
        local_head = subprocess.check_output(
            ["git", "-C", REPO_DIR, "log", "-1", "--format=%h %s"],
            text=True, timeout=5,
        ).strip()
    except Exception as e:  # noqa: BLE001
        local_head = f"ERR: {e!s:.80}"
    try:
        subprocess.run(
            ["git", "-C", REPO_DIR, "fetch", "--quiet", "origin", "main"],
            check=False, timeout=15, capture_output=True,
        )
        origin_head = subprocess.check_output(
            ["git", "-C", REPO_DIR, "log", "-1", "--format=%h %s", "origin/main"],
            text=True, timeout=5,
        ).strip()
    except Exception as e:  # noqa: BLE001
        origin_head = f"ERR: {e!s:.80}"
    lines.append("<b>1) HEAD-сравнение</b>")
    lines.append(f"  local: <code>{local_head}</code>")
    lines.append(f"  origin/main: <code>{origin_head}</code>")
    if local_head == origin_head and "ERR" not in local_head:
        lines.append("  ✅ совпадают")
    else:
        lines.append("  ⚠ расходятся — poll-deploy должен был догнать")

    # 2. Crontab
    lines.append("")
    lines.append("<b>2) Crontab юзера bot</b>")
    try:
        crontab = subprocess.check_output(
            ["crontab", "-l"], text=True, timeout=5,
        )
        poll_lines = [l for l in crontab.splitlines() if "poll-deploy" in l]
        if poll_lines:
            for l in poll_lines:
                lines.append(f"  ✅ <code>{l.replace('<', '&lt;').replace('>', '&gt;')}</code>")
        else:
            lines.append("  ❌ строки с poll-deploy НЕТ — нужен /redeploy")
    except Exception as e:  # noqa: BLE001
        lines.append(f"  ⚠ crontab -l failed: <code>{e!s:.80}</code>")

    # 3. poll-deploy.log tail
    lines.append("")
    lines.append("<b>3) poll-deploy.log (последние 30 строк):</b>")
    log_file = Path("/opt/bot-metrics/logs/poll-deploy.log")
    if not log_file.exists():
        lines.append("  ⚠ файла нет — поллер ни разу не находил новых коммитов")
        lines.append("  (норм если main не менялся; ненорм если был мердж >2 мин назад)")
    else:
        try:
            tail = log_file.read_text(encoding="utf-8").splitlines()[-30:]
            if tail:
                escaped = "\n".join(tail).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                lines.append(f"<pre>{escaped[:2500]}</pre>")
            else:
                lines.append("  (файл пустой)")
        except Exception as e:  # noqa: BLE001
            lines.append(f"  ⚠ <code>{e!s}</code>")

    # 4. poll-deploy.sh в bin?
    lines.append("")
    lines.append("<b>4) /opt/bot-metrics/bin/poll-deploy.sh</b>")
    pd = Path("/opt/bot-metrics/bin/poll-deploy.sh")
    if pd.exists():
        mtime = datetime.fromtimestamp(pd.stat().st_mtime)
        size = pd.stat().st_size
        lines.append(f"  ✅ есть (mtime {mtime:%Y-%m-%d %H:%M:%S}, {size} байт)")
    else:
        lines.append("  ❌ файла нет — нужен /redeploy чтобы синкнуть из репы")

    await message.answer("\n".join(lines)[:4000], parse_mode="HTML")


# ─── /restart_listener ─────────────────────────────────────────────
#
# Перезапускает сервис bot-metrics-deploy.service. deploy.sh специально его
# НЕ рестартует (иначе бы оборвал самого себя — listener-же его и запустил),
# поэтому новые версии deploy_listener.py подхватываются только через эту
# команду или ручной systemctl restart на сервере.
#
# sudoers (deploy/sudoers.d-bot-metrics) уже разрешает `bot` юзеру запускать
# `systemctl restart bot-metrics-deploy` без пароля.

@router.message(Command("restart_listener"))
async def cmd_restart_listener(message: Message) -> None:
    if not _is_owner(message):
        return
    try:
        r = subprocess.run(
            ["sudo", "/bin/systemctl", "restart", "bot-metrics-deploy"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:  # noqa: BLE001
        await message.answer(f"⚠ Не удалось рестартануть: <code>{e!s}</code>", parse_mode="HTML")
        return
    if r.returncode == 0:
        await message.answer(
            "🔁 <code>bot-metrics-deploy</code> рестартанут.\n\n"
            "Теперь listener читает свежий <code>DEPLOY_SECRET</code> из .env.\n"
            "Можно проверить автодеплой через GitHub Actions: "
            "https://github.com/vladislavsharlovskiy-ops/bot-metrics/actions → Re-run jobs",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    else:
        await message.answer(
            f"⚠ systemctl exit={r.returncode}\n<code>{(r.stderr or r.stdout)[:500]}</code>",
            parse_mode="HTML",
        )


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


# ─── /setprodamus_sub_key — подписочный продукт ────────────────────
#
# Подписочный продукт Prodamus имеет ОТДЕЛЬНЫЙ secret key (раздел
# «Подписки → Общие настройки»). webhook'и с автосписаний по подписке
# подписываются этим ключом, а наш основной PRODAMUS_SECRET_KEY его
# не знает → bad signature → платёж теряется.
#
# Эта команда кладёт подписочный секрет в PRODAMUS_SUBSCRIPTION_SECRET_KEY
# и рестартует web — prodamus.verify() теперь принимает webhook'и от
# обоих продуктов (одноразовый + подписка).

@router.message(Command("setprodamus_sub_key"))
async def cmd_set_prodamus_sub_key(message: Message, command: CommandObject) -> None:
    if not _is_owner(message):
        return
    arg = (command.args or "").strip()
    if not arg or len(arg) < 10:
        await message.answer(
            "Использование: <code>/setprodamus_sub_key твой_подписочный_secret</code>\n\n"
            "Где взять: panel.prodamus.ru → <b>Подписки</b> → "
            "<b>Общие настройки</b> → секретный ключ.\n\n"
            "Это <i>отдельный</i> ключ от основного (для одноразовых "
            "продуктов). Webhook'и с автосписаний подписки подписываются им.\n\n"
            "⚠ После выполнения сообщение удали — ключ виден в истории чата.",
            parse_mode="HTML",
        )
        return

    persisted = _persist_env(ENV_FILE, "PRODAMUS_SUBSCRIPTION_SECRET_KEY", arg)
    if persisted is not True:
        await message.answer(f"⚠ Не удалось записать в .env: {persisted}")
        return

    await message.answer(
        "🔄 Подписочный ключ записан. Перезапускаю web-сервис…"
    )
    try:
        r = subprocess.run(
            ["sudo", "/bin/systemctl", "restart", "bot-metrics-web"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            await message.answer(
                "✅ Готово. Webhook /webhook/prodamus теперь принимает оплаты "
                "и от одноразовых, и от подписочных продуктов.\n\n"
                "Дальше укажи в Prodamus URL для уведомлений о подписке:\n"
                "<code>https://dashboard.sharlovsky.pro/webhook/prodamus</code>\n\n"
                "⚠ Удали сообщение с ключом.",
                parse_mode="HTML",
            )
        else:
            await message.answer(
                f"⚠ Restart не удался:\n<pre>{r.stderr[:1000]}</pre>",
                parse_mode="HTML",
            )
    except Exception as e:  # noqa: BLE001
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

    # Альтернативный URL — напрямую через публичный IP по HTTP. Минует DNS,
    # Cloudflare/CDN/WAF (если там что-то стоит) и попадает прямо в
    # nginx default_server (порт 80, server_name `_`), у которого есть
    # location /__deploy/. Использовать если основной URL ловит 403/blank.
    pubip = ""
    try:
        r = subprocess.run(
            ["curl", "-sS", "-4", "--max-time", "3", "ifconfig.me"],
            capture_output=True, text=True, timeout=5,
        )
        pubip = r.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    alt_url = f"http://{pubip}/__deploy/{secret}" if pubip else ""

    text = (
        "🪝 <b>Авто-деплой при push в main</b>\n\n"
        f"URL (через домен, HTTPS):\n<code>{url}</code>\n\n"
    )
    if alt_url:
        text += (
            f"⚙ <b>Альтернатива через прямой IP (HTTP)</b>:\n"
            f"<code>{alt_url}</code>\n"
            f"Использовать если основной URL ловит 403 — частая причина "
            f"Cloudflare/CDN перед сервером, который не пускает Actions. "
            f"IP-форма обходит DNS целиком.\n\n"
        )
    text += (
        "<b>Способ A — GitHub Actions (рекомендую)</b>\n"
        "1. https://github.com/vladislavsharlovskiy-ops/bot-metrics/settings/secrets/actions\n"
        "2. <b>New repository secret</b>\n"
        "3. Name: <code>DEPLOY_URL</code>\n"
        "4. Value: один из URL выше (со скобками)\n"
        "5. Готово — workflow <code>.github/workflows/deploy.yml</code> сам "
        "будет дёргать его на каждом push в main.\n\n"
        "<b>Способ B — GitHub Webhook (альтернатива)</b>\n"
        "1. https://github.com/vladislavsharlovskiy-ops/bot-metrics/settings/hooks\n"
        "2. <b>Add webhook</b>\n"
        "3. Payload URL: вставить URL\n"
        "4. Content type: <code>application/json</code>\n"
        "5. Events: Just the push event → <b>Add webhook</b>\n\n"
        "Любой из способов — после настройки <code>/redeploy</code> больше не нужен."
    )
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)


# ─── /version ──────────────────────────────────────────────────────
#
# Показывает что сейчас задеплоено: SHA + заголовок последнего коммита
# в репе на сервере, плюс mtime скриптов в bin/. Нужно чтобы быстро
# проверять «доехал ли мой фикс до прода» без SSH — особенно после
# настройки авто-деплоя через GitHub Actions.

@router.message(Command("version"))
async def cmd_version(message: Message) -> None:
    if not _is_owner(message):
        return
    try:
        head = subprocess.check_output(
            ["git", "-C", REPO_DIR, "log", "-1", "--format=%h %ci%n%s"],
            stderr=subprocess.STDOUT,
            timeout=5,
            text=True,
        ).strip()
    except Exception as e:  # noqa: BLE001
        await message.answer(f"⚠ git log упал: <code>{e!s}</code>", parse_mode="HTML")
        return

    bin_lines: list[str] = []
    for path in (BACKUP_SCRIPT, DEPLOY_SCRIPT, "/opt/bot-metrics/bin/deploy_listener.py"):
        p = Path(path)
        if p.exists():
            mtime = datetime.fromtimestamp(p.stat().st_mtime)
            bin_lines.append(f"  {p.name}: <code>{mtime:%Y-%m-%d %H:%M:%S}</code>")
        else:
            bin_lines.append(f"  {p.name}: ❌ нет")

    await message.answer(
        "📦 <b>Сейчас задеплоено</b>\n"
        f"<code>{head}</code>\n\n"
        "<b>bin/ mtime</b>\n" + "\n".join(bin_lines),
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
    # Убедимся, что скрипт исполняемый (после git checkout exec-bit мог
    # потеряться, если коммитили без него)
    try:
        os.chmod(FIX_HTTPS_SCRIPT, 0o755)
    except Exception:
        pass
    await message.answer("🔄 Включаю http→https редирект и HSTS…")
    try:
        # Запускаем скрипт напрямую (по shebang #!/usr/bin/env bash) — без
        # явного /bin/bash, чтобы sudoers-entry не зависел от пути bash'а
        # (Ubuntu 24.04+ симлинкует /bin/bash → /usr/bin/bash, и sudo
        # канонизирует путь — старая entry с /bin/bash не матчится).
        result = subprocess.run(
            ["sudo", FIX_HTTPS_SCRIPT],
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


# ─── /diag ─────────────────────────────────────────────────────────

@router.message(Command("diag"))
async def cmd_diag(message: Message) -> None:
    """
    Диагностика состояния сервера: версии bin-скриптов, sudo permissions,
    наличие сертификата, базовая инфа о nginx-конфиге.
    Только для ловли багов, ничего не меняет.
    """
    if not _is_owner(message):
        return

    lines: list[str] = ["🔍 <b>Диагностика</b>\n"]

    # 1. Версия admin.py — берём commit hash из .git
    try:
        commit = subprocess.run(
            ["git", "-C", "/opt/bot-metrics/repo", "log", "-1", "--pretty=%h %s"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        lines.append(f"📍 repo HEAD: <code>{commit}</code>")
    except Exception as e:
        lines.append(f"📍 repo HEAD: ⚠ {e}")

    # 2. Хеши deploy.sh — bin/ vs repo/, чтоб увидеть, синканулись или нет
    import hashlib
    def _md5(path: str) -> str:
        try:
            with open(path, "rb") as f:
                return hashlib.md5(f.read()).hexdigest()[:8]
        except Exception:
            return "—"
    bin_md5 = _md5("/opt/bot-metrics/bin/deploy.sh")
    repo_md5 = _md5("/opt/bot-metrics/repo/deploy/deploy.sh")
    same = "✅ совпадают" if bin_md5 == repo_md5 and bin_md5 != "—" else "❌ РАЗНЫЕ"
    lines.append(f"📦 deploy.sh md5: bin=<code>{bin_md5}</code> repo=<code>{repo_md5}</code> {same}")

    # 3. sudo -ln (показывает что bot может через sudo без пароля)
    try:
        sudo_l = subprocess.run(
            ["sudo", "-ln"], capture_output=True, text=True, timeout=5,
        ).stdout
        # отфильтруем чтобы было читаемо
        sudo_lines = [l.strip() for l in sudo_l.splitlines()
                      if "NOPASSWD" in l or "ALL =" in l]
        if sudo_lines:
            lines.append("\n🔑 <b>sudo permissions (от bot):</b>")
            for l in sudo_lines:
                lines.append(f"  <code>{l}</code>")
        else:
            lines.append("🔑 sudo permissions: пусто или не получилось прочитать")
    except Exception as e:
        lines.append(f"🔑 sudo -ln: ⚠ {e}")

    # 4. Сертификат — проверяем через curl https (см. шаг 7), а не путь
    #    /etc/letsencrypt/live/, потому что папка owned by root 0700, бот
    #    физически не может её прочитать. False-negative «нет» вводил в
    #    заблуждение, хотя cert на самом деле был.

    # 5. fix-https-redirect.sh — есть и исполняемый?
    fixscript = FIX_HTTPS_SCRIPT
    if os.path.exists(fixscript):
        executable = os.access(fixscript, os.X_OK)
        lines.append(
            f"🛠 fix-https-redirect.sh: ✅ есть, "
            f"{'исполняемый ✅' if executable else 'НЕ исполняемый ❌'}"
        )
    else:
        lines.append(f"🛠 fix-https-redirect.sh: ❌ нет ({fixscript})")

    # 6. Содержимое nginx-конфига — критично, чтобы понять что там реально
    nginx_conf = "/etc/nginx/sites-available/bot-metrics.conf"
    try:
        with open(nginx_conf) as f:
            content = f.read()
        # Telegram-friendly: показываем первые ~1500 символов
        snippet = content[:1500]
        if len(content) > 1500:
            snippet += f"\n…(+{len(content)-1500} chars)"
        lines.append(f"\n📄 <b>{nginx_conf}</b>:\n<pre>{snippet}</pre>")
    except Exception as e:
        lines.append(f"\n📄 nginx-конфиг: ⚠ {e}")

    # 7. Что отвечает nginx на HTTP и HTTPS — критично для диагностики
    #    Используем Host: header, чтобы попасть на правильный server-блок.
    for proto, port in [("http", 80), ("https", 443)]:
        try:
            r = subprocess.run(
                [
                    "curl", "-sI", "-k", "--max-time", "5",
                    "-H", "Host: dashboard.sharlovsky.pro",
                    f"{proto}://127.0.0.1:{port}/",
                ],
                capture_output=True, text=True, timeout=10,
            )
            head = r.stdout[:500] if r.stdout else f"(stderr: {r.stderr[:300]})"
            lines.append(f"\n🌐 <b>curl {proto}://localhost</b>:\n<pre>{head}</pre>")
        except Exception as e:
            lines.append(f"\n🌐 curl {proto} failed: {e}")

    # Telegram message limit ~4096 — собираем
    text = "\n".join(lines)[:4000]
    await message.answer(text, parse_mode="HTML")


# ─── /testprodamus ─────────────────────────────────────────────────

@router.message(Command("testprodamus"))
async def cmd_test_prodamus(message: Message) -> None:
    """
    Проверяет, что PRODAMUS_SECRET_KEY на сервере совпадает с тем, чем
    Prodamus подписывает webhook'и. Шлёт тестовый POST на локальный
    /webhook/prodamus с payment_status='pending' (нерабочий статус —
    обработчик его игнорирует, в БД ничего не запишется).

    HTTP 200  → ключ совпадает, реальные оплаты будут записываться сами
    HTTP 403  → ключ не совпадает, нужен /setprodamuskey
    """
    if not _is_owner(message):
        return

    key = os.environ.get("PRODAMUS_SECRET_KEY", "").strip()
    if not key:
        await message.answer(
            "⚠ <code>PRODAMUS_SECRET_KEY</code> в .env пустой.\n"
            "Сначала задай: <code>/setprodamuskey &lt;ключ&gt;</code>",
            parse_mode="HTML",
        )
        return

    # Импорт лениво — модуль prodamus тащит свои зависимости
    try:
        from prodamus import _build_signature, parse_form_to_dict
        import urllib.parse
    except Exception as e:
        await message.answer(f"⚠ Не получается импортировать prodamus.py: {e}")
        return

    test_data = {
        "date": datetime.now().isoformat(timespec="seconds"),
        "order_id": "diag-" + str(int(datetime.now().timestamp())),
        "order_num": "Diagnostic Test (testprodamus)",
        "sum": "1.00",
        "currency": "rub",
        "customer_phone": "+70000000000",
        "customer_email": "test@diag.local",
        # Сознательно НЕ "success" — обработчик такие пропускает, в БД не пишет
        "payment_status": "pending",
        "payment_status_description": "Diagnostic test, ignore",
    }

    nested = parse_form_to_dict(test_data)
    sig = _build_signature(nested, key)
    form_with_sig = {**test_data, "signature": sig}
    form_data = urllib.parse.urlencode(form_with_sig)

    await message.answer(
        f"🔄 Шлю тестовый webhook со свежей подписью "
        f"(ключ {len(key)} симв.)…"
    )

    try:
        result = subprocess.run(
            [
                "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                "--max-time", "10",
                "-X", "POST",
                "-H", "Content-Type: application/x-www-form-urlencoded",
                "--data", form_data,
                "http://127.0.0.1:8765/webhook/prodamus",
            ],
            capture_output=True, text=True, timeout=15,
        )
        http_code = result.stdout.strip()

        if http_code == "200":
            await message.answer(
                "✅ <b>Webhook работает!</b>\n\n"
                "Тестовый POST с правильной HMAC-SHA256 подписью → <b>HTTP 200</b>.\n\n"
                "Это значит:\n"
                "• <code>PRODAMUS_SECRET_KEY</code> в .env совпадает с тем, что "
                "Prodamus использует для подписи\n"
                "• Реальные оплаты с <code>payment_status=success</code> теперь "
                "запишутся в БД сами и появятся в дашборде\n\n"
                "В БД ничего не добавилось — тестовый <code>payment_status=pending</code> "
                "обработчик пропускает.",
                parse_mode="HTML",
            )
        elif http_code == "403":
            await message.answer(
                "❌ <b>Webhook валится с HTTP 403 (bad signature)</b>\n\n"
                f"<code>PRODAMUS_SECRET_KEY</code> ({len(key)} симв.) в .env есть, "
                "но HMAC не совпадает с тем, что вычисляется. Скорее всего:\n"
                "• в ключе при копировании затесался пробел/перенос строки\n"
                "• ключ не от того кабинета Prodamus\n\n"
                "Перезайди в <code>panel.prodamus.ru</code> → Настройки → "
                "Защита от поддельных уведомлений → скопируй ключ ещё раз "
                "(аккуратно, без пробелов) → <code>/setprodamuskey &lt;ключ&gt;</code>",
                parse_mode="HTML",
            )
        else:
            await message.answer(
                f"⚠ Webhook вернул HTTP <b>{http_code}</b> — не 200 и не 403.\n"
                "Что-то нестандартное. Пришли скрин, разберусь.",
                parse_mode="HTML",
            )
    except Exception as e:
        await message.answer(f"⚠ Ошибка теста: {e}")


# ─── /setautoreply, /getautoreply ──────────────────────────────────

@router.message(Command("getautoreply"))
async def cmd_get_auto_reply(message: Message) -> None:
    """Показывает текущий текст автоответа на новые заявки в Business-чатах."""
    if not _is_owner(message):
        return
    current = _read_auto_reply()
    if current:
        await message.answer(
            f"📣 <b>Текущий автоответ на новые заявки:</b>\n\n"
            f"<i>{current}</i>\n\n"
            "Поменять: <code>/setautoreply &lt;текст&gt;</code>\n"
            "Выключить: <code>/setautoreply off</code>",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "❌ Автоответ выключен.\n\n"
            "Включить: <code>/setautoreply &lt;текст&gt;</code>\n"
            "Например: <code>/setautoreply Здравствуйте! Спасибо за заявку, "
            "скоро свяжусь с вами лично.</code>",
            parse_mode="HTML",
        )


@router.message(Command("setautoreply"))
async def cmd_set_auto_reply(message: Message, command: CommandObject) -> None:
    """
    Задаёт текст автоответа на первое сообщение нового лида в Telegram
    Business. Для работы у бота должно быть разрешение «Ответы на
    сообщения» в настройках Telegram Business.

    /setautoreply Здравствуйте! …    — задать текст
    /setautoreply off                — выключить автоответ
    /getautoreply                    — показать текущий
    """
    if not _is_owner(message):
        return
    arg = (command.args or "").strip()
    if not arg:
        # Без аргументов — показываем текущий
        await cmd_get_auto_reply(message)
        return

    if arg.lower() in ("off", "выкл", "выключить", "false", "—", "-"):
        new_value = ""
    else:
        new_value = arg

    # Сохраняем в отдельный файл (a не в .env). systemd EnvironmentFile
    # коверкает backslash-последовательности и обрезает на первом реальном
    # newline — раньше это давало то «Добрый день!» без остальных строк,
    # то текст с буквами `n` вместо переносов. Текстовый файл — простой
    # UTF-8, любые символы и переносы хранятся as-is.
    persisted = _write_auto_reply(new_value)
    if persisted is not True:
        await message.answer(
            f"⚠ Не удалось сохранить автоответ: {persisted}",
        )
        return

    # На всякий случай чистим устаревшую запись из .env, чтобы legacy-fallback
    # не подсовывал старое значение если файл вдруг исчезнет.
    _persist_env(ENV_FILE, "BUSINESS_AUTO_REPLY", "")
    os.environ.pop("BUSINESS_AUTO_REPLY", None)

    if new_value:
        await message.answer(
            f"✅ <b>Автоответ обновлён.</b>\n\n"
            f"<i>{new_value}</i>\n\n"
            "Теперь каждый раз, когда новый клиент пишет «Я из …», "
            "бот ответит ему этим текстом от твоего имени.\n\n"
            "⚠ Проверь, что у бота включено «Ответы на сообщения» "
            "в настройках Telegram Business.",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "✅ Автоответ выключен. Новые лиды получают только тебя самого, без бота."
        )


# ─── /purge_orphans ────────────────────────────────────────────────

@router.message(Command("purge_orphans"))
async def cmd_purge_orphans(message: Message) -> None:
    """Подчистить осиротевшие Clients/Payments — БЕЗОПАСНАЯ версия.

    Раньше эта команда была опасной: удаляла «дважды осиротевшие» Payment
    с payment_type IN ('first','repeat'), а также cascade-удаляла Payments
    через orphan-Client. Из-за этого утром потерялись оплаты Александра
    и Никиты — они были классифицированы (first), но при удалении лида
    через дашборд Client тоже удалялся, Payment оставался без привязок,
    и /purge_orphans его «подчищал».

    Теперь правила:
      1. Classified Payment (first/repeat) — НИКОГДА не удаляем. Это
         историческая выручка. Если осиротел — просто остаётся в БД.
      2. Client удаляем только если у него НЕТ classified-оплат
         (то есть все его платежи unclassified или ignored, либо нет
         вообще). Cascade в этом случае безопасен — теряем только мусор.
      3. Sessions удаляются только в составе удалённого client
         (через ORM cascade).

    /purge_orphans теперь умеет починить ситуацию, не ломая выручку.
    """
    if not _is_owner(message):
        return
    with get_session() as session:
        orphan_clients = session.execute(
            select(Client).where(Client.lead_id.is_(None))
        ).scalars().all()

        deleted_clients = 0
        deleted_unclassified_payments = 0
        deleted_sessions = 0
        kept_clients_with_classified = 0
        kept_classified_payments = 0

        for c in orphan_clients:
            classified = [p for p in c.payments if p.payment_type in ("first", "repeat")]
            if classified:
                # Сохраняем — это выручка. Client остаётся как «orphan client
                # with revenue»: лида у него нет, но история оплат живая.
                kept_clients_with_classified += 1
                kept_classified_payments += len(classified)
                continue
            # Все платежи у клиента — unclassified/ignored, либо нет вовсе.
            # Cascade-delete безопасен: ничего ценного нет.
            deleted_unclassified_payments += len(c.payments)
            deleted_sessions += len(c.sessions)
            session.delete(c)
            deleted_clients += 1

        # «Дважды осиротевших» classified Payments (нет лида И нет клиента)
        # — НЕ удаляем больше. Просто считаем для отчёта.
        bare_classified = session.execute(
            select(Payment)
            .where(Payment.lead_id.is_(None))
            .where(Payment.client_id.is_(None))
            .where(Payment.payment_type.in_(("first", "repeat")))
        ).scalars().all()
        bare_classified_count = len(bare_classified)
        bare_total_amount = sum(p.amount or 0 for p in bare_classified)

        session.commit()

    if (deleted_clients == 0 and deleted_unclassified_payments == 0
            and deleted_sessions == 0 and kept_clients_with_classified == 0
            and bare_classified_count == 0):
        await message.answer(
            "🧹 Ничего лишнего не нашёл — БД чистая.",
            parse_mode="HTML",
        )
        return

    lines = ["🧹 <b>Purge orphans</b> (безопасный режим)\n"]
    if deleted_clients or deleted_unclassified_payments or deleted_sessions:
        lines.append("<b>Удалили (только мусор):</b>")
        lines.append(f"  • orphan-клиентов без classified-оплат: {deleted_clients}")
        lines.append(f"  • их unclassified/ignored платежей: {deleted_unclassified_payments}")
        lines.append(f"  • их сессий повтора: {deleted_sessions}")
    if kept_clients_with_classified or bare_classified_count:
        if lines[-1] != "":
            lines.append("")
        lines.append("<b>Сохранили</b> (это выручка, удалять нельзя):")
        if kept_clients_with_classified:
            lines.append(
                f"  • orphan-клиентов с classified-оплатами: "
                f"{kept_clients_with_classified} (платежей: {kept_classified_payments})"
            )
        if bare_classified_count:
            lines.append(
                f"  • bare classified-оплат (нет ни лида, ни клиента): "
                f"{bare_classified_count} на сумму {bare_total_amount:.0f} ₽"
            )

    await message.answer("\n".join(lines), parse_mode="HTML")


# ─── /diag_clients ─────────────────────────────────────────────────

@router.message(Command("diag_clients"))
async def cmd_diag_clients(message: Message) -> None:
    """Read-only диагностика расхождения между «Первичных оплат: N»
    в карточке дашборда и «Клиенты: M» в нижнем блоке.

    Карточка считает по таблице Payment (классифицированные платежи),
    нижний блок — по Lead.stage IN CLIENT_CODES. Расхождение появляется
    если у клиента есть оплата (Payment), но его Lead удалён или
    переведён в другой этап (LOST / IGNORING / актив). Эта команда
    ничего не меняет, только показывает что и где.
    """
    if not _is_owner(message):
        return
    with get_session() as session:
        client_payments = session.execute(
            select(Payment, Client, Lead)
            .outerjoin(Client, Client.id == Payment.client_id)
            .outerjoin(Lead, Lead.id == Client.lead_id)
            .where(Payment.payment_type.in_(("first", "repeat")))
            .where(Payment.client_id.is_not(None))
        ).all()

        lead_only_payments = session.execute(
            select(Payment, Lead)
            .outerjoin(Lead, Lead.id == Payment.lead_id)
            .where(Payment.payment_type.in_(("first", "repeat")))
            .where(Payment.client_id.is_(None))
        ).all()

    problems: list[str] = []
    for pay, client, lead in client_payments:
        if client is None:
            problems.append(
                f"💥 Payment #{pay.id} ({pay.amount:.0f}₽, {pay.customer_name or '?'}): "
                f"client_id={pay.client_id}, но клиента в БД нет"
            )
            continue
        if lead is None:
            problems.append(
                f"⚠️ Client «{client.name or client.id}» (Payment #{pay.id}, "
                f"{pay.amount:.0f}₽): Client.lead_id={client.lead_id}, "
                f"но такого Lead в БД нет — лид был удалён, клиент остался"
            )
            continue
        if lead.stage not in CLIENT_CODES:
            problems.append(
                f"⚠️ «{lead.name or lead.username or lead.id}» (Lead #{lead.id}, "
                f"Payment {pay.amount:.0f}₽): stage=<code>{lead.stage}</code> — "
                f"не входит в CLIENT_CODES, поэтому в нижнем блоке «Клиенты» не виден"
            )
    for pay, lead in lead_only_payments:
        if lead is None:
            problems.append(
                f"💥 Payment #{pay.id} ({pay.amount:.0f}₽, {pay.customer_name or '?'}): "
                f"lead_id={pay.lead_id}, client_id=NULL, лид в БД отсутствует"
            )
        elif lead.stage not in CLIENT_CODES:
            problems.append(
                f"⚠️ «{lead.name or lead.username or lead.id}» (Lead #{lead.id}, "
                f"Payment {pay.amount:.0f}₽, без Client): stage=<code>{lead.stage}</code>"
            )

    if not problems:
        await message.answer(
            "✅ Расхождений между Payment и Lead нет — карточка и список "
            "«Клиенты» должны показывать одинаковые цифры.",
            parse_mode="HTML",
        )
        return
    text = "🔍 <b>Расхождения, видимые на дашборде:</b>\n\n" + "\n\n".join(problems)
    await message.answer(text[:4000], parse_mode="HTML")


# ─── /diag_backup ──────────────────────────────────────────────────

@router.message(Command("diag_backup"))
async def cmd_diag_backup(message: Message) -> None:
    """Read-only диагностика подсистемы бэкапа: cron-расписание, лог
    последних запусков, локальные файлы. Чтобы понять, почему ожидаемый
    еженедельный бэкап не приходит в TG.
    """
    if not _is_owner(message):
        return

    lines: list[str] = ["🔍 <b>Диагностика бэкапов</b>\n"]

    # 1. Существует ли скрипт по ожидаемому пути
    if os.path.exists(BACKUP_SCRIPT):
        executable = os.access(BACKUP_SCRIPT, os.X_OK)
        lines.append(
            f"📜 <code>{BACKUP_SCRIPT}</code>: ✅ есть, "
            f"{'исполняемый ✅' if executable else 'НЕ исполняемый ❌'}"
        )
    else:
        lines.append(f"📜 <code>{BACKUP_SCRIPT}</code>: ❌ нет — install.sh не отрабатывал?")

    # 2. Crontab сервисного юзера. Бот сам в нём же — должен прочитать.
    try:
        ct = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=5,
        )
        if ct.returncode == 0:
            backup_lines = [
                l for l in ct.stdout.splitlines() if "backup.sh" in l
            ]
            if backup_lines:
                lines.append("📅 <b>Cron-запись о бэкапе:</b>")
                for l in backup_lines:
                    lines.append(f"  <code>{l}</code>")
            else:
                lines.append("📅 Cron-запись о бэкапе: ❌ <b>НЕТ</b> — поэтому и не запускается. Нужно <code>/redeploy</code> (deploy.sh её ставит).")
        else:
            lines.append(f"📅 crontab -l: stderr=<code>{ct.stderr.strip()[:200]}</code>")
    except Exception as e:
        lines.append(f"📅 crontab -l: ⚠ {e}")

    # 3. Лог последних запусков бэкапа
    log_path = "/opt/bot-metrics/logs/backup.log"
    if os.path.exists(log_path):
        try:
            tail = subprocess.run(
                ["tail", "-30", log_path], capture_output=True, text=True, timeout=5,
            ).stdout
            if tail.strip():
                lines.append(f"\n📓 <b>{log_path}</b> (последние 30 строк):\n<pre>{tail[:1500]}</pre>")
            else:
                lines.append(f"\n📓 <code>{log_path}</code>: пустой — cron бэкап ни разу не запускался")
        except Exception as e:
            lines.append(f"\n📓 backup.log: ⚠ {e}")
    else:
        lines.append(f"\n📓 <code>{log_path}</code>: ❌ нет — cron-запуска не было ни одного")

    # 4. Локальные файлы бэкапа (хранится 7 последних)
    backups_dir = "/opt/bot-metrics/data/backups"
    if os.path.isdir(backups_dir):
        try:
            ls = subprocess.run(
                ["ls", "-lht", backups_dir], capture_output=True, text=True, timeout=5,
            ).stdout
            head_lines = ls.splitlines()[:10]
            lines.append(
                f"\n📦 <b>Локальные бэкапы в {backups_dir}:</b>\n"
                f"<pre>{chr(10).join(head_lines)[:1500]}</pre>"
            )
        except Exception as e:
            lines.append(f"\n📦 ls backups: ⚠ {e}")
    else:
        lines.append(f"\n📦 <code>{backups_dir}</code>: ❌ нет директории")

    text = "\n".join(lines)[:4000]
    await message.answer(text, parse_mode="HTML")


# ─── /recent_payments ──────────────────────────────────────────────

@router.message(Command("recent_payments"))
async def cmd_recent_payments(message: Message, command: CommandObject) -> None:
    """Показать последние N платежей из БД с их типом и привязкой.

    Используется когда оплата прошла в Prodamus, но не видна в дашборде:
    можно увидеть, дошёл ли webhook (есть ли запись), и как платёж
    классифицирован (unclassified/first/repeat/ignored). Если случайно
    помечен ignored — повторно классифицировать через /reclassify <id>.

    /recent_payments      — 15 последних
    /recent_payments 50   — последние 50
    """
    if not _is_owner(message):
        return
    try:
        limit = int(command.args.strip()) if command.args else 15
    except (ValueError, AttributeError):
        limit = 15
    limit = max(1, min(limit, 100))

    with get_session() as session:
        rows = session.execute(
            select(Payment)
            .order_by(Payment.paid_at.desc())
            .limit(limit)
        ).scalars().all()

    if not rows:
        await message.answer("В БД нет ни одного платежа.")
        return

    type_emoji = {
        "first": "🟢 first",
        "repeat": "🔁 repeat",
        "unclassified": "⏳ unclassified",
        "ignored": "🚫 ignored",
    }
    lines: list[str] = []
    for p in rows:
        who = p.customer_name or p.customer_phone or p.customer_email or "—"
        typ = type_emoji.get(p.payment_type, p.payment_type)
        link = []
        if p.lead_id:
            link.append(f"lead#{p.lead_id}")
        if p.client_id:
            link.append(f"client#{p.client_id}")
        link_s = " " + ",".join(link) if link else " (без привязки)"
        date = p.paid_at.strftime("%d.%m %H:%M") if p.paid_at else "—"
        lines.append(
            f"<code>#{p.id}</code> {date} {p.amount:.0f}₽ — {who[:30]} → {typ}{link_s}"
        )

    text = (
        f"💳 <b>Последние {len(rows)} платежей</b>\n\n"
        + "\n".join(lines)
        + "\n\nПерекрутить классификацию любого: <code>/reclassify ID</code>"
    )
    await message.answer(text[:4000], parse_mode="HTML")


# ─── /reclassify ───────────────────────────────────────────────────

@router.message(Command("reclassify"))
async def cmd_reclassify(message: Message, command: CommandObject) -> None:
    """Передёрнуть классификационный диалог для существующего платежа,
    в том числе уже помеченного first/repeat/ignored. Удобно если
    случайно нажал «Игнорировать» или платёж привязан не к тому."""
    if not _is_owner(message):
        return
    if not command.args or not command.args.strip().isdigit():
        await message.answer(
            "Использование: <code>/reclassify ID</code>\n"
            "ID можно посмотреть через /recent_payments",
            parse_mode="HTML",
        )
        return
    payment_id = int(command.args.strip())
    with get_session() as session:
        payment = session.get(Payment, payment_id)
        if not payment:
            await message.answer(f"Платёж #{payment_id} не найден.")
            return
        # Сбрасываем привязку — диалог классификации заново привяжет
        # к выбранному лиду/клиенту через те же pay:* callback'и.
        payment.payment_type = "unclassified"
        payment.lead_id = None
        payment.client_id = None
        session.commit()
        session.refresh(payment)

        # Импортируем тут, чтобы избежать циклической зависимости на старте.
        from webhook import _match_clients, _match_leads, _notify_classify
        client_candidates = _match_clients(
            session, payment.customer_name, payment.customer_phone, payment.customer_email,
        )
        lead_candidates = _match_leads(
            session, payment.customer_name, payment.customer_phone, payment.customer_email,
        )
        _notify_classify(payment, lead_candidates, client_candidates)
    await message.answer(f"♻️ Платёж #{payment_id} сброшен в unclassified, диалог отправлен выше.")


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
        "<code>/testprodamus</code> — проверить, что ключ Prodamus в .env "
        "совпадает с тем, что используется для подписи (без записи в БД)\n"
        "<code>/addpayment &lt;…&gt;</code> — дозаписать платёж вручную "
        "(если webhook потерял оплату). Потом /fixpay для классификации\n"
        "<code>/forcehttps</code> — принудительно включить http→https редирект "
        "и HSTS (если в браузере «Не защищено» с валидным сертификатом)\n"
        "<code>/diag</code> — диагностика состояния сервера "
        "(версии скриптов, sudo permissions, сертификаты)\n"
        "<code>/setautoreply &lt;текст&gt;</code> — текст автоответа на новые "
        "заявки в Telegram Business\n"
        "<code>/getautoreply</code> — показать текущий автоответ\n"
        "<code>/diag_backup</code> — read-only: почему еженедельный "
        "бэкап не приходит (есть ли cron-запись, что в backup.log)\n"
        "<code>/recent_payments [N]</code> — список последних N платежей "
        "(unclassified / first / repeat / ignored), чтобы найти потерявшийся\n"
        "<code>/reclassify ID</code> — сбросить классификацию платежа и "
        "передёрнуть диалог (если случайно нажал Игнор или привязал не к тому)\n"
        "<code>/diag_clients</code> — read-only: показать почему "
        "«Первичных оплат: N» не совпадает с «Клиенты: M» в нижнем блоке "
        "(у кого нет лида / лид в другом этапе)\n"
        "<code>/purge_orphans</code> — подчистить «осиротевшие» Clients/"
        "Payments после удалений лидов до cascade-delete фикса\n"
        "<code>/deployurl</code> — показать URL для GitHub-вебхука "
        "(один раз настроишь — авто-деплой при push в main, /redeploy больше не нужен)\n"
        "<code>/version</code> — какой коммит сейчас на сервере + mtime "
        "скриптов в bin/ (быстрая проверка, доехал ли свежий фикс)\n"
        "<code>/restart_listener</code> — рестарт сервиса auto-deploy "
        "(нужен один раз когда обновился deploy_listener.py или менялся "
        "DEPLOY_SECRET в .env)\n"
        "<code>/test_deploy</code> — диагностика: совпадает ли DEPLOY_SECRET "
        "у бота и у listener'а, через self-curl на 127.0.0.1:9876",
        parse_mode="HTML",
    )
