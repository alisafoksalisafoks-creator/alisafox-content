"""БОТ_секретарь AlisaFox: недельный отчёт менеджерам по их категориям (Ozon). Только чтение API.

Отдельный бот, не «Аналитик»: свой токен, своя база, свой сервис. Из папки bot/ берутся только модули
чтения Ozon и рисования листов — «Аналитик» от этого не меняется.
Запуск: python -m secretary.app
Переменные окружения: TELEGRAM_BOT_TOKEN (токен БОТ_секретаря), TELEGRAM_CHAT_ID (владелец),
OZON_BOT_CLIENT_ID / OZON_BOT_API_KEY, OZON_PERF_CLIENT_ID / OZON_PERF_CLIENT_SECRET, MOYSKLAD_TOKEN;
необязательно WEEKLY_SEND=1 (рассылка по понедельникам), HEALTHCHECK_URL, SECRETARY_DB.
"""
import asyncio
import logging
import os
import shutil
import ssl
import tempfile
import threading
import time
from datetime import date, datetime, timedelta

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import CommandStart
from aiogram.types import (BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                           InputMediaPhoto, Message)

from bot import env  # noqa: F401  (подхватывает .env до чтения ключей)
from bot import cards, dashboard, store
from bot.category_report import MSK
from bot.sources import gsheet_costs
from bot.sources.ozon import OzonClient
from bot.sources.performance import PerformanceClient
from secretary import excel, managers, report, shared

log = logging.getLogger("secretary")
ADMINS = {int(x) for x in os.environ.get("TELEGRAM_CHAT_ID", "").replace(" ", "").split(",") if x}
WEEKLY_HOUR = 9    # недельный отчёт в понедельник в 9:00 МСК; в остальные дни бот ничего не собирает сам
WEEKLY_SEND = os.environ.get("WEEKLY_SEND", "1") == "1"
PREPARE_HOUR = 7   # в понедельник с 7:00 (после обновления «Аналитика» в 6:00) готовим данные к рассылке
store.FIRST_LOAD_DAYS = 63  # хватает на 8 недель сравнения

dp = Dispatcher()
db = store.connect(os.environ.get("SECRETARY_DB", os.path.join(os.path.dirname(__file__), "data.sqlite")))
report.ensure(db)
oz = OzonClient()
perf = PerformanceClient()
waiting: dict[int, str] = {}       # кто что вводит: "add" — ID пользователя, "note" — «что я сделал»
access_requests: dict[int, str] = {}
_cache: dict[str, tuple[float, object]] = {}


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in r]
                                                 for r in rows])


_fresh = {"day": None}


_lock = threading.Lock()  # кнопки и понедельничная подготовка не качают данные одновременно


def stale():
    return (store.last_day(db) or date.min) < datetime.now(MSK).date() - timedelta(days=1)


def ensure_fresh():
    """Данные собираются только по запросу (владелец 2026-10-26): при первом нажатии за день — докачка."""
    with _lock:
        today = datetime.now(MSK).date()
        if _fresh["day"] != today and stale() or store.last_day(db) is None:
            refresh_data()
        _fresh["day"] = today


def prepare():
    """Понедельник: данные за неделю считаются заранее, к 9:00 всё уже в кэше. Повторный вызов ничего не делает."""
    ensure_fresh()
    cur_p, prev_p = weeks()
    for period in (cur_p, prev_p):
        week_losses(period)
        week_ads(period)
    all_signals()


def data_day():
    y = datetime.now(MSK).date() - timedelta(days=1)
    return min(y, store.last_day(db) or y)


def weeks(n=7):
    return report.period(data_day(), n)


def label(period):
    return f"{period[0]:%d.%m}–{period[1]:%d.%m}"


# --- кто что ведёт ---

def sheet():
    return managers.load()


def manager_names():
    """Артикулы без менеджера в юнитке не показываем нигде (владелец 2026-10-26)."""
    return [m for m in managers.names(sheet()) if m != managers.NO_MANAGER]


def skus_of(manager):
    return {s for s, m in managers.assign(db, sheet()).items() if m == manager}


def cached(key, build, ttl=report.SIGNAL_TTL):
    at, value = _cache.get(key, (0, None))
    if value is None or time.time() - at > ttl:
        value = build()
        _cache[key] = (time.time(), value)
    return value


def all_skus():
    """Все артикулы, за которыми в юнитке есть менеджер."""
    names = set(manager_names())
    return {s for s, m in managers.assign(db, sheet()).items() if m in names}


def week_losses(period):
    """{sku: прибыль...} за период или {} — если отчётов о реализации за него у Ozon уже нет."""
    return cached(f"loss:{period[0]}:{period[1]}", lambda: report.losses(db, oz, perf, period) or {}, ttl=6 * 3600)


def all_signals():
    return cached("signals", lambda: report.stock_signals(db, oz, data_day(), all_skus()))


def week_ads(period):
    """{sku: расход ₽} за период, только активные кампании. В прибыли (минусовые, ЧП) — весь расход."""
    def build():
        rev = {r[0]: r[1] for r in db.execute("select sku, sum(revenue) from daily where day between ? and ? "
                                              "group by sku", (str(period[0]), str(period[1])))}
        return report.active_ads(perf, period[0], period[1], rev)
    return cached(f"ads:{period[0]}:{period[1]}", build, ttl=6 * 3600)


# --- пользователи ---

def users():
    return {r[0]: (r[1], r[2]) for r in db.execute("select id, name, manager from users")}


def allowed(uid):
    return uid in ADMINS or uid in users() or not ADMINS


def is_admin(uid):
    return uid in ADMINS


def who(u):
    return " ".join(x for x in (u.first_name, u.last_name) if x) + (f" (@{u.username})" if u.username else "")


def main_menu(uid):
    names = manager_names()
    rows = [[(f"👩 {n}", f"m:{i}") for i, n in enumerate(names[:3])]]
    if len(names) > 3:
        rows.append([(f"📋 {n}", f"m:{i}") for i, n in enumerate(names[3:], 3)])
    rows += [[("📂 Категории", "cats")], [("📝 Что я сделал", "note")]]
    if is_admin(uid):
        rows.append([("➕ Добавить пользователя", "u")])
    return kb(rows)


def manager(i):
    names = manager_names()
    return names[int(i)] if int(i) < len(names) else None


# --- отправка ---

async def send_sheets(msg: Message, render, caption, markup=None):
    tmp = tempfile.mkdtemp()
    try:
        paths = await asyncio.to_thread(render, os.path.join(tmp, "sheet"))
        await msg.bot.send_media_group(msg.chat.id, [InputMediaPhoto(media=FSInputFile(p)) for p in paths])
        await msg.answer(caption, reply_markup=markup)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def long_text(msg: Message, text, markup=None):
    """Телеграм режет сообщения на 4096 знаков — делим по строкам."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for i, c in enumerate(chunks):
        await msg.answer(c, reply_markup=markup if i == len(chunks) - 1 else None)


# --- менеджер → категории → 4 кнопки ---

def scope(mi, cat):
    """Артикулы менеджера: все или одной категории. Возвращает (заголовок, skus)."""
    name = manager(mi)
    skus = skus_of(name)
    if cat == "all":
        return f"{name} · все категории", skus
    cats = report.categories(db, skus)
    title, part = cats.get(cat, ("?", []))
    return f"{name} · {title}", set(part)


def manager_kb(mi):
    cats = report.categories(db, skus_of(manager(mi)))
    buttons = [(dashboard.short(t, 28), f"mc:{mi}:{tid}") for tid, (t, _) in cats.items()]
    return kb([[("📊 Все категории", f"mc:{mi}:all")]] + [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
              + [[("⬅️ Меню", "menu")]])


def actions_kb(mi, cat):
    t = f"{mi}:{cat}"
    return kb([[("📊 Сводка 7 дней", f"o:{t}:7"), ("📊 Сводка месяц", f"o:{t}:30")], [("🔻 Минусовые артикулы", f"n:{t}")],
               [("🚚 Нужно отгрузить", f"s:{t}")], [("📣 Реклама 7 дней", f"a:{t}:7"), ("📣 Реклама месяц", f"a:{t}:30")],
               [("📥 Скачать отчёт", f"dl:{t}")], [("⬅️ Категории", f"m:{mi}"), ("⬅️ Меню", "menu")]])


TILES = [  # (название, ключ, меньше — лучше)
    ("Заказы, шт", "orders", False), ("Показы", "views", False), ("Клики в карточку", "clicks", False),
    ("Корзины", "carts", False), ("CTR, %", "ctr", False), ("Средняя позиция", "position", True),
    ("Выкуп, %", "buyout", False), ("ДРР, %", "drr", True), ("Чистая прибыль, ₽", "profit", False),
    ("Средняя цена, ₽", "check", False), ("Остаток FBO, шт", "fbo", False), ("Заказы, ₽", "revenue", False),
]


def summary(skus, period):
    """Все показатели сводки за период: kpis + клики, корзины, ДРР, прибыль, остаток FBO."""
    t = report.totals(db, skus, *period)
    k = report.kpis(t)
    ads = sum(v for s, v in week_ads(period).items() if s in skus)
    loss = week_losses(period)
    k.update(clicks=t["hits_view_pdp"], carts=t["hits_tocart"], ads=ads, drr=report.pct(ads, t["revenue"]),
             profit=sum(v["profit"] for s, v in loss.items() if s in skus) if loss else None,
             fbo=report.fbo(db, skus, period[1] + timedelta(days=1)))
    return k


def tiles(c, p):
    out = []
    for name, key, lower in TILES:
        cur, old = c.get(key), p.get(key)
        out.append({"name": name, "value": cur or 0, "lower_is_better": lower,
                    "delta": report.change(cur, old) if cur is not None and old else None})
    return out


def overview(mi, cat, n=7):
    """Сводка неделя/месяц к прошлому периоду: 12 показателей со стрелками, категории и выводы по правилам."""
    title, skus = scope(mi, cat)
    cur_p, prev_p = weeks(n)
    c, p = summary(skus, cur_p), summary(skus, prev_p)
    loss = week_losses(cur_p)
    sig = all_signals()
    rows, found = [], []
    for tid, (name, part) in report.categories(db, skus).items():
        kc, kp = summary(part, cur_p), summary(part, prev_p)
        if not kc["orders"] and not kp["orders"]:
            continue
        n_loss = sum(1 for s in part if s in loss and loss[s]["profit"] < 0)
        n_out = sum(1 for s in part if s in sig and sig[s]["speed"] > 0 and sig[s]["stock"] == 0)
        # пишем только о том, что скачет и стоит денег: спокойное и мелкое в текст не попадает
        arts = []
        for s in part:
            c1, p1 = summary([s], cur_p), summary([s], prev_p)
            a = report.alerts(c1, p1)
            money = report.impact(c1, p1)
            if a and (money >= report.MIN_IMPACT or any("в минус" in x for x in a)):
                r = store.find_sku(db, s)
                arts.append((money, f"   – {(r['offer_id'] if r else s)}: {', '.join(a)}"))
        cat_alerts = report.alerts(kc, kp, "category")
        extra = ([f"в минусе {n_loss} арт."] if n_loss else []) + ([f"нет на Ozon {n_out} арт."] if n_out else [])
        if cat_alerts or arts or extra:
            arts.sort(key=lambda x: -x[0])
            found.append((report.impact(kc, kp) + sum(m for m, _ in arts),
                          [f"• {name}: " + (", ".join(cat_alerts + extra) or "в целом ровно, но:")]
                          + [t for _, t in arts[:report.TOP_SKUS]]))
        lab, col = dashboard.delta_style(report.change(kc["orders"], kp["orders"]))
        lab_r, col_r = dashboard.delta_style(report.change(kc["revenue"], kp["revenue"]))
        rows.append(([(dashboard.short(name, 22), dashboard.INK, True), (dashboard.num(kc["orders"]), dashboard.INK, False),
                      (lab, col, True), (dashboard.num(kc["revenue"]), dashboard.INK, False), (lab_r, col_r, True)],
                     f"ДРР {kc['drr']:.0f}% · CTR {kc['ctr']:.1f}% · выкуп {kc['buyout']:.0f}% · минус {n_loss}"))
    found.sort(key=lambda x: -x[0])
    lines = [line for _, ls in found[:report.TOP_CATS] for line in ls]
    if len(found) > report.TOP_CATS:
        lines.append(f"Ещё {len(found) - report.TOP_CATS} категорий с мелкими скачками — в «📥 Скачать отчёт».")
    if not lines:
        lines.append("Скачков нет: заказы, реклама, прибыль и места в норме.")
    sub = f"Ozon · {label(cur_p)} к {label(prev_p)}"
    profit = "нет данных" if c["profit"] is None else f"{dashboard.num(c['profit'])} ₽"
    head = (f"{title}\n{'Неделя' if n == 7 else f'{n} дней'} {label(cur_p)} к {label(prev_p)}, Ozon.\n"
            f"Заказы {dashboard.num(c['orders'])} шт ({report.fmt_pct(report.change(c['orders'], p['orders']))}), "
            f"ДРР {c['drr']:.1f}%, чистая прибыль {profit}.")
    note = ("CTR — переходы в карточку из показов. Выкуп — доставлено без возвратов из доставленного и отменённого.\n"
            "ДРР — реклама артикулов к заказам ₽. Прибыль — как в «Аналитике». Остаток FBO — на сегодня.")
    cols = [(0.05, "Категория", "left"), (0.50, "шт", "right"), (0.63, "к прошл.", "right"),
            (0.82, "₽", "right"), (0.95, "к прошл.", "right")]

    def render(prefix):
        pg = dashboard.Page(0.35 + 0.78 + 6 * 1.0 + 0.75, dashboard.SHEET_W)
        pg.header(dashboard.short(title, 30), sub)
        pg.tiles(tiles(c, p), per_row=2)
        paths = []
        dashboard._save(pg, prefix + "a", paths, note)
        return paths + dashboard._sheets("Категории", sub, "Заказы по категориям", cols, rows, "● заказов нет",
                                         prefix + "b", "▲ лучше · ▼ хуже · ≈ меньше 5%.")
    return head + "\n\nНа что смотреть:\n" + "\n".join(lines), render


async def show_overview(msg, mi, cat, n="7"):
    n = int(n)
    await msg.answer(f"Собираю сводку за {'7 дней' if n == 7 else 'месяц'}: продажи, реклама, прибыль, остатки. 1–3 минуты…")
    await asyncio.to_thread(ensure_fresh)
    text, render = await asyncio.to_thread(overview, mi, cat, n)
    await send_sheets(msg, render, "Сводка готова, выводы ниже.")
    await long_text(msg, text, actions_kb(mi, cat))


async def show_losses(msg, mi, cat):
    await msg.answer("Считаю прибыль по каждому артикулу за неделю и прошлую. 1–3 минуты…")
    await asyncio.to_thread(ensure_fresh)
    title, skus = scope(mi, cat)
    cur_p, prev_p = weeks()
    cur = await asyncio.to_thread(week_losses, cur_p)
    prev = await asyncio.to_thread(week_losses, prev_p)
    items = []
    for s, x in cur.items():
        if s in skus and x["profit"] < 0:
            row = store.find_sku(db, s)
            items.append({**x, "sku": s, "name": row["name"] if row else "",
                          "offer_id": x["offer_id"] or (row["offer_id"] if row else s),
                          "twice": s in prev and prev[s]["profit"] < 0})
    items.sort(key=lambda x: x["profit"])
    if not items:
        return await msg.answer(f"{title}: за {label(cur_p)} минусовых артикулов нет.", reply_markup=actions_kb(mi, cat))
    for x in items:
        if x["twice"]:
            x["name"] = "⚠ 2-я неделя в минусе · " + (x["name"] or "")
    sub = f"Ozon · {label(cur_p)} · {len(items)} SKU · {dashboard.num(sum(x['profit'] for x in items))} ₽"
    await send_sheets(msg, lambda p: dashboard.losses_png(dashboard.short(title, 30), sub, items, p),
                      f"Минусовые: {len(items)}, из них вторую неделю подряд: {sum(x['twice'] for x in items)}.")
    await msg.answer("Нажмите на артикул — сводка и почему он в минусе:", reply_markup=kb(
        [[(f"{'⚠ ' if x['twice'] else ''}{(x['offer_id'] or x['sku'])[:20]} · {dashboard.num(x['profit'])} ₽",
           f"art:{x['sku']}")] for x in items[:40]] + [[("⬅️ Назад", f"mc:{mi}:{cat}")]]))


async def show_ship(msg, mi, cat):
    await msg.answer("Смотрю остатки по всем кластерам с учётом сезона. 1–2 минуты…")
    await asyncio.to_thread(ensure_fresh)
    title, skus = scope(mi, cat)
    by = {k: v for k, v in (await asyncio.to_thread(all_signals)).items() if k in skus}
    today = data_day() + timedelta(days=1)
    from bot import signals
    ship, resort, paid = signals.to_ship(by), signals.resort(by), signals.paid(by)
    lines = [f"{title}: пора отгрузить {len(ship)}, пересорт {len(resort)}, платное хранение {len(paid)}."]
    for s in ship[:10]:
        zero = [r["cluster"].split(",")[0] for r in s["ship"] if r["signal"] == "zero"]
        lines.append(f"🔴 {s['offer_id']}: " + (f"обнулился в {', '.join(zero[:3])}" if zero else
                                               f"кончится к {dashboard.ship_date(s['ship_by'], today)}"))
    for s in resort[:10]:
        lines.append(f"🔀 {s['offer_id']}: кончается в {dashboard._names(s['ship'], 2)}, "
                     f"лежит без продаж в {dashboard._names(s['excess'], 2)}")
    if ship:
        await send_sheets(msg, lambda p: dashboard.signals_png("ship", ship, today, p, f"Отгрузить: {dashboard.short(title, 18)}"),
                          "Пора отгрузить")
    if resort:
        await send_sheets(msg, lambda p: dashboard.signals_png("resort", resort, today, p, "Пересорт"), "Пересорт")
    lines.append("Сколько штук и куда — в боте поставок.")
    await long_text(msg, "\n".join(lines), actions_kb(mi, cat))


async def show_ads(msg, mi, cat, n="7"):
    n = int(n)
    await msg.answer(f"Считаю рекламу артикулов за {'7 дней' if n == 7 else 'месяц'} и прошлый такой же период. 1–3 минуты…")
    await asyncio.to_thread(ensure_fresh)
    title, skus = scope(mi, cat)
    cur_p, prev_p = weeks(n)
    cur, prev = await asyncio.to_thread(week_ads, cur_p), await asyncio.to_thread(week_ads, prev_p)
    rev_c = report.totals(db, skus, *cur_p)["revenue"]
    rev_p = report.totals(db, skus, *prev_p)["revenue"]
    sc, sp = sum(v for k, v in cur.items() if k in skus), sum(v for k, v in prev.items() if k in skus)
    drr_c, drr_p = report.pct(sc, rev_c), report.pct(sp, rev_p)
    lines = [f"{title}\n{label(cur_p)} к {label(prev_p)}. Реклама {dashboard.num(sc)} ₽ ({report.fmt_pct(report.change(sc, sp))}), "
             f"ДРР {drr_c:.1f}% (было {drr_p:.1f}%)."]
    top = sorted(((k, v) for k, v in cur.items() if k in skus and v > 0), key=lambda kv: -kv[1])[:10]
    for sku, spend in top:
        r = store.find_sku(db, sku)
        rev = report.totals(db, [sku], *cur_p)["revenue"]
        drr = report.pct(spend, rev)
        flag = "🔴" if drr > 25 or not rev else "🟡" if drr > 15 else "🟢"
        lines.append(f"{flag} {(r['offer_id'] if r else sku)}: {dashboard.num(spend)} ₽, ДРР {drr:.0f}%")
    lines.append("Только активные сейчас кампании. ДРР = их расход на артикул / его заказы ₽ за те же дни. «Оплата за заказ» — точно "
                 "по заказам, кампании за клики с несколькими товарами делятся по выручке товаров.")
    await long_text(msg, "\n".join(lines), actions_kb(mi, cat))


async def download(msg, mi, cat):
    await msg.answer("Готовлю Excel за неделю…")
    await asyncio.to_thread(ensure_fresh)
    title, skus = scope(mi, cat)
    cur_p, prev_p = weeks()
    loss = await asyncio.to_thread(week_losses, cur_p)
    sig = await asyncio.to_thread(all_signals)
    data = await asyncio.to_thread(excel.build, db, title, skus, cur_p, prev_p, loss, sig)
    name = f"Отчёт_{manager(mi)}_{cur_p[0]:%d.%m}-{cur_p[1]:%d.%m}.xlsx"
    await msg.answer_document(BufferedInputFile(data, filename=name), reply_markup=actions_kb(mi, cat))


# --- категории → артикулы → сводка / комментарий ---

def cats_kb():
    cats = report.categories(db, all_skus())
    buttons = [(dashboard.short(t, 28), f"ca:{tid}") for tid, (t, _) in list(cats.items())[:60]]
    return kb([buttons[i:i + 2] for i in range(0, len(buttons), 2)] + [[("⬅️ Меню", "menu")]])


def articles_kb(type_id):
    cur_p, prev_p = weeks()
    rows = db.execute("select d.sku, s.offer_id, sum(d.ordered_units) u from daily d join sku s using (sku) "
                      "where s.type_id = ? and d.day between ? and ? group by d.sku order by u desc",
                      (type_id, str(prev_p[0]), str(cur_p[1]))).fetchall()
    mine = all_skus()
    rows = [r for r in rows if r["sku"] in mine][:60]
    return kb([[(f"{(r['offer_id'] or r['sku'])[:24]} · {r['u']:.0f} шт за 2 нед", f"art:{r['sku']}")] for r in rows]
              + [[("⬅️ Категории", "cats"), ("⬅️ Меню", "menu")]])


def article_kb(sku):
    return kb([[("📊 Сводка", f"as:{sku}"), ("💬 Комментарий", f"ac:{sku}")], [("⬅️ Меню", "menu")]])


def comment(sku):
    """Версии, почему артикул в таком состоянии: каждая с цифрой, подтверждённые сверху."""
    row = store.find_sku(db, sku)
    offer = row["offer_id"] if row else sku
    cur_p, prev_p = weeks()
    c, p = report.kpis(report.totals(db, [sku], *cur_p)), report.kpis(report.totals(db, [sku], *prev_p))
    ads_c, ads_p = week_ads(cur_p).get(sku, 0.0), week_ads(prev_p).get(sku, 0.0)
    sig = all_signals().get(sku)
    notes = [r[0] for r in db.execute("select text from notes where day >= ? and (sku = ? or upper(text) like ?)",
                                      (str(prev_p[0]), sku, f"%{(offer or '').upper()}%"))]
    extra = {"ads": (ads_c, ads_p), "price": report.price_change(db, offer, *cur_p), "rating": report.rating(db, sku),
             "index": report.price_index(db, offer), "notes": notes,
             "zero_clusters": sum(1 for r in sig["ship"] if r["signal"] == "zero") if sig else None}
    d = report.change(c["orders"], p["orders"])
    loss = week_losses(cur_p).get(sku)
    head = (f"{offer} · {dashboard.short(row['name'] if row else '', 40)}\n"
            f"Неделя {label(cur_p)}: заказы {dashboard.num(c['orders'])} шт, {report.trend(d)}"
            + (f", прибыль {dashboard.num(loss['profit'])} ₽" if loss else "") + ".")
    versions = report.reasons(c, p, extra)
    lines = [head, "", "Почему (✅ подтверждается цифрами, ▫️ нет):"]
    lines += [f"{'✅' if ok else '▫️'} {t}" for ok, t in versions]
    if not any(ok for ok, _ in versions):
        lines.append("Явной причины в данных Ozon нет: проверьте отзывы и выдачу вручную.")
    lines.append("Звёзды и отзывы в API закрыты подпиской, продавцов на карточке API не показывает.")
    return "\n".join(lines)


async def article_summary(msg, sku):
    await asyncio.to_thread(ensure_fresh)
    cur_p, prev_p = weeks()
    row = store.find_sku(db, sku)
    name = f"{row['offer_id'] or sku} · {dashboard.short(row['name'], 30)}" if row else sku
    data = cards.build(db, data_day(), sku=sku, compare=(cur_p, prev_p))
    await send_sheets(msg, lambda p: dashboard.card_png(name, data, p), f"{name}\n{data['label']}", article_kb(sku))


# --- обработчики ---

@dp.message(CommandStart())
async def start(msg: Message):
    u = msg.from_user
    if not allowed(u.id):
        if u.id not in access_requests:
            access_requests[u.id] = who(u)
            for a in ADMINS:
                await msg.bot.send_message(a, f"Просит доступ к БОТ_секретарю: {who(u)}, ID {u.id}", reply_markup=kb(
                    [[("✅ Открыть", f"u:ok:{u.id}"), ("❌ Отказать", f"u:no:{u.id}")]]))
        return await msg.answer("Запрос отправлен владельцу. Как только откроет доступ, нажмите /start ещё раз.")
    await msg.answer(f"Отчёты за неделю {label(weeks()[0])}. Выберите менеджера или категорию:",
                     reply_markup=main_menu(u.id))


@dp.callback_query()
async def button(cb: CallbackQuery):
    uid, d, msg = cb.from_user.id, cb.data or "", cb.message
    await cb.answer()
    if not allowed(uid):
        return
    try:
        if d != "menu" and not d.startswith("u") and d != "note" and (store.last_day(db) is None or stale()):
            await msg.answer("Обновляю данные Ozon: копия у «Аналитика» и докачка. Первый раз 2–5 минут, потом быстрее…")
            await asyncio.to_thread(ensure_fresh)
        if d == "menu":
            await msg.answer("Меню:", reply_markup=main_menu(uid))
        elif d.startswith("m:"):
            await msg.answer(f"{manager(d[2:])}: выберите категорию.", reply_markup=manager_kb(d[2:]))
        elif d.startswith("mc:"):
            _, mi, cat = d.split(":", 2)
            await msg.answer(scope(mi, cat)[0], reply_markup=actions_kb(mi, cat))
        elif d[:2] in ("o:", "a:"):
            kind, mi, cat, n = d.split(":")
            await (show_overview if kind == "o" else show_ads)(msg, mi, cat, n)
        elif d[:2] in ("n:", "s:") or d.startswith("dl:"):
            kind, mi, cat = d.split(":", 2)
            await {"n": show_losses, "s": show_ship, "dl": download}[kind](msg, mi, cat)
        elif d == "cats":
            await msg.answer("Все категории:", reply_markup=cats_kb())
        elif d.startswith("ca:"):
            await msg.answer("Артикулы категории:", reply_markup=articles_kb(d[3:]))
        elif d.startswith("art:"):
            r = store.find_sku(db, d[4:])
            await msg.answer(f"{(r['offer_id'] if r else d[4:])}: что показать?", reply_markup=article_kb(d[4:]))
        elif d.startswith("as:"):
            await article_summary(msg, d[3:])
        elif d.startswith("ac:"):
            await msg.answer("Проверяю версии по цифрам Ozon…")
            await asyncio.to_thread(ensure_fresh)
            await long_text(msg, await asyncio.to_thread(comment, d[3:]), article_kb(d[3:]))
        elif d == "note":
            waiting[uid] = "note"
            await msg.answer("Напишите одним сообщением, что сделали и по какому артикулу. Например: "
                             "«OZ066: подключила скидку от количества 10%». Это попадёт в комментарий к артикулу.")
        elif d.startswith("u") and is_admin(uid):
            await users_button(msg, d)
    except Exception:
        log.exception("кнопка %s", d)
        await msg.answer("Не получилось собрать отчёт, попробуйте ещё раз через пару минут.", reply_markup=main_menu(uid))


async def users_button(msg, d):
    if d == "u":
        rows = [[(f"❌ {name} · {mgr or 'все'}", f"u:del:{i}")] for i, (name, mgr) in users().items()]
        await msg.answer("Пользователи БОТ_секретаря. Нажмите, чтобы закрыть доступ:",
                         reply_markup=kb(rows + [[("➕ Добавить по ID", "u:add")], [("⬅️ Меню", "menu")]]))
    elif d == "u:add":
        waiting[msg.chat.id] = "add"
        await msg.answer("Пришлите Telegram ID нового пользователя (цифрами). Проще так: пусть человек нажмёт /start, "
                         "и вам придёт кнопка «Открыть».")
    elif d.startswith("u:ok:"):
        i = int(d[5:])
        db.execute("insert or replace into users values (?, ?, null, ?)",
                   (i, access_requests.pop(i, str(i)), datetime.now(MSK).strftime("%d.%m.%Y")))
        db.commit()
        await msg.answer("Доступ открыт.")
    elif d.startswith("u:no:"):
        access_requests.pop(int(d[5:]), None)
        await msg.answer("Отказано.")
    elif d.startswith("u:del:"):
        db.execute("delete from users where id = ?", (int(d[6:]),))
        db.commit()
        await msg.answer("Доступ закрыт.")


@dp.message(F.text)
async def text(msg: Message):
    uid = msg.from_user.id
    if not allowed(uid):
        return
    mode = waiting.pop(uid, None)
    if mode == "add" and is_admin(uid) and msg.text.strip().isdigit():
        i = int(msg.text.strip())
        db.execute("insert or replace into users values (?, ?, null, ?)", (i, str(i), datetime.now(MSK).strftime("%d.%m.%Y")))
        db.commit()
        return await msg.answer("Добавлен. Пусть нажмёт /start в боте.", reply_markup=main_menu(uid))
    if mode == "note":
        found = next((r for w in msg.text.replace(":", " ").split() if (r := store.find_sku(db, w))), None)
        db.execute("insert into notes (day, user_id, author, sku, offer_id, text) values (?,?,?,?,?,?)",
                   (str(datetime.now(MSK).date()), uid, who(msg.from_user), found["sku"] if found else None,
                    found["offer_id"] if found else None, msg.text.strip()))
        db.commit()
        return await msg.answer("Записала" + (f" к {found['offer_id']}." if found else ". Артикул не нашла, сохранила общей заметкой."),
                                reply_markup=main_menu(uid))
    r = store.find_sku(db, msg.text)
    if r:
        return await msg.answer(f"{r['offer_id'] or r['sku']}: что показать?", reply_markup=article_kb(r["sku"]))
    await msg.answer("Меню:", reply_markup=main_menu(uid))


# --- расписание ---

def refresh_data():
    for name, job in (("себестоимость", gsheet_costs.refresh), ("менеджеры", managers.refresh)):
        try:
            log.info("%s из юнитки: %s", name, job())
        except Exception:
            log.exception("%s: таблица не прочиталась, работаю со старым файлом", name)
    try:
        shared.sync(db)  # что уже собрал «Аналитик» — копией, из API докачивается только недостающее
    except Exception:
        log.exception("база «Аналитика» не прочиталась, качаю из API сам")
    store.refresh(db, datetime.now(MSK).date() - timedelta(days=1), oz)
    store.set_offer_ids(db, oz.stocks())
    try:
        report.snapshot(db, oz, datetime.now(MSK).date())
    except Exception:
        log.exception("снимок цен и рейтинга не сохранился")
    _cache.clear()
    log.info("данные обновлены по %s", store.last_day(db))


async def weekly(bot: Bot):
    """Понедельник 9:00: всем пользователям бота — сводка по всем менеджерам (владелец 2026-10-26)."""
    wk = str(weeks()[0][0])
    names = manager_names()
    targets = [(i, range(len(names))) for i in ADMINS | set(users())]
    for chat, idx in targets:
        if db.execute("select 1 from sent where week = ? and chat = ?", (wk, chat)).fetchone():
            continue
        for mi in idx:
            head, render = await asyncio.to_thread(overview, str(mi), "all")
            tmp = tempfile.mkdtemp()
            try:
                paths = await asyncio.to_thread(render, os.path.join(tmp, "s"))
                await bot.send_media_group(chat, [InputMediaPhoto(media=FSInputFile(p)) for p in paths])
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            await bot.send_message(chat, head[:4000], reply_markup=actions_kb(str(mi), "all"))
        db.execute("insert into sent values (?, ?)", (wk, chat))
        db.commit()


async def scheduler(bot: Bot):
    """Сам бот работает только в понедельник: в 9:00 докачивает данные и рассылает недельный отчёт."""
    url = os.environ.get("HEALTHCHECK_URL")
    while True:
        now = datetime.now(MSK)
        if WEEKLY_SEND and now.weekday() == 0 and now.hour >= PREPARE_HOUR:
            try:
                await asyncio.to_thread(prepare)  # 7:00: копия у «Аналитика» + реклама и прибыль, до рассылки
                if now.hour >= WEEKLY_HOUR:
                    await weekly(bot)
            except Exception:
                log.exception("недельный отчёт не отправлен")
        if url:
            try:
                async with httpx.AsyncClient() as c:
                    await c.get(url, timeout=10)
            except Exception:
                log.warning("healthcheck недоступен")
        await asyncio.sleep(300)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    session = AiohttpSession()
    if os.environ.get("SSL_CERT_FILE"):
        session._connector_init["ssl"] = ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])
    bot = Bot(os.environ["TELEGRAM_BOT_TOKEN"], session=session)
    asyncio.create_task(scheduler(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
