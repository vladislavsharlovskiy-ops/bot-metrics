#!/usr/bin/env bash
# Восстановление потерянных лидов после ручного импорта старой bot.db.
# Делает за один запуск: inspect → merge → inspect → swap → restart.
#
# Использование:
#     sudo bash /opt/bot-metrics/repo/tools/restore-from-backup.sh
#     sudo bash /opt/bot-metrics/repo/tools/restore-from-backup.sh --dry-run
#
# --dry-run: только показывает, что получилось бы. Live не трогает.
#
# Источники не модифицируются. Перед подменой live-база сохраняется
# как bot.db.before-merge-YYYYMMDD-HHMMSS — откат в одну команду.

set -euo pipefail

DATA_DIR="/opt/bot-metrics/data"
BACKUP_DIR="$DATA_DIR/backups"
LIVE="$DATA_DIR/bot.db"
MERGED="$DATA_DIR/bot-merged.db"
TOOLS_DIR="/opt/bot-metrics/repo/tools"

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=1
fi

if [[ $EUID -ne 0 ]]; then
    echo "Запускай через sudo: sudo bash $0"
    exit 1
fi

if [[ ! -f "$LIVE" ]]; then
    echo "Живая БД не найдена: $LIVE"
    exit 1
fi

if [[ ! -d "$BACKUP_DIR" ]]; then
    echo "Каталог бэкапов не найден: $BACKUP_DIR"
    exit 1
fi

# Самый свежий bot-before-import-*.db в backups
DONOR=$(ls -1t "$BACKUP_DIR"/bot-before-import-*.db 2>/dev/null | head -1 || true)
if [[ -z "$DONOR" ]]; then
    echo "Не нашёл bot-before-import-*.db в $BACKUP_DIR"
    echo "Имеющиеся файлы:"
    ls -la "$BACKUP_DIR" || true
    exit 1
fi

echo "═══════════════════════════════════════════════════════════════════"
echo "  Восстановление лидов: live + backup → merged"
echo "═══════════════════════════════════════════════════════════════════"
echo "  LIVE  : $LIVE"
echo "  DONOR : $DONOR"
echo "  MERGED: $MERGED"
if [[ $DRY_RUN -eq 1 ]]; then
    echo "  РЕЖИМ : DRY-RUN (live не подменяется, сервисы не трогаются)"
fi
echo

echo "──────── 1. Что в обеих базах сейчас ────────"
sudo -u bot python3 "$TOOLS_DIR/db_inspect.py" "$LIVE" "$DONOR"

echo
echo "──────── 2. Слияние (запись в $MERGED) ────────"
# Перезаписываем merged-файл, если остался от прошлого запуска
[[ -f "$MERGED" ]] && sudo -u bot rm -f "$MERGED"
sudo -u bot python3 "$TOOLS_DIR/db_merge.py" "$LIVE" "$DONOR" "$MERGED"

echo
echo "──────── 3. Что получилось ────────"
sudo -u bot python3 "$TOOLS_DIR/db_inspect.py" "$MERGED"

if [[ $DRY_RUN -eq 1 ]]; then
    echo
    echo "──────────────────────────────────────────────────────"
    echo "  DRY-RUN: $MERGED создан, но live не подменяется."
    echo "  Если результат устраивает — запусти БЕЗ --dry-run:"
    echo "      sudo bash $0"
    echo "──────────────────────────────────────────────────────"
    exit 0
fi

echo
echo "──────── 4. Останавливаю сервисы ────────"
systemctl stop bot-metrics-bot bot-metrics-web

echo "──────── 5. Подменяю bot.db ────────"
TS=$(date +%Y%m%d-%H%M%S)
PRESERVED="$DATA_DIR/bot.db.before-merge-$TS"
sudo -u bot mv "$LIVE" "$PRESERVED"
sudo -u bot mv "$MERGED" "$LIVE"
# WAL-файлы старой базы тоже отодвигаем, чтобы новая стартовала чисто
for ext in -wal -shm; do
    if [[ -f "$LIVE$ext" ]]; then
        sudo -u bot mv "$LIVE$ext" "$PRESERVED$ext"
    fi
done
echo "  Старая live ушла в: $PRESERVED"
echo "  Новая live: $LIVE"

echo "──────── 6. Стартую сервисы ────────"
systemctl start bot-metrics-bot bot-metrics-web
sleep 2
systemctl is-active bot-metrics-bot bot-metrics-web || true

echo
echo "──────── 7. Финал ────────"
sudo -u bot python3 "$TOOLS_DIR/db_inspect.py" "$LIVE"

echo
echo "═══════════════════════════════════════════════════════════════════"
echo "  Готово. Если что-то не так — откат:"
echo "      systemctl stop bot-metrics-bot bot-metrics-web"
echo "      sudo -u bot mv $PRESERVED $LIVE"
echo "      systemctl start bot-metrics-bot bot-metrics-web"
echo "═══════════════════════════════════════════════════════════════════"
