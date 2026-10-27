"""Telegram-бот «Цены AlisaFox»: цены и скидка от количества на Ozon по кнопкам.

Разделы:
- 💰 Цены: категория → артикул → дашборд (цена, вилка «Эластичного бустинга», ЧП и ДРР из ТС, соинвест,
  скидка от количества, место в выдаче) → изменить цену с подтверждением «было → стало».
- 📦 Скидка от количества: категория → артикул или вся категория → поставить / изменить / удалить.
Ничего не меняет в Ozon без кнопки «✅ Да, применить».

Запуск: python -m price_bot.app
Переменные окружения: TELEGRAM_PRICE_BOT_TOKEN, OZON_CLIENT_ID, OZON_API_KEY, PRICE_BOT_OWNER;
для ЧП и ДРР — TRUESTATS_MCP_URL, TRUESTATS_API_KEY; необязательно PRICE_BOT_DATA.
"""
import asyncio
import datetime as dt
import html
import logging
import os
import ssl
import time
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, \
    ReplyKeyboardMarkup

from price_bot import env  # noqa: F401  (подхватывает .env до чтения ключей)
from price_bot import calc, store
from price_bot.ozon import Ozon, OzonError
from price_bot.truestats import TrueStats

log = logging.getLogger("price_bot")
OWNER = int(os.environ.get("PRICE_BOT_OWNER", "0"))
MSK = ZoneInfo("Europe/Moscow")
PAGE = 20  # кнопок на странице списка

PRICES = "💰 Цены"
QTY = "📦 Скидка от количества"
REFRESH = "🔄 Обновить остатки"
HISTORY = "📜 История"
HIST_PAGE = 15
ACCESS = "👥 Доступ"

dp = Dispatcher()
ozon: Ozon = None
ts = TrueStats()
waiting: dict[int, dict] = {}  # что ждём текстом: цену, ступени скидки или ID пользователя
pending: dict[int, dict] = {}  # подготовленное изменение, ждёт «✅ Да, применить»
requests: dict[int, str] = {}  # кто просил доступ: id → имя
cache: dict[str, tuple[float, object]] = {}  # общие данные с временем загрузки
ozon_lock = asyncio.Lock()  # аналитика Ozon — не чаще раза в минуту


def allowed(user_id):
    return user_id == OWNER or user_id in store.users()


def menu(user_id):
    rows = [[KeyboardButton(text=PRICES), KeyboardButton(text=QTY)],
            [KeyboardButton(text=HISTORY), KeyboardButton(text=REFRESH)] + ([KeyboardButton(text=ACCESS)] if user_id == OWNER else [])]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in r]
                                                 for r in rows])


HOME = ("🏠 Меню", "home")


def nav_kb(back=None):
    """Кнопки «⬅️ Назад» (если есть куда) и «🏠 Меню» — на каждом экране, чтобы не застрять."""
    return kb([([("⬅️ Назад", back)] if back else []) + [HOME]])


def who(user):
    return user.full_name + (f" (@{user.username})" if user.username else "")


def today():
    return dt.datetime.now(MSK).date()


# --- данные ---

async def cached(key, ttl, load):
    hit = cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    value = await load()
    cache[key] = (time.monotonic(), value)
    return value


async def items():
    c = store.catalog()
    if not c["items"] or "fbo" not in c["items"][0]:  # пусто или каталог старого формата, ещё без остатков
        await refresh_catalog()
        c = store.catalog()
    return c["items"]


async def refresh_catalog():
    goods = await ozon.catalog()
    store.save_catalog(dt.datetime.now(MSK).strftime("%d.%m %H:%M"), goods)
    return goods


def categories(goods):
    return sorted({g["cat"] for g in goods})


def in_category(goods, ci):
    cats = categories(goods)
    if not 0 <= ci < len(cats):
        return None, []
    return cats[ci], sorted((g for g in goods if g["cat"] == cats[ci]), key=lambda g: g["offer_id"])


def product(goods, pid):
    return next((g for g in goods if g["product_id"] == pid), None)


async def positions(day):
    async def load():
        async with ozon_lock:
            try:
                return await ozon.positions(day.isoformat())
            finally:
                await asyncio.sleep(61)  # следующий запрос аналитики — не раньше чем через минуту
    return await cached(f"pos:{day}", 3600, load)


async def collect(p):
    """Всё для дашборда артикула. Каждый источник может отказать — показываем то, что есть."""
    d, sku, pid = {}, p["sku"], p["product_id"]
    jobs = {
        "price": ozon.prices([pid]),
        "customer": ozon.customer_prices([sku]),
        "elastic": cached("elastic", 1800, ozon.elastic),
        "stairway": ozon.stairways([sku]),
        "ts": ts.unit_metrics([sku], today()),
    }
    res = dict(zip(jobs, await asyncio.gather(*jobs.values(), return_exceptions=True)))
    for name, r in res.items():
        if isinstance(r, Exception):
            log.warning("%s для %s: %s", name, p["offer_id"], r)
    pr = res["price"].get(pid, {}) if isinstance(res["price"], dict) else {}
    d.update(pr)
    if pr.get("price") and pr.get("sale_percent") is not None:
        acq = (pr.get("acquiring") or 0) / pr["price"] * 100
        d["commission"] = pr["sale_percent"] + acq
    cu = res["customer"].get(sku, {}) if isinstance(res["customer"], dict) else {}
    d["customer_price"], d["ozon_percent"] = cu.get("customer_price"), cu.get("ozon_percent")
    d["soinvest"] = calc.soinvest(d.get("seller_price"), d.get("customer_price"))
    d["elastic"] = res["elastic"].get(pid) if isinstance(res["elastic"], dict) else None
    d["stairway"] = res["stairway"].get(sku) if isinstance(res["stairway"], dict) else None
    if isinstance(res["ts"], dict):
        d["ts"] = res["ts"].get(sku)
        if not d["ts"]:
            d["ts_error"] = "нет продаж за 10 дней"
    else:
        d["ts_error"] = str(res["ts"])[:80]
    t, y = today(), today() - dt.timedelta(days=1)
    store.save_snapshot(t.isoformat(), {sku: {"soinvest": d["soinvest"]}})
    snap_y = store.snapshot(y.isoformat()).get(sku, {})
    d["soinvest_y"] = snap_y.get("soinvest")
    d["position"] = store.snapshot(t.isoformat()).get(sku, {}).get("position")
    d["position_y"] = snap_y.get("position")
    if d["position"] is None and d["position_y"] is not None:
        # за сегодня Ozon ещё не посчитал — показываем вчера против позавчера
        d["position_day"] = "вчера"
        d["position"] = d["position_y"]
        d["position_y"] = store.snapshot((y - dt.timedelta(days=1)).isoformat()).get(sku, {}).get("position")
    return d


# --- доступ ---

@dp.message(CommandStart())
@dp.message(Command("menu"))
async def start(msg: Message):
    uid = msg.from_user.id
    if not allowed(uid):
        if not OWNER:
            return await msg.answer(f"Бот ещё не настроен. Ваш ID: {uid}")
        requests[uid] = who(msg.from_user)
        await msg.bot.send_message(OWNER, f"Просит доступ к боту цен: {requests[uid]}, ID {uid}",
                                   reply_markup=kb([[("✅ Пустить", f"acc:y:{uid}"), ("❌ Отказать", f"acc:n:{uid}")]]))
        return await msg.answer("Запрос отправлен владельцу. Как только он откроет доступ, я напишу.")
    await msg.answer("Бот цен AlisaFox для Ozon. Выберите раздел внизу.\n"
                     "Ничего не меняю в Ozon без вашей кнопки «✅ Да, применить».", reply_markup=menu(uid))


async def access_menu(msg: Message):
    u = store.users()
    text = "Доступ к боту. У вас доступ всегда.\n" + (
        "\n".join(f"- {name}, ID {i}" for i, name in u.items()) if u else "Других пользователей пока нет.")
    await msg.answer(text, reply_markup=kb([[(f"Убрать: {name[:28]}", f"acc:del:{i}")] for i, name in u.items()]
                                           + [[("➕ Добавить по ID", "acc:add")]]))


async def on_access(cb: CallbackQuery):
    if cb.from_user.id != OWNER:
        return await cb.answer("Только для владельца")
    await cb.answer()
    parts = cb.data.split(":")
    if parts[1] == "add":
        waiting[OWNER] = {"kind": "add"}
        return await cb.message.answer("Пришлите Telegram ID человека и, через пробел, его имя. "
                                       "Например: 123456789 Анна. Свой ID человек увидит, если напишет боту /start.")
    uid = int(parts[2])
    if parts[1] == "y":
        store.add_user(uid, requests.pop(uid, f"ID {uid}"))
        await cb.message.edit_text(cb.message.text + "\n\n✅ Доступ открыт")
        await cb.bot.send_message(uid, "Доступ открыт. Выберите раздел.", reply_markup=menu(uid))
    elif parts[1] == "n":
        requests.pop(uid, None)
        await cb.message.edit_text(cb.message.text + "\n\n❌ Отказано")
    elif parts[1] == "del":
        store.remove_user(uid)
        await cb.message.answer(f"Доступ для ID {uid} закрыт.")
        await access_menu(cb.message)


async def add_by_id(msg: Message):
    waiting.pop(OWNER, None)
    head, _, name = (msg.text or "").strip().partition(" ")
    if not head.isdigit():
        return await msg.answer("Не похоже на ID: нужны только цифры, например 123456789 Анна. "
                                "Нажмите «Добавить по ID» ещё раз.")
    store.add_user(int(head), name.strip() or f"ID {head}")
    await msg.answer(f"Добавил: {name.strip() or head}. Пусть напишет боту /start.")
    try:
        await msg.bot.send_message(int(head), "Вам открыли доступ к боту цен AlisaFox. Нажмите /start.")
    except Exception:
        pass  # человек ещё не писал боту — Telegram не даст написать первым


# --- списки: категории и артикулы ---

SECTIONS = {"p": "💰 Цены", "q": "📦 Скидка от количества"}


async def show_categories(msg: Message, sec, page=0, edit=False):
    goods = await items()
    cats = categories(goods)
    chunk = cats[page * PAGE:(page + 1) * PAGE]
    rows = [[(f"{c} ({sum(g['cat'] == c for g in goods)})", f"ca:{sec}:{page * PAGE + i}:0")]
            for i, c in enumerate(chunk)]
    nav = ([("◀️", f"cl:{sec}:{page - 1}")] if page else []) + \
          ([("▶️", f"cl:{sec}:{page + 1}")] if (page + 1) * PAGE < len(cats) else [])
    text = f"{SECTIONS[sec]} · выберите категорию ({len(cats)}, товары от {store.catalog()['updated']})"
    markup = kb(rows + ([nav] if nav else []) + [[HOME]])
    await (msg.edit_text(text, reply_markup=markup) if edit else msg.answer(text, reply_markup=markup))


async def show_category(msg: Message, sec, ci, page=0, edit=True):
    cat, goods = in_category(await items(), ci)
    if cat is None:
        return await msg.edit_text("Список товаров обновился. Откройте раздел заново.", reply_markup=nav_kb())
    chunk = goods[page * PAGE:(page + 1) * PAGE]
    rows = [[(f"{g['offer_id']} · {g['fbo']} шт · {g['name'][:24]}", f"pr:{sec}:{g['product_id']}")] for g in chunk]
    nav = ([("◀️", f"ca:{sec}:{ci}:{page - 1}")] if page else []) + \
          ([("▶️", f"ca:{sec}:{ci}:{page + 1}")] if (page + 1) * PAGE < len(goods) else [])
    if nav:
        rows.append(nav)
    if sec == "q":
        rows.append([("📦 Вся категория: уровни скидки", f"qcs:{ci}")])
        rows.append([("🗑 Вся категория: удалить скидку", f"qcd:{ci}")])
    rows.append([("⬅️ Категории", f"cl:{sec}:0"), HOME])
    text = f"{SECTIONS[sec]} · {html.escape(cat)}: {len(goods)} шт. Выберите артикул."
    await (msg.edit_text(text, reply_markup=kb(rows)) if edit else msg.answer(text, reply_markup=kb(rows)))


async def show_product(msg: Message, sec, pid, edit=True):
    goods = await items()
    p = product(goods, pid)
    if not p:
        return await msg.answer("Товар больше не в продаже или закончился на складе Ozon. Нажмите «Обновить остатки».", reply_markup=nav_kb())
    ci = categories(goods).index(p["cat"])
    back = ("⬅️ Назад", f"ca:{sec}:{ci}:0")
    if sec == "p":
        wait = await msg.answer("Собираю данные по артикулу…")
        d = await collect(p)
        await wait.delete()
        return await msg.answer(calc.dashboard(p, d), reply_markup=kb(
            [[("✏️ Изменить цену", f"pe:{pid}"), ("✅ Оставить", f"ca:p:{ci}:0")], [back, HOME]]))
    try:
        st = (await ozon.stairways([p["sku"]])).get(p["sku"])
    except OzonError as e:
        return await msg.answer(f"Ozon не отдал скидку: {e}", reply_markup=nav_kb(back[1]))
    has = bool(st and st["enabled"] and st["steps"])
    text = (f"📦 <b>{html.escape(p['offer_id'])}</b> · {html.escape(p['name'][:80])}\n"
            f"Скидка от количества: {calc.fmt_steps(st['steps']) if has else 'нет'}"
            + (f"\nСтатус в Ozon: {st['status']}" if st and st.get("status") not in (None, "SUCCESS") else ""))
    rows = [[("✏️ Изменить уровни" if has else "➕ Поставить скидку", f"qs:{pid}")]]
    if has:
        rows[0].append(("🗑 Удалить все", f"qd:{pid}"))
    rows.append([back, HOME])
    await (msg.edit_text(text, reply_markup=kb(rows)) if edit else msg.answer(text, reply_markup=kb(rows)))


# --- скидка от количества ---

drafts: dict[int, dict] = {}  # редактор уровней скидки: черновик до «Сохранить»


async def open_editor(cb: CallbackQuery, pids, title, back):
    """Редактор берёт текущие уровни из Ozon; у категории — общие уровни, если у всех товаров они одинаковые."""
    goods = await items()
    targets = [t for t in (product(goods, pid) for pid in pids) if t]
    current = await ozon.stairways([t["sku"] for t in targets])
    variants = {tuple(c["steps"]) if (c := current.get(t["sku"])) and c["enabled"] else () for t in targets}
    orig = list(variants.pop()) if len(variants) == 1 else []
    note = "" if len(targets) == 1 or len(variants) == 0 else \
        "⚠️ У товаров категории сейчас разные уровни. После сохранения у всех станут одинаковые."
    drafts[cb.from_user.id] = {"targets": [t["product_id"] for t in targets], "title": title, "back": back,

                               "orig": orig, "steps": [list(x) for x in orig], "note": note,
                               "current": {t["sku"]: current.get(t["sku"]) for t in targets}}
    await show_editor(cb.message, cb.from_user.id, edit=False)


def editor_kb(dr):
    rows = []
    for i, (q, d) in enumerate(dr["steps"]):
        n = "①②③④"[i]
        rows.append([(f"{n} −1%", f"dq:-:{i}"), (f"{n} +1%", f"dq:+:{i}"), (f"{n} ✏️", f"dq:e:{i}"),
                     (f"{n} 🗑", f"dq:x:{i}")])
    extra = []
    if len(dr["steps"]) < calc.MAX_STEPS:
        extra.append(("➕ Добавить уровень", "dq:a"))
    if dr["steps"]:
        extra.append(("🗑 Убрать все", "dq:c"))
    if extra:
        rows.append(extra)
    rows.append([("✅ Сохранить", "dq:s"), ("❌ Отмена", f"no|{dr['back']}")])
    rows.append([HOME])
    return kb(rows)


async def show_editor(msg: Message, uid, edit=True):
    dr = drafts[uid]
    text = calc.editor_text(dr["title"], dr["orig"], dr["steps"], dr["note"])
    if not edit:
        return await msg.answer(text, reply_markup=editor_kb(dr))
    try:
        await msg.edit_text(text, reply_markup=editor_kb(dr))
    except TelegramBadRequest:
        pass  # ничего не поменялось (например, −1% у скидки 1%) — Telegram не даёт «править» тем же текстом


def next_qty(steps):
    return (max(q for q, _ in steps) + 1) if steps else 2


async def on_editor(cb: CallbackQuery, a):
    uid = cb.from_user.id
    dr = drafts.get(uid)
    if not dr:
        return await cb.message.answer("Редактор уже закрыт. Откройте товар заново.", reply_markup=nav_kb())
    op = a[1]
    if op in "+-":
        i = int(a[2])
        dr["steps"][i][1] = max(1, min(90, dr["steps"][i][1] + (1 if op == "+" else -1)))
        return await show_editor(cb.message, uid)
    if op == "x":
        del dr["steps"][int(a[2])]
        return await show_editor(cb.message, uid)
    if op == "c":
        dr["steps"] = []
        return await show_editor(cb.message, uid)
    if op in ("a", "e"):
        # шаг 1 из 2: количество. Для нового уровня подсказываем следующее после последнего
        dr["edit"] = int(a[2]) if op == "e" else None
        others = [x for j, x in enumerate(dr["steps"]) if j != dr["edit"]]
        start = dr["steps"][dr["edit"]][0] if op == "e" else next_qty(others)
        opts = sorted({start, start + 1, start + 2, start + 3, 5, 10} - {q for q, _ in others} - {0, 1})[:6]
        waiting[uid] = {"kind": "dq_qty", "back": dr["back"]}
        return await cb.message.edit_text("От скольких штук действует уровень? Нажмите или пришлите число.",
                                          reply_markup=kb([[(f"от {q} шт", f"dq:q:{q}") for q in opts[:3]],
                                                           [(f"от {q} шт", f"dq:q:{q}") for q in opts[3:]],
                                                           [("⬅️ К уровням", "dq:v")]]))
    if op == "q":
        return await ask_pct(cb.message, uid, int(a[2]))
    if op == "d":
        return await put_level(cb.message, uid, int(a[2]))
    if op == "v":
        waiting.pop(uid, None)
        return await show_editor(cb.message, uid)
    if op == "s":
        return await save_editor(cb, uid)


async def ask_pct(msg: Message, uid, qty, edit=True):
    """Шаг 2 из 2: скидка. Подсказки — чуть больше, чем у уровня с меньшим количеством."""
    dr = drafts[uid]
    dr["qty"] = qty
    others = [x for j, x in enumerate(dr["steps"]) if j != dr.get("edit")]
    below = max((d for q, d in others if q < qty), default=0)
    cur = dr["steps"][dr["edit"]][1] if dr.get("edit") is not None else None
    opts = sorted({x for x in (cur, below + 1, below + 2, below + 3, below + 5, below + 10) if x and x <= 90})[:6]
    waiting[uid] = {"kind": "dq_pct", "back": dr["back"]}
    text = f"Уровень от {qty} шт. Какая скидка, %? Нажмите или пришлите число."
    markup = kb([[(f"−{d}%", f"dq:d:{d}") for d in opts[:3]], [(f"−{d}%", f"dq:d:{d}") for d in opts[3:]],
                 [("⬅️ К уровням", "dq:v")]])
    await (msg.edit_text(text, reply_markup=markup) if edit else msg.answer(text, reply_markup=markup))


async def put_level(msg: Message, uid, pct, edit=True):
    dr = drafts[uid]
    waiting.pop(uid, None)
    if dr.get("edit") is not None:
        dr["steps"][dr["edit"]] = [dr["qty"], pct]
    else:
        dr["steps"].append([dr["qty"], pct])
    dr["steps"].sort()
    dr["edit"] = None
    await show_editor(msg, uid, edit=edit)


async def got_level_number(msg: Message, w):
    """Число вместо кнопки на шаге «количество» или «скидка»."""
    uid = msg.from_user.id
    t = (msg.text or "").strip().replace("%", "").replace("шт", "").strip()
    if not t.isdigit():
        return await msg.answer("Нужно целое число, например 3.", reply_markup=kb([[("⬅️ К уровням", "dq:v")]]))
    n = int(t)
    if w["kind"] == "dq_qty":
        if n < 2:
            return await msg.answer("Количество — от 2 шт.", reply_markup=kb([[("⬅️ К уровням", "dq:v")]]))
        return await ask_pct(msg, uid, n, edit=False)
    if not 1 <= n <= 90:
        return await msg.answer("Скидка — от 1 до 90%.", reply_markup=kb([[("⬅️ К уровням", "dq:v")]]))
    await put_level(msg, uid, n, edit=False)


async def save_editor(cb: CallbackQuery, uid):
    dr = drafts[uid]
    try:
        steps = calc.validate_steps(dr["steps"])
    except ValueError as e:
        return await cb.message.answer(f"⚠️ {e} Поправьте уровни и нажмите «Сохранить» ещё раз.")
    goods = await items()
    targets = [t for t in (product(goods, pid) for pid in dr["targets"]) if t]
    jobs, lines = [], [f"Скидка от количества: <b>{dr['title']}</b>", ""]
    for t in targets:
        c = dr["current"].get(t["sku"])
        was = c["steps"] if c and c["enabled"] else []
        if was == steps:
            continue  # у этого товара уже так
        if steps:
            jobs.append((t["sku"], True, steps))
        elif was:
            jobs.append((t["sku"], False, was))  # Ozon выключает скидку только вместе с текущими уровнями
        if len(jobs) <= 15:
            lines.append(f"{t['offer_id']}: {calc.fmt_steps(was)} → {calc.fmt_steps(steps)}")
    if not jobs:
        return await cb.message.answer("Изменений нет — сохранять нечего.")
    if len(jobs) > 15:
        lines.append(f"…и ещё {len(jobs) - 15}")
    drafts.pop(uid, None)
    pending[uid] = {"kind": "steps", "back": dr["back"], "items": jobs,
                    "was": {t["sku"]: (dr["current"].get(t["sku"]) or {}).get("steps") if
                            (dr["current"].get(t["sku"]) or {}).get("enabled") else [] for t in targets},
                    "names": {t["sku"]: t["offer_id"] for t in targets}}
    await cb.message.edit_text("\n".join(lines + ["", "Точно применяем?"]), reply_markup=confirm_kb(dr["back"]))


async def ask_delete(cb: CallbackQuery, pids, title, back):
    goods = await items()
    targets = [t for t in (product(goods, pid) for pid in pids) if t]
    current = await ozon.stairways([t["sku"] for t in targets])
    on = [t for t in targets if current.get(t["sku"], {}).get("enabled") and current[t["sku"]]["steps"]]
    if not on:
        return await cb.message.answer("Скидки от количества здесь нет — удалять нечего.", reply_markup=nav_kb(back))
    lines = [f"Удаляем скидку от количества: <b>{title}</b>", ""] + \
            [f"{t['offer_id']}: {calc.fmt_steps(current[t['sku']]['steps'])} → нет" for t in on[:15]]
    if len(on) > 15:
        lines.append(f"…и ещё {len(on) - 15}")
    pending[cb.from_user.id] = {"kind": "steps", "back": back,
                                "items": [(t["sku"], False, current[t["sku"]]["steps"]) for t in on],
                                "was": {t["sku"]: current[t["sku"]]["steps"] for t in on},
                                "names": {t["sku"]: t["offer_id"] for t in on}}
    await cb.message.answer("\n".join(lines + ["", "Точно удаляем?"]), reply_markup=confirm_kb(back))


async def apply_steps(cb: CallbackQuery, job):
    try:
        r = await ozon.set_stairways(job["items"])
    except OzonError as e:
        return await cb.message.answer(f"❌ Ozon не принял: {e}", reply_markup=nav_kb(job["back"]))
    ts = f"{dt.datetime.now(MSK):%d.%m %H:%M}"
    store.add_history([{"ts": ts, "who": cb.from_user.full_name, "kind": "qty", "offer": job["names"].get(s, str(s)),
                        "old": [list(x) for x in job["was"].get(s) or []], "new": [list(x) for x in st] if on else []}
                       for s, on, st in job["items"] if s not in r["errors"]])
    errs = {s: m for s, m in r["errors"].items() if m}
    text = f"📤 Отправила в Ozon: {len(job['items'])} шт. Ozon применяет не сразу — проверю через минуту."
    if errs:
        text += "\n\n❌ Ошибки:\n" + "\n".join(f"{job['names'].get(s, s)}: {'; '.join(m)}" for s, m in errs.items())
    await cb.message.answer(text, reply_markup=nav_kb(job["back"]))
    asyncio.create_task(check_steps(cb.bot, cb.message.chat.id, job))


async def check_steps(bot: Bot, chat_id, job):
    await asyncio.sleep(60)
    try:
        st = await ozon.stairways([s for s, _, _ in job["items"]])
    except OzonError as e:
        return await bot.send_message(chat_id, f"Не смогла проверить статус: {e}")
    by = {"SUCCESS": [], "IN_PROCESS": [], "ERROR": []}
    for s, _, _ in job["items"]:
        by.setdefault((st.get(s) or {}).get("status") or "IN_PROCESS", []).append(job["names"].get(s, str(s)))
    lines = [f"✅ Применено: {len(by['SUCCESS'])}"]
    if by["IN_PROCESS"]:
        lines.append(f"⏳ Ещё в процессе: {len(by['IN_PROCESS'])} — проверьте позже в карточке")
    if by["ERROR"]:
        lines.append("❌ Ошибка у: " + ", ".join(by["ERROR"][:20]) + " — попробуйте ещё раз")
    await bot.send_message(chat_id, "Скидка от количества:\n" + "\n".join(lines), reply_markup=nav_kb(job["back"]))


# --- цена ---

async def got_price(msg: Message, w):
    try:
        new = calc.parse_price(msg.text)
    except ValueError as e:
        return await msg.answer(f"{e}\nПришлите ещё раз или нажмите «Отмена».", reply_markup=cancel_kb(w["back"]))
    waiting.pop(msg.from_user.id, None)
    p = product(await items(), w["pid"])
    d = await collect(p)
    if d.get("price") == new:
        return await msg.answer("Цена такая же, как сейчас. Ничего не меняю.", reply_markup=nav_kb(w["back"]))
    pending[msg.from_user.id] = {"kind": "price", "back": w["back"], "pid": p["product_id"], "offer_id": p["offer_id"],
                                 "old": d.get("price"), "new": new}
    await msg.answer(calc.confirm_price(p, d, new), reply_markup=confirm_kb(w["back"]))


async def apply_price(cb: CallbackQuery, job):
    try:
        errors = await ozon.set_price(job["offer_id"], job["new"])
    except OzonError as e:
        errors = [str(e)]
    if errors:
        return await cb.message.answer(f"❌ Ozon не принял цену {job['offer_id']}: " + "; ".join(errors),
                                       reply_markup=nav_kb(job["back"]))
    store.add_history([{"ts": f"{dt.datetime.now(MSK):%d.%m %H:%M}", "who": cb.from_user.full_name, "kind": "price",
                        "offer": job["offer_id"], "old": job["old"], "new": job["new"]}])
    cache.pop("elastic", None)  # участие в бустинге зависит от цены
    await cb.message.answer(f"✅ Цена {job['offer_id']}: {calc.rub(job['old'])} → {calc.rub(job['new'])}. "
                            "На витрине Ozon обновится в течение нескольких минут.",
                            reply_markup=kb([[("🔁 Открыть артикул", f"pr:p:{job['pid']}"), HOME]]))


def confirm_kb(back):
    return kb([[("✅ Да, применить", "ok"), ("❌ Отмена", f"no|{back}")], [HOME]])


def cancel_kb(back):
    """Под вопросом «пришлите цену / уровни»: отменить ввод и вернуться назад."""
    return kb([[("❌ Отмена", f"no|{back}"), HOME]])


# --- снимки на каждый день: соинвест и место в выдаче ---

async def daily_snapshots():
    """Каждые 3 часа сохраняет соинвест и место в выдаче по всем SKU — для сравнения «сегодня / вчера»."""
    while True:
        try:
            goods = await items()
            day = today()
            prices = await ozon.prices([g["product_id"] for g in goods])
            cust = await ozon.customer_prices([g["sku"] for g in goods])
            values = {}
            for g in goods:
                si = calc.soinvest((prices.get(g["product_id"]) or {}).get("seller_price"),
                                   (cust.get(g["sku"]) or {}).get("customer_price"))
                values[g["sku"]] = {"soinvest": si}
            for sku, pos in (await positions(day)).items():
                values.setdefault(sku, {})["position"] = pos
            store.save_snapshot(day.isoformat(), values)
            # аналитика Ozon за сегодня может быть ещё пустой — держим и вчерашнее место
            y = day - dt.timedelta(days=1)
            store.save_snapshot(y.isoformat(), {sku: {"position": pos} for sku, pos in (await positions(y)).items()})
            log.info("снимок за %s: %s SKU", day, len(values))
        except Exception:
            log.exception("снимок не удался")
        await asyncio.sleep(3 * 3600)


# --- обработчики ---

@dp.message(F.text.in_({PRICES, QTY}))
async def on_section(msg: Message):
    if not allowed(msg.from_user.id):
        return
    waiting.pop(msg.from_user.id, None)
    try:
        await show_categories(msg, "p" if msg.text == PRICES else "q")
    except OzonError as e:
        await msg.answer(f"Ozon не отдал товары: {e}")


async def show_history(msg: Message, page=0, edit=False):
    h = store.history()
    if not h:
        return await msg.answer("История пока пустая: через бота ещё ничего не меняли.", reply_markup=nav_kb())
    chunk = h[page * HIST_PAGE:(page + 1) * HIST_PAGE]
    pages = (len(h) - 1) // HIST_PAGE + 1
    text = f"📜 История изменений через бота (стр. {page + 1} из {pages}, новые сверху)\n\n" + \
           "\n".join(calc.fmt_history(e) for e in chunk)
    nav = ([("◀️ Новее", f"hi:{page - 1}")] if page else []) + \
          ([("Старее ▶️", f"hi:{page + 1}")] if page + 1 < pages else [])
    markup = kb(([nav] if nav else []) + [[HOME]])
    await (msg.edit_text(text, reply_markup=markup) if edit else msg.answer(text, reply_markup=markup))


@dp.message(F.text == HISTORY)
async def on_history(msg: Message):
    if allowed(msg.from_user.id):
        await show_history(msg)


@dp.message(F.text == REFRESH)
async def on_refresh(msg: Message):
    """Список товаров обновляется только по этой кнопке: в боте — то, что в продаже и есть на складах Ozon."""
    if not allowed(msg.from_user.id):
        return
    wait = await msg.answer("Обновляю остатки из Ozon…")
    try:
        goods = await refresh_catalog()
    except OzonError as e:
        return await wait.edit_text(f"Не получилось: {e}")
    cache.pop("elastic", None)
    await wait.edit_text(f"Готово: {len(goods)} товаров в продаже с остатком на складах Ozon, "
                         f"{len(categories(goods))} категорий.", reply_markup=nav_kb())


@dp.message(F.text == ACCESS)
async def on_access_menu(msg: Message):
    if msg.from_user.id == OWNER:
        await access_menu(msg)


@dp.message()
async def on_message(msg: Message):
    uid = msg.from_user.id
    if not allowed(uid):
        return await msg.answer("Нет доступа. Нажмите /start, чтобы отправить запрос владельцу.")
    w = waiting.get(uid)
    if not w:
        return await msg.answer("Выберите раздел кнопкой внизу.", reply_markup=menu(uid))
    if w["kind"] == "add":
        return await add_by_id(msg)
    try:
        if w["kind"] in ("dq_qty", "dq_pct"):
            return await got_level_number(msg, w)
        if w["kind"] == "price":
            return await got_price(msg, w)
    except OzonError as e:
        await msg.answer(f"Ozon не ответил: {e}. Попробуйте ещё раз.")


@dp.callback_query()
async def on_button(cb: CallbackQuery):
    if cb.data.startswith("acc:"):
        return await on_access(cb)
    uid = cb.from_user.id
    if not allowed(uid):
        return await cb.answer("Нет доступа")
    await cb.answer()
    await route(cb, cb.data)


async def show_home(msg: Message, uid):
    await msg.answer("Главное меню. Выберите раздел.", reply_markup=kb([[("💰 Цены", "cl:p:0n"),
                                                                       ("📦 Скидка от количества", "cl:q:0n")],
                                                                      [("📜 История", "hi:0")]]))
    await msg.answer("Кнопки разделов есть и внизу экрана.", reply_markup=menu(uid))


async def route(cb: CallbackQuery, data):
    uid = cb.from_user.id
    if data == "home" or data.startswith("no|"):
        waiting.pop(uid, None)
        drafts.pop(uid, None)
        had = pending.pop(uid, None)
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass  # сообщение уже без кнопок или слишком старое
        if data == "home":
            return await show_home(cb.message, uid)
        await cb.message.answer("Отменено, в Ozon ничего не меняла." if had else "Отменено.")
        return await route(cb, data[3:])
    fresh = data.endswith("n")  # «n» в конце — показать новым сообщением, а не править старое
    a = data.rstrip("n").split(":")
    try:
        if a[0] == "cl":
            return await show_categories(cb.message, a[1], int(a[2]), edit=not fresh)
        if a[0] == "ca":
            return await show_category(cb.message, a[1], int(a[2]), int(a[3]), edit=not fresh)
        if a[0] == "pr":
            return await show_product(cb.message, a[1], int(a[2]), edit=a[1] == "q" and not fresh)
        if a[0] == "pe":
            p = product(await items(), int(a[1]))
            back = f"pr:p:{p['product_id']}"
            waiting[uid] = {"kind": "price", "pid": p["product_id"], "back": back}
            return await cb.message.answer(f"Новая цена для {p['offer_id']}? Пришлите число, например 1290.",
                                           reply_markup=cancel_kb(back))
        if a[0] == "qs":
            p = product(await items(), int(a[1]))
            return await open_editor(cb, [p["product_id"]], html.escape(p["offer_id"]), f"pr:q:{p['product_id']}n")
        if a[0] == "qd":
            p = product(await items(), int(a[1]))
            return await ask_delete(cb, [p["product_id"]], p["offer_id"], f"pr:q:{p['product_id']}n")
        if a[0] in ("qcs", "qcd"):
            cat, goods = in_category(await items(), int(a[1]))
            pids = [g["product_id"] for g in goods]
            title = f"вся категория «{html.escape(cat)}» ({len(pids)} шт.)"
            back = f"ca:q:{a[1]}:0n"
            return await (open_editor(cb, pids, title, back) if a[0] == "qcs" else ask_delete(cb, pids, title, back))
        if a[0] == "hi":
            return await show_history(cb.message, int(a[1]), edit=True)
        if a[0] == "dq":
            return await on_editor(cb, a)
        if a[0] == "ok":
            job = pending.pop(uid, None)
            await cb.message.edit_reply_markup(reply_markup=None)
            if not job:
                return await cb.message.answer("Это подтверждение уже неактуально.", reply_markup=nav_kb())
            return await (apply_price(cb, job) if job["kind"] == "price" else apply_steps(cb, job))
    except OzonError as e:
        await cb.message.answer(f"Ozon не ответил: {e}. Попробуйте ещё раз.", reply_markup=nav_kb())


async def main():
    global ozon
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    session = AiohttpSession()
    if os.environ.get("SSL_CERT_FILE"):  # свой корневой сертификат (прокси облачной сессии)
        session._connector_init["ssl"] = ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])
    bot = Bot(os.environ["TELEGRAM_PRICE_BOT_TOKEN"], session=session,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    ozon = Ozon()
    asyncio.create_task(daily_snapshots())
    try:
        await dp.start_polling(bot)
    finally:
        await ozon.close()


if __name__ == "__main__":
    asyncio.run(main())
