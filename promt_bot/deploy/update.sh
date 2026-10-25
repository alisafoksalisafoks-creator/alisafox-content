#!/bin/bash
# Обновление бота после правок. Запуск: bash /opt/promt-bot/promt_bot/deploy/update.sh
set -e
cd /opt/promt-bot
# свой ключ root, а если его нет в GitHub — ключ бота поставок (только чтение)
git pull -q 2>/dev/null || GIT_SSH_COMMAND="ssh -i /opt/supply-bot/.ssh/id_ed25519" git pull -q
.venv/bin/pip install -q -r promt_bot/requirements.txt
systemctl restart promt-bot
sleep 3
echo "promt-bot: $(systemctl is-active promt-bot), версия $(git log --oneline -1)"
