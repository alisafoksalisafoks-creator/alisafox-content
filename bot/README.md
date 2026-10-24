# Бот аналитики AlisaFox

Telegram-бот: утренняя сводка по категориям и ответы по артикулам. Только читает API площадок, в кабинетах ничего не меняет.

ТЗ: https://claude.ai/artifact/2kdv2wXiLdVoAzb8AM2z6W

## Ключи (только переменные окружения, не в код)
- `OZON_CLIENT_ID`, `OZON_API_KEY` — Ozon Seller API
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — отправка в Telegram

## Сейчас готово
- `python -m bot.category_card "<тип товара>" [YYYY-MM-DD] [out.png]` — карточка категории для кнопки: KPI недели к прошлой (стрелки и цвет), заказы по дням 28 дн к предыдущим 28, топ-8 SKU с остатком в днях, выводы по правилам.
- `python -m bot.category_report [YYYY-MM-DD]` — сводка Ozon по типам товаров за день: заказы шт/₽, к вчера и к тому же дню недели, дни остатка (FBO+FBS свободный / средние заказы за 14 дней), SKU с остатком меньше 14 дней.
