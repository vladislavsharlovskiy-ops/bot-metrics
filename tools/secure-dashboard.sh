#!/usr/bin/env bash
# Закрывает дашборд basic-auth: создаёт/обновляет /etc/nginx/.htpasswd_bot_metrics,
# применяет nginx-конфиг из репы (с auth_basic), перезагружает nginx.
#
# Использование (на сервере):
#     sudo bash /opt/bot-metrics/repo/tools/secure-dashboard.sh
#     sudo bash /opt/bot-metrics/repo/tools/secure-dashboard.sh admin
#     sudo bash /opt/bot-metrics/repo/tools/secure-dashboard.sh admin 'СвойПароль'
#
# Без аргументов — логин 'admin', пароль 20 случайных символов
# (печатается ОДИН раз — обязательно сохрани его).

set -euo pipefail

USERNAME="${1:-admin}"
PASSWORD="${2:-}"
GENERATED=0

if [[ $EUID -ne 0 ]]; then
    echo "Запуск через sudo: sudo bash $0 [username] [password]"
    exit 1
fi

if [[ -z "$PASSWORD" ]]; then
    PASSWORD="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20)"
    GENERATED=1
fi

if ! command -v htpasswd >/dev/null 2>&1; then
    echo "==> Ставлю apache2-utils для htpasswd"
    apt-get update -y >/dev/null
    apt-get install -y apache2-utils
fi

HTPASSWD=/etc/nginx/.htpasswd_bot_metrics
htpasswd -bc "$HTPASSWD" "$USERNAME" "$PASSWORD" >/dev/null
chmod 640 "$HTPASSWD"
chown root:www-data "$HTPASSWD"

# Применяем nginx-конфиг из репы (он с auth_basic)
NGINX_SRC=/opt/bot-metrics/repo/deploy/nginx-bot-metrics.conf
NGINX_DST=/etc/nginx/sites-available/bot-metrics.conf
install -m 0644 "$NGINX_SRC" "$NGINX_DST"

nginx -t
systemctl reload nginx

echo
echo "═══════════════════════════════════════════════════════════════"
echo "  Дашборд защищён basic auth."
echo "  Логин:  $USERNAME"
if [[ $GENERATED -eq 1 ]]; then
    echo "  Пароль: $PASSWORD"
    echo
    echo "  ⚠ Сохрани пароль! Повторно я его НЕ покажу."
else
    echo "  Пароль: задан вручную через аргумент"
fi
echo
echo "  Сменить позже: sudo bash $0 $USERNAME 'новыйПароль'"
echo "═══════════════════════════════════════════════════════════════"
