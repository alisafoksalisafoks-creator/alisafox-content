#!/bin/bash
# Установка бота на чистый сервер Ubuntu 24.04. Запуск: bash install.sh
set -e
BRANCH=${BRANCH:-claude/project-thread-eb5j0j}
DIR=/opt/alisafox-content
REPO=git@github.com:alisafoksalisafoks-creator/alisafox-content.git

apt-get update -q && apt-get install -y -q git python3-venv python3-pip fonts-dejavu-core

if [ ! -f ~/.ssh/id_ed25519 ]; then
  ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519 -C "alisafox-bot-server" >/dev/null
fi
ssh-keyscan github.com >> ~/.ssh/known_hosts 2>/dev/null

if ! git ls-remote "$REPO" >/dev/null 2>&1; then
  echo
  echo "=== Добавьте этот ключ в GitHub: репозиторий → Settings → Deploy keys → Add deploy key ==="
  cat ~/.ssh/id_ed25519.pub
  echo "=== Галочку «Allow write access» НЕ ставьте. Потом запустите этот скрипт ещё раз. ==="
  exit 1
fi

[ -d "$DIR" ] || git clone -q -b "$BRANCH" "$REPO" "$DIR"
cd "$DIR"
python3 -m venv .venv
.venv/bin/pip install -q -r bot/requirements.txt

if [ ! -f .env ]; then
  cp bot/env.example .env
  chmod 600 .env
  echo
  echo "=== Впишите ключи: nano $DIR/.env  (сохранить: Ctrl+O, Enter, выйти: Ctrl+X) ==="
  echo "=== Потом запустите этот скрипт ещё раз. ==="
  exit 1
fi

cp bot/deploy/alisafox-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now alisafox-bot
systemctl restart alisafox-bot
echo
echo "Готово. Бот запущен и будет сам подниматься после сбоя и перезагрузки сервера."
echo "Журнал: journalctl -u alisafox-bot -f   (выйти: Ctrl+C)"
