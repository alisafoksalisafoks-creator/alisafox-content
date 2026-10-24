"""Telegram-бот аналитики AlisaFox (Ozon). Только чтение API.

Запуск: python -m bot.app
Переменные окружения: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (один или несколько ID через запятую),
OZON_CLIENT_ID, OZON_API_KEY; необязательно HEALTHCHECK_URL, BOT_DB.
"""
import asyncio
import logging
import os
import re
import tempfile
from datetime import date, datetime, timedelta

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot import cards, dashboard, restock, store
from bot.category_report import MSK
from bot.sources.ozon import OzonClient

log = logging.getLogger("bot")
ALLOWED = {int(x) for x in os.environ.get("TELEGRAM_CHAT_ID", "").replace(" ", "").split(",") if x}
REFRESH_HOUR = 6  # обновление истории каждое утро в 6:00 МСК

dp = Dispatcher()
db = store.connect()
oz = OzonClient()
waiting_sku: set[int] = set()


def yesterday():
    return datetime.now(MSK).date() - timedelta(days=1)


def data_day():
    return min(yesterday(), store.last_day(db) or yesterday())


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in r]
                                                 for r in rows])


MENU = kb([[("📦 Категории", "cats"), ("🔎 Артикул", "ask_sku")],
           [("🚚 Что подгрузить", "restock_all")]])


def period_kb(target, kind):
    """target = c:<type_id> или s:<sku>."""
    mark = {k: ("• " if k == kind else "") for k in cards.PERIODS}
    return kb([[(f"{mark['day']}Вчера", f"p:{target}:day"), (f"{mark['week']}Неделя", f"p:{target}:week"),
                (f"{mark['month']}Месяц", f"p:{target}:month")],
               [("🚚 Подгрузка", f"r:{target}"), ("⬅️ Меню", "menu")]])


def allowed(user_id):
    return not ALLOWED or user_id in ALLOWED


async def send_png(msg: Message, render, caption, markup=None):
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        path = f.name
    await asyncio.to_thread(render, path)
    await msg.answer_photo(FSInputFile(path), caption=caption, reply_markup=markup)
    os.unlink(path)


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


async def restock_card(msg: Message, target=None):
    end = data_day()
    if target is None:
        skus = [r[0] for r in db.execute(
            "select sku from daily where day >= ? group by sku order by sum(revenue) desc limit 200",
            (str(end - timedelta(days=13)),))]
        title = "весь ассортимент"
    elif target.startswith("c:"):
        tid = target[2:]
        skus = [r["sku"] for r in store.top_skus(db, end - timedelta(days=13), end, tid, limit=100)]
        title = db.execute("select type_name from sku where type_id = ? limit 1", (tid,)).fetchone()[0]
    else:
        s = store.find_sku(db, target[2:])
        skus, title = [s["sku"]], s["offer_id"] or s["sku"]
    rows = restock.plan(await asyncio.to_thread(restock.cluster_stocks, oz, skus))
    summ = restock.summary(rows)
    await send_png(msg, lambda p: dashboard.restock_png(title, rows, summ, p),
                   f"Подгрузка: {title}", kb([[("⬅️ Меню", "menu")]]))


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
    elif d == "cats":
        rows = store.categories(db, data_day() - timedelta(days=29))[:24]
        buttons = [(r["type_name"][:30], f"p:c:{r['type_id']}:week") for r in rows]
        await msg.answer("Категории по выручке за 30 дней:",
                         reply_markup=kb([buttons[i:i + 2] for i in range(0, len(buttons), 2)]))
    elif d == "ask_sku":
        waiting_sku.add(cb.from_user.id)
        await msg.answer("Пришлите артикул (например OZ353NEW2) или SKU Ozon.")
    elif d == "restock_all":
        await msg.answer("Собираю остатки по складам, около минуты…")
        await restock_card(msg)
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
    waiting_sku.discard(msg.from_user.id)
    await card(msg, f"s:{s['sku']}", "week")


def refresh_data():
    store.refresh(db, yesterday(), oz)
    store.set_offer_ids(db, oz.stocks())
    log.info("данные обновлены по %s", store.last_day(db))


async def scheduler():
    """Обновление данных утром и пинг мониторинга каждые 5 минут."""
    url = os.environ.get("HEALTHCHECK_URL")
    while True:
        now = datetime.now(MSK)
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
    bot = Bot(os.environ["TELEGRAM_BOT_TOKEN"])
    asyncio.create_task(scheduler())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
