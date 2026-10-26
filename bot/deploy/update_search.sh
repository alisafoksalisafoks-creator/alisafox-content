#!/bin/bash
# Обновление бота «Позиции и ключи». Запуск: bash /opt/alisafox-search/bot/deploy/update_search.sh
set -e
cd /opt/alisafox-search
git pull -q
.venv/bin/pip install -q -r bot/requirements.txt
systemctl restart alisafox-search
echo "Обновлено до: $(git log --oneline -1)"
