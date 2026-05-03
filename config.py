import logging
import os
import shutil
import time
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger("config")

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Часовой пояс. По умолчанию Москва (UTC+3) — чтобы datetime.now() и время
# в карточках совпадало с тем, что владелец видит у себя в Telegram.
# Можно переопределить через env var TZ (например, "Europe/Moscow", "UTC").
os.environ.setdefault("TZ", "Europe/Moscow")
try:
    time.tzset()
except AttributeError:
    pass  # Windows — tzset недоступен, но прод у нас Linux

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])

# bothost сетит:
#   DATA_DIR=/app/data     — персистентный том бота (живая БД)
#   SHARED_DIR=/app/shared — общее хранилище (сюда удобно загружать seed-файлы через UI)
#
# Логика выбора БД:
#   1. Если задан DATA_DIR — это рабочая база. При первом старте, если её там
#      ещё нет, но в SHARED_DIR/bot.db лежит seed — копируем его. Это разовая
#      миграция данных при первом деплое.
#   2. Если DATA_DIR не задан, но есть SHARED_DIR — используем его напрямую.
#   3. Локальный fallback — BASE_DIR/bot.db.
_data_dir = os.environ.get("DATA_DIR", "").strip()
_shared_dir = os.environ.get("SHARED_DIR", "").strip()

if _data_dir:
    Path(_data_dir).mkdir(parents=True, exist_ok=True)
    DB_PATH = Path(_data_dir) / "bot.db"
    if not DB_PATH.exists() and _shared_dir:
        seed = Path(_shared_dir) / "bot.db"
        if seed.exists():
            shutil.copy2(seed, DB_PATH)
            log.warning("Seeded %s from %s", DB_PATH, seed)
elif _shared_dir:
    Path(_shared_dir).mkdir(parents=True, exist_ok=True)
    DB_PATH = Path(_shared_dir) / "bot.db"
else:
    DB_PATH = BASE_DIR / "bot.db"

# Дополнительные пользователи (например, жена для теста). Через запятую.
_extra = os.environ.get("EXTRA_USER_IDS", "").strip()
EXTRA_USER_IDS = {int(x.strip()) for x in _extra.split(",") if x.strip().isdigit()}
ALLOWED_USERS = {OWNER_ID} | EXTRA_USER_IDS

# Если "1" — пускаем кого угодно. Удобно для тестов.
OPEN_ACCESS = os.environ.get("BOT_OPEN_ACCESS", "").strip() == "1"

# Ключ для внешнего API создания лидов (POST /api/external/leads).
# Используется отдельным лид-ботом, который пишет лиды по HTTP в общую базу.
# Если переменная не задана — эндпоинт отключён (возвращает 403).
LEADS_API_KEY = os.environ.get("LEADS_API_KEY", "").strip()
