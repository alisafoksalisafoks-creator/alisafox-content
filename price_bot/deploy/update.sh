#!/bin/bash
# Обновление бота после правок. Запуск: bash /opt/price-bot/price_bot/deploy/update.sh
set -e
cd /opt/price-bot
# свой ключ root, а если его нет в GitHub — ключ бота поставок (только чтение)
git pull -q 2>/dev/null || GIT_SSH_COMMAND="ssh -i /opt/supply-bot/.ssh/id_ed25519" git pull -q
.venv/bin/pip install -q -r price_bot/requirements.txt
systemctl restart price-bot
sleep 3
echo "price-bot: $(systemctl is-active price-bot), версия $(git log --oneline -1)"
