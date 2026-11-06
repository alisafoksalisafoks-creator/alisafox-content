#!/bin/bash
# Диагностика: почему сервер завис и почему боты не поднялись. Только читает, ничего не меняет.
# Запуск от root: bash /root/diagnose.sh
# Отчёт: на экран, в /root/diagnose-<дата>.txt и, если найден токен бота, файлом в Telegram.
OUT=/root/diagnose-$(date +%F-%H%M).txt
bots() { grep -l '^WorkingDirectory=/opt/' /etc/systemd/system/*.service 2>/dev/null | xargs -r -n1 basename | sed 's/\.service$//'; }
# токены и ключи в журналах прячем, чтобы отчёт можно было переслать
mask() { sed -E 's#bot[0-9]{6,}:[A-Za-z0-9_-]{20,}#bot***#g; s#((KEY|TOKEN|SECRET|PASSWORD|Api-Key|Client-Id)[^=: ]*[=: ]+)[^ ]+#\1***#Ig'; }
h() { echo; echo "===== $* ====="; }

{
h "Сейчас"; date; echo "Сервер работает с: $(uptime -s)"; uptime
h "Перезагрузки и выключения"; last -x reboot shutdown 2>/dev/null | head -8
h "Загрузки в журнале"; journalctl --list-boots --no-pager 2>/dev/null | tail -5
h "Как закончилась прошлая загрузка (последние 30 строк)"
journalctl -b -1 -n 30 --no-pager 2>&1 | mask
h "Нехватка памяти, зависания ядра, ошибки диска в прошлой загрузке"
journalctl -b -1 --no-pager 2>/dev/null | grep -iE 'out of memory|oom-kill|killed process|soft lockup|hung_task|blocked for more than|i/o error|no space left|watchdog' | tail -25 | mask
echo "(пусто = таких записей нет)"
h "Память, swap, диск, процессор"; free -h; swapon --show; df -h / /var; echo "Ядер: $(nproc)"
h "Размер журналов"; journalctl --disk-usage
h "Боты"
printf '%-22s %-10s %-10s %-8s %s\n' СЕРВИС АВТОЗАПУСК СЕЙЧАС РЕСТАРТЫ "ЗАПУЩЕН"
for b in $(bots); do
  printf '%-22s %-10s %-10s %-8s %s\n' "$b" "$(systemctl is-enabled $b 2>/dev/null)" "$(systemctl is-active $b)" \
    "$(systemctl show -p NRestarts --value $b)" "$(systemctl show -p ActiveEnterTimestamp --value $b)"
done
h "Упавшие сервисы"; systemctl --failed --no-legend --no-pager; echo "(пусто = нет)"
for b in $(bots); do
  h "$b: ошибки с момента загрузки"
  journalctl -u $b -b -p warning -n 8 --no-pager 2>&1 | mask
done
h "Сторож (watchdog) и защита"
ls /dev/watchdog* 2>/dev/null || echo "устройства watchdog нет"
systemctl is-active earlyoom bots-guard.timer 2>/dev/null | paste -sd' ' | sed 's/^/earlyoom, bots-guard.timer: /'
sysctl kernel.panic kernel.softlockup_panic 2>/dev/null
} 2>&1 | tee "$OUT"

# Отправить отчёт файлом в Telegram, если на сервере есть токен бота
for f in /etc/bots-guard.env /opt/alisafox-content/.env; do [ -f $f ] && . <(grep -E '^(ALERT_BOT_TOKEN|ALERT_CHAT_ID|TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID)=' $f); done
T=${ALERT_BOT_TOKEN:-$TELEGRAM_BOT_TOKEN}; C=${ALERT_CHAT_ID:-${TELEGRAM_CHAT_ID%%,*}}
if [ -n "$T" ] && [ -n "$C" ]; then
  echo "url = \"https://api.telegram.org/bot$T/sendDocument\"" | curl -sS -m 30 -K - -F chat_id="$C" -F document=@"$OUT" -F caption="Диагностика сервера $(hostname)" -o /dev/null \
    && echo && echo "Отчёт отправлен в Telegram." || echo "Отправить в Telegram не вышло, отчёт в $OUT"
else
  echo; echo "Отчёт сохранён: $OUT"
fi
