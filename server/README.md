# Сервер ботов AlisaFox: автозапуск и защита от сбоев

Все боты (`/opt/*`, сервисы systemd) уже были настроены на `Restart=always` и автозапуск. Это спасает, когда **падает бот**, но не когда **зависает весь сервер**: тогда systemd тоже стоит, и пока сервер не перезагрузят, ничего не поднимется. `bots-guard.sh` закрывает этот пробел.

## Порядок (Timeweb → сервер «AlisaFox» → «Консоль», root)

1. Скачать скрипты (одна строка):
```
cd /opt/promt-bot && (git fetch -q origin claude/project-thread-9mrfpe 2>/dev/null || GIT_SSH_COMMAND="ssh -i /opt/supply-bot/.ssh/id_ed25519" git fetch -q origin claude/project-thread-9mrfpe) && git show FETCH_HEAD:server/diagnose.sh > /root/diagnose.sh && git show FETCH_HEAD:server/bots-guard.sh > /root/bots-guard.sh && echo СКАЧАНО
```
2. Диагностика (только читает): `bash /root/diagnose.sh` — отчёт придёт файлом в Telegram (токены в нём скрыты) и сохранится в `/root/diagnose-*.txt`.
3. Защита: `bash /root/bots-guard.sh`.

## Что включает bots-guard.sh

| Что | Зачем |
|---|---|
| Добавка `90-autorestart.conf` к каждому боту, `enable` | Автозапуск после загрузки, перезапуск без лимита попыток. Не пропадает при `update.sh` |
| Таймер `bots-guard.timer` (каждые 2 мин) | Включённый бот не работает → перезапуск + сообщение в Telegram (не чаще раза в час). Диск > 90%, память < 150 МБ → предупреждение |
| `bots-guard-boot.service` | После каждой перезагрузки: «🔄 Сервер перезагрузился, ✅/❌ по ботам» |
| swap 2 ГБ, earlyoom | Главная причина «зависаний» VPS — кончилась память. Теперь убивается тяжёлый процесс, systemd его поднимает |
| `kernel.panic=10`, `softlockup_panic=1` | Сбой/зависание ядра → перезагрузка через 10 с вместо вечного зависания |
| watchdog 60 с (softdog) | systemd перестал отвечать → перезагрузка |
| journald: persistent, 300 МБ | Журналы прошлой загрузки остаются для разбора, диск не забивается |

Сообщения шлёт бот аналитики (токен и чат берутся из `/opt/alisafox-content/.env`). Другой бот — `nano /etc/bots-guard.env`.

## Внешний мониторинг (рекомендуется)

Если сервер завис наглухо, сам он о себе не сообщит. Заведите бесплатную проверку на healthchecks.io (период 5 мин, уведомление в Telegram) и впишите её адрес в `/etc/bots-guard.env` → `HEALTHCHECK_URL=`. Сервер перестал пинговать → вам придёт сообщение → «Перезагрузить» в панели Timeweb.

## Команды

- Боты сейчас: `bash /root/diagnose.sh` (раздел «Боты»)
- Журнал проверок: `journalctl -t bots-guard -n 30 --no-pager`
- Новый бот поставлен → ещё раз `bash /root/bots-guard.sh`.
