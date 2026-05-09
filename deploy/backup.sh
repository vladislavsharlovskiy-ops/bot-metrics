#!/usr/bin/env bash
# Ежедневный бэкап bot.db: snapshot через sqlite3, отправка владельцу в TG,
# хранение последних 7 локальных копий.
set -euo pipefail

ROOT_DIR="/opt/bot-metrics"
DATA_DIR="$ROOT_DIR/data"
BACKUP_DIR="$DATA_DIR/backups"
ENV_FILE="$ROOT_DIR/.env"

# Раньше тут был `source "$ENV_FILE"`, но он падал на значениях с пробелами
# и `!` (например BUSINESS_AUTO_REPLY="Добрый день! ..."): bash трактовал
# пробел как конец присваивания, а слово после — как команду. systemd
# EnvironmentFile такие значения ест нормально, поэтому сами сервисы
# работали, а вот этот скрипт — нет. Читаем нужные ключи точечно.
get_env() {
    # head -1 — на случай если ключ дублирован (берём первый); cut -d= -f2-
    # сохраняет '=' внутри значения (часть base64-токенов их содержит).
    grep -E "^${1}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-
}
BOT_TOKEN="$(get_env BOT_TOKEN)"
OWNER_ID="$(get_env OWNER_ID)"

if [[ -z "${BOT_TOKEN:-}" || -z "${OWNER_ID:-}" ]]; then
  echo "[backup] BOT_TOKEN/OWNER_ID не заданы в $ENV_FILE — отправка в TG невозможна"
  exit 1
fi

mkdir -p "$BACKUP_DIR"
TS="$(date +%Y%m%d-%H%M)"
SNAPSHOT="$BACKUP_DIR/bot-$TS.db"

# .backup — безопасный consistent snapshot работающей БД (WAL-режим)
sqlite3 "$DATA_DIR/bot.db" ".backup '$SNAPSHOT'"

gzip -f "$SNAPSHOT"
SNAPSHOT="$SNAPSHOT.gz"

echo "[backup] snapshot saved: $SNAPSHOT ($(du -h "$SNAPSHOT" | cut -f1))"

# Отправляем файл в TG владельцу
curl -sS -F "chat_id=$OWNER_ID" \
        -F "caption=📦 Бэкап bot.db от $(date '+%d.%m.%Y %H:%M')" \
        -F "document=@$SNAPSHOT" \
        "https://api.telegram.org/bot${BOT_TOKEN}/sendDocument" \
        -o /dev/null && echo "[backup] sent to TG"

# Чистим старые: оставляем 7 последних
ls -1t "$BACKUP_DIR"/bot-*.db.gz 2>/dev/null | tail -n +8 | xargs -r rm -f

echo "[backup] done"
