#!/usr/bin/env bash
# poll-deploy.sh — cron-driven автодеплой.
#
# Каждые 2 минуты:
#   1. git fetch origin main
#   2. сравнить HEAD с origin/main
#   3. если есть новые коммиты — запустить /opt/bot-metrics/bin/deploy.sh
#
# Альтернатива GitHub-webhook'у / Actions, когда внешний HTTP-трафик
# к серверу режется WAF/Anti-DDoS-провайдером (например TimeWeb) или
# другими сетевыми ограничениями. Работает в одну сторону: сервер сам
# ходит к GitHub, входящий трафик не нужен.
#
# Запускается из cron от юзера bot. deploy.sh — через sudo (разрешено
# в /etc/sudoers.d/bot-metrics).

set -u

REPO_DIR="/opt/bot-metrics/repo"
DEPLOY_SCRIPT="/opt/bot-metrics/bin/deploy.sh"
LOG_DIR="/opt/bot-metrics/logs"
LOG_FILE="$LOG_DIR/poll-deploy.log"
LOCK_FILE="/tmp/bot-metrics-poll-deploy.lock"

mkdir -p "$LOG_DIR" 2>/dev/null || true

# flock — гарантия что одновременно бежит только один экземпляр.
# Если предыдущий деплой ещё в процессе — пропускаем этот тик молча.
exec 200>"$LOCK_FILE" 2>/dev/null || exit 0
flock -n 200 || exit 0

cd "$REPO_DIR" 2>/dev/null || exit 0

before=$(git rev-parse HEAD 2>/dev/null) || exit 0
git fetch --quiet origin main 2>/dev/null || exit 0
after=$(git rev-parse origin/main 2>/dev/null) || exit 0

if [[ "$before" != "$after" ]]; then
    {
        echo ""
        echo "[poll-deploy] $(date '+%F %T') HEAD: ${before:0:7} -> ${after:0:7}, deploying"
        sudo "$DEPLOY_SCRIPT" 2>&1
        echo "[poll-deploy] $(date '+%F %T') deploy.sh exit=$?"
    } >> "$LOG_FILE"
fi
