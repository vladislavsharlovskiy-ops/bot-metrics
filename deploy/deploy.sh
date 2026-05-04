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

echo "[deploy] restarting services"
systemctl restart bot-metrics-bot bot-metrics-web

echo "[deploy] done"
