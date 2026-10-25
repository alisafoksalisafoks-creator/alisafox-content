# Бот «Промт инженер AlisaFox»

Telegram-бот, который делает промпты, инструкции проектов и скилы для магазина AlisaFox. Внутри — Claude (по умолчанию Opus 5.5) с системным промптом промпт-инженера v3 (`system_prompt.md`, собран из инструкций всех проектов магазина).

## Что умеет

- Меню внизу экрана: **🧩 Скил**, **📋 Инструкция**, **✍️ Промпт**, **💬 Консультация** (если не знаете, что нужно), **🆕 Новый диалог**.
- Перед работой всегда задаёт уточняющие вопросы **тестом**: каждый вопрос отдельным сообщением с кнопками вариантов, рекомендованный со ⭐, плюс «✍️ Свой ответ».
- Готовый промпт или инструкцию присылает текстом и файлом `.md`, скил — файлом `SKILL.md`. Очень длинные тексты — только файлом.
- Читает присланные `.md`, `.txt`, `.pdf` и скриншоты (например, промпт на аудит).
- **👥 Доступ** (видит только владелец): список пользователей, «Убрать», «Добавить по ID». Новый человек пишет боту /start — владельцу приходит «✅ Пустить / ❌ Отказать».
- Помнит диалог, пока не нажать «Новый диалог» или другой режим.

Бот ничего не меняет в кабинетах и таблицах: у него нет ключей площадок, только ключ Claude.

## Установка на сервер Timeweb «AlisaFox»

Панель Timeweb → сервер «AlisaFox» → «Консоль», вход под root. Вставить и нажать Enter:

```
git clone -b claude/project-thread-n5x765 git@github.com:alisafoksalisafoks-creator/alisafox-content.git /opt/promt-bot && bash /opt/promt-bot/promt_bot/deploy/install.sh
```

Скрипт остановится и попросит вписать ключи: `nano /opt/promt-bot/.env` → `TELEGRAM_PROMT_BOT_TOKEN` (от @BotFather) и `ANTHROPIC_API_KEY` (console.anthropic.com). Сохранить Ctrl+O, Enter, выйти Ctrl+X. Затем ещё раз:

```
bash /opt/promt-bot/promt_bot/deploy/install.sh
```

Должно появиться «promt-bot: active». Бот поднимается сам после сбоя и перезагрузки сервера.

## Команды

- Обновить после правок: `bash /opt/promt-bot/promt_bot/deploy/update.sh`
- Проверить все боты: `systemctl is-active supply-bot alisafox-bot promt-bot`
- Журнал: `journalctl -u promt-bot -n 50 --no-pager`

## Файлы

- Код: `/opt/promt-bot`, ключи: `/opt/promt-bot/.env`, данные (доступы и диалоги): `/var/lib/promt-bot`.
- Поменять промпт-инженера: правка `promt_bot/system_prompt.md` → `update.sh`.
- Модель и глубина: `PROMT_BOT_MODEL`, `PROMT_BOT_EFFORT` в `.env`.

## Тесты

```
python -m unittest promt_bot.tests.test_render
```
