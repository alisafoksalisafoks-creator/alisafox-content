#!/bin/bash
# Установка бота цен на сервер AlisaFox (рядом с ботами поставок, аналитики и промптов).
# Запуск: bash /opt/price-bot/price_bot/deploy/install.sh
set -e
BRANCH=${BRANCH:-claude/project-thread-xqdb0s}
DIR=/opt/price-bot
REPO=git@github.com:alisafoksalisafoks-creator/alisafox-content.git

apt-get install -y -q git python3-venv python3-pip >/dev/null

[ -d "$DIR/.git" ] || git clone -q -b "$BRANCH" "$REPO" "$DIR" \
  || GIT_SSH_COMMAND="ssh -i /opt/supply-bot/.ssh/id_ed25519" git clone -q -b "$BRANCH" "$REPO" "$DIR"
cd "$DIR"
python3 -m venv .venv
.venv/bin/pip install -q -r price_bot/requirements.txt
mkdir -p /var/lib/price-bot

need() { grep -q "^$1=." .env; }
if [ ! -f .env ] || ! need TELEGRAM_PRICE_BOT_TOKEN || ! need OZON_CLIENT_ID || ! need OZON_API_KEY; then
  [ -f .env ] || { cp price_bot/env.example .env; chmod 600 .env; }
  echo
  echo "=== Впишите токен бота, ключи Ozon и TrueStats: nano $DIR/.env  (сохранить: Ctrl+O, Enter, выйти: Ctrl+X) ==="
  echo "=== Потом запустите этот скрипт ещё раз: bash $DIR/price_bot/deploy/install.sh ==="
  exit 1
fi

cp price_bot/deploy/price-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q --now price-bot
systemctl restart price-bot
sleep 3
echo
echo "price-bot: $(systemctl is-active price-bot), версия $(git log --oneline -1)"
echo "Готово. Напишите боту /start. Журнал: journalctl -u price-bot -n 50 --no-pager"
