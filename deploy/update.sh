#!/bin/bash
# Обновить бота поставок на сервере вручную, когда в код внесли правку. Запуск от root одной командой:
#   bash /opt/supply-bot/src/deploy/update.sh
# Берёт новую версию кода и перезапускает бота поставок. Если новый код не собирается (ошибка в Python),
# откатывает на прежнюю версию, и бот продолжает работать на старой.
# Весь скрипт в фигурных скобках: bash читает его целиком до запуска, и git может спокойно заменить этот файл.
{
set -u
# таймер автообновления больше не используем: обновляем только по команде
if [ -f /etc/systemd/system/bots-autoupdate.timer ]; then
  systemctl disable -q --now bots-autoupdate.timer
  rm -f /etc/systemd/system/bots-autoupdate.timer /etc/systemd/system/bots-autoupdate.service
  systemctl daemon-reload
fi

# update <папка с кодом> <venv> <requirements> <служба> <пакет для проверки>
update() {
  local dir=$1 venv=$2 req=$3 unit=$4 pkg=$5
  [ -d "$dir/.git" ] || return 1
  # git — от supplybot: его ключ (/opt/supply-bot/.ssh) читает GitHub, и root не трогает чужой репозиторий
  local AS="runuser -u supplybot --"
  $AS git -C "$dir" fetch -q origin || { echo "$unit: нет связи с GitHub"; return 1; }
  local old new
  old=$($AS git -C "$dir" rev-parse HEAD)
  new=$($AS git -C "$dir" rev-parse '@{u}')
  $AS git -C "$dir" merge -q --ff-only '@{u}' || { echo "$unit: не удалось обновить"; return 1; }
  if ! $AS git -C "$dir" diff --quiet "$old" "$new" -- "$req"; then
    $AS "$venv/bin/pip" install -q -r "$dir/$req"
  fi
  if ! $AS "$venv/bin/python" -m compileall -q "$dir/$pkg" >/dev/null; then
    echo "$unit: новая версия с ошибкой, откатываю на ${old:0:7}"
    $AS git -C "$dir" reset -q --hard "$old"
    return 1
  fi
  systemctl restart "$unit"
  sleep 3
  systemctl is-active -q "$unit" || { echo "$unit: не запустился"; return 1; }
  echo "$unit: работает, версия ${new:0:7}"
}

ok=1
update /opt/supply-bot/src /opt/supply-bot/venv supply_bot/requirements.txt supply-bot supply_bot || ok=0
# бот аналитики обновляется своей командой: bash /opt/alisafox-content/bot/deploy/update.sh
[ $ok = 1 ] && echo ГОТОВО
exit 0
}
