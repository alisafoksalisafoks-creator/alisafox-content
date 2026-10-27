# Бот «Цены AlisaFox» (Ozon)

Telegram-бот для цен и скидки от количества на Ozon. Всё по кнопкам, ничего не меняет в Ozon без «✅ Да, применить».

## Что умеет

**💰 Цены** → категория → артикул → дашборд на одном экране:
- текущая цена, цена с вашими акциями и цена у покупателя;
- вилка акции «Эластичный бустинг» (колонки AY/AZ шаблона цен): «снизьте на 14 ₽ — попадёте в акцию», «снизьте на 144 ₽ — макс. бустинг»;
- ЧП на 1 шт за 10 дней, ДРР общий и РК ДРР — из TrueStats;
- соинвест сегодня и ▲/▼ к вчера: (цена с вашими акциями − цена покупателя) / цена с вашими акциями;
- текущая скидка от количества;
- среднее место в выдаче и ▲/▼ к вчера.

Кнопки: **✏️ Изменить цену** → ввод цены → «было → стало», прогноз ЧП и ДРР, предупреждения → «✅ Да, применить». **✅ Оставить**, **⬅️ Назад**.

**📦 Скидка от количества** → категория → артикул или «Вся категория» → **➕ Поставить / ✏️ Изменить / 🗑 Удалить**. Уровни пишутся так: `2-5, 3-10` (от 2 шт −5%, от 3 шт −10%), до 4 уровней, только в %. Через минуту бот проверяет, применил ли Ozon.

**🔄 Обновить товары** — перечитать каталог (новые карточки). **👥 Доступ** — только владельцу, как в промпт-боте.

### Откуда цифры
| Что | Источник |
|---|---|
| Цена, мин. цена, комиссия | Ozon `/v5/product/info/prices` |
| Цена покупателя, соинвест | Ozon `/v1/product/prices/details` (нужен Premium Pro) |
| Вилка бустинга AY/AZ | Ozon `/v2/actions/products` и `/v2/actions/candidates`, поля `price_min_elastic` / `price_max_elastic` |
| Скидка от количества | Ozon `/v1/product/stairway-discount/by-quantity/get` и `/set` (бета-методы Ozon) |
| Место в выдаче | Ozon `/v1/analytics/data`, метрика `position_category` |
| ЧП на 1 шт, ДРР, РК ДРР | TrueStats, отчёт по товарам: `averageProfitPerPiece`, `drr`, `drrz` |

Прогноз ЧП при новой цене: `ЧП + (новая − текущая) × (1 − комиссия Ozon − общий ДРР) × (1 − 7% УСН)`. Это оценка, не факт.

«Вчера» для соинвеста и места в выдаче бот берёт из своих снимков (каждые 3 часа). Сравнение появится на второй день после запуска.

## Установка на сервер Timeweb «AlisaFox»

1. В @BotFather создайте нового бота (/newbot), скопируйте токен.
2. Панель Timeweb → сервер «AlisaFox» → «Консоль», вход под root. Вставить и нажать Enter:

```
git clone -b claude/project-thread-xqdb0s git@github.com:alisafoksalisafoks-creator/alisafox-content.git /opt/price-bot; bash /opt/price-bot/price_bot/deploy/install.sh
```

3. Скрипт попросит ключи: `nano /opt/price-bot/.env` → токен бота, `OZON_CLIENT_ID`, `OZON_API_KEY`, `TRUESTATS_MCP_URL`, `TRUESTATS_API_KEY`. Сохранить Ctrl+O, Enter, выйти Ctrl+X. Затем ещё раз:

```
bash /opt/price-bot/price_bot/deploy/install.sh
```

Должно появиться «price-bot: active».

## Команды

- Обновить после правок: `bash /opt/price-bot/price_bot/deploy/update.sh`
- Проверить все боты: `systemctl is-active supply-bot alisafox-bot promt-bot price-bot`
- Журнал: `journalctl -u price-bot -n 50 --no-pager`
- Кто что менял: `cat /var/lib/price-bot/changes.log`

## Файлы

Код: `/opt/price-bot`, ключи: `/opt/price-bot/.env`, данные (доступы, каталог, снимки, журнал изменений): `/var/lib/price-bot`.
