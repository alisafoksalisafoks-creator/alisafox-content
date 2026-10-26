#!/bin/bash
# Отдельный бот «Позиции и ключи» на том же сервере, что и бот аналитики. Бот аналитики не трогает.
#   curl -fsSL https://raw.githubusercontent.com/alisafoksalisafoks-creator/alisafox-content/claude/project-thread-xas3ls/bot/deploy/install_search.sh | bash
# Своя папка /opt/alisafox-search, своя база, свой токен Telegram. Ключи Ozon берёт из .env бота аналитики.
set -e
BRANCH=${BRANCH:-claude/project-thread-xas3ls}
DIR=/opt/alisafox-search
MAIN_ENV=/opt/alisafox-content/.env
HTTPS=https://github.com/alisafoksalisafoks-creator/alisafox-content.git

apt-get update -q && apt-get install -y -q git python3-venv python3-pip fonts-dejavu-core
[ -d "$DIR/.git" ] || git clone -q -b "$BRANCH" "$HTTPS" "$DIR"
cd "$DIR"
git pull -q
python3 -m venv .venv
.venv/bin/pip install -q -r bot/requirements.txt

if [ ! -f .env ]; then
  umask 077
  : > .env
  echo
  echo "=== Вставьте токен НОВОГО бота из @BotFather (правая кнопка мыши → Вставить, потом Enter). Символы не видны — так и задумано. ==="
  read -rsp "Токен нового бота: " v </dev/tty; echo
  echo "TELEGRAM_BOT_TOKEN=$v" >> .env
  if [ -f "$MAIN_ENV" ]; then
    # Ozon, Performance и ID владельца — те же, что у бота аналитики; токен Telegram у него НЕ берём
    grep -E '^(TELEGRAM_CHAT_ID|OZON_[A-Z_]+|OZON_PERF_[A-Z_]+)=' "$MAIN_ENV" >> .env || true
    echo "Ключи Ozon и ваш Telegram ID скопированы из $MAIN_ENV."
  else
    read -rp "Telegram ID (свой и менеджеров через запятую): " v </dev/tty; echo "TELEGRAM_CHAT_ID=$v" >> .env
    read -rp "Ozon Client ID: " v </dev/tty; echo "OZON_CLIENT_ID=$v" >> .env
    read -rsp "Ozon API-ключ: " v </dev/tty; echo; echo "OZON_API_KEY=$v" >> .env
    read -rp "Ozon Performance Client ID: " v </dev/tty; echo "OZON_PERF_CLIENT_ID=$v" >> .env
    read -rsp "Ozon Performance Client Secret: " v </dev/tty; echo; echo "OZON_PERF_CLIENT_SECRET=$v" >> .env
  fi
  echo "Ключи сохранены в $DIR/.env (доступ только у root)."
fi

cp bot/deploy/alisafox-search.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now alisafox-search
systemctl restart alisafox-search
echo
echo "Готово. Бот «Позиции и ключи» запущен. Бот аналитики работает как раньше."
echo "Первый запуск 15–20 минут: бот скачивает историю Ozon. Журнал: journalctl -u alisafox-search -f   (выйти: Ctrl+C)"
