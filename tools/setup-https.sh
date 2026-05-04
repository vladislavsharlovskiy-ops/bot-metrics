#!/usr/bin/env bash
# Включает HTTPS для дашборда: получает Let's Encrypt сертификат через
# certbot --nginx и настраивает редирект http → https.
#
# Использование (на сервере):
#     sudo bash /opt/bot-metrics/repo/tools/setup-https.sh <domain> <email>
#
# Пример:
#     sudo bash /opt/bot-metrics/repo/tools/setup-https.sh dashboard.sharlovsky.pro you@example.com
#
# ПЕРЕД ЗАПУСКОМ убедись, что A-запись <domain> → IP сервера уже работает
# (DNS пропагировался). Скрипт сам это проверит и скажет, если нет.

set -euo pipefail

DOMAIN="${1:-}"
EMAIL="${2:-}"

if [[ $EUID -ne 0 ]]; then
    echo "Запуск через sudo: sudo bash $0 <domain> <email>"
    exit 1
fi

if [[ -z "$DOMAIN" || -z "$EMAIL" ]]; then
    echo "Использование: sudo bash $0 <domain> <email>"
    echo "  domain — например, dashboard.sharlovsky.pro"
    echo "  email  — для уведомлений Let's Encrypt об истечении"
    exit 1
fi

# ─── 1. Проверяем DNS ─────────────────────────────────────────────
SERVER_IP="$(curl -s -4 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"
if ! command -v dig >/dev/null 2>&1; then
    apt-get install -y dnsutils >/dev/null
fi
RESOLVED="$(dig +short "$DOMAIN" @1.1.1.1 | tail -1)"

echo "Сервер:    $SERVER_IP"
echo "Домен:     $DOMAIN → ${RESOLVED:-<не резолвится>}"

if [[ -z "$RESOLVED" ]]; then
    echo
    echo "⚠ DNS для $DOMAIN не резолвится."
    echo "   Добавь A-запись $DOMAIN → $SERVER_IP в панели домена,"
    echo "   подожди 5-30 минут (TTL пропагации), запусти ещё раз."
    exit 1
fi

if [[ "$RESOLVED" != "$SERVER_IP" ]]; then
    echo
    echo "⚠ $DOMAIN указывает не на этот сервер."
    echo "   $DOMAIN → $RESOLVED, а сервер: $SERVER_IP"
    echo "   Поправь A-запись и подожди DNS, потом запусти ещё раз."
    exit 1
fi
echo "✓ DNS ок"

# ─── 2. Ставим certbot ────────────────────────────────────────────
if ! command -v certbot >/dev/null 2>&1; then
    echo "==> Ставлю certbot"
    apt-get update -y >/dev/null
    apt-get install -y certbot python3-certbot-nginx
fi

# ─── 3. Обновляем server_name в nginx ─────────────────────────────
# Текущий конфиг репы использует server_name _, certbot ему не понравится.
# Подменяем _ → нашим доменом (только в bot-metrics.conf, не во всём nginx).
NGINX_CONF=/etc/nginx/sites-available/bot-metrics.conf
if ! grep -q "server_name $DOMAIN" "$NGINX_CONF"; then
    echo "==> Прописываю server_name $DOMAIN в nginx"
    # Заменяем существующий server_name (что бы там ни было — _ или старый домен)
    sed -i -E "s/server_name[[:space:]]+[^;]+;/server_name $DOMAIN;/" "$NGINX_CONF"
    nginx -t
    systemctl reload nginx
fi

# ─── 4. Получаем сертификат + редирект ────────────────────────────
echo "==> certbot для $DOMAIN"
certbot --nginx \
    -d "$DOMAIN" \
    --non-interactive \
    --agree-tos \
    --redirect \
    -m "$EMAIL"

# ─── 5. Готово ────────────────────────────────────────────────────
echo
echo "═══════════════════════════════════════════════════════════════"
echo "  HTTPS включён для $DOMAIN"
echo "  Дашборд: https://$DOMAIN/"
echo
echo "  Сертификат автообновляется (systemd timer certbot.timer)."
echo "  Проверить: systemctl list-timers | grep certbot"
echo "═══════════════════════════════════════════════════════════════"
