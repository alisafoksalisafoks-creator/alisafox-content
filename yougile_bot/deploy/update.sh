#!/bin/bash
# Обновление бота после правок. Запуск: bash /opt/yougile-bot/yougile_bot/deploy/update.sh
set -e
cd /opt/yougile-bot
git pull -q 2>/dev/null || GIT_SSH_COMMAND="ssh -i /opt/supply-bot/.ssh/id_ed25519" git pull -q
.venv/bin/pip install -q -r yougile_bot/requirements.txt
systemctl restart yougile-bot
sleep 3
echo "yougile-bot: $(systemctl is-active yougile-bot), версия $(git log --oneline -1)"
