#!/usr/bin/env bash
# Установщик для свежего Ubuntu (22.04/24.04/26.04) сервера.
# Один раз запускается руками с правами root, дальше всё работает само.
#
# Требования: репо склонирован в /opt/bot-metrics/repo
# Запуск:     bash /opt/bot-metrics/repo/deploy/install.sh
set -euo pipefail

ROOT_DIR="/opt/bot-metrics"
REPO_DIR="$ROOT_DIR/repo"
DATA_DIR="$ROOT_DIR/data"
VENV_DIR="$ROOT_DIR/venv"
LOG_DIR="$ROOT_DIR/logs"
BIN_DIR="$ROOT_DIR/bin"
ENV_FILE="$ROOT_DIR/.env"
SERVICE_USER="bot"

if [[ $EUID -ne 0 ]]; then
  echo "Скрипт запускается от root. Используй: sudo bash $0"
  exit 1
fi

if [[ ! -d "$REPO_DIR/.git" ]]; then
  echo "Не вижу git-репо в $REPO_DIR. Сначала:"
  echo "  mkdir -p $ROOT_DIR && git clone https://github.com/vladislavsharlovskiy-ops/bot-metrics.git $REPO_DIR"
  exit 1
fi

echo "==> Обновляю apt и ставлю системные пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip \
  git curl ca-certificates tzdata sqlite3 \
  nginx ufw

echo "==> Часовой пояс Europe/Moscow"
timedatectl set-timezone Europe/Moscow || true

echo "==> Создаю системного пользователя $SERVICE_USER"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home "$ROOT_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "==> Каталоги в $ROOT_DIR"
mkdir -p "$DATA_DIR" "$DATA_DIR/backups" "$LOG_DIR" "$BIN_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$ROOT_DIR"

echo "==> Python venv + зависимости"
if [[ ! -d "$VENV_DIR" ]]; then
  sudo -u "$SERVICE_USER" python3 -m venv "$VENV_DIR"
fi
sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install --upgrade pip wheel
sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install -r "$REPO_DIR/requirements.txt"
# gunicorn для прод-сервинга Flask
sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install gunicorn

# ------------------------------------------------------------------
# .env — секреты
# ------------------------------------------------------------------
if [[ ! -f "$ENV_FILE" ]]; then
  echo
  echo "==> Создаю $ENV_FILE — введи токены и параметры"
  ask() {
    local var="$1" prompt="$2" default="${3:-}" silent="${4:-}"
    local val=""
    if [[ -n "$silent" ]]; then
      read -r -s -p "$prompt: " val
      echo
    else
      if [[ -n "$default" ]]; then
        read -r -p "$prompt [$default]: " val
        val="${val:-$default}"
      else
        read -r -p "$prompt: " val
      fi
    fi
    # systemd EnvironmentFile понимает простой KEY=VALUE без кавычек.
    # Спецсимволы в TG-токенах нам не страшны (там только [A-Za-z0-9:_-]).
    printf '%s=%s\n' "$var" "$val" >> "$ENV_FILE"
  }

  : > "$ENV_FILE"
  echo "# /opt/bot-metrics/.env — заполнено install.sh $(date)"   >> "$ENV_FILE"
  echo "DATA_DIR=$DATA_DIR"                                        >> "$ENV_FILE"

  ask BOT_TOKEN          "Telegram BOT_TOKEN (из @BotFather)" "" silent
  ask OWNER_ID           "Твой Telegram OWNER_ID (число)"
  ask EXTRA_USER_IDS     "Дополнительные user_ids через запятую (Enter — пропустить)" ""
  ask LEADS_API_KEY      "LEADS_API_KEY для /api/external/leads (Enter — отключить)" ""
  ask PRODAMUS_SECRET_KEY "PRODAMUS_SECRET_KEY для проверки платёжных webhook (Enter — пропустить)" "" silent
  ask WEBHOOK_SITE_UUID  "WEBHOOK_SITE_UUID (для тестового webhook.site, Enter — пропустить)" ""
  ask SHEETS_WEBHOOK_URL "SHEETS_WEBHOOK_URL (Apps Script URL для синка с Google Sheets, Enter — пропустить)" ""
  ask SHEET_URL          "SHEET_URL (ссылка на саму таблицу для команды /sheet, Enter — пропустить)" ""

  # DASHBOARD_URL — публичный адрес дашборда. По умолчанию http://<публичный IP>/.
  PUBLIC_IP="$(curl -s -4 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')"
  echo "DASHBOARD_URL=http://$PUBLIC_IP/" >> "$ENV_FILE"

  # Секрет для GitHub-вебхука авто-деплоя
  DEPLOY_SECRET="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)"
  echo "DEPLOY_SECRET=$DEPLOY_SECRET" >> "$ENV_FILE"

  chown "$SERVICE_USER:$SERVICE_USER" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "==> .env сохранён"
else
  echo "==> .env уже есть, не трогаю. Чтобы перенастроить — удали $ENV_FILE и запусти заново."
fi

# Подгружаем DEPLOY_SECRET для дальнейших шагов
DEPLOY_SECRET="$(grep -E '^DEPLOY_SECRET=' "$ENV_FILE" | cut -d= -f2- | tr -d '\"' | tr -d "'")"

# ------------------------------------------------------------------
# Bin: deploy.sh, backup.sh, deploy-listener.py
# ------------------------------------------------------------------
echo "==> Кладу служебные скрипты в $BIN_DIR"
install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 \
  "$REPO_DIR/deploy/deploy.sh"          "$BIN_DIR/deploy.sh"
install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 \
  "$REPO_DIR/deploy/backup.sh"          "$BIN_DIR/backup.sh"
install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 \
  "$REPO_DIR/deploy/deploy_listener.py" "$BIN_DIR/deploy_listener.py"

# ------------------------------------------------------------------
# systemd units
# ------------------------------------------------------------------
echo "==> systemd units"
install -m 0644 "$REPO_DIR/deploy/systemd/bot-metrics-bot.service"    /etc/systemd/system/
install -m 0644 "$REPO_DIR/deploy/systemd/bot-metrics-web.service"    /etc/systemd/system/
install -m 0644 "$REPO_DIR/deploy/systemd/bot-metrics-deploy.service" /etc/systemd/system/

# sudoers — service-юзеру разрешено перезапускать свои сервисы без пароля
install -m 0440 "$REPO_DIR/deploy/sudoers.d-bot-metrics" /etc/sudoers.d/bot-metrics
visudo -cf /etc/sudoers.d/bot-metrics

systemctl daemon-reload
systemctl enable bot-metrics-bot bot-metrics-web bot-metrics-deploy
systemctl restart bot-metrics-bot bot-metrics-web bot-metrics-deploy

# ------------------------------------------------------------------
# nginx — прокси на 127.0.0.1:8765 (дашборд) + /__deploy на 9876
# ------------------------------------------------------------------
echo "==> nginx"
install -m 0644 "$REPO_DIR/deploy/nginx-bot-metrics.conf" /etc/nginx/sites-available/bot-metrics.conf
ln -sf /etc/nginx/sites-available/bot-metrics.conf /etc/nginx/sites-enabled/bot-metrics.conf
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl restart nginx

# ------------------------------------------------------------------
# UFW — фаервол: разрешаем SSH и HTTP
# ------------------------------------------------------------------
echo "==> ufw"
ufw allow OpenSSH || true
ufw allow 80/tcp  || true
ufw --force enable || true

# ------------------------------------------------------------------
# cron — ежедневный бэкап
# ------------------------------------------------------------------
echo "==> cron для бэкапа"
CRON_LINE="0 3 * * * $BIN_DIR/backup.sh >> $LOG_DIR/backup.log 2>&1"
( crontab -u "$SERVICE_USER" -l 2>/dev/null | grep -v "$BIN_DIR/backup.sh" ; echo "$CRON_LINE" ) | crontab -u "$SERVICE_USER" -

echo
echo "════════════════════════════════════════════════════════════════"
echo "  Установка завершена."
echo
echo "  Дашборд:     http://$(curl -s -4 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')/"
echo "  Авто-деплой: http://$(curl -s -4 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')/__deploy/$DEPLOY_SECRET"
echo
echo "  Добавь этот URL в GitHub: Settings → Webhooks → Add webhook"
echo "  Content type: application/json. Event: Just the push event."
echo
echo "  Логи:          journalctl -u bot-metrics-bot -f"
echo "  Перезапуск:    systemctl restart bot-metrics-bot"
echo "  Ручной деплой: sudo $BIN_DIR/deploy.sh"
echo "════════════════════════════════════════════════════════════════"
