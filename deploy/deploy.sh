#!/usr/bin/env bash
# Подтянуть свежий код и перезапустить сервисы.
# Запускается либо вручную (`sudo /opt/bot-metrics/bin/deploy.sh`), либо
# автоматически из deploy_listener.py при пуше в main.
#
# ВАЖНО: каждый шаг обернут в || true (best-effort), чтобы отказ одного шага
# не помешал последующим. Раньше set -euo pipefail обрывал скрипт ДО рестарта,
# и бот зависал на старом коде. Теперь рестарт ВСЕГДА в конце.

set -u  # unset vars — ошибка. set -e и pipefail НЕ ставим.

ROOT_DIR="/opt/bot-metrics"
REPO_DIR="$ROOT_DIR/repo"
VENV_DIR="$ROOT_DIR/venv"
SERVICE_USER="bot"
BIN_DIR="$ROOT_DIR/bin"
LOG_DIR="$ROOT_DIR/logs"

step() { echo "[deploy] $(date '+%H:%M:%S') $*"; }
warn() { echo "[deploy] $(date '+%H:%M:%S') WARN: $*" >&2; }

step "starting"

# 0. Git pull — раньше других шагов, чтобы repo/deploy.sh обновился, и
#    self-update ниже мог бы переключиться на свежую версию.
cd "$REPO_DIR" || { warn "cd $REPO_DIR failed"; }
sudo -u "$SERVICE_USER" git fetch --quiet origin main || warn "git fetch failed"
sudo -u "$SERVICE_USER" git reset --hard origin/main || warn "git reset failed"
step "git pulled to $(sudo -u "$SERVICE_USER" git -C "$REPO_DIR" log -1 --format='%h %s' 2>/dev/null || echo '?')"

# 0.1. SELF-UPDATE: если repo/deploy/deploy.sh отличается от текущего ($0),
#      перезаписываем себя и re-exec'имся. Без этого фикстауризменения
#      в самом deploy.sh не доезжают до bin/ при auto-deploy (только
#      через admin.py /redeploy с self-sync). Теперь — даже автосинхрон.
SELF_UPDATE_SRC="$REPO_DIR/deploy/deploy.sh"
if [[ -f "$SELF_UPDATE_SRC" ]] && [[ "$0" != "$SELF_UPDATE_SRC" ]] \
        && ! cmp -s "$SELF_UPDATE_SRC" "$0" 2>/dev/null; then
    step "self-update: $SELF_UPDATE_SRC newer than $0"
    cp "$SELF_UPDATE_SRC" "$0" && chmod +x "$0" \
        || warn "self-update copy failed"
    # exec заменяет процесс на свежий deploy.sh (с теми же args)
    exec "$0" "$@"
fi

# 1. Python deps
sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install --quiet \
    -r "$REPO_DIR/requirements.txt" || warn "pip install failed"

# 3. Cron — bulletproof: temp-file подход, без пайплайнов с pipefail.
#    Понедельник 03:00 МСК.
TMP_CRON="$(mktemp)"
sudo -u "$SERVICE_USER" crontab -l > "$TMP_CRON" 2>/dev/null || true  # пусто если нет
TMP_CRON_NEW="$(mktemp)"
grep -v "$BIN_DIR/backup.sh" "$TMP_CRON" > "$TMP_CRON_NEW" 2>/dev/null || true
echo "0 3 * * 1 $BIN_DIR/backup.sh >> $LOG_DIR/backup.log 2>&1" >> "$TMP_CRON_NEW"
sudo -u "$SERVICE_USER" crontab "$TMP_CRON_NEW" || warn "crontab install failed"
rm -f "$TMP_CRON" "$TMP_CRON_NEW"
step "cron updated"

# 4. Sudoers — критично для админ-команд (/forcehttps и т.п.)
if [[ -f "$REPO_DIR/deploy/sudoers.d-bot-metrics" ]]; then
    install -m 0440 "$REPO_DIR/deploy/sudoers.d-bot-metrics" \
        /etc/sudoers.d/bot-metrics || warn "sudoers install failed"
    visudo -cf /etc/sudoers.d/bot-metrics >/dev/null 2>&1 \
        || warn "sudoers visudo check failed (правил всё равно применены)"
    step "sudoers updated"
else
    warn "sudoers source not found in repo"
fi

# 5. Nginx-config — синхронизируем из репы (там, например, появился
#    location /webhook/ для платёжных webhook'ов). Если cert уже есть,
#    после переустановки конфига certbot install --redirect возвращает
#    ssl-блок и редирект http→https.
NGINX_SRC="$REPO_DIR/deploy/nginx-bot-metrics.conf"
NGINX_DST="/etc/nginx/sites-available/bot-metrics.conf"
DOMAIN="dashboard.sharlovsky.pro"
if [[ -f "$NGINX_SRC" ]]; then
    install -m 0644 "$NGINX_SRC" "$NGINX_DST" || warn "nginx install failed"
    if [[ -d "/etc/letsencrypt/live/$DOMAIN" ]] && command -v certbot >/dev/null 2>&1; then
        certbot install --cert-name "$DOMAIN" --installer nginx --redirect \
            --non-interactive >/dev/null 2>&1 || warn "certbot install failed"
        # HSTS — добавляем если ещё нет
        if ! grep -q "Strict-Transport-Security" "$NGINX_DST"; then
            sed -i -E '0,/ssl_certificate_key/{/ssl_certificate_key/a\    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
}' "$NGINX_DST" || warn "HSTS sed failed"
        fi
    fi
    if nginx -t >/dev/null 2>&1; then
        systemctl reload nginx || warn "nginx reload failed"
        step "nginx config synced + reloaded"
    else
        warn "nginx -t failed, NOT reloading (текущий конфиг работает)"
    fi
else
    warn "nginx config source not found in repo"
fi

# 6. Restart — ВСЕГДА в конце, даже если выше что-то упало. Без рестарта
#    бот остаётся на старом коде, и пользователь не видит фиксов.
step "restarting services"
systemctl restart bot-metrics-bot bot-metrics-web || warn "service restart failed"

step "done"
