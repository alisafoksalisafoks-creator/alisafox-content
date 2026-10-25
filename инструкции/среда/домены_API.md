# Домены для облачной среды AlisaFox

Обновлено: 25.10.2026. Куда вносить: Project settings → Environment → шестерёнка у среды → **Allowed domains** (каждый домен отдельной строкой). Токены и ключи вносятся в переменные окружения той же среды; в этом файле их нет и быть не должно.

Статусы:
- **работает** — проверено из среды (дата в строке или общая проверка 25.10.2026): домен открыт, запрос с ключом вернул 200 или сервер отвечает (не прокси).
- **открыт** — домен доступен из среды, но API-запрос с ключом не делали.
- **закрыт** — прокси среды не пускает (403 от прокси или нет соединения). Нужно добавить, если понадобится.

## Ozon

| Домен | Для чего | Статус | Переменные среды |
|---|---|---|---|
| api-seller.ozon.ru | Seller API: товары, остатки, продажи, финансы, поставки FBO, вопросы | работает (POST /v1/seller/info → 200) | OZON_CLIENT_ID, OZON_API_KEY |
| api-performance.ozon.ru | Performance API: реклама, расход, ДРР | работает (получение токена → 200) | OZON_PERF_CLIENT_ID, OZON_PERF_CLIENT_SECRET |
| docs.ozon.ru | Справка и документация API Ozon (правила площадки) | открыт | — |
| seller-edu.ozon.ru | База знаний продавца Ozon (правила площадки) | открыт | — |
| ozon.ru | Витрина (карточки конкурентов) | по памяти: витрина не грузится, запрет robots.txt | — |

## Wildberries

Можно внести одной маской `*.wildberries.ru`, если поле её принимает. Иначе по одному:

| Домен | Для чего | Статус |
|---|---|---|
| common-api.wildberries.ru | Проверка связи, данные продавца | работает (GET /ping → 200) |
| content-api.wildberries.ru | Карточки товаров | работает (12.10, по памяти) |
| statistics-api.wildberries.ru | Заказы и продажи | работает (12.10, по памяти) |
| marketplace-api.wildberries.ru | FBS: склады, сборочные задания | работает (12.10, по памяти) |
| seller-analytics-api.wildberries.ru | Остатки, воронка продаж | работает (12.10, по памяти) |
| feedbacks-api.wildberries.ru | Вопросы и отзывы | работает (12.10, по памяти) |
| advert-api.wildberries.ru | Продвижение (реклама) | работает (12.10, по памяти) |
| discounts-prices-api.wildberries.ru | Цены и скидки | работает (12.10, по памяти) |
| finance-api.wildberries.ru | Баланс, финансовые отчёты | работает (12.10, по памяти) |
| documents-api.wildberries.ru | Документы продавца | работает (12.10, по памяти) |
| returns-api.wildberries.ru | Возвраты и претензии | работает (12.10, по памяти) |
| buyer-chat-api.wildberries.ru | Чат с покупателями | работает (12.10, по памяти) |
| user-management-api.wildberries.ru | Пользователи кабинета | работает (12.10, по памяти) |
| supplies-api.wildberries.ru | Поставки FBW | домен открыт, но методы складов и тарифов WB временно отключил (404) |
| dev.wildberries.ru | Документация API WB | открыт |
| seller.wildberries.ru | Справка продавца WB (правила площадки) | открыт |

Переменная среды: WB_API_TOKEN (только чтение, действует до 13.04.2027).

## Яндекс Маркет

| Домен | Для чего | Статус |
|---|---|---|
| api.partner.market.yandex.ru | Partner API Маркета, заголовок Api-Key, переменная YM_API_TOKEN | работает (25.10.2026: GET /v2/campaigns → 200) |
| yandex.ru | Справка Маркета (yandex.ru/support/marketplace) и документация API (yandex.ru/dev/market) | открыт |
| partner.market.yandex.ru | Кабинет продавца | открыт |

## Прочее

| Домен | Для чего | Статус |
|---|---|---|
| api.telegram.org | Telegram-бот (уведомления, отчёты), переменные TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID | работает (25.10.2026: getMe → 200; getChat → личный чат найден). В TELEGRAM_CHAT_ID есть лишний пробел или перенос строки — лучше убрать |
| api.moysklad.ru | МойСклад JSON API 1.2 (остатки, товары, документы). Заголовок Authorization: Bearer, переменная MOYSKLAD_TOKEN | работает (20.10.2026: справочники, остатки и заказы → 200; часть тяжёлых запросов отвечает медленно, ставить таймаут 60 с; 25.10.2026 повторно → 200) |
| truestats.ru, api.truestats.ru | TrueStats API, заголовок X-Api-Token, переменная TRUESTATS_TOKEN | открыты; ключ в среде валиден (23 и 25.10.2026: 403 «Эндпоинт недоступен по API-токену» на корне). Методы для токена пока неизвестны; app.truestats.ru (кабинет) закрыт |
| mpstats.io | MPStats | закрыт; сейчас работаем через расширение браузера, домен не нужен |
| drive.google.com, docs.google.com | Google Диск и Таблицы | не нужен в списке: работает через коннекторы Google, а не через среду |

## Все переменные среды (имена, без значений)

Проверка только чтением, 25.10.2026:

| Переменная | Сервис | Статус |
|---|---|---|
| OZON_CLIENT_ID, OZON_API_KEY | Ozon Seller API | работает |
| OZON_PERF_CLIENT_ID, OZON_PERF_CLIENT_SECRET | Ozon Performance API | работает |
| WB_API_TOKEN | Wildberries (только чтение, до 13.04.2027) | работает |
| YM_API_TOKEN | Яндекс Маркет Partner API | работает |
| MOYSKLAD_TOKEN | МойСклад | работает |
| TRUESTATS_TOKEN | TrueStats | ключ принят, методы для ключа неизвестны |
| TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID | Telegram-бот | работает |

## Как быстро проверить доступ

- Ozon: POST https://api-seller.ozon.ru/v1/seller/info → 200.
- Ozon реклама: POST https://api-performance.ozon.ru/api/client/token → 200.
- WB: GET https://common-api.wildberries.ru/ping → 200.
- Ответ 403 с текстом от прокси или код 000 — домена нет в Allowed domains.
- Яндекс Маркет: GET https://api.partner.market.yandex.ru/v2/campaigns с заголовком Api-Key → 200.
- МойСклад: GET https://api.moysklad.ru/api/remap/1.2/entity/organization (Authorization: Bearer, Accept-Encoding: gzip) → 200.
- TrueStats: любой запрос с X-Api-Token; 401 «Невалидный или истёкший API-токен» — ключ неверный, 403 «Эндпоинт недоступен» — ключ верный, метод не открыт.
- Telegram: GET https://api.telegram.org/bot<токен>/getMe → 200.
