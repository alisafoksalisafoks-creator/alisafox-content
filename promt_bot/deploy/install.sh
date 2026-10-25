#!/bin/bash
# Установка бота «Промт инженер» на сервер AlisaFox (рядом с ботами поставок и аналитики).
# Запуск: bash /opt/promt-bot/promt_bot/deploy/install.sh
set -e
BRANCH=${BRANCH:-claude/project-thread-n5x765}
DIR=/opt/promt-bot
REPO=git@github.com:alisafoksalisafoks-creator/alisafox-content.git

apt-get install -y -q git python3-venv python3-pip >/dev/null

[ -d "$DIR/.git" ] || git clone -q -b "$BRANCH" "$REPO" "$DIR"
cd "$DIR"
python3 -m venv .venv
.venv/bin/pip install -q -r promt_bot/requirements.txt
mkdir -p /var/lib/promt-bot

if [ ! -f .env ] || ! grep -q '^TELEGRAM_PROMT_BOT_TOKEN=.' .env || ! grep -q '^ANTHROPIC_API_KEY=.' .env; then
  [ -f .env ] || { cp promt_bot/env.example .env; chmod 600 .env; }
  echo
  echo "=== Впишите токен бота и ключ Claude: nano $DIR/.env  (сохранить: Ctrl+O, Enter, выйти: Ctrl+X) ==="
  echo "=== Потом запустите этот скрипт ещё раз: bash $DIR/promt_bot/deploy/install.sh ==="
  exit 1
fi

cp promt_bot/deploy/promt-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q --now promt-bot
systemctl restart promt-bot
sleep 3
echo
echo "promt-bot: $(systemctl is-active promt-bot), версия $(git log --oneline -1)"
echo "Готово. Напишите боту /start. Журнал: journalctl -u promt-bot -n 50 --no-pager"
