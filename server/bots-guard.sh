#!/bin/bash
# Защита ботов AlisaFox от сбоев сервера. Запуск от root: bash /root/bots-guard.sh
# Можно запускать повторно (например, после установки нового бота) — ничего не задвоится.
# Что делает:
#  1. Все боты из /opt: автозапуск при загрузке + перезапуск при любом падении, без лимита попыток.
#  2. Проверка каждые 2 минуты: включённый бот не работает — перезапуск и сообщение в Telegram.
#  3. Сообщение в Telegram после каждой перезагрузки сервера: какие боты поднялись.
#  4. Защита от зависания: swap 2 ГБ, earlyoom (не даёт памяти кончиться), автоперезагрузка при сбое ядра,
#     сторож watchdog, ограничение размера журналов.
#  5. Пинг внешнего мониторинга (HEALTHCHECK_URL), чтобы узнать о зависании, когда сервер молчит.
set -e
[ "$(id -u)" = 0 ] || { echo "Запустите от root"; exit 1; }
say() { echo "— $*"; }

# ---------- 1. Автозапуск и перезапуск каждого бота ----------
BOTS=$(grep -l '^WorkingDirectory=/opt/' /etc/systemd/system/*.service 2>/dev/null | xargs -r -n1 basename | sed 's/\.service$//')
[ -n "$BOTS" ] || { echo "Не нашёл ботов в /etc/systemd/system"; exit 1; }
for b in $BOTS; do
  mkdir -p /etc/systemd/system/$b.service.d
  # отдельный файл-добавка: не пропадёт, когда install.sh/update.sh бота перезапишут основной .service
  cat > /etc/systemd/system/$b.service.d/90-autorestart.conf <<UNIT
[Unit]
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Restart=always
RestartSec=10
UNIT
done
systemctl daemon-reload
for b in $BOTS; do systemctl enable -q $b; done
say "автозапуск и перезапуск: $(echo $BOTS | tr ' ' ',')"

# ---------- 2-3. Проверка каждые 2 минуты и сообщение после загрузки ----------
if [ ! -f /etc/bots-guard.env ]; then
  # берём токен и чат у бота аналитики, если он есть; можно вписать другой бот вручную
  [ -f /opt/alisafox-content/.env ] && . <(grep -E '^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|HEALTHCHECK_URL)=' /opt/alisafox-content/.env)
  { echo "ALERT_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}"; echo "ALERT_CHAT_ID=${TELEGRAM_CHAT_ID%%,*}"; echo "HEALTHCHECK_URL=${HEALTHCHECK_URL}"; } > /etc/bots-guard.env
  chmod 600 /etc/bots-guard.env
fi

cat > /usr/local/bin/bots-guard-check <<'CHECK'
#!/bin/bash
# bots-guard-check        — проверка (по таймеру каждые 2 минуты)
# bots-guard-check boot   — отчёт после загрузки сервера
. /etc/bots-guard.env 2>/dev/null
send() {
  [ -n "$ALERT_BOT_TOKEN" ] && [ -n "$ALERT_CHAT_ID" ] || return 0
  echo "url = \"https://api.telegram.org/bot$ALERT_BOT_TOKEN/sendMessage\"" | \
    curl -sS -m 15 -K - --data-urlencode chat_id="$ALERT_CHAT_ID" --data-urlencode text="$1" -o /dev/null || true
}
BOTS=$(grep -l '^WorkingDirectory=/opt/' /etc/systemd/system/*.service 2>/dev/null | xargs -r -n1 basename | sed 's/\.service$//')
mkdir -p /run/bots-guard
msg=""
for b in $BOTS; do
  [ "$(systemctl is-enabled $b 2>/dev/null)" = enabled ] || continue   # выключенные вручную не трогаем
  systemctl is-active -q $b && continue
  systemctl reset-failed $b 2>/dev/null; systemctl restart $b
  sleep 5
  st=$(systemctl is-active $b)
  logger -t bots-guard "$b был остановлен, перезапуск: $st"
  # одно сообщение по боту не чаще раза в час
  if [ -z "$(find /run/bots-guard/$b -mmin -60 2>/dev/null)" ]; then
    touch /run/bots-guard/$b
    msg+="$b не работал → перезапуск: $([ $st = active ] && echo '✅ работает' || echo "❌ $st, журнал: journalctl -u $b -n 50")"$'\n'
  fi
done
disk=$(df --output=pcent / | tail -1 | tr -dc 0-9)
mem=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
if [ "$disk" -ge 90 ] && [ -z "$(find /run/bots-guard/disk -mmin -360 2>/dev/null)" ]; then touch /run/bots-guard/disk; msg+="⚠️ Диск занят на $disk%"$'\n'; fi
if [ "$mem" -lt 150 ] && [ -z "$(find /run/bots-guard/mem -mmin -60 2>/dev/null)" ]; then touch /run/bots-guard/mem; msg+="⚠️ Свободной памяти $mem МБ"$'\n'; fi

if [ "$1" = boot ]; then
  list=""
  for b in $BOTS; do
    [ "$(systemctl is-enabled $b 2>/dev/null)" = enabled ] || continue
    list+="$(systemctl is-active -q $b && echo ✅ || echo ❌) $b"$'\n'
  done
  msg="🔄 Сервер $(hostname) перезагрузился в $(uptime -s | cut -c1-16)"$'\n'"$list$msg"
fi
[ -n "$msg" ] && send "$msg"
[ -n "$HEALTHCHECK_URL" ] && curl -fsS -m 10 -o /dev/null "$HEALTHCHECK_URL" || true
exit 0
CHECK
chmod 755 /usr/local/bin/bots-guard-check

cat > /etc/systemd/system/bots-guard.service <<'UNIT'
[Unit]
Description=AlisaFox: проверка, что все боты работают
[Service]
Type=oneshot
ExecStart=/usr/local/bin/bots-guard-check
UNIT
cat > /etc/systemd/system/bots-guard.timer <<'UNIT'
[Unit]
Description=AlisaFox: проверка ботов каждые 2 минуты
[Timer]
OnBootSec=3min
OnUnitActiveSec=2min
[Install]
WantedBy=timers.target
UNIT
cat > /etc/systemd/system/bots-guard-boot.service <<'UNIT'
[Unit]
Description=AlisaFox: сообщение в Telegram после загрузки сервера
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStartPre=/bin/sleep 90
ExecStart=/usr/local/bin/bots-guard-check boot
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable -q --now bots-guard.timer
systemctl enable -q bots-guard-boot.service
say "проверка каждые 2 минуты и отчёт после загрузки включены"

# ---------- 4. Защита от зависания ----------
# swap: без него при нехватке памяти сервер «замирает»
if [ -z "$(swapon --show --noheadings)" ]; then
  free_gb=$(df --output=avail -BG / | tail -1 | tr -dc 0-9)
  if [ "$free_gb" -ge 5 ]; then
    fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
    chmod 600 /swapfile; mkswap -q /swapfile; swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    say "swap 2 ГБ создан"
  else
    say "swap НЕ создан: на диске меньше 5 ГБ свободно"
  fi
else
  say "swap уже есть"
fi

# earlyoom: при нехватке памяти убивает самый тяжёлый процесс, systemd его тут же поднимает
if apt-get install -y -q earlyoom >/dev/null 2>&1; then
  systemctl enable -q --now earlyoom; say "earlyoom включён"
else
  say "earlyoom не установился (apt), пропускаю"
fi

# журналы: храним после перезагрузки (для разбора), но не больше 300 МБ
mkdir -p /etc/systemd/journald.conf.d /var/log/journal
printf '[Journal]\nStorage=persistent\nSystemMaxUse=300M\n' > /etc/systemd/journald.conf.d/90-alisafox.conf
systemctl restart systemd-journald
say "журналы: сохраняются после перезагрузки, лимит 300 МБ"

# сбой или зависание ядра — автоперезагрузка через 10 секунд
cat > /etc/sysctl.d/90-alisafox-autoreboot.conf <<'SYS'
kernel.panic = 10
kernel.panic_on_oops = 1
kernel.softlockup_panic = 1
vm.swappiness = 10
SYS
sysctl -q --system || true
say "автоперезагрузка при сбое ядра: через 10 секунд"

# сторож: если система перестанет отвечать на 60 секунд — перезагрузка
if [ ! -e /dev/watchdog ]; then
  modprobe softdog 2>/dev/null && echo softdog > /etc/modules-load.d/softdog.conf
fi
if [ -e /dev/watchdog ]; then
  mkdir -p /etc/systemd/system.conf.d
  printf '[Manager]\nRuntimeWatchdogSec=60\n' > /etc/systemd/system.conf.d/90-watchdog.conf
  systemctl daemon-reexec
  say "сторож watchdog: 60 секунд"
else
  say "сторож watchdog недоступен на этом сервере"
fi

# ---------- Итог ----------
echo
printf '%-22s %-10s %s\n' БОТ АВТОЗАПУСК СЕЙЧАС
for b in $BOTS; do printf '%-22s %-10s %s\n' $b "$(systemctl is-enabled $b)" "$(systemctl is-active $b)"; done
echo
. /etc/bots-guard.env
[ -n "$ALERT_BOT_TOKEN" ] && echo "Сообщения о сбоях придут в Telegram (чат $ALERT_CHAT_ID)." \
  || echo "Токена для сообщений нет. Чтобы получать сообщения о сбоях: nano /etc/bots-guard.env (ALERT_BOT_TOKEN, ALERT_CHAT_ID)."
[ -n "$HEALTHCHECK_URL" ] || echo "Внешний мониторинг не задан: впишите HEALTHCHECK_URL в /etc/bots-guard.env (см. README)."
echo "Готово. Проверка: bash /root/bots-guard.sh можно запускать повторно; журнал проверок: journalctl -t bots-guard"
