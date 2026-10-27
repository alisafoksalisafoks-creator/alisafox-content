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

[ -f .env ] || { cp price_bot/env.example .env; chmod 600 .env; }

# спрашиваем ключи по очереди; уже вписанные не трогаем. Ввод не показывается на экране.
ask() {
  grep -q "^$1=." .env && return
  local v=""
  while [ -z "$v" ]; do
    read -r -s -p "$2: " v </dev/tty; echo
    v=$(echo "$v" | tr -d '[:space:]')
    [ -z "$v" ] && [ -n "$3" ] && return  # необязательный ключ — можно пропустить Enter
  done
  grep -v "^$1=" .env > .env.tmp; echo "$1=$v" >> .env.tmp; mv .env.tmp .env; chmod 600 .env
  echo "  ✓ сохранено"
}
echo
echo "=== Вставьте ключи по очереди (правая кнопка мыши → Вставить, потом Enter). На экране они не видны — так и задумано. ==="
ask TELEGRAM_PRICE_BOT_TOKEN "Токен бота от @BotFather"
ask OZON_CLIENT_ID "Ozon Client ID"
ask OZON_API_KEY "Ozon API-ключ"
ask TRUESTATS_MCP_URL "Адрес API TrueStats (нет — просто Enter)" optional
ask TRUESTATS_API_KEY "Ключ TrueStats (нет — просто Enter)" optional

cp price_bot/deploy/price-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q --now price-bot
systemctl restart price-bot
sleep 3
echo
echo "price-bot: $(systemctl is-active price-bot), версия $(git log --oneline -1)"
echo "Готово. Напишите боту /start. Журнал: journalctl -u price-bot -n 50 --no-pager"
