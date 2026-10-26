#!/bin/bash
# Обновление БОТ_секретаря: bash /opt/secretary-bot/secretary/deploy/update.sh
set -e
cd /opt/secretary-bot
git pull -q
.venv/bin/pip install -q -r bot/requirements.txt
.venv/bin/python -m bot.sources.gsheet_costs
.venv/bin/python -m secretary.managers
systemctl restart secretary-bot
echo "Обновлено до: $(git log --oneline -1)"
