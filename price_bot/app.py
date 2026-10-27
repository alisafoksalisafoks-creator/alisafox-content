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
REFRESH = "🔄 Обновить товары"
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
            [KeyboardButton(text=REFRESH)] + ([KeyboardButton(text=ACCESS)] if user_id == OWNER else [])]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in r]
                                                 for r in rows])


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
    markup = kb(rows + ([nav] if nav else []))
    await (msg.edit_text(text, reply_markup=markup) if edit else msg.answer(text, reply_markup=markup))


async def show_category(msg: Message, sec, ci, page=0):
    cat, goods = in_category(await items(), ci)
    if cat is None:
        return await msg.edit_text("Список товаров обновился. Откройте раздел заново.")
    chunk = goods[page * PAGE:(page + 1) * PAGE]
    rows = [[(f"{g['offer_id']} · {g['fbo']} шт · {g['name'][:24]}", f"pr:{sec}:{g['product_id']}")] for g in chunk]
    nav = ([("◀️", f"ca:{sec}:{ci}:{page - 1}")] if page else []) + \
          ([("▶️", f"ca:{sec}:{ci}:{page + 1}")] if (page + 1) * PAGE < len(goods) else [])
    if nav:
        rows.append(nav)
    if sec == "q":
        rows.append([("📦 Вся категория: поставить / изменить", f"qcs:{ci}")])
        rows.append([("🗑 Вся категория: удалить скидку", f"qcd:{ci}")])
    rows.append([("⬅️ Категории", f"cl:{sec}:0")])
    await msg.edit_text(f"{SECTIONS[sec]} · {html.escape(cat)}: {len(goods)} шт. Выберите артикул.", reply_markup=kb(rows))


async def show_product(msg: Message, sec, pid, edit=True):
    goods = await items()
    p = product(goods, pid)
    if not p:
        return await msg.answer("Товар не найден, нажмите «Обновить товары».")
    ci = categories(goods).index(p["cat"])
    back = ("⬅️ Назад", f"ca:{sec}:{ci}:0")
    if sec == "p":
        wait = await msg.answer("Собираю данные по артикулу…")
        d = await collect(p)
        await wait.delete()
        return await msg.answer(calc.dashboard(p, d), reply_markup=kb(
            [[("✏️ Изменить цену", f"pe:{pid}"), ("✅ Оставить", f"ca:p:{ci}:0")], [back]]))
    try:
        st = (await ozon.stairways([p["sku"]])).get(p["sku"])
    except OzonError as e:
        return await msg.answer(f"Ozon не отдал скидку: {e}")
    has = bool(st and st["enabled"] and st["steps"])
    text = (f"📦 <b>{html.escape(p['offer_id'])}</b> · {html.escape(p['name'][:80])}\n"
            f"Скидка от количества: {calc.fmt_steps(st['steps']) if has else 'нет'}"
            + (f"\nСтатус в Ozon: {st['status']}" if st and st.get("status") not in (None, "SUCCESS") else ""))
    rows = [[("✏️ Изменить" if has else "➕ Поставить скидку", f"qs:{pid}")]]
    if has:
        rows[0].append(("🗑 Удалить", f"qd:{pid}"))
    rows.append([back])
    await (msg.edit_text(text, reply_markup=kb(rows)) if edit else msg.answer(text, reply_markup=kb(rows)))


# --- скидка от количества ---

STEPS_HELP = ("Пришлите уровни одним сообщением: количество-скидка через запятую.\n"
              "Пример: <code>2-5, 3-10</code> — от 2 шт скидка 5%, от 3 шт — 10%.\n"
              "До 4 уровней, скидка только в %.")


async def ask_steps(cb: CallbackQuery, targets, title):
    waiting[cb.from_user.id] = {"kind": "steps", "targets": targets, "title": title}
    await cb.message.answer(f"Скидка от количества для: {title}\n\n{STEPS_HELP}")


async def got_steps(msg: Message, w):
    try:
        steps = calc.parse_steps(msg.text or "")
    except ValueError as e:
        return await msg.answer(f"{e}\nПришлите ещё раз или /menu, чтобы отменить.")
    waiting.pop(msg.from_user.id, None)
    goods = await items()
    targets = [product(goods, pid) for pid in w["targets"]]
    targets = [t for t in targets if t]
    current = await ozon.stairways([t["sku"] for t in targets])
    lines = [f"Ставим скидку от количества: <b>{w['title']}</b>", f"Станет: {calc.fmt_steps(steps)}", ""]
    for t in targets[:15]:
        c = current.get(t["sku"])
        lines.append(f"{t['offer_id']}: {calc.fmt_steps(c['steps']) if c and c['enabled'] else 'нет'} → "
                     f"{calc.fmt_steps(steps)}")
    if len(targets) > 15:
        lines.append(f"…и ещё {len(targets) - 15}")
    pending[msg.from_user.id] = {"kind": "steps", "items": [(t["sku"], True, steps) for t in targets],
                                 "names": {t["sku"]: t["offer_id"] for t in targets},
                                 "summary": f"скидка от количества {w['title']}: {calc.fmt_steps(steps)}"}
    await msg.answer("\n".join(lines + ["", "Точно применяем?"]), reply_markup=confirm_kb())


async def ask_delete(cb: CallbackQuery, pids, title):
    goods = await items()
    targets = [t for t in (product(goods, pid) for pid in pids) if t]
    current = await ozon.stairways([t["sku"] for t in targets])
    on = [t for t in targets if current.get(t["sku"], {}).get("enabled") and current[t["sku"]]["steps"]]
    if not on:
        return await cb.message.answer("Скидки от количества здесь нет — удалять нечего.")
    lines = [f"Удаляем скидку от количества: <b>{title}</b>", ""] + \
            [f"{t['offer_id']}: {calc.fmt_steps(current[t['sku']]['steps'])} → нет" for t in on[:15]]
    if len(on) > 15:
        lines.append(f"…и ещё {len(on) - 15}")
    pending[cb.from_user.id] = {"kind": "steps",
                                "items": [(t["sku"], False, current[t["sku"]]["steps"]) for t in on],
                                "names": {t["sku"]: t["offer_id"] for t in on},
                                "summary": f"удалена скидка от количества {title}"}
    await cb.message.answer("\n".join(lines + ["", "Точно удаляем?"]), reply_markup=confirm_kb())


async def apply_steps(cb: CallbackQuery, job):
    try:
        r = await ozon.set_stairways(job["items"])
    except OzonError as e:
        return await cb.message.answer(f"❌ Ozon не принял: {e}")
    store.log_change(f"{dt.datetime.now(MSK):%Y-%m-%d %H:%M} {who(cb.from_user)}: {job['summary']}")
    errs = {s: m for s, m in r["errors"].items() if m}
    text = f"📤 Отправила в Ozon: {len(job['items'])} шт. Ozon применяет не сразу — проверю через минуту."
    if errs:
        text += "\n\n❌ Ошибки:\n" + "\n".join(f"{job['names'].get(s, s)}: {'; '.join(m)}" for s, m in errs.items())
    await cb.message.answer(text)
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
    await bot.send_message(chat_id, "Скидка от количества:\n" + "\n".join(lines))


# --- цена ---

async def got_price(msg: Message, w):
    try:
        new = calc.parse_price(msg.text)
    except ValueError as e:
        return await msg.answer(f"{e}\nПришлите ещё раз или /menu, чтобы отменить.")
    waiting.pop(msg.from_user.id, None)
    p = product(await items(), w["pid"])
    d = await collect(p)
    if d.get("price") == new:
        return await msg.answer("Цена такая же, как сейчас. Ничего не меняю.")
    pending[msg.from_user.id] = {"kind": "price", "pid": p["product_id"], "offer_id": p["offer_id"],
                                 "old": d.get("price"), "new": new}
    await msg.answer(calc.confirm_price(p, d, new), reply_markup=confirm_kb())


async def apply_price(cb: CallbackQuery, job):
    try:
        errors = await ozon.set_price(job["offer_id"], job["new"])
    except OzonError as e:
        errors = [str(e)]
    if errors:
        return await cb.message.answer(f"❌ Ozon не принял цену {job['offer_id']}: " + "; ".join(errors))
    store.log_change(f"{dt.datetime.now(MSK):%Y-%m-%d %H:%M} {who(cb.from_user)}: цена {job['offer_id']} "
                     f"{job['old']} → {job['new']}")
    cache.pop("elastic", None)  # участие в бустинге зависит от цены
    await cb.message.answer(f"✅ Цена {job['offer_id']}: {calc.rub(job['old'])} → {calc.rub(job['new'])}. "
                            "На витрине Ozon обновится в течение нескольких минут.",
                            reply_markup=kb([[("🔁 Открыть артикул", f"pr:p:{job['pid']}")]]))


def confirm_kb():
    return kb([[("✅ Да, применить", "ok"), ("❌ Отмена", "no")]])


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


@dp.message(F.text == REFRESH)
async def on_refresh(msg: Message):
    if not allowed(msg.from_user.id):
        return
    wait = await msg.answer("Обновляю список товаров из Ozon…")
    try:
        goods = await refresh_catalog()
    except OzonError as e:
        return await wait.edit_text(f"Не получилось: {e}")
    cache.pop("elastic", None)
    await wait.edit_text(f"Готово: {len(goods)} товаров в продаже с остатком на складах Ozon, "
                         f"{len(categories(goods))} категорий.")


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
        if w["kind"] == "steps":
            return await got_steps(msg, w)
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
    a = cb.data.split(":")
    try:
        if a[0] == "cl":
            return await show_categories(cb.message, a[1], int(a[2]), edit=True)
        if a[0] == "ca":
            return await show_category(cb.message, a[1], int(a[2]), int(a[3]))
        if a[0] == "pr":
            return await show_product(cb.message, a[1], int(a[2]), edit=a[1] == "q")
        if a[0] == "pe":
            p = product(await items(), int(a[1]))
            waiting[uid] = {"kind": "price", "pid": p["product_id"]}
            return await cb.message.answer(f"Новая цена для {p['offer_id']}? Пришлите число, например 1290.")
        if a[0] == "qs":
            p = product(await items(), int(a[1]))
            return await ask_steps(cb, [p["product_id"]], p["offer_id"])
        if a[0] == "qd":
            p = product(await items(), int(a[1]))
            return await ask_delete(cb, [p["product_id"]], p["offer_id"])
        if a[0] in ("qcs", "qcd"):
            cat, goods = in_category(await items(), int(a[1]))
            pids = [g["product_id"] for g in goods]
            title = f"вся категория «{html.escape(cat)}» ({len(pids)} шт.)"
            return await (ask_steps(cb, pids, title) if a[0] == "qcs" else ask_delete(cb, pids, title))
        if a[0] in ("ok", "no"):
            job = pending.pop(uid, None)
            await cb.message.edit_reply_markup(reply_markup=None)
            if a[0] == "no" or not job:
                return await cb.message.answer("Отменено, в Ozon ничего не меняла." if a[0] == "no"
                                               else "Это подтверждение уже неактуально.")
            return await (apply_price(cb, job) if job["kind"] == "price" else apply_steps(cb, job))
    except OzonError as e:
        await cb.message.answer(f"Ozon не ответил: {e}. Попробуйте ещё раз.")


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
