#!/usr/bin/env bash
# Ежедневный бэкап bot.db: snapshot через sqlite3, отправка владельцу в TG,
# хранение последних 7 локальных копий.
set -euo pipefail

ROOT_DIR="/opt/bot-metrics"
DATA_DIR="$ROOT_DIR/data"
BACKUP_DIR="$DATA_DIR/backups"
ENV_FILE="$ROOT_DIR/.env"

# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

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
