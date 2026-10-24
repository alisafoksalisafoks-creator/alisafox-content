"""Telegram-бот аналитики AlisaFox (Ozon). Только чтение API.

Запуск: python -m bot.app
Переменные окружения: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (один или несколько ID через запятую),
OZON_CLIENT_ID, OZON_API_KEY; необязательно HEALTHCHECK_URL, BOT_DB.
"""
import asyncio
import logging
import os
import re
import shutil
import ssl
import tempfile
import time
from datetime import date, datetime, timedelta

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto,
                           Message)

from bot import env  # noqa: F401  (подхватывает .env до чтения ключей)
from bot import cards, dashboard, morning, restock, store
from bot.category_report import MSK
from bot.sources.moysklad import Catalog
from bot.sources.ozon import OzonClient

log = logging.getLogger("bot")
ALLOWED = {int(x) for x in os.environ.get("TELEGRAM_CHAT_ID", "").replace(" ", "").split(",") if x}
REFRESH_HOUR = 6  # обновление истории каждое утро в 6:00 МСК
MORNING_HOUR = 9  # утренняя сводка в 9:00 МСК

dp = Dispatcher()
db = store.connect()
oz = OzonClient()
waiting: dict[int, str] = {}  # кто прислал артикул: "card" — карточка, "supply" — поставки
_supply = {"at": 0.0, "rows": None}
SUPPLY_TTL = 3600  # план поставок пересчитывается не чаще раза в час


def yesterday():
    return datetime.now(MSK).date() - timedelta(days=1)


def data_day():
    return min(yesterday(), store.last_day(db) or yesterday())


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in r]
                                                 for r in rows])


MENU = kb([[("📊 Сводка за вчера", "morning")],
           [("📦 Категории", "cats"), ("🔎 Артикул", "ask_sku")],
           [("🚚 Поставки", "sup")]])
SUPPLY_MENU = kb([[("🏬 По складу", "sup:w")], [("🔎 По артикулу", "sup:a")],
                  [("📋 Что отгрузить", "sup:l")], [("⬅️ Меню", "menu")]])


def period_kb(target, kind):
    """target = c:<type_id> или s:<sku>."""
    mark = {k: ("• " if k == kind else "") for k in cards.PERIODS}
    return kb([[(f"{mark['day']}Вчера", f"p:{target}:day"), (f"{mark['week']}Неделя", f"p:{target}:week"),
                (f"{mark['month']}Месяц", f"p:{target}:month")],
               [("🚚 Подгрузка", f"r:{target}"), ("⬅️ Меню", "menu")]])


def allowed(user_id):
    return not ALLOWED or user_id in ALLOWED


async def send_sheets(bot: Bot, chats, render, caption, markup=None):
    """render(prefix) рисует листы и возвращает пути; листы уходят альбомом, подпись с кнопками — следом."""
    tmp = tempfile.mkdtemp()
    try:
        paths = await asyncio.to_thread(render, os.path.join(tmp, "sheet"))
        album = [InputMediaPhoto(media=FSInputFile(p)) for p in paths]
        for chat in chats:
            await bot.send_media_group(chat, album)
            await bot.send_message(chat, caption, reply_markup=markup)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def send_png(msg: Message, render, caption, markup=None):
    await send_sheets(msg.bot, [msg.chat.id], render, caption, markup)


async def card(msg: Message, target, kind="week", compare=None):
    t, key = target.split(":", 1)
    if t == "c":
        name = db.execute("select type_name from sku where type_id = ? limit 1", (key,)).fetchone()[0]
        data = cards.build(db, data_day(), kind, type_id=key, compare=compare)
        top = await asyncio.to_thread(top_rows, key, data["cur_period"])
        render = lambda p: dashboard.card_png(name, data, p, top)  # noqa: E731
    else:
        s = store.find_sku(db, key)
        name = f"{s['offer_id'] or s['sku']} · {dashboard.short(s['name'], 30)}"
        data = cards.build(db, data_day(), kind, sku=key, compare=compare)
        render = lambda p: dashboard.card_png(name, data, p)  # noqa: E731
    worst = next((r for r in data["funnel"] if r.get("worst")), None)
    caption = f"{name}\n{data['label']}"
    if worst:
        caption += f"\nСильнее всего просел этап «{worst['name']}»: {worst['delta_conv']:+.0f}% к конверсии."
    await send_png(msg, render, caption, period_kb(target, kind))


def top_rows(type_id, period, limit=8):
    stocks = oz.stocks()
    n = (period[1] - period[0]).days + 1
    rows = []
    for r in store.top_skus(db, *period, type_id, limit):
        prev = store.totals(db, period[0] - timedelta(days=n), period[1] - timedelta(days=n), sku=r["sku"])
        lab, col = dashboard.delta_style(cards.change(r["units"], prev["ordered_units"]))
        speed = store.totals(db, period[1] - timedelta(days=13), period[1], sku=r["sku"])["ordered_units"] / 14
        st = stocks.get(r["sku"], {})
        free = st.get("fbo", 0) + st.get("fbs", 0)
        days = free / speed if speed else None
        dcol = (dashboard.MUTED if days is None else dashboard.BAD if days < restock.MIN_DAYS
                else dashboard.EXCESS if days > restock.EXCESS_DAYS else dashboard.GOOD)
        rows.append(([(r["offer_id"] or r["sku"], dashboard.INK, True), (f"{r['units']:.0f}", dashboard.INK, False),
                      (lab, col, True), ("● " + (f"{days:.0f} дн" if days is not None else "—"), dcol, False)],
                     dashboard.short(r["name"], 62)))
    return rows


def build_supply():
    """План по всем SKU с продажами за 30 дней: остатки по складам Ozon + свой остаток, поделённый по срочности."""
    skus = [r[0] for r in db.execute("select sku from daily where day >= ? group by sku having sum(ordered_units) > 0",
                                     (str(data_day() - timedelta(days=29)),))]
    return restock.allocate(restock.plan(restock.cluster_stocks(oz, skus)), Catalog())


async def supply_rows(msg: Message):
    if _supply["rows"] is None or time.time() - _supply["at"] > SUPPLY_TTL:
        await msg.answer("Считаю остатки по всем складам и свой склад, 1–2 минуты…")
        _supply["rows"] = await asyncio.to_thread(build_supply)
        _supply["at"] = time.time()
    return _supply["rows"]


async def restock_card(msg: Message, target):
    rows = await supply_rows(msg)
    if target.startswith("c:"):
        tid = target[2:]
        skus = {r[0] for r in db.execute("select sku from sku where type_id = ?", (tid,))}
        title = db.execute("select type_name from sku where type_id = ? limit 1", (tid,)).fetchone()[0]
    else:
        s = store.find_sku(db, target[2:])
        skus, title = {s["sku"]}, s["offer_id"] or s["sku"]
    rows = [r for r in rows if r["sku"] in skus]
    summ = restock.summary(rows)
    await send_png(msg, lambda p: dashboard.restock_png(title, rows, summ, p),
                   f"Подгрузка: {title}", kb([[("🚚 Поставки", "sup"), ("⬅️ Меню", "menu")]]))


async def supply_cluster(msg: Message, i):
    rows = await supply_rows(msg)
    c = restock.by_cluster(rows)[i]
    part = sorted((r for r in rows if r["cluster"] == c["cluster"]), key=lambda r: (r["days"] or 0, -r["need"]))
    await send_png(msg, lambda p: dashboard.supply_png(
        f"Склад {c['cluster']}", f"Что везти · {c['skus']} SKU · нужно {c['need']} шт, везти {c['ship']} шт",
        part, p, by="offer"), f"Поставка на склад {c['cluster']}",
        kb([[("🏬 Другой склад", "sup:w"), ("⬅️ Поставки", "sup")]]))


async def supply_sku(msg: Message, sku):
    rows = await supply_rows(msg)
    part = [r for r in rows if r["sku"] == sku]
    if not part:
        s = store.find_sku(db, sku)
        part = restock.allocate(restock.plan(await asyncio.to_thread(restock.cluster_stocks, oz, [sku])),
                                await asyncio.to_thread(Catalog))
    if not part:
        return await msg.answer("По этому артикулу нет остатков и продаж на складах Ozon.", reply_markup=SUPPLY_MENU)
    part.sort(key=lambda r: (r["days"] or 0, -r["need"]))
    offer, own = part[0]["offer_id"], max(0, (Catalog.get_cached(part[0]["offer_id"]) if False else part[0].get("own", 0)) or 0)
    need = sum(r["need"] for r in part if r["status"] in ("urgent", "low"))
    ship = sum(r.get("ship", 0) for r in part)
    tiles = [{"name": "Нужно всего, шт", "value": need, "plain": True},
             {"name": "Везти, шт", "value": ship, "plain": True},
             {"name": "Заказать, шт", "value": need - ship, "plain": True}]
    await send_png(msg, lambda p: dashboard.supply_png(
        offer, dashboard.short(part[0]["name"], 60), part, p, by="cluster", tiles=tiles),
        f"Поставка: {offer}", kb([[("📋 Что отгрузить", "sup:l"), ("⬅️ Поставки", "sup")]]))


def render_morning(prefix):
    return dashboard.morning_png(morning.build(db, oz, data_day()), prefix)


async def push_morning(bot: Bot, chats):
    await send_sheets(bot, chats, render_morning, f"Утренняя сводка Ozon за {data_day():%d.%m.%Y}", MENU)


async def morning_card(msg: Message):
    await msg.answer("Собираю сводку, это займёт около минуты…")
    await push_morning(msg.bot, [msg.chat.id])


async def send_morning(bot: Bot):
    await push_morning(bot, ALLOWED)


@dp.message(CommandStart())
@dp.message(Command("menu"))
async def start(msg: Message):
    if not allowed(msg.from_user.id):
        return await msg.answer(f"Нет доступа. Ваш ID: {msg.from_user.id}")
    await msg.answer(f"Данные Ozon по {data_day():%d.%m.%Y}. Что смотрим?", reply_markup=MENU)


@dp.message(Command("compare"))
async def compare(msg: Message):
    """/compare OZ353NEW2 01.10-15.10 01.09-15.09"""
    if not allowed(msg.from_user.id):
        return
    m = re.match(r"/compare\s+(\S+)\s+(\d\d\.\d\d)-(\d\d\.\d\d)\s+(\d\d\.\d\d)-(\d\d\.\d\d)", msg.text or "")
    s = m and store.find_sku(db, m.group(1))
    if not s:
        return await msg.answer("Формат: /compare АРТИКУЛ 01.10-15.10 01.09-15.09")
    y = data_day().year

    def d(x):
        dd = datetime.strptime(f"{x}.{y}", "%d.%m.%Y").date()
        return dd if dd <= data_day() else dd.replace(year=y - 1)

    await msg.answer("Считаю…")
    await card(msg, f"s:{s['sku']}", compare=((d(m.group(2)), d(m.group(3))), (d(m.group(4)), d(m.group(5)))))


@dp.callback_query()
async def on_button(cb: CallbackQuery):
    if not allowed(cb.from_user.id):
        return await cb.answer("Нет доступа")
    await cb.answer()
    msg, d = cb.message, cb.data
    if d == "menu":
        await msg.answer("Что смотрим?", reply_markup=MENU)
    elif d == "morning":
        await msg.answer("Собираю сводку: финансы, реклама, остатки. Около минуты…")
        await morning_card(msg)
    elif d == "cats":
        rows = store.categories(db, data_day() - timedelta(days=29))[:24]
        buttons = [(r["type_name"][:30], f"p:c:{r['type_id']}:week") for r in rows]
        await msg.answer("Категории по выручке за 30 дней:",
                         reply_markup=kb([buttons[i:i + 2] for i in range(0, len(buttons), 2)]))
    elif d == "ask_sku":
        waiting[cb.from_user.id] = "card"
        await msg.answer("Пришлите артикул (например OZ353NEW2) или SKU Ozon.")
    elif d == "sup":
        await msg.answer("Поставки на склады Ozon. Норма — запас на 20 дней, везём до 30.", reply_markup=SUPPLY_MENU)
    elif d == "sup:w":
        cl = restock.by_cluster(await supply_rows(msg))
        if not cl:
            return await msg.answer("Везти никуда не нужно: на всех складах запас больше 20 дней.",
                                    reply_markup=SUPPLY_MENU)
        await msg.answer("Склады, куда пора везти. «Везти X из Y»: Y — нужно до 30 дней, X — есть на своём складе:", reply_markup=kb(
            [[(f"{'🔴' if c['urgent'] else '🟠'} {c['cluster'][:22]} · везти {c['ship']} из {c['need']} шт", f"sup:w:{i}")]
             for i, c in enumerate(cl[:40])] + [[("⬅️ Поставки", "sup")]]))
    elif d.startswith("sup:w:"):
        await supply_cluster(msg, int(d[6:]))
    elif d == "sup:a":
        waiting[cb.from_user.id] = "supply"
        await msg.answer("Пришлите артикул — покажу, на какие склады и сколько везти.")
    elif d == "sup:l":
        sk = restock.by_sku(await supply_rows(msg))
        if not sk:
            return await msg.answer("Отгружать нечего: запаса везде больше 20 дней.", reply_markup=SUPPLY_MENU)
        await msg.answer(f"Нужна отгрузка: {len(sk)} артикулов. Сверху — что можно везти со своего склада, "
                         "ниже — что надо заказать у поставщика:",
                         reply_markup=kb([[(f"{s['offer_id'][:20]} · {s['clusters']} скл · "
                                            + (f"везти {s['ship']} шт" if s["ship"] else "заказать"),
                                            f"sup:k:{s['sku']}")] for s in sk[:40]] + [[("⬅️ Поставки", "sup")]]))
    elif d.startswith("sup:k:"):
        await supply_sku(msg, d[6:])
    elif d.startswith("p:"):
        _, t, key, kind = d.split(":")
        await card(msg, f"{t}:{key}", kind)
    elif d.startswith("r:"):
        await msg.answer("Собираю остатки по складам…")
        await restock_card(msg, d[2:])


@dp.message(F.text)
async def on_text(msg: Message):
    if not allowed(msg.from_user.id):
        return
    s = store.find_sku(db, msg.text)
    if not s:
        return await msg.answer("Не нашёл такой артикул в продажах за 120 дней. Проверьте код.", reply_markup=MENU)
    if waiting.pop(msg.from_user.id, "card") == "supply":
        return await supply_sku(msg, s["sku"])
    await card(msg, f"s:{s['sku']}", "week")


def refresh_data():
    store.refresh(db, yesterday(), oz)
    store.set_offer_ids(db, oz.stocks())
    log.info("данные обновлены по %s", store.last_day(db))


async def scheduler(bot: Bot):
    """Обновление данных утром, сводка в 9:00 и пинг мониторинга каждые 5 минут.
    Если в 9:00 бот был выключен, сводка уйдёт сразу после запуска (до конца дня)."""
    url = os.environ.get("HEALTHCHECK_URL")
    db.execute("create table if not exists sent (day text primary key)")
    while True:
        now = datetime.now(MSK)
        today = str(now.date())
        if now.hour >= MORNING_HOUR and not db.execute("select 1 from sent where day = ?", (today,)).fetchone():
            try:
                if (store.last_day(db) or date.min) < yesterday():
                    await asyncio.to_thread(refresh_data)
                await send_morning(bot)
                db.execute("insert into sent values (?)", (today,))
                db.commit()
            except Exception:
                log.exception("утренняя сводка не отправлена")
        if now.hour == REFRESH_HOUR and now.minute < 5 or (store.last_day(db) or date.min) < yesterday() - timedelta(days=1):
            try:
                await asyncio.to_thread(refresh_data)
            except Exception:
                log.exception("обновление не удалось")
        if url:
            try:
                async with httpx.AsyncClient() as c:
                    await c.get(url, timeout=10)
            except Exception:
                log.warning("healthcheck недоступен")
        await asyncio.sleep(300)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if store.last_day(db) is None:
        log.info("первая загрузка истории за %s дней, это займёт ~10 минут", store.FIRST_LOAD_DAYS)
        await asyncio.to_thread(refresh_data)
    session = AiohttpSession()
    if os.environ.get("SSL_CERT_FILE"):  # свой корневой сертификат (прокси облачной сессии)
        session._connector_init["ssl"] = ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])
    bot = Bot(os.environ["TELEGRAM_BOT_TOKEN"], session=session)
    asyncio.create_task(scheduler(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
