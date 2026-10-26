#!/bin/bash
# Установка БОТ_секретаря рядом с «Аналитиком» — в свою папку /opt/secretary-bot, свой сервис secretary-bot.
# «Аналитик» (/opt/alisafox-content, сервис alisafox-bot) не трогается.
#   curl -fsSL https://raw.githubusercontent.com/alisafoksalisafoks-creator/alisafox-content/claude/project-thread-zvp8jw/secretary/deploy/install.sh | bash
set -e
BRANCH=${BRANCH:-claude/project-thread-zvp8jw}
DIR=/opt/secretary-bot
REPO=https://github.com/alisafoksalisafoks-creator/alisafox-content.git

apt-get update -q && apt-get install -y -q git python3-venv python3-pip fonts-dejavu-core
[ -d "$DIR/.git" ] || git clone -q -b "$BRANCH" "$REPO" "$DIR"
cd "$DIR"
python3 -m venv .venv
.venv/bin/pip install -q -r bot/requirements.txt

if [ ! -f .env ]; then
  echo
  echo "=== Ключи БОТ_секретаря. Вставьте по очереди (правая кнопка → Вставить, Enter). Символы не видны — так и задумано. ==="
  umask 077
  : > .env
  ask() { local v; if [ "$3" = 1 ]; then read -rsp "$2: " v </dev/tty; echo; else read -rp "$2: " v </dev/tty; fi; echo "$1=$v" >> .env; }
  ask TELEGRAM_BOT_TOKEN "Токен БОТ_секретаря из BotFather (НЕ токен «Аналитика»)" 1
  ask TELEGRAM_CHAT_ID "Ваш Telegram ID (владелец)" 0
  # свой ключ Ozon секретаря (только чтение), чтобы не делить лимиты с «Аналитиком»
  ask OZON_BOT_CLIENT_ID "Ozon Client ID для секретаря" 0
  ask OZON_BOT_API_KEY "Ozon API-ключ секретаря (только чтение)" 1
  # остальное (общий ключ Ozon на запас, Performance, МойСклад) — из .env «Аналитика», чтобы не вводить заново
  for k in OZON_CLIENT_ID OZON_API_KEY OZON_PERF_CLIENT_ID OZON_PERF_CLIENT_SECRET MOYSKLAD_TOKEN; do
    grep -h "^$k=" /opt/alisafox-content/.env >> .env 2>/dev/null || true
  done
  ask HEALTHCHECK_URL "Ссылка Healthchecks.io (можно пропустить — Enter)" 0
  echo "WEEKLY_SEND=0" >> .env
  echo "Ключи сохранены в $DIR/.env (доступ только у root)."
fi

.venv/bin/python -m bot.sources.gsheet_costs
.venv/bin/python -m secretary.managers
cp secretary/deploy/secretary-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now secretary-bot
systemctl restart secretary-bot
echo
echo "Готово. БОТ_секретарь запущен. Откройте его в Telegram и нажмите /start."
echo "Первое нажатие кнопки займёт 2–5 минут: бот копирует историю у «Аналитика» и докачивает рекламу и прибыль."
echo "Журнал: journalctl -u secretary-bot -f   (выйти: Ctrl+C)"
