#!/bin/bash
# Обновление бота после правок. Запуск: bash /opt/promt-bot/promt_bot/deploy/update.sh
set -e
cd /opt/promt-bot
git pull -q
.venv/bin/pip install -q -r promt_bot/requirements.txt
systemctl restart promt-bot
sleep 3
echo "promt-bot: $(systemctl is-active promt-bot), версия $(git log --oneline -1)"
