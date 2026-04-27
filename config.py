import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
DB_PATH = BASE_DIR / "bot.db"

# Дополнительные пользователи (например, жена для теста). Через запятую.
_extra = os.environ.get("EXTRA_USER_IDS", "").strip()
EXTRA_USER_IDS = {int(x.strip()) for x in _extra.split(",") if x.strip().isdigit()}
ALLOWED_USERS = {OWNER_ID} | EXTRA_USER_IDS

# Если "1" — пускаем кого угодно. Удобно для тестов.
OPEN_ACCESS = os.environ.get("BOT_OPEN_ACCESS", "").strip() == "1"
