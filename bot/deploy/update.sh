#!/bin/bash
# Обновление бота после правок в коде. Запуск: bash /opt/alisafox-content/bot/deploy/update.sh
set -e
cd /opt/alisafox-content
git pull -q
.venv/bin/pip install -q -r bot/requirements.txt
.venv/bin/python -m bot.sources.gsheet_costs
systemctl restart alisafox-bot
echo "Обновлено до: $(git log --oneline -1)"
