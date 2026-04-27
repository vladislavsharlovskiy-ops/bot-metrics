import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])

# bothost сетит DATA_DIR=/app/data — это персистентный том,
# переживает редеплой. Локально переменная не задана, БД лежит рядом с кодом.
_data_dir = os.environ.get("DATA_DIR", "").strip()
if _data_dir:
    Path(_data_dir).mkdir(parents=True, exist_ok=True)
    DB_PATH = Path(_data_dir) / "bot.db"
else:
    DB_PATH = BASE_DIR / "bot.db"

# Дополнительные пользователи (например, жена для теста). Через запятую.
_extra = os.environ.get("EXTRA_USER_IDS", "").strip()
EXTRA_USER_IDS = {int(x.strip()) for x in _extra.split(",") if x.strip().isdigit()}
ALLOWED_USERS = {OWNER_ID} | EXTRA_USER_IDS

# Если "1" — пускаем кого угодно. Удобно для тестов.
OPEN_ACCESS = os.environ.get("BOT_OPEN_ACCESS", "").strip() == "1"
