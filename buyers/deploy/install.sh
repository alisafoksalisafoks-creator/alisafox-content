#!/bin/bash
# Установка бота «Покупатели» в свою папку /opt/buyers-bot, свой сервис buyers-bot.
# «Аналитик» (/opt/alisafox-content) и БОТ_секретарь (/opt/secretary-bot) не трогаются.
#   curl -fsSL https://raw.githubusercontent.com/alisafoksalisafoks-creator/alisafox-content/claude/project-thread-qarkfk/buyers/deploy/install.sh | bash
set -e
BRANCH=${BRANCH:-claude/project-thread-qarkfk}
DIR=/opt/buyers-bot
REPO=https://github.com/alisafoksalisafoks-creator/alisafox-content.git

apt-get update -q && apt-get install -y -q git python3-venv python3-pip fonts-dejavu-core
[ -d "$DIR/.git" ] || git clone -q -b "$BRANCH" "$REPO" "$DIR"
cd "$DIR"
python3 -m venv .venv
.venv/bin/pip install -q -r buyers/requirements.txt

if [ ! -f .env ]; then
  echo
  echo "=== Ключи бота «Покупатели». Вставьте по очереди (правая кнопка → Вставить, Enter). Символы не видны — так и задумано. ==="
  umask 077
  : > .env
  ask() { local v; if [ "$3" = 1 ]; then read -rsp "$2: " v </dev/tty; echo; else read -rp "$2: " v </dev/tty; fi; echo "$1=$v" >> .env; }
  ask TELEGRAM_BOT_TOKEN "Токен бота «Покупатели» из BotFather (НЕ токен «Аналитика» или секретаря)" 1
  ask TELEGRAM_CHAT_ID "Ваш Telegram ID (владелец)" 0
  # ключ Ozon (только чтение) берём из .env «Аналитика», чтобы не вводить заново
  for k in OZON_BOT_CLIENT_ID OZON_BOT_API_KEY OZON_CLIENT_ID OZON_API_KEY; do
    grep -h "^$k=" /opt/alisafox-content/.env >> .env 2>/dev/null || true
  done
  if ! grep -q "^OZON_.*CLIENT_ID=." .env; then
    ask OZON_BOT_CLIENT_ID "Ozon Client ID" 0
    ask OZON_BOT_API_KEY "Ozon API-ключ (только чтение)" 1
  fi
  echo "Ключи сохранены в $DIR/.env (доступ только у root)."
fi

cp buyers/deploy/buyers-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now buyers-bot
systemctl restart buyers-bot
echo
echo "Готово. Бот «Покупатели» запущен. Откройте его в Telegram и нажмите /start."
echo "Первые ~20 минут бот загружает историю заказов Ozon за 40 дней — ID покупателей можно добавлять сразу."
echo "Журнал: journalctl -u buyers-bot -f   (выйти: Ctrl+C)"
