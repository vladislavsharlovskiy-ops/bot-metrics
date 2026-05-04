#!/usr/bin/env bash
# Подменяет /opt/bot-metrics/data/bot.db на ранее загруженный (через scp)
# /opt/bot-metrics/data/bot.db.imported. Делает это безопасно:
#   1. проверяет, что imported-файл — валидная SQLite-база
#   2. останавливает bot/web
#   3. сохраняет текущую bot.db в data/backups/bot-before-import-<TS>.db
#   4. ставит imported на её место с правильным владельцем
#   5. поднимает сервисы обратно
#
# Запуск (от root):
#   sudo /opt/bot-metrics/bin/import_db.sh
set -euo pipefail

ROOT_DIR="/opt/bot-metrics"
DATA_DIR="$ROOT_DIR/data"
BACKUP_DIR="$DATA_DIR/backups"
SERVICE_USER="bot"

LIVE_DB="$DATA_DIR/bot.db"
IMPORTED_DB="$DATA_DIR/bot.db.imported"

if [[ $EUID -ne 0 ]]; then
  echo "Скрипт запускается от root. Используй: sudo $0"
  exit 1
fi

if [[ ! -f "$IMPORTED_DB" ]]; then
  echo "Не вижу $IMPORTED_DB."
  echo "Сначала загрузи файл с локальной машины, например:"
  echo "  scp ~/Downloads/bot.db root@<IP>:$IMPORTED_DB"
  exit 1
fi

echo "[import] проверяю imported-файл как SQLite"
if ! sqlite3 "$IMPORTED_DB" "PRAGMA integrity_check;" | grep -q '^ok$'; then
  echo "[import] $IMPORTED_DB не проходит integrity_check — отказываюсь подменять"
  exit 1
fi

mkdir -p "$BACKUP_DIR"
TS="$(date +%Y%m%d-%H%M%S)"

echo "[import] останавливаю bot и web"
systemctl stop bot-metrics-bot bot-metrics-web

if [[ -f "$LIVE_DB" ]]; then
  SAFETY="$BACKUP_DIR/bot-before-import-$TS.db"
  echo "[import] сохраняю текущую базу в $SAFETY"
  cp -a "$LIVE_DB" "$SAFETY"
  # WAL/SHM-хвосты от старой базы не должны примешаться к новой
  rm -f "$LIVE_DB-wal" "$LIVE_DB-shm"
fi

echo "[import] ставлю imported на место $LIVE_DB"
mv "$IMPORTED_DB" "$LIVE_DB"
chown "$SERVICE_USER:$SERVICE_USER" "$LIVE_DB"
chmod 640 "$LIVE_DB"

echo "[import] поднимаю сервисы"
systemctl start bot-metrics-bot bot-metrics-web

echo "[import] готово. Проверь:"
echo "  systemctl status bot-metrics-bot bot-metrics-web"
echo "  journalctl -u bot-metrics-bot -n 50 --no-pager"
