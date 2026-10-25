#!/bin/bash
# Автообновление ботов на сервере: раз в 10 минут проверяет новую версию кода и перезапускает бота.
#   bash /opt/supply-bot/src/deploy/autoupdate.sh install   — один раз: поставить таймер (от root)
#   bash /opt/supply-bot/src/deploy/autoupdate.sh           — проверка и обновление (запускает таймер)
# Если новый код не собирается (ошибка в Python), откатывает на прежнюю версию и бота не трогает.
set -u
SELF=/opt/supply-bot/src/deploy/autoupdate.sh

if [ "${1:-}" = "install" ]; then
  cat > /etc/systemd/system/bots-autoupdate.service <<UNIT
[Unit]
Description=AlisaFox: автообновление ботов
[Service]
Type=oneshot
ExecStart=/bin/bash $SELF
UNIT
  cat > /etc/systemd/system/bots-autoupdate.timer <<UNIT
[Unit]
Description=AlisaFox: проверка обновлений ботов каждые 10 минут
[Timer]
OnBootSec=2min
OnUnitActiveSec=10min
[Install]
WantedBy=timers.target
UNIT
  systemctl daemon-reload
  systemctl enable --now bots-autoupdate.timer
  bash $SELF
  systemctl is-active bots-autoupdate.timer && echo ГОТОВО
  exit 0
fi

# update <папка с кодом> <venv> <requirements> <служба> <пакет для проверки>
update() {
  local dir=$1 venv=$2 req=$3 unit=$4 pkg=$5
  [ -d "$dir/.git" ] || return 0
  # git — от supplybot: его ключ (/opt/supply-bot/.ssh) читает GitHub, и root не трогает чужой репозиторий
  local AS="runuser -u supplybot --"
  $AS git -C "$dir" fetch -q origin || { echo "$unit: нет связи с GitHub"; return 0; }
  local old new
  old=$($AS git -C "$dir" rev-parse HEAD)
  new=$($AS git -C "$dir" rev-parse '@{u}')
  [ "$old" = "$new" ] && return 0
  $AS git -C "$dir" merge -q --ff-only '@{u}' || { echo "$unit: не удалось обновить"; return 0; }
  if ! $AS git -C "$dir" diff --quiet "$old" "$new" -- "$req"; then
    runuser -u supplybot -- "$venv/bin/pip" install -q -r "$dir/$req"
  fi
  if ! runuser -u supplybot -- "$venv/bin/python" -m compileall -q "$dir/$pkg" >/dev/null; then
    echo "$unit: новая версия с ошибкой, откатываю на ${old:0:7}"
    $AS git -C "$dir" reset -q --hard "$old"
    return 0
  fi
  systemctl restart "$unit"
  echo "$unit: обновлён ${old:0:7} → ${new:0:7}"
}

update /opt/supply-bot/src /opt/supply-bot/venv supply_bot/requirements.txt supply-bot supply_bot
if systemctl list-unit-files alisafox-bot.service >/dev/null 2>&1 && [ -f /opt/alisafox-content/.env ]; then
  update /opt/alisafox-content /opt/alisafox-content/.venv bot/requirements.txt alisafox-bot bot
fi
exit 0
