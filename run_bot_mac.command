#!/bin/bash
cd "$(dirname "$0")"
if [ ! -f .env ]; then
  cp bot/env.example .env
  echo "Впишите ключи в файл .env и запустите снова."
  open -e .env
  exit 0
fi
if [ ! -d .venv ]; then
  echo "Первый запуск: ставлю библиотеки..."
  python3 -m venv .venv && .venv/bin/pip install -q -r bot/requirements.txt
fi
while true; do
  echo "Бот запущен. Не закрывайте это окно."
  .venv/bin/python -m bot.app
  echo "Бот остановился, перезапуск через 10 секунд..."
  sleep 10
done
