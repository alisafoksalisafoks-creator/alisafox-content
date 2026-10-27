#!/bin/bash
# Обновление бота «Покупатели»: bash /opt/buyers-bot/buyers/deploy/update.sh
set -e
cd /opt/buyers-bot
git pull -q
.venv/bin/pip install -q -r buyers/requirements.txt
systemctl restart buyers-bot
echo "Обновлено до: $(git log --oneline -1)"
