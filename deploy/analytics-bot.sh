#!/bin/bash
# Бот аналитики (@audit_alisafox_bot) на общем сервере ботов. Запуск от root:
#   bash /opt/supply-bot/src/deploy/analytics-bot.sh          — установить / обновить
#   bash /opt/supply-bot/src/deploy/analytics-bot.sh start    — запустить после того, как вписаны ключи
# Код берём тем же ключом только для чтения, что и у бота поставок. Работает от пользователя supplybot.
set -e
BRANCH=${BRANCH:-claude/project-thread-23jgj6}
DIR=/opt/alisafox-content
REPO=git@github.com:alisafoksalisafoks-creator/alisafox-content.git
AS="runuser -u supplybot --"
export GIT_SSH_COMMAND="ssh -i /opt/supply-bot/.ssh/id_ed25519 -o StrictHostKeyChecking=accept-new"

if [ "$1" = "start" ]; then
  grep -q '^TELEGRAM_BOT_TOKEN=.\+' $DIR/.env || { echo "Нет TELEGRAM_BOT_TOKEN в $DIR/.env"; exit 1; }
  systemctl enable -q alisafox-bot
  systemctl restart alisafox-bot
  sleep 5
  systemctl is-active alisafox-bot && echo ГОТОВО
  exit 0
fi

dpkg -s fonts-dejavu-core >/dev/null 2>&1 || apt-get install -y -q fonts-dejavu-core >/dev/null
if [ -d $DIR/.git ]; then
  $AS env GIT_SSH_COMMAND="$GIT_SSH_COMMAND" git -C $DIR pull -q
else
  mkdir -p $DIR && chown supplybot:supplybot $DIR
  $AS env GIT_SSH_COMMAND="$GIT_SSH_COMMAND" git clone -q -b "$BRANCH" "$REPO" $DIR
fi
[ -d $DIR/.venv ] || $AS python3 -m venv $DIR/.venv
$AS $DIR/.venv/bin/pip install -q -r $DIR/bot/requirements.txt

if [ ! -f $DIR/.env ]; then
  # общие ключи берём у бота поставок, свои ключи бота аналитики владелец вписывает сам
  {
    echo "TELEGRAM_BOT_TOKEN="
    echo "TELEGRAM_CHAT_ID=$(grep '^SUPPLY_BOT_CHATS=' /etc/supply-bot.env | cut -d= -f2 | cut -d, -f1)"
    grep -E '^(OZON_CLIENT_ID|OZON_API_KEY|MOYSKLAD_TOKEN)=' /etc/supply-bot.env
    echo "OZON_PERF_CLIENT_ID="
    echo "OZON_PERF_CLIENT_SECRET="
    echo "HEALTHCHECK_URL="
  } > $DIR/.env
  chown supplybot:supplybot $DIR/.env
  chmod 600 $DIR/.env
fi

cp $DIR/bot/deploy/alisafox-bot.service /etc/systemd/system/
mkdir -p /etc/systemd/system/alisafox-bot.service.d
printf '[Service]\nUser=supplybot\n' > /etc/systemd/system/alisafox-bot.service.d/user.conf
systemctl daemon-reload
systemctl is-active -q alisafox-bot && systemctl restart alisafox-bot
echo ГОТОВО
