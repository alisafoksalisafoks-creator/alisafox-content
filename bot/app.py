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
from bot import cards, coinvest, dashboard, keywords, morning, positions, prices, profit, restock, signals, stockfilter, store
from bot.category_report import MSK
from bot.sources import gsheet_costs, truestats
from bot.sources.moysklad import Catalog, is_test
from bot.sources.ozon import OzonClient
from bot.sources.performance import PerformanceClient

log = logging.getLogger("bot")
ADMINS = {int(x) for x in os.environ.get("TELEGRAM_CHAT_ID", "").replace(" ", "").split(",") if x}  # владелец
REFRESH_HOUR = 6  # обновление истории каждое утро в 6:00 МСК
KEYWORDS_HOUR = 3  # ночная загрузка запросов и рекламных фраз для «Ключей»
MORNING_HOUR = 9  # утренняя сводка в 9:00 МСК

dp = Dispatcher()
db = store.connect()
oz = OzonClient()
waiting: dict[int, str] = {}  # кто прислал артикул: "card" — карточка, "signals" — сигналы по остаткам
_signals = {"at": 0.0, "skus": None}
_losses = {"at": 0.0, "data": None}
_actions = {"at": 0.0, "data": None}
ACTIONS_TTL = 1800  # акции Ozon перечитываются не чаще раза в 30 минут
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
           [("🚦 Сигналы по остаткам", "sig")],
           [("📍 Позиции", "pos"), ("🔑 Ключи", "kw")],
           [("📉 Минусовые артикулы", "neg"), ("🤝 Соинвест", "co")],
           [("📈 TrueStats", "ts")]])
TS_MENU = kb([[("Вчера", "ts:y"), ("Неделя", "ts:w")], [("📅 Свой период", "ts:p")], [("⬅️ Меню", "menu")]])
KW_MENU = kb([[("📂 Категория", "kw:c"), ("🔎 Артикул", "kw:a")], [("⬅️ Меню", "menu")]])
SIGNALS_MENU = kb([[("⏰ Пора отгрузить", "sig:s")], [("🔀 Пересорт", "sig:x")],
                   [("💸 Платное хранение", "sig:p")], [("🔎 По артикулу", "sig:a")], [("⬅️ Меню", "menu")]])
POS_MENU = kb([[("⭐ Новинки (Супер)", "pos:n")], [("📦 По категории", "pos:c")], [("🔎 Ввести артикул", "pos:a")],
               [("⬅️ Меню", "menu")]])


def period_kb(target, kind):
    """target = c:<type_id> или s:<sku>."""
    mark = {k: ("• " if k == kind else "") for k in cards.PERIODS}
    return kb([[(f"{mark['day']}Вчера", f"p:{target}:day"), (f"{mark['week']}Неделя", f"p:{target}:week"),
                (f"{mark['month']}Месяц", f"p:{target}:month")],
               [("🚦 Сигналы", f"r:{target}")] + ([("💰 Цены", f"pr:{target[2:]}")] if target.startswith("s:") else [])
               + [("⬅️ Меню", "menu")]])


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
    markup = period_kb(target, kind)
    if t == "c":  # из категории — в артикул: там «💰 Цены» и «🚦 Сигналы»
        caption += "\nНажмите на артикул — откроется его карточка с кнопкой «💰 Цены». Рядом — остаток FBO."
        try:
            fbo = await asyncio.to_thread(oz.stocks)
        except Exception as e:  # noqa: BLE001 — без остатков кнопки всё равно нужны
            log.warning("остатки для кнопок: %r", e)
            fbo = {}
        items = [r for r in store.top_skus(db, data_day() - timedelta(days=29), data_day(), key, 200)
                 if not is_test(r["offer_id"]) and not (fbo and fbo.get(str(r["sku"]), {}).get("fbo", 0) < 1)][:30]
        buttons = [(dashboard.short(r["offer_id"] or r["sku"], 18)
                    + (f" · {fbo.get(str(r['sku']), {}).get('fbo', 0)} шт" if fbo else ""), f"p:s:{r['sku']}:week")
                   for r in items]
        markup = kb([buttons[i:i + 2] for i in range(0, len(buttons), 2)]
                    + [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard])
    await send_png(msg, render, caption, markup)


def top_rows(type_id, period, limit=8):
    stocks = oz.stocks()
    n = (period[1] - period[0]).days + 1
    rows = []
    for r in store.top_skus(db, *period, type_id, limit + 40):
        if is_test(r["offer_id"]) or len(rows) >= limit:
            continue
        if stocks and stocks.get(str(r["sku"]), {}).get("fbo", 0) < 1:
            continue  # без остатка на FBO в топ не берём (владелец 2026-10-29)
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
    by = {k: v for k, v in by.items() if not stockfilter.no_fbo(oz, k)}
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
        if is_test(x["offer_id"] or (s["offer_id"] if s else "")) or stockfilter.no_fbo(oz, sku):
            continue
        x.update(sku=sku, name=s["name"] if s else "", offer_id=x["offer_id"] or (s["offer_id"] if s else None))
        tid, tname = (s["type_id"], s["type_name"]) if s else ("", "Без категории")
        c = cats.setdefault(tid or "-", {"type_id": tid or "-", "name": tname or "Без категории", "items": [], "loss": 0.0})
        c["items"].append(x)
        c["loss"] += x["profit"]
    for c in cats.values():
        c["items"].sort(key=lambda x: x["profit"])
    return {"start": start, "end": end, "cats": sorted(cats.values(), key=lambda c: c["loss"]), "total": len(rows),
            "all": rows}


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


def kw_categories(action):
    rows = store.categories(db, data_day() - timedelta(days=27))[:30]
    buttons = [(r["type_name"][:30], f"kw:{action}:{r['type_id']}") for r in rows]
    extra = [[("✏️ Ввести артикул вручную", "kw:m")]] if action == "al" else []
    return kb([buttons[i:i + 2] for i in range(0, len(buttons), 2)] + extra + [[("⬅️ Ключи", "kw")]])


def kw_days_kb(target, back):
    return kb([[(f"{n} дней", f"kw:{target}:{n}") for n in keywords.DAYS], [back, ("⬅️ Меню", "menu")]])


def kw_sku_back(sku):
    row = db.execute("select type_id from sku where sku = ?", (str(sku),)).fetchone()
    return ("⬅️ Артикулы категории", f"kw:al:{row[0]}") if row and row[0] else ("⬅️ Ключи", "kw")


async def kw_articles(msg: Message, type_id):
    end = data_day()
    await asyncio.to_thread(stockfilter.load, oz)
    items = [r for r in store.top_skus(db, end - timedelta(days=27), end, type_id, 120)
             if not is_test(r["offer_id"]) and not stockfilter.no_fbo(oz, r["sku"])][:40]
    name = db.execute("select type_name from sku where type_id = ? limit 1", (type_id,)).fetchone()[0]
    rows = [[(f"{(r['offer_id'] or r['sku'])[:16]} · {dashboard.short(r['name'] or '', 18)}", f"kw:sd:{r['sku']}")]
            for r in items]
    await msg.answer(f"{name}: артикулы по выручке за 28 дней. Выберите или введите вручную:", reply_markup=kb(
        rows + [[("✏️ Ввести вручную", "kw:m")], [("⬅️ Категории", "kw:a"), ("⬅️ Меню", "menu")]]))


_perf = None


def perf():
    global _perf
    _perf = _perf or PerformanceClient()
    return _perf


def kw_build(kind, key, days):
    today = datetime.now(MSK).date()
    end = keywords.ready_day(oz, db, today)
    if kind == "s":
        return keywords.article(db, oz, perf(), key, end, days, today)
    return keywords.category(db, oz, key, end, days, today)


async def kw_show(msg: Message, kind, key, days):
    await msg.answer("Собираю запросы и рекламу. Если ночью данные не подгрузились, это 1–2 минуты…")
    try:
        data = await asyncio.to_thread(kw_build, kind, key, days)
    except Exception:
        log.exception("ключи не собрались")
        return await msg.answer("Не получилось собрать запросы: Ozon не ответил. Попробуйте через пару минут.",
                                reply_markup=KW_MENU)
    other = [(f"{n} дней", f"kw:{kind}p:{key}:{n}") for n in keywords.DAYS if n != days]
    back = kw_sku_back(key) if kind == "s" else ("⬅️ Категории", "kw:c")
    markup = kb([[("⬇️ Скачать Excel", f"kw:x{kind}:{key}:{days}")] + other, [back, ("⬅️ Меню", "menu")]])
    if kind == "s":
        caption = (f"{data['offer_id']} · {data['start']:%d.%m}–{data['end']:%d.%m}\n"
                   f"Заказы {dashboard.num(data['total_orders'])} шт: с рекламы {dashboard.num(data['ad_orders'])}, "
                   f"органика {dashboard.num(data['organic_orders'])}. "
                   + (f"ДРР {data['drr']:.1f}%." if data["drr"] is not None else ""))
    else:
        caption = f"{data['name']} · {data['start']:%d.%m}–{data['end']:%d.%m} · {data['n_skus']} артикулов"
    await send_png(msg, lambda p: dashboard.keywords_png(data, p), caption, markup)


async def kw_excel(msg: Message, kind, key, days):
    data = await asyncio.to_thread(kw_build, kind, key, days)
    tmp = tempfile.mkdtemp()
    try:
        label = data["offer_id"] if kind == "s" else data["name"]
        safe = re.sub(r"[^\w\-]+", "_", label)[:40]
        path = os.path.join(tmp, f"Ключи_{safe}_{data['start']:%d.%m}-{data['end']:%d.%m}.xlsx")
        await asyncio.to_thread(keywords.to_xlsx, data, path)
        await msg.answer_document(FSInputFile(path), caption=f"Запросы за {days} дней")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def kw_button(msg: Message, user_id, d):
    if d == "kw":
        await msg.answer("Ключевые запросы Ozon: по каким запросам находят и покупают, место в выдаче, "
                         "что идёт от рекламы. Категория — общий топ-15, артикул — его топ-15 и фразы РК.",
                         reply_markup=KW_MENU)
    elif d == "kw:c":
        await msg.answer("Выберите категорию:", reply_markup=kw_categories("cd"))
    elif d == "kw:a":
        await msg.answer("Выберите категорию, в ней — артикул:", reply_markup=kw_categories("al"))
    elif d == "kw:m":
        waiting[user_id] = "kw"
        await msg.answer("Пришлите артикул (например OZ214) или SKU Ozon.")
    elif d.startswith("kw:al:"):
        await kw_articles(msg, d[6:])
    elif d.startswith("kw:cd:"):
        await msg.answer("За какой период?", reply_markup=kw_days_kb(f"cp:{d[6:]}", ("⬅️ Категории", "kw:c")))
    elif d.startswith("kw:sd:"):
        await msg.answer("За какой период?", reply_markup=kw_days_kb(f"sp:{d[6:]}", kw_sku_back(d[6:])))
    elif d.startswith(("kw:sp:", "kw:cp:")):
        _, t, key, days = d.split(":")
        await kw_show(msg, t[0], key, int(days))
    elif d.startswith(("kw:xs:", "kw:xc:")):
        _, t, key, days = d.split(":")
        await kw_excel(msg, t[1], key, int(days))


def kw_prefetch():
    """Ночью: запросы всех продающих артикулов за 7 и 28 дней (и прошлые периоды) + реклама активных кампаний."""
    today = datetime.now(MSK).date()
    end = keywords.ready_day(oz, db, today)
    skus = [r[0] for r in db.execute(
        "select d.sku from daily d join sku s using (sku) where d.day >= ? group by d.sku "
        "having sum(d.ordered_units) > 0", (str(end - timedelta(days=55)),)) ]
    skus = [x for x in skus if not is_test((store.find_sku(db, x) or {"offer_id": ""})["offer_id"])]
    # дневные срезы за всё окно Ozon (35 дн): из них потом считается место за прошлые 28 дней,
    # когда Ozon этот период уже не отдаёт; уже скачанные дни пропускаются
    keywords.oq.fetch_range(db, skus, today - timedelta(days=keywords.oq.HISTORY_DAYS - 1), end, oz)
    for days in keywords.DAYS:
        cur, prev = keywords.periods(end, days)
        keywords.oq.period(db, skus, *cur, oz)
        keywords.oq.period_or_days(db, skus, *prev, oz, today)
        keywords.aq.prefetch(db, perf(), *cur, today)
    log.info("ключи загружены: %s SKU по %s", len(skus), end)


_coinvest = {"at": 0.0}


async def coinvest_end(msg: Message):
    """Последний день с отчётом о реализации; раз в час докачивает новые дни."""
    if time.time() - _coinvest["at"] > SIGNALS_TTL:
        await msg.answer("Собираю отчёты Ozon о реализации за 2 недели…")
        await asyncio.to_thread(coinvest.refresh, db, oz, yesterday())
        _coinvest["at"] = time.time()
    day = db.execute("select max(day) from coinvest_days").fetchone()[0]
    return date.fromisoformat(day) if day else None


COINVEST_PERIODS = {"y": "Вчера", "w": "7 дней"}


def coinvest_sub(end, k):
    return (f"Ozon · {end:%d.%m} к {end - timedelta(days=1):%d.%m}" if k == "y"
            else f"Ozon · {end - timedelta(days=6):%d.%m}–{end:%d.%m} к прошлым 7 дням")


def pp_label(x):
    p, d = x.get("pct"), x.get("delta")
    return ("—" if p is None else f"{p:.0f}%") + ("" if d is None else f" {'▲' if d >= 0.5 else '▼' if d <= -0.5 else '≈'}{d:+.1f}")


async def coinvest_categories(msg: Message, k):
    end = await coinvest_end(msg)
    if not end:
        return await msg.answer("Ozon пока не отдал отчёты о реализации. Попробуйте позже.", reply_markup=MENU)
    period = COINVEST_PERIODS[k]
    rows = coinvest.table(db, end, "type", period=period)
    await send_png(msg, lambda p: dashboard.coinvest_png(f"Соинвест · {period.lower()}", coinvest_sub(end, k), rows,
                                                        period, p), f"Соинвест по категориям · {period.lower()}")
    await msg.answer("Выберите категорию:", reply_markup=kb(
        [[(f"{dashboard.short(r['name'], 24)} · {pp_label(r[period])}", f"co:{k}:c:{r['key']}")]
         for r in rows[:40] if r["key"] != "-"] + [[("⬅️ Соинвест", f"co:p:{k}"), ("⬅️ Меню", "menu")]]))


async def coinvest_category(msg: Message, k, type_id):
    end = await coinvest_end(msg)
    period = COINVEST_PERIODS[k]
    await asyncio.to_thread(stockfilter.load, oz)
    rows = [r for r in coinvest.table(db, end, "sku", type_id, period=period) if not stockfilter.no_fbo(oz, r["key"])]
    name = db.execute("select type_name from sku where type_id = ? limit 1", (type_id,)).fetchone()
    title = dashboard.short(name[0] if name else "Категория", 30)
    await send_png(msg, lambda p: dashboard.coinvest_png(title, coinvest_sub(end, k), rows, period, p, by="sku"),
                   f"Соинвест: {title} · {period.lower()}")
    await msg.answer("Нажмите на артикул — покажу по дням:", reply_markup=kb(
        [[(f"{(r['offer_id'] or r['key'])[:20]} · {pp_label(r[period])}", f"co:{k}:s:{r['key']}")]
         for r in rows[:40]] + [[("⬅️ Категории", f"co:{k}:c"), ("⬅️ Меню", "menu")]]))


async def coinvest_sku(msg: Message, k, sku):
    end = await coinvest_end(msg)
    period = COINVEST_PERIODS[k]
    tid = db.execute("select type_id from sku where sku = ?", (sku,)).fetchone()
    r = next((x for x in coinvest.table(db, end, "sku", tid[0] if tid else None, period=period) if x["key"] == sku), None)
    if not r:
        return await msg.answer(f"У этого артикула нет продаж за период «{period.lower()}».",
                                reply_markup=kb([[("⬅️ Соинвест", f"co:p:{k}"), ("⬅️ Меню", "menu")]]))
    await send_png(msg, lambda p: dashboard.coinvest_sku_png(r, period, coinvest.daily(db, sku, end), p),
                   f"Соинвест: {r['offer_id'] or sku} · {period.lower()}",
                   kb([[("💰 Цены", f"pr:{sku}")],
                       [("⬅️ Категория", f"co:{k}:c:{tid[0]}" if tid else f"co:p:{k}"), ("⬅️ Меню", "menu")]]))


async def price_card(msg: Message, sku):
    """Кнопка «Цены» в карточке артикула: цена, соинвест, пороги акций, скидка от количества. Только чтение."""
    s = store.find_sku(db, sku)
    if not s or not s["offer_id"]:
        return await msg.answer("Не нашла артикул продавца для этого SKU.", reply_markup=MENU)
    first = _actions["data"] is None or time.time() - _actions["at"] > ACTIONS_TTL
    await msg.answer("Собираю цены и акции Ozon" + (", прибыль за 2 недели — до 2 минут…" if _losses["data"] is None
                                                    else "…" if not first else ", до минуты…"))
    if first:
        _actions["data"] = await asyncio.to_thread(prices.actions, oz)
        _actions["at"] = time.time()
    p = await asyncio.to_thread(prices.price_info, oz, s["offer_id"])
    if not p:
        return await msg.answer("Ozon не отдал цену по этому артикулу.", reply_markup=kb([[("⬅️ Меню", "menu")]]))
    try:
        steps = await asyncio.to_thread(prices.stairway, oz, sku)
    except Exception as e:  # noqa: BLE001 — бета-метод Ozon, без него остальная сводка нужна
        log.warning("скидка от количества %s: %r", sku, e)
        steps = []
    x = (await losses(msg))["all"].get(sku)
    unit = {"profit": x["profit"] / x["units"], "drr": x["ads"] / x["revenue"] * 100 if x["revenue"] else None} \
        if x and x["units"] > 0 else {}
    sm = prices.summary(p, _actions["data"], unit)
    end = await coinvest_end(msg)
    co = next((r for r in coinvest.table(db, end, "sku", s["type_id"], period="7 дней") if r["key"] == sku), None) \
        if end else None
    rec, now = sm["rec"], [r for r in sm["rows"] if not r["inside"] and r["need"] == 0]
    caption = f"Цены: {s['offer_id']} · {dashboard.num(p['price'] or 0)} ₽"
    if now:
        caption += f"\nМожно добавить без снижения цены: «{now[0]['action']}»."
    if rec:
        caption += (f"\nЧтобы попасть в «{rec['action']}» ({rec['what']}), снизьте цену на {dashboard.num(rec['need'])} ₽ "
                    f"(−{rec['need_pct']:.0f}%) до {dashboard.num(rec['limit'])} ₽"
                    + (f", прибыль ≈ {dashboard.num(rec['profit'])} ₽/шт." if rec["profit"] is not None else "."))
    elif not now and any(not r["inside"] and r["need"] for r in sm["rows"]):
        caption += "\nВо все доступные акции — только в минус по прибыли. Снижать не советую."
    caption += "\nМенять цену — в боте «Цены»."
    await send_png(msg, lambda path: dashboard.price_png(sm, s["offer_id"], s["name"], (co or {}).get("7 дней"), steps, path),
                   caption, kb([[("⬅️ Артикул", f"p:s:{sku}:week"), ("⬅️ Меню", "menu")]]))


def ts_periods(k, start=None, end=None):
    """Вчера к позавчера, 7 дней к прошлым 7 или свой период к такому же по длине перед ним."""
    if k == "y":
        end = start = yesterday()
    elif k == "w":
        end = yesterday()
        start = end - timedelta(days=6)
    n = (end - start).days + 1
    return start, end, start - timedelta(days=n), start - timedelta(days=1)


def ts_label(start, end):
    return f"{start:%d.%m}" if start == end else f"{start:%d.%m}–{end:%d.%m}"


def render_truestats(prefix, k, start=None, end=None):
    a, b, pa, pb = ts_periods(k, start, end)
    cur, prev = truestats.stats(a, b), truestats.stats(pa, pb)
    name = {"y": "вчера", "w": "неделя"}.get(k, "период")
    return dashboard.truestats_png(cur, prev, f"TrueStats · {name}", f"{ts_label(a, b)} к {ts_label(pa, pb)}", prefix)


async def truestats_card(msg: Message, k, start=None, end=None):
    if not truestats.token():
        return await msg.answer("Нет ключа TrueStats на сервере: добавьте TRUESTATS_TOKEN в .env бота.", reply_markup=MENU)
    await msg.answer("Беру данные из TrueStats…")
    a, b, pa, pb = ts_periods(k, start, end)
    await send_png(msg, lambda p: render_truestats(p, k, start, end),
                   f"TrueStats: {ts_label(a, b)} к {ts_label(pa, pb)}", TS_MENU)


def render_morning(prefix):
    paths = dashboard.morning_png(morning.build(db, oz, data_day()), prefix)
    if truestats.token():
        try:  # лист TrueStats: вчера к позавчера; сбой TrueStats не должен ронять сводку
            paths += render_truestats(f"{prefix}_ts", "y")
        except Exception as e:  # noqa: BLE001
            log.warning("TrueStats в сводке: %r", e)
    return paths


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
    elif d == "co":
        await msg.answer("Соинвест Ozon: за какой период?", reply_markup=kb(
            [[("Вчера (к позавчера)", "co:p:y")], [("7 дней (к прошлым 7)", "co:p:w")], [("⬅️ Меню", "menu")]]))
    elif d.startswith("co:p:"):
        k = d[5:]
        await msg.answer(f"Соинвест · {COINVEST_PERIODS[k].lower()}. Что смотрим?", reply_markup=kb(
            [[("📂 Категории", f"co:{k}:c"), ("🔎 Артикул", f"co:{k}:a")], [("⬅️ Период", "co"), ("⬅️ Меню", "menu")]]))
    elif re.fullmatch(r"co:[yw]:c", d):
        await coinvest_categories(msg, d[3])
    elif d.startswith(("co:y:c:", "co:w:c:")):
        await coinvest_category(msg, d[3], d[7:])
    elif d.startswith(("co:y:s:", "co:w:s:")):
        await coinvest_sku(msg, d[3], d[7:])
    elif re.fullmatch(r"co:[yw]:a", d):
        waiting[cb.from_user.id] = f"co:{d[3]}"
        await msg.answer("Пришлите артикул (например OZ214) или SKU Ozon.")
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
    elif d == "ts":
        await msg.answer("TrueStats — общие показатели, как на экране оцифровки. За какой период?", reply_markup=TS_MENU)
    elif d in ("ts:y", "ts:w"):
        await truestats_card(msg, d[3])
    elif d == "ts:p":
        waiting[cb.from_user.id] = "ts"
        await msg.answer(f"Пришлите период, например 01.10-28.10 (данные есть по {yesterday():%d.%m}). "
                         "Сравню с таким же периодом перед ним.")
    elif d.startswith("pr:"):
        await price_card(msg, d[3:])
    elif d.startswith("r:"):
        t, key = d[2:].split(":", 1)
        await (category_signals(msg, key) if t == "c" else signal_sku(msg, key))
    elif d == "kw" or d.startswith("kw:"):
        await kw_button(msg, cb.from_user.id, d)


async def pos_day(msg: Message):
    """Последний день с данными: позиция приходит вместе с продажами в store.daily."""
    return data_day()


async def pos_items(msg: Message, target):
    """target: n — новинки (Супер), c<type_id> — категория, s<sku> — один артикул. → (название, артикулы)."""
    if target.startswith("n"):  # n — все новинки, n<type_id> — новинки одной категории
        items = await asyncio.to_thread(positions.super_skus, oz)
        await asyncio.to_thread(stockfilter.load, oz)
        items = [x for x in items if not stockfilter.no_fbo(oz, x["sku"])]
        if target == "n":
            return "Новинки (Супер)", items
        row = db.execute("select type_name from sku where type_id = ? limit 1", (target[1:],)).fetchone()
        return f"Новинки: {row[0] if row else 'прочее'}", [x for x in items if x["type_id"] == target[1:]]
    if target.startswith("c"):
        tid = target[1:]
        row = db.execute("select type_name from sku where type_id = ? limit 1", (tid,)).fetchone()
        rows = store.top_skus(db, data_day() - timedelta(days=29), data_day(), tid, 120)
        await asyncio.to_thread(stockfilter.load, oz)
        items = [{"sku": r["sku"], "offer_id": r["offer_id"], "name": r["name"]} for r in rows
                 if not is_test(r["offer_id"]) and not stockfilter.no_fbo(oz, r["sku"])][:40]
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
    days, rows = await asyncio.to_thread(positions.matrix, db, items, day)
    sub = f"Ozon · {days[0]:%d.%m}–{day:%d.%m} · {len(rows)} SKU · сначала упавшие"
    await send_png(msg, lambda p: dashboard.positions_matrix_png(dashboard.short(title, 30), sub, rows, days,
                                                                  positions.move, p),
                   f"Позиции: {title}, {days[0]:%d.%m}–{day:%d.%m}")
    back = ("⬅️ Категории", "pos:c") if target.startswith("c") else ("⬅️ Новинки", "pos:n")
    await msg.answer("Нажмите на артикул — покажу позиции по дням за 28 дней:", reply_markup=kb(
        [[(f"{(r['offer_id'] or r['sku'])[:20]} · {dashboard._pos(r['cells'][-1])} "
           f"{positions.move(r['cells'][-1], r['cells'][-2])[0]}", f"pos:s:{r['sku']}")] for r in rows[:60]]
        + [excel_kb(target), [("🔎 Ввести артикул", "pos:a"), back]]))


async def pos_new_cats(msg: Message):
    """Новинки по категориям: в кнопке — сколько артикулов и сколько упали к предыдущему дню."""
    day = await pos_day(msg)
    items = await asyncio.to_thread(positions.super_skus, oz)
    names = dict(db.execute("select type_id, type_name from sku"))
    cats = {}
    for r in await asyncio.to_thread(positions.summary, db, items, day):
        cats.setdefault(r["type_id"], []).append(r)
    buttons = []
    for tid, rows in sorted(cats.items(), key=lambda x: -len(x[1])):
        down = sum(1 for r in rows if positions.move(r["pos"], r["prev"])[1] == "down")
        label = f"{dashboard.short(names.get(tid) or 'Прочее', 34)} · {len(rows)}" + (f" · ▼{down}" if down else "")
        buttons.append((label, f"pos:n:{tid}"))
    await msg.answer(f"Новинки со статусом «Супер»: {len(items)} SKU. Выберите категорию "
                     "(▼ — сколько упали к предыдущему дню):", reply_markup=kb(
                         [[b] for b in buttons] + [[("📥 Excel по всем новинкам, 28 дн", "pos:x:n:28")],
                                                   [("⬅️ Позиции", "pos")]]))


async def pos_sku(msg: Message, sku):
    day = await pos_day(msg)
    s = store.find_sku(db, sku)
    name = (s["offer_id"] or s["sku"]) if s else sku
    c = positions.sku_card(db, sku, day)
    sub = f"Ozon · {dashboard.short(s['name'], 40) if s else sku} · 28 дн по {day:%d.%m}"
    lab = positions.move(c["cur"], c["prev"])[0]
    best = f", лучшая {dashboard._pos(c['best'][1])} ({c['best'][0]:%d.%m})" if c["best"] else ""
    days, rows = positions.matrix(db, [{"sku": sku}], day, positions.SPAN)
    await send_png(msg, lambda p: dashboard.position_calendar_png(name, sub, rows[0]["cells"], days, positions.move, p),
                   f"{name}: позиция {dashboard._pos(c['cur'])} за {day:%d.%m} ({lab} к пред. дню). "
                   f"За 28 дней: средняя {dashboard._pos(c['avg'])}{best}.",
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
        await pos_new_cats(msg)
    elif d.startswith("pos:n:"):
        await pos_list(msg, "n" + d[6:])
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
    if mode == "ts":
        period = parse_period(msg.text, yesterday())
        if not period:
            return await msg.answer(f"Не поняла период. Формат: 01.10-28.10 (данные есть по {yesterday():%d.%m}).")
        waiting.pop(msg.from_user.id)
        return await truestats_card(msg, "p", *period)
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
    await asyncio.to_thread(stockfilter.load, oz)
    if stockfilter.no_fbo(oz, s["sku"]):
        waiting.pop(msg.from_user.id, None)
        return await msg.answer(f"У {s['offer_id'] or s['sku']} на FBO меньше 1 шт — такие артикулы бот не показывает.",
                                reply_markup=MENU)
    mode = waiting.pop(msg.from_user.id, "card")
    if mode.startswith("co:"):
        return await coinvest_sku(msg, mode[3], s["sku"])
    if mode == "signals":
        return await signal_sku(msg, s["sku"])
    if mode == "pos":
        return await pos_sku(msg, s["sku"])
    if mode == "kw":
        return await msg.answer(f"{s['offer_id'] or s['sku']}: за какой период?",
                                reply_markup=kw_days_kb(f"sp:{s['sku']}", kw_sku_back(s["sku"])))
    await card(msg, f"s:{s['sku']}", "week")


def refresh_data():
    try:
        log.info("себестоимость из таблицы: %s", gsheet_costs.refresh())
    except Exception:
        log.exception("таблица себестоимости не прочиталась, работаю со старым файлом")
    store.refresh(db, yesterday(), oz)
    try:
        coinvest.refresh(db, oz, yesterday())
    except Exception:
        log.exception("соинвест не обновился")
    store.set_offer_ids(db, oz.stocks())
    log.info("данные обновлены по %s", store.last_day(db))


async def scheduler(bot: Bot):
    """Обновление данных утром, сводка в 9:00 и пинг мониторинга каждые 5 минут.
    Если в 9:00 бот был выключен, сводка уйдёт сразу после запуска (до конца дня)."""
    url = os.environ.get("HEALTHCHECK_URL")
    db.execute("create table if not exists sent (day text primary key)")
    db.execute("create table if not exists kw_loaded (day text primary key)")
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
        if now.hour >= KEYWORDS_HOUR and not db.execute("select 1 from kw_loaded where day = ?", (today,)).fetchone():
            try:
                await asyncio.to_thread(kw_prefetch)
                db.execute("insert into kw_loaded values (?)", (today,))
                db.commit()
            except Exception:
                log.exception("ночная загрузка ключей не удалась, повторю через 5 минут")
        if url:
            try:
                async with httpx.AsyncClient() as c:
                    await c.get(url, timeout=10)
            except Exception:
                log.warning("healthcheck недоступен")
        await asyncio.sleep(300)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if store.last_day(db) is None or store.no_positions(db):
        log.info("загрузка истории за %s дней (продажи и позиции), это займёт 10–20 минут", store.FIRST_LOAD_DAYS)
        await asyncio.to_thread(refresh_data)
    session = AiohttpSession()
    if os.environ.get("SSL_CERT_FILE"):  # свой корневой сертификат (прокси облачной сессии)
        session._connector_init["ssl"] = ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])
    bot = Bot(os.environ["TELEGRAM_BOT_TOKEN"], session=session)
    asyncio.create_task(scheduler(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
