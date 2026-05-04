# Деплой на TimeWeb Cloud (или любой Ubuntu-сервер)

Один раз заходишь на сервер по SSH, выполняешь команды ниже — всё установится и
запустится. Дальше код обновляется автоматически: `git push` → сервер сам
подтягивает свежий код и перезапускает бота.

## Установка с нуля

```bash
git clone https://github.com/vladislavsharlovskiy-ops/bot-metrics.git /opt/bot-metrics/repo
cd /opt/bot-metrics/repo
bash deploy/install.sh
```

Скрипт сам спросит токены (BOT_TOKEN, OWNER_ID, и т.д.) и сохранит их в
`/opt/bot-metrics/.env`. Если хочется не интерактивно — заранее положи
переменные в окружение перед запуском.

## Обновление кода (вручную)

```bash
sudo /opt/bot-metrics/bin/deploy.sh
```

Скрипт делает `git pull`, ставит новые зависимости, прогоняет миграции БД,
перезапускает сервисы.

## Автодеплой при `git push`

Установка настраивает `deploy-listener` — маленький http-сервис, который
слушает GitHub-вебхук и при пуше в `main` запускает `deploy.sh`.

После установки добавь в GitHub: `Settings → Webhooks → Add webhook`:
- Payload URL: `http://<IP>/__deploy/<DEPLOY_SECRET>`
- Content type: `application/json`
- Events: `Just the push event`

`<DEPLOY_SECRET>` сохранён в `/opt/bot-metrics/.env` (его покажет установщик
в конце).

## Структура каталогов

```
/opt/bot-metrics/
├── repo/        # git-клон, обновляется при deploy
├── data/        # bot.db (живая база лидов, бэкапится)
├── venv/        # Python venv с зависимостями
├── logs/        # логи systemd
├── bin/         # вспомогательные скрипты (deploy.sh, backup.sh)
└── .env         # секреты (chmod 600)
```

## Сервисы systemd

```
bot-metrics-bot.service       # Telegram-бот (aiogram)
bot-metrics-web.service       # дашборд (Flask + gunicorn) на 127.0.0.1:8765
bot-metrics-deploy.service    # http-вебхук для авто-деплоя
```

Управление:
```bash
systemctl status  bot-metrics-bot bot-metrics-web bot-metrics-deploy
systemctl restart bot-metrics-bot
journalctl -u bot-metrics-bot -f -n 100
```

## Бэкапы

Cron каждый день в 3:00 МСК запускает `bin/backup.sh`, который шлёт `bot.db`
владельцу в Telegram (тот же бот). Хранится 7 последних в `/opt/bot-metrics/data/backups/`.

## Перенос базы со старого сервера (BotHost)

1. На BotHost скачай `bot.db` (через панель или `cat /app/data/bot.db | base64`).
2. На новом сервере замени файл:
   ```bash
   systemctl stop bot-metrics-bot bot-metrics-web
   mv /opt/bot-metrics/data/bot.db /opt/bot-metrics/data/bot.db.fresh
   # положи сюда старый bot.db (через scp / nano + base64 -d)
   chown bot:bot /opt/bot-metrics/data/bot.db
   systemctl start bot-metrics-bot bot-metrics-web
   ```
