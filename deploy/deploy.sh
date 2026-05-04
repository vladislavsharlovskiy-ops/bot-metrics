#!/usr/bin/env bash
# Подтянуть свежий код и перезапустить сервисы.
# Запускается либо вручную (`sudo /opt/bot-metrics/bin/deploy.sh`), либо
# автоматически из deploy_listener.py при пуше в main.
set -euo pipefail

ROOT_DIR="/opt/bot-metrics"
REPO_DIR="$ROOT_DIR/repo"
VENV_DIR="$ROOT_DIR/venv"
SERVICE_USER="bot"

echo "[deploy] $(date) — pulling latest"

cd "$REPO_DIR"
sudo -u "$SERVICE_USER" git fetch --quiet origin main
sudo -u "$SERVICE_USER" git reset --hard origin/main

# Зависимости — переустанавливаем тихо, чтобы захватить новые
sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt"

# Cron-расписание бэкапа (понедельник 03:00 МСК) — переустанавливаем на каждом
# деплое, чтоб смена расписания в репе автоматом докатилась до сервера, без
# ручного crontab -e на сервере.
BIN_DIR="$ROOT_DIR/bin"
LOG_DIR="$ROOT_DIR/logs"
CRON_LINE="0 3 * * 1 $BIN_DIR/backup.sh >> $LOG_DIR/backup.log 2>&1"
( sudo -u "$SERVICE_USER" crontab -l 2>/dev/null | grep -v "$BIN_DIR/backup.sh" ; echo "$CRON_LINE" ) | sudo -u "$SERVICE_USER" crontab -

# Sudoers для service-юзера — обновляем на каждом деплое, чтобы новые
# разрешения (например, fix-https-redirect.sh) автоматом докатывались на
# сервер без ручного visudo.
install -m 0440 "$REPO_DIR/deploy/sudoers.d-bot-metrics" /etc/sudoers.d/bot-metrics
visudo -cf /etc/sudoers.d/bot-metrics >/dev/null

echo "[deploy] restarting services"
systemctl restart bot-metrics-bot bot-metrics-web

echo "[deploy] done"
