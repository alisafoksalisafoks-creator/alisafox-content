#!/bin/bash
# Установка бота на чистый сервер Ubuntu 24.04 одной командой:
#   curl -fsSL https://raw.githubusercontent.com/alisafoksalisafoks-creator/alisafox-content/claude/project-thread-eb5j0j/bot/deploy/install.sh | bash
# Скрипт можно запускать повторно: что уже сделано, он пропускает.
set -e
BRANCH=${BRANCH:-claude/project-thread-eb5j0j}
DIR=/opt/alisafox-content
HTTPS=https://github.com/alisafoksalisafoks-creator/alisafox-content.git
SSH=git@github.com:alisafoksalisafoks-creator/alisafox-content.git

apt-get update -q && apt-get install -y -q git python3-venv python3-pip fonts-dejavu-core

if [ ! -d "$DIR/.git" ]; then
  if git ls-remote "$HTTPS" >/dev/null 2>&1; then
    REPO=$HTTPS  # репозиторий публичный — ключ не нужен
  else
    # репозиторий приватный — нужен ключ только для чтения (deploy key)
    [ -f ~/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519 -C "alisafox-bot-server" >/dev/null
    ssh-keyscan github.com >> ~/.ssh/known_hosts 2>/dev/null
    if ! git ls-remote "$SSH" >/dev/null 2>&1; then
      echo
      echo "=== Добавьте этот ключ в GitHub: репозиторий → Settings → Deploy keys → Add deploy key ==="
      cat ~/.ssh/id_ed25519.pub
      echo "=== Галочку «Allow write access» НЕ ставьте. Потом запустите команду установки ещё раз. ==="
      exit 1
    fi
    REPO=$SSH
  fi
  git clone -q -b "$BRANCH" "$REPO" "$DIR"
fi
cd "$DIR"
python3 -m venv .venv
.venv/bin/pip install -q -r bot/requirements.txt

if [ ! -f .env ]; then
  echo
  echo "=== Вставьте ключи по очереди (правая кнопка мыши → Вставить, потом Enter). Символы не видны — так и задумано. ==="
  umask 077
  : > .env
  ask() {  # имя, подсказка, скрыть ввод
    local v
    if [ "$3" = 1 ]; then read -rsp "$2: " v </dev/tty; echo; else read -rp "$2: " v </dev/tty; fi
    echo "$1=$v" >> .env
  }
  ask TELEGRAM_BOT_TOKEN "Токен бота из BotFather" 1
  ask TELEGRAM_CHAT_ID "Telegram ID (свой и менеджеров через запятую)" 0
  ask OZON_CLIENT_ID "Ozon Client ID" 0
  ask OZON_API_KEY "Ozon API-ключ" 1
  ask OZON_PERF_CLIENT_ID "Ozon Performance Client ID" 0
  ask OZON_PERF_CLIENT_SECRET "Ozon Performance Client Secret" 1
  ask MOYSKLAD_TOKEN "Токен МоегоСклада" 1
  ask HEALTHCHECK_URL "Ссылка Healthchecks.io (можно пропустить — просто Enter)" 0
  echo "Ключи сохранены в $DIR/.env (доступ только у root)."
fi

cp bot/deploy/alisafox-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now alisafox-bot
systemctl restart alisafox-bot
echo
echo "Готово. Бот запущен и будет сам подниматься после сбоя и перезагрузки сервера."
echo "Первый запуск 10–20 минут: бот скачивает историю Ozon. Журнал: journalctl -u alisafox-bot -f   (выйти: Ctrl+C)"
