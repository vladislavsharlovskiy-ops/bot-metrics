#!/usr/bin/env bash
# Принудительно включает редирект http→https и HSTS-заголовок.
#
# Когда пригодится:
#   - после secure-dashboard.sh, который раньше затирал SSL-конфиг от certbot
#   - если certbot --nginx --redirect не дописал redirect при повторном запуске
#   - если в браузере «Не защищено» с валидным сертификатом
#
# Что делает:
#   1. certbot install --redirect — переустанавливает cert в nginx-конфиг
#      с явным редиректом для HTTP-блока
#   2. Добавляет HSTS-заголовок (Strict-Transport-Security) в HTTPS-блок,
#      если ещё нет — браузер будет принудительно ходить по https
#   3. nginx -t && systemctl reload nginx

set -euo pipefail

DOMAIN="${1:-dashboard.sharlovsky.pro}"
NGINX_CONF=/etc/nginx/sites-available/bot-metrics.conf

if [[ $EUID -ne 0 ]]; then
    echo "Запуск через sudo: sudo bash $0 [domain]"
    exit 1
fi

if [[ ! -d "/etc/letsencrypt/live/$DOMAIN" ]]; then
    echo "⚠ Сертификат для $DOMAIN не найден в /etc/letsencrypt/live/."
    echo "   Запусти сначала setup-https.sh."
    exit 1
fi

if [[ ! -f "$NGINX_CONF" ]]; then
    echo "⚠ Не найден $NGINX_CONF"
    exit 1
fi

# 1. certbot install с --redirect — переустанавливает cert в nginx-конфиг
#    и добавляет редирект для HTTP-блока. На повторных запусках идемпотентно.
echo "==> certbot install --cert-name $DOMAIN --redirect"
certbot install \
    --cert-name "$DOMAIN" \
    --installer nginx \
    --redirect \
    --non-interactive

# 2. HSTS — добавляем только если ещё нет (идемпотентно).
if ! grep -q "Strict-Transport-Security" "$NGINX_CONF"; then
    echo "==> добавляю HSTS"
    # Вставляем после первой строки с ssl_certificate_key (она в HTTPS-блоке,
    # созданном certbot'ом).
    sed -i -E \
        '0,/ssl_certificate_key.*managed by Certbot/{/ssl_certificate_key.*managed by Certbot/a\    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
}' "$NGINX_CONF" || true
    # Если ssl_certificate_key managed by Certbot не нашли (custom installer),
    # пробуем просто после ssl_certificate_key
    if ! grep -q "Strict-Transport-Security" "$NGINX_CONF"; then
        sed -i -E '0,/ssl_certificate_key/{/ssl_certificate_key/a\    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
}' "$NGINX_CONF"
    fi
fi

# 3. Тест и reload
nginx -t
systemctl reload nginx

echo
echo "═══════════════════════════════════════════════════════════════"
echo "  HTTPS-редирект и HSTS включены для $DOMAIN."
echo
echo "  Проверь:"
echo "    curl -sI http://$DOMAIN/   # должен 301 на https://"
echo "    curl -sI https://$DOMAIN/  # 200, видим Strict-Transport-Security"
echo
echo "  В браузере — открой в режиме инкогнито (Cmd+Shift+N), чтобы кеш"
echo "  не мешал, и набери https://$DOMAIN/"
echo "═══════════════════════════════════════════════════════════════"
