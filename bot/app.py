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
from bot import cards, dashboard, morning, positions, profit, restock, signals, store
from bot.category_report import MSK
from bot.sources import gsheet_costs
from bot.sources.moysklad import Catalog, is_test
from bot.sources.ozon import OzonClient
from bot.sources.performance import PerformanceClient

log = logging.getLogger("bot")
ADMINS = {int(x) for x in os.environ.get("TELEGRAM_CHAT_ID", "").replace(" ", "").split(",") if x}  # владелец
REFRESH_HOUR = 6  # обновление истории каждое утро в 6:00 МСК
MORNING_HOUR = 9  # утренняя сводка в 9:00 МСК

dp = Dispatcher()
db = store.connect()
oz = OzonClient()
waiting: dict[int, str] = {}  # кто прислал артикул: "card" — карточка, "signals" — сигналы по остаткам
_signals = {"at": 0.0, "skus": None}
_losses = {"at": 0.0, "data": None}
LOSS_DAYS = 14  # минусовые артикулы — за последние 2 недели
SIGNALS_TTL = 3600  # сигналы по остаткам пересчитываются не чаще раза в час


def yesterday():
    return datetime.now(MSK).date() - timedelta(days=1)


def data_day():
    return min(yesterday(), store.last_day(db) or yesterday())


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in r]
                                                 for r in rows])


MENU = kb([[("📊 Сводка за вчера", "morning")],
           [("📦 Категории", "cats"), ("🔎 Артикул", "ask_sku")],
           [("🚦 Сигналы по остаткам", "sig"), ("📍 Позиции", "pos")],
           [("📉 Минусовые артикулы", "neg")]])
SIGNALS_MENU = kb([[("⏰ Пора отгрузить", "sig:s")], [("🔀 Пересорт", "sig:x")],
                   [("💸 Платное хранение", "sig:p")], [("🔎 По артикулу", "sig:a")], [("⬅️ Меню", "menu")]])
POS_MENU = kb([[("⭐ Новинки (Супер)", "pos:n")], [("📦 По категории", "pos:c")], [("🔎 Ввести артикул", "pos:a")],
               [("⬅️ Меню", "menu")]])


def period_kb(target, kind):
    """target = c:<type_id> или s:<sku>."""
    mark = {k: ("• " if k == kind else "") for k in cards.PERIODS}
    return kb([[(f"{mark['day']}Вчера", f"p:{target}:day"), (f"{mark['week']}Неделя", f"p:{target}:week"),
                (f"{mark['month']}Месяц", f"p:{target}:month")],
               [("🚦 Сигналы", f"r:{target}"), ("⬅️ Меню", "menu")]])


db.execute("create table if not exists users (id integer primary key, name text, added text)")
access_requests: dict[int, str] = {}  # кто нажал /start без доступа: id → имя


def users():
    """Пользователи, которых владелец добавил кнопкой: {id: имя}."""
    return {r[0]: r[1] for r in db.execute("select id, name from users")}


def recipients():
    return ADMINS | set(users())


def allowed(user_id):
    return user_id in ADMINS or user_id in users() or not ADMINS


def is_admin(user_id):
    return user_id in ADMINS


def menu_for(user_id):
    rows = [[(b.text, b.callback_data) for b in r] for r in MENU.inline_keyboard]
    return kb(rows + [[("👥 Пользователи", "u")]]) if is_admin(user_id) else MENU


def add_user(uid, name):
    db.execute("insert or replace into users values (?, ?, ?)", (uid, name, datetime.now(MSK).strftime("%d.%m.%Y")))
    db.commit()


def users_kb():
    rows = [[(f"❌ {name or uid}", f"u:del:{uid}")] for uid, name in users().items()]
    return kb(rows + [[("➕ Добавить по ID", "u:add")], [("⬅️ Меню", "menu")]])


def who(u):
    return " ".join(x for x in (u.first_name, u.last_name) if x) + (f" (@{u.username})" if u.username else "")


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
    for r in store.top_skus(db, *period, type_id, limit + 5):
        if is_test(r["offer_id"]) or len(rows) >= limit:
            continue
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


def build_signals():
    """Сигналы по всем SKU с продажами за 30 дней: остатки по кластерам Ozon, сезонная скорость, свой склад."""
    skus = [r[0] for r in db.execute("select sku from daily where day >= ? group by sku having sum(ordered_units) > 0",
                                     (str(data_day() - timedelta(days=29)),))]
    catalog = Catalog()
    _, by = signals.build(db, restock.cluster_stocks(oz, skus), data_day(), turnover=morning.turnover(oz),
                          catalog=catalog)
    try:
        profits = profit.by_sku(db, oz, catalog, data_day())
    except Exception:
        log.exception("прибыль по артикулам не посчиталась")
        profits = {}
    for k, v in by.items():
        v["profit"] = (profits.get(k) or {}).get("profit")
    return by


async def signal_skus(msg: Message):
    if _signals["skus"] is None or time.time() - _signals["at"] > SIGNALS_TTL:
        await msg.answer("Считаю остатки по всем кластерам с учётом сезона, 1–2 минуты…")
        _signals["skus"] = await asyncio.to_thread(build_signals)
        _signals["at"] = time.time()
    return _signals["skus"]


def sig_today():
    """Сегодня для прогноза: день после последних данных."""
    return data_day() + timedelta(days=1)


SIGNAL_LISTS = {
    "s": ("ship", signals.to_ship, "Пора отгрузить", lambda s: f"{s['offer_id'][:20]} · до {dashboard.ship_date(s['ship_by'], sig_today())}"),
    "x": ("resort", signals.resort, "Пересорт",
          lambda s: f"{s['offer_id'][:20]} · лишнее {len(s['excess'])} кл. / кончается {len(s['ship'])}"),
    "p": ("paid", signals.paid, "Платное хранение",
          lambda s: f"{s['offer_id'][:20]} · {dashboard.GRADE[s['grade']][0]}"),
}


async def signal_list(msg: Message, key, items=None, title=None, back=("⬅️ Сигналы", "sig")):
    kind, pick, name, label = SIGNAL_LISTS[key]
    items = pick(await signal_skus(msg)) if items is None else items
    await send_png(msg, lambda p: dashboard.signals_png(kind, items, sig_today(), p, title), f"{title or name}: {len(items)} SKU")
    if items:
        await msg.answer("Нажмите на артикул — покажу все кластеры:", reply_markup=kb(
            [[(label(s), f"sig:k:{s['sku']}")] for s in items[:60]] + [[back]]))


async def signal_sku(msg: Message, sku):
    by = await signal_skus(msg)
    s = by.get(sku)
    if not s:
        _, part = await asyncio.to_thread(lambda: signals.build(db, restock.cluster_stocks(oz, [sku]), data_day()))
        s = part.get(sku)
    if not s:
        return await msg.answer("По этому артикулу нет остатков и продаж в кластерах Ozon.", reply_markup=SIGNALS_MENU)
    caption = f"{s['offer_id']}: " + (f"отгрузить до {dashboard.ship_date(s['ship_by'], sig_today())}" if s["ship"]
                                      else "отгружать пока рано")
    if s["resort"]:
        caption += ", есть пересорт"
    caption += ".\nСколько штук и в какие кластеры — в боте поставок."
    await send_png(msg, lambda p: dashboard.sku_signals_png(s, sig_today(), p), caption,
                   kb([[("⏰ Пора отгрузить", "sig:s"), ("⬅️ Сигналы", "sig")]]))


async def category_signals(msg: Message, type_id):
    by = await signal_skus(msg)
    skus = {r[0] for r in db.execute("select sku from sku where type_id = ?", (type_id,))}
    title = db.execute("select type_name from sku where type_id = ? limit 1", (type_id,)).fetchone()[0]
    part = {k: v for k, v in by.items() if k in skus}
    await signal_list(msg, "s", signals.to_ship(part), f"Пора отгрузить: {dashboard.short(title, 18)}")
    resort = signals.resort(part)
    if resort:
        await signal_list(msg, "x", resort, f"Пересорт: {dashboard.short(title, 22)}")


def build_losses():
    """Прибыль по артикулам за 14 дней с рекламой каждого товара; минусовые — по категориям."""
    end = data_day()
    start = end - timedelta(days=LOSS_DAYS - 1)
    revenue = {r[0]: r[1] for r in db.execute("select sku, sum(revenue) from daily where day between ? and ? group by sku",
                                              (str(start), str(end)))}
    ads = PerformanceClient().spend_by_sku(start, end, revenue)
    rows = profit.by_sku(db, oz, Catalog(), end, start=start, ads=ads)
    cats = {}
    for sku, x in rows.items():
        if x["profit"] >= 0:
            continue
        s = db.execute("select * from sku where sku = ?", (sku,)).fetchone()
        if is_test(x["offer_id"] or (s["offer_id"] if s else "")):
            continue
        x.update(sku=sku, name=s["name"] if s else "", offer_id=x["offer_id"] or (s["offer_id"] if s else None))
        tid, tname = (s["type_id"], s["type_name"]) if s else ("", "Без категории")
        c = cats.setdefault(tid or "-", {"type_id": tid or "-", "name": tname or "Без категории", "items": [], "loss": 0.0})
        c["items"].append(x)
        c["loss"] += x["profit"]
    for c in cats.values():
        c["items"].sort(key=lambda x: x["profit"])
    return {"start": start, "end": end, "cats": sorted(cats.values(), key=lambda c: c["loss"]), "total": len(rows)}


async def losses(msg: Message):
    if _losses["data"] is None or time.time() - _losses["at"] > SIGNALS_TTL:
        await msg.answer("Считаю прибыль по каждому артикулу за 2 недели: реализация, реклама, себестоимость. 1–2 минуты…")
        _losses["data"] = await asyncio.to_thread(build_losses)
        _losses["at"] = time.time()
    return _losses["data"]


async def losses_menu(msg: Message):
    d = await losses(msg)
    n = sum(len(c["items"]) for c in d["cats"])
    loss = sum(c["loss"] for c in d["cats"])
    period = f"{d['start']:%d.%m}–{d['end']:%d.%m}"
    if not n:
        return await msg.answer(f"За {period} минусовых артикулов нет.", reply_markup=MENU)
    await msg.answer(f"За {period} в минусе {n} артикулов из {d['total']}, вместе {dashboard.num(loss)} ₽. "
                     "Выберите категорию:", reply_markup=kb(
                         [[(f"{dashboard.short(c['name'], 24)} · {len(c['items'])} · {dashboard.num(c['loss'])} ₽",
                            f"neg:c:{c['type_id']}")] for c in d["cats"][:40]] + [[("⬅️ Меню", "menu")]]))


async def losses_category(msg: Message, type_id):
    d = await losses(msg)
    c = next((c for c in d["cats"] if c["type_id"] == type_id), None)
    if not c:
        return await msg.answer("В этой категории минусовых артикулов нет.", reply_markup=MENU)
    sub = f"Ozon · {d['start']:%d.%m}–{d['end']:%d.%m} · {len(c['items'])} SKU · {dashboard.num(c['loss'])} ₽"
    await send_png(msg, lambda p: dashboard.losses_png(dashboard.short(c["name"], 30), sub, c["items"], p),
                   f"Минусовые: {c['name']}", None)
    await msg.answer("Нажмите на артикул — откроется его карточка с воронкой:", reply_markup=kb(
        [[(f"{(x['offer_id'] or x['sku'])[:22]} · {dashboard.num(x['profit'])} ₽", f"p:s:{x['sku']}:week")]
         for x in c["items"][:40]] + [[("⬅️ Категории", "neg"), ("⬅️ Меню", "menu")]]))


def render_morning(prefix):
    return dashboard.morning_png(morning.build(db, oz, data_day()), prefix)


async def push_morning(bot: Bot, chats):
    await send_sheets(bot, chats, render_morning, f"Утренняя сводка Ozon за {data_day():%d.%m.%Y}", MENU)


async def morning_card(msg: Message):
    await msg.answer("Собираю сводку, это займёт около минуты…")
    await push_morning(msg.bot, [msg.chat.id])


async def send_morning(bot: Bot):
    await push_morning(bot, recipients())


@dp.message(CommandStart())
@dp.message(Command("menu"))
async def start(msg: Message):
    u = msg.from_user
    if not allowed(u.id):
        if u.id not in access_requests:
            access_requests[u.id] = who(u)
            for admin in ADMINS:
                await msg.bot.send_message(admin, f"Запрос доступа к боту: {who(u)}, ID {u.id}", reply_markup=kb(
                    [[("✅ Дать доступ", f"u:ok:{u.id}"), ("✖️ Отклонить", f"u:no:{u.id}")]]))
        return await msg.answer(f"Нет доступа. Запрос отправлен владельцу, бот напишет, когда доступ откроют.\n"
                                f"Ваш ID: {u.id}")
    await msg.answer(f"Данные Ozon по {data_day():%d.%m.%Y}. Что смотрим?", reply_markup=menu_for(u.id))


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
        await msg.answer("Что смотрим?", reply_markup=menu_for(cb.from_user.id))
    elif d.startswith("u") and (d == "u" or d.startswith("u:")):
        if not is_admin(cb.from_user.id):
            return await msg.answer("Пользователей добавляет только владелец.")
        await users_button(msg, cb.from_user.id, d)
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
    elif d == "neg":
        await losses_menu(msg)
    elif d.startswith("neg:c:"):
        await losses_category(msg, d[6:])
    elif d == "sig":
        await msg.answer("Сигналы по остаткам Ozon: когда отгружать, где пересорт, что уходит в платное хранение. "
                         "Скорость — с учётом сезона. Штуки по кластерам считает бот поставок.", reply_markup=SIGNALS_MENU)
    elif d in ("sig:s", "sig:x", "sig:p"):
        await signal_list(msg, d[4:])
    elif d == "sig:a":
        waiting[cb.from_user.id] = "signals"
        await msg.answer("Пришлите артикул — покажу запас по кластерам и когда отгружать.")
    elif d.startswith("sig:k:"):
        await signal_sku(msg, d[6:])
    elif d.startswith("p:"):
        _, t, key, kind = d.split(":")
        await card(msg, f"{t}:{key}", kind)
    elif d == "pos" or d.startswith("pos:"):
        await pos_button(msg, cb.from_user.id, d)
    elif d.startswith("r:"):
        t, key = d[2:].split(":", 1)
        await (category_signals(msg, key) if t == "c" else signal_sku(msg, key))


async def pos_day(msg: Message):
    """Последний день с данными: позиция приходит вместе с продажами в store.daily."""
    return data_day()


async def pos_items(msg: Message, target):
    """target: n — новинки (Супер), c<type_id> — категория, s<sku> — один артикул. → (название, артикулы)."""
    if target == "n":
        return "Новинки (Супер)", await asyncio.to_thread(positions.super_skus, oz)
    if target.startswith("c"):
        tid = target[1:]
        row = db.execute("select type_name from sku where type_id = ? limit 1", (tid,)).fetchone()
        rows = store.top_skus(db, data_day() - timedelta(days=29), data_day(), tid, 50)
        items = [{"sku": r["sku"], "offer_id": r["offer_id"], "name": r["name"]} for r in rows
                 if not is_test(r["offer_id"])][:40]
        return (row[0] if row else "Категория"), items
    s = store.find_sku(db, target[1:])
    return (s["offer_id"] or s["sku"]) if s else target[1:], [dict(s)] if s else [{"sku": target[1:]}]


def excel_kb(target):
    return [("📥 Excel 7 дн", f"pos:x:{target}:7"), ("28 дн", f"pos:x:{target}:28"),
            ("свой период", f"pos:x:{target}:p")]


async def pos_list(msg: Message, target):
    day = await pos_day(msg)
    title, items = await pos_items(msg, target)
    if not items:
        return await msg.answer("Здесь нет артикулов.", reply_markup=POS_MENU)
    rows = await asyncio.to_thread(positions.summary, db, items, day)
    sub = f"Ozon · {day:%d.%m.%Y} · {len(rows)} SKU · сначала упавшие"
    await send_png(msg, lambda p: dashboard.positions_list_png(dashboard.short(title, 30), sub, rows, positions.move, p),
                   f"Позиции: {title} за {day:%d.%m.%Y}")
    back = ("⬅️ Категории", "pos:c") if target.startswith("c") else ("⬅️ Позиции", "pos")
    await msg.answer("Нажмите на артикул — покажу позицию по дням за 28 дней:", reply_markup=kb(
        [[(f"{(r['offer_id'] or r['sku'])[:20]} · {dashboard._pos(r['pos'])} {positions.move(r['pos'], r['prev'])[0]}",
           f"pos:s:{r['sku']}")] for r in rows[:60]]
        + [excel_kb(target), [("🔎 Ввести артикул", "pos:a"), back]]))


async def pos_sku(msg: Message, sku):
    day = await pos_day(msg)
    s = store.find_sku(db, sku)
    name = (s["offer_id"] or s["sku"]) if s else sku
    c = positions.sku_card(db, sku, day)
    sub = f"Ozon · {dashboard.short(s['name'], 40) if s else sku} · 28 дн по {day:%d.%m}"
    lab = positions.move(c["cur"], c["prev"])[0]
    await send_png(msg, lambda p: dashboard.position_png(name, sub, c, positions.move, p),
                   f"{name}: позиция {dashboard._pos(c['cur'])} за {day:%d.%m} ({lab} к пред. дню)",
                   kb([excel_kb(f"s{sku}"), [("⭐ Новинки", "pos:n"), ("⬅️ Позиции", "pos")]]))


def parse_period(text, last):
    """«01.10-20.10» → (start, end); год — текущий, будущие даты — прошлого года."""
    m = re.match(r"\s*(\d\d?\.\d\d)(?:\.\d{2,4})?\s*[-–—]\s*(\d\d?\.\d\d)", text or "")
    if not m:
        return None

    def d(x):
        dd = datetime.strptime(f"{x}.{last.year}", "%d.%m.%Y").date()
        return dd if dd <= last else dd.replace(year=last.year - 1)

    a, b = d(m.group(1)), d(m.group(2))
    return (a, min(b, last)) if a <= b else None


async def pos_excel(msg: Message, target, start, end):
    title, items = await pos_items(msg, target)
    tmp = tempfile.mkdtemp()
    try:
        name = re.sub(r"[^\w.-]+", "_", f"Позиции_{title}_{start:%d.%m}-{end:%d.%m}.xlsx")
        path = await asyncio.to_thread(positions.excel, db, items, start, end, os.path.join(tmp, name))
        await msg.answer_document(FSInputFile(path, filename=name),
                                  caption=f"Позиции Ozon: {title}, {start:%d.%m}–{end:%d.%m}, {len(items)} SKU")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def pos_button(msg: Message, user, d):
    if d == "pos":
        await msg.answer("Средняя позиция артикула в поиске и каталоге Ozon по дням. Что смотрим?", reply_markup=POS_MENU)
    elif d == "pos:n":
        await msg.answer("Собираю новинки со статусом «Супер»…")
        await pos_list(msg, "n")
    elif d == "pos:c":
        rows = store.categories(db, data_day() - timedelta(days=29))[:24]
        buttons = [(r["type_name"][:30], f"pos:c:{r['type_id']}") for r in rows]
        await msg.answer("Категории по выручке за 30 дней:", reply_markup=kb(
            [buttons[i:i + 2] for i in range(0, len(buttons), 2)] + [[("🔎 Ввести артикул", "pos:a"), ("⬅️ Позиции", "pos")]]))
    elif d.startswith("pos:c:"):
        await pos_list(msg, "c" + d[6:])
    elif d == "pos:a":
        waiting[user] = "pos"
        await msg.answer("Пришлите артикул (например OZ353NEW2) или SKU Ozon.")
    elif d.startswith("pos:s:"):
        await pos_sku(msg, d[6:])
    elif d.startswith("pos:x:"):
        target, n = d[6:].rsplit(":", 1)
        if n == "p":
            waiting[user] = f"posx:{target}"
            return await msg.answer("Пришлите период, например 01.10-25.10")
        end = await pos_day(msg)
        await pos_excel(msg, target, end - timedelta(days=int(n) - 1), end)


async def users_button(msg: Message, admin, d):
    if d == "u":
        n = len(users())
        await msg.answer(f"Пользователи бота: {n}. Нажмите на имя, чтобы закрыть доступ.\n"
                         "Проще всего добавить так: человек пишет боту /start, вам приходит запрос с кнопкой «Дать доступ».",
                         reply_markup=users_kb())
    elif d == "u:add":
        waiting[admin] = "adduser"
        await msg.answer("Пришлите Telegram ID пользователя (цифры) или перешлите сюда любое его сообщение.")
    elif d.startswith("u:ok:"):
        uid = int(d[5:])
        add_user(uid, access_requests.pop(uid, str(uid)))
        await msg.answer(f"Доступ открыт: {users()[uid]}.", reply_markup=users_kb())
        await msg.bot.send_message(uid, "Доступ к боту аналитики AlisaFox открыт. Нажмите /start")
    elif d.startswith("u:no:"):
        access_requests.pop(int(d[5:]), None)
        await msg.answer("Запрос отклонён.")
    elif d.startswith("u:del:"):
        uid = int(d[6:])
        db.execute("delete from users where id = ?", (uid,))
        db.commit()
        await msg.answer("Доступ закрыт.", reply_markup=users_kb())


@dp.message(F.text)
async def on_text(msg: Message):
    if not allowed(msg.from_user.id):
        return
    if waiting.get(msg.from_user.id) == "adduser":
        origin = getattr(msg, "forward_origin", None)
        fwd = getattr(origin, "sender_user", None)
        if fwd:
            uid, name = fwd.id, who(fwd)
        elif (msg.text or "").strip().isdigit():
            uid, name = int(msg.text.strip()), msg.text.strip()
        else:
            return await msg.answer("Не вижу ID. Пришлите цифры или перешлите сообщение пользователя. "
                                    "Если он скрыл аккаунт при пересылке, попросите его написать боту /start.")
        waiting.pop(msg.from_user.id)
        add_user(uid, name)
        await msg.answer(f"Добавлен: {name}. Пусть нажмёт /start в боте.", reply_markup=users_kb())
        return
    mode = waiting.get(msg.from_user.id, "")
    if mode.startswith("posx:"):
        last = await pos_day(msg)
        period = parse_period(msg.text, last)
        if not period:
            return await msg.answer(f"Не понял период. Формат: 01.10-25.10 (данные есть по {last:%d.%m}).")
        waiting.pop(msg.from_user.id)
        return await pos_excel(msg, mode[5:], *period)
    s = store.find_sku(db, msg.text)
    if not s:
        return await msg.answer("Не нашёл такой артикул в продажах за 120 дней. Проверьте код.", reply_markup=MENU)
    mode = waiting.pop(msg.from_user.id, "card")
    if mode == "signals":
        return await signal_sku(msg, s["sku"])
    if mode == "pos":
        return await pos_sku(msg, s["sku"])
    await card(msg, f"s:{s['sku']}", "week")


def refresh_data():
    try:
        log.info("себестоимость из таблицы: %s", gsheet_costs.refresh())
    except Exception:
        log.exception("таблица себестоимости не прочиталась, работаю со старым файлом")
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
