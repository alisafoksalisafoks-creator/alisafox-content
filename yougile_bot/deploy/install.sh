#!/bin/bash
# Установка бота YouGile на сервер AlisaFox (рядом с другими ботами).
# Запуск: bash /opt/yougile-bot/yougile_bot/deploy/install.sh
set -e
BRANCH=${BRANCH:-claude/project-thread-nl3oqt}
DIR=/opt/yougile-bot
REPO=git@github.com:alisafoksalisafoks-creator/alisafox-content.git

apt-get install -y -q git python3-venv python3-pip >/dev/null

[ -d "$DIR/.git" ] || git clone -q -b "$BRANCH" "$REPO" "$DIR" \
  || GIT_SSH_COMMAND="ssh -i /opt/supply-bot/.ssh/id_ed25519" git clone -q -b "$BRANCH" "$REPO" "$DIR"
cd "$DIR"
python3 -m venv .venv
.venv/bin/pip install -q -r yougile_bot/requirements.txt
# голосовой «Ввод» — по желанию: если не встанет, бот работает и без него
.venv/bin/pip install -q -r yougile_bot/requirements-voice.txt || echo "Голосовой ввод не установлен, остальное работает"
mkdir -p /var/lib/yougile-bot

if [ ! -f .env ] || ! grep -q '^TELEGRAM_YOUGILE_BOT_TOKEN=.' .env || ! grep -q '^YOUGILE_API_KEY=.' .env; then
  if [ ! -f .env ]; then
    cp yougile_bot/env.example .env; chmod 600 .env
    # секрет для адреса событий YouGile и IP сервера — подставляем сами
    IP=$(curl -s -4 ifconfig.me || hostname -I | awk '{print $1}')
    sed -i "s|^YG_WEBHOOK_URL=.*|YG_WEBHOOK_URL=http://$IP:8090/yougile/$(openssl rand -hex 16)|" .env
    # ключ Claude уже есть у бота «Промт инженер» — берём оттуда, чтобы не вводить заново
    K=$(grep -s '^ANTHROPIC_API_KEY=.' /opt/promt-bot/.env | head -1)
    [ -n "$K" ] && sed -i "s|^ANTHROPIC_API_KEY=.*|$K|" .env
  fi
  echo
  echo "=== Впишите токен бота и ключ YouGile: nano $DIR/.env  (сохранить: Ctrl+O, Enter, выйти: Ctrl+X) ==="
  echo "=== Потом запустите этот скрипт ещё раз: bash $DIR/yougile_bot/deploy/install.sh ==="
  exit 1
fi

# порт для событий YouGile, если включён файрвол
command -v ufw >/dev/null && ufw status | grep -q active && ufw allow 8090/tcp >/dev/null || true

cp yougile_bot/deploy/yougile-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q --now yougile-bot
systemctl restart yougile-bot
sleep 5
echo
echo "yougile-bot: $(systemctl is-active yougile-bot), версия $(git log --oneline -1)"
echo "Готово. Напишите боту /start. Журнал: journalctl -u yougile-bot -n 50 --no-pager"
