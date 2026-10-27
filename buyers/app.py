"""Бот «Покупатели» AlisaFox: что и по какой цене купили отслеживаемые ID покупателей Ozon. Только чтение API.

Отдельный бот: свой токен, своя база, свой сервис. «Аналитик» и БОТ_секретарь не трогает.
Запуск: python -m buyers.app
Переменные окружения: TELEGRAM_BOT_TOKEN (токен этого бота), TELEGRAM_CHAT_ID (владелец),
OZON_BOT_CLIENT_ID / OZON_BOT_API_KEY (или OZON_CLIENT_ID / OZON_API_KEY); необязательно BUYERS_DB.

Меню: 👥 Покупатели → имя (или «Все») → Сегодня / 7 дней / Месяц → 🔢 В штуках / 📋 Подробнее.
«В штуках» — только число по дням (у «Все» — по именам). «Подробнее» — артикул, штуки, сумма в заказе
и сколько заплатил покупатель. Отменённые заказы в штуки не идут, пишутся примечанием.
"""
import asyncio
import logging
import os
import shutil
import ssl
import tempfile
import threading
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message

from buyers import env  # noqa: F401  (подхватывает .env до чтения ключей)
from buyers import render, report, store
from buyers.ozon import Ozon, buyer_of
from buyers.render import money, short
from buyers.store import MSK

log = logging.getLogger("buyers")
ADMINS = {int(x) for x in os.environ.get("TELEGRAM_CHAT_ID", "").replace(" ", "").split(",") if x}
SYNC_EVERY = timedelta(minutes=10)   # перед отчётом докачиваем, если данные старше 10 минут
RECHECK_HOUR = 4                     # в 4:00 МСК перечитываем статусы за 10 дней (отмены)
NOTE_PRICE = ("«В заказе» — наша цена продажи в заказе Ozon. «Заплатил» — цена для покупателя по данным Ozon,\n"
              "со скидками за счёт Ozon. «—» — Ozon эту цену не отдал.")

dp = Dispatcher()
db = store.connect(os.environ.get("BUYERS_DB", os.path.join(os.path.dirname(__file__), "data.sqlite")))
oz = Ozon()
waiting: dict[int, tuple] = {}       # кто что вводит: ("add",), ("ren", id), ("user",)
access_requests: dict[int, str] = {}
_lock = threading.Lock()             # кнопки и фоновая докачка не качают одновременно
state = {"loading": False}


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in r]
                                                 for r in rows])


# --- данные ---

def fresh():
    last = store.get_meta(db, "last_sync")
    return last and datetime.now(MSK) - datetime.fromisoformat(last) < SYNC_EVERY


def ensure_fresh():
    with _lock:
        if not fresh():
            report.sync(db, oz)


def locked(fn, *a):
    with _lock:
        return fn(*a)


def ids_of(who):
    return [b for b, _ in store.buyers(db)] if who == "all" else [int(who)]


def title_of(who):
    return "Все покупатели" if who == "all" else store.buyer_name(db, int(who))


# --- пользователи ---

def users():
    return {r[0]: r[1] for r in db.execute("select id, name from users")}


def allowed(uid):
    return uid in ADMINS or uid in users() or not ADMINS


def is_admin(uid):
    return uid in ADMINS or not ADMINS


def who_is(u):
    return " ".join(x for x in (u.first_name, u.last_name) if x) + (f" (@{u.username})" if u.username else "")


# --- меню ---

def main_menu(uid):
    rows = [[("👥 Покупатели", "b")], [("➕ Добавить ID покупателя", "add")]]
    if is_admin(uid):
        rows.append([("👤 Добавить пользователя в бот", "u")])
    return kb(rows)


def buyers_kb():
    bs = store.buyers(db)
    rows = [[("👥 Все сразу", "b:all")]] if len(bs) > 1 else []
    for i in range(0, len(bs), 2):
        rows.append([(f"🙍 {short(name, 18)}", f"b:{bid}") for bid, name in bs[i:i + 2]])
    return kb(rows + [[("➕ Добавить ID", "add"), ("⬅️ Меню", "menu")]])


def periods_kb(who):
    rows = [[("Сегодня", f"p:{who}:today"), ("7 дней", f"p:{who}:7"), (f"Месяц · {report.month_label()}",
                                                                       f"p:{who}:month")]]
    if who != "all":
        rows.append([("✏️ Изменить имя", f"ren:{who}"), ("🗑 Убрать из списка", f"del:{who}")])
    return kb(rows + [[("⬅️ Покупатели", "b"), ("🏠 Меню", "menu")]])


def views_kb(who, kind):
    return kb([[("🔢 В штуках", f"v:{who}:{kind}:q"), ("📋 Подробнее", f"v:{who}:{kind}:d")],
               [("⬅️ Период", f"b:{who}"), ("👥 Покупатели", "b"), ("🏠 Меню", "menu")]])


# --- отчёты ---

def build(who, kind, view, prefix):
    """(картинки, подпись)."""
    ids = ids_of(who)
    start, end, label = report.period(kind)
    lines = store.buyer_lines(db, ids, start, end) if ids else []
    if view == "d":
        report.fill_customer_prices(db, oz, lines)
    act = report.active(lines)
    qty = sum(x["qty"] for x in act)
    orders = len({x["posting"].rsplit("-", 1)[0] for x in act})
    total = sum(x["price"] * x["qty"] for x in act)
    canc = report.cancelled_qty(lines)
    note_c = f"Отменено: {canc} шт. — в итог не вошло." if canc else None
    name = title_of(who)
    caption = f"{name} · {label}: {qty} шт., заказов {orders}, в заказах {money(total)}" + \
              (f"\nОтменено {canc} шт." if canc else "")

    if view == "q" and who == "all":
        q = report.by_buyer(lines, ids)
        rows = [[short(store.buyer_name(db, b), 30), str(b), n] for b, n in sorted(q.items(), key=lambda t: -t[1])]
        paths = render.table_png(f"{name} · в штуках", label, [(0.04, "Имя", "left"), (0.55, "ID", "left"),
                                                                (0.96, "шт.", "right")],
                                 rows, prefix, total=["Итого", "", qty], note=note_c)
    elif view == "q":
        days = report.by_day(lines, start, end)
        rows = [[f"{d:%d.%m}, {'пн вт ср чт пт сб вс'.split()[d.weekday()]}", n] for d, n in days if n]
        idle = sum(1 for _, n in days if not n)
        note = "\n".join(x for x in (f"Показаны дни с покупками. Дней без покупок: {idle}." if idle and rows else "",
                                      note_c) if x) or None
        paths = render.table_png(f"{name} · в штуках", label, [(0.04, "Дата", "left"), (0.96, "шт.", "right")],
                                 rows, prefix, total=["Итого", qty] if rows else None, note=note)
    else:
        det = report.details(lines)
        paid = None if any(r["paid"] is None for r in det) else sum(r["paid"] for r in det)
        if who == "all":
            cols = [(0.04, "Имя", "left"), (0.27, "Артикул", "left"), (0.64, "шт.", "right"),
                    (0.81, "В заказе", "right"), (0.97, "Заплатил", "right")]
            rows = [[short(store.buyer_name(db, r["buyer"]), 12), short(r["offer_id"], 20), r["qty"],
                     money(r["sum"]), money(r["paid"])] for r in det]
            tot = ["Итого", "", qty, money(total), money(paid)]
        else:
            cols = [(0.04, "Артикул", "left"), (0.33, "Товар", "left"), (0.64, "шт.", "right"),
                    (0.81, "В заказе", "right"), (0.97, "Заплатил", "right")]
            rows = [[short(r["offer_id"], 16), short(r["name"], 17), r["qty"], money(r["sum"]), money(r["paid"])]
                    for r in det]
            tot = ["Итого", "", qty, money(total), money(paid)]
        note = NOTE_PRICE + (f"\n{note_c}" if note_c else "")
        paths = render.table_png(f"{name} · подробнее", label, cols, rows, prefix, total=tot, note=note)
        if paid is not None:
            caption += f", заплатили {money(paid)}"
    return paths, caption


async def send_report(msg: Message, who, kind, view):
    tmp = tempfile.mkdtemp()
    try:
        paths, caption = await asyncio.to_thread(locked, build, who, kind, view, os.path.join(tmp, "r"))
        markup = views_kb(who, kind)
        if len(paths) == 1:
            await msg.answer_photo(FSInputFile(paths[0]), caption=caption, reply_markup=markup)
        else:
            await msg.bot.send_media_group(msg.chat.id, [InputMediaPhoto(media=FSInputFile(p)) for p in paths[:10]])
            await msg.answer(caption, reply_markup=markup)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- обработчики ---

@dp.message(CommandStart())
async def start(msg: Message):
    u = msg.from_user
    if not allowed(u.id):
        if u.id not in access_requests:
            access_requests[u.id] = who_is(u)
            for a in ADMINS:
                await msg.bot.send_message(a, f"Просит доступ к боту «Покупатели»: {who_is(u)}, ID {u.id}",
                                           reply_markup=kb([[("✅ Открыть", f"u:ok:{u.id}"),
                                                             ("❌ Отказать", f"u:no:{u.id}")]]))
        return await msg.answer("Запрос отправлен владельцу. Как только откроет доступ, нажмите /start ещё раз.")
    await msg.answer(f"Слежу за покупателями Ozon: {len(store.buyers(db))} ID в списке. Что смотрим?",
                     reply_markup=main_menu(u.id))


@dp.callback_query()
async def button(cb: CallbackQuery):
    uid, d, msg = cb.from_user.id, cb.data or "", cb.message
    await cb.answer()
    if not allowed(uid):
        return
    try:
        if d == "menu":
            await msg.answer("Меню:", reply_markup=main_menu(uid))
        elif d == "b":
            if not store.buyers(db):
                return await msg.answer("Список пуст. Добавьте первого покупателя.", reply_markup=main_menu(uid))
            await msg.answer("За кем смотрим?", reply_markup=buyers_kb())
        elif d.startswith("b:"):
            who = d[2:]
            head = title_of(who) + ("" if who == "all" else f" · ID {who}")
            await msg.answer(f"{head}\nВыберите период:", reply_markup=periods_kb(who))
        elif d.startswith("p:"):
            _, who, kind = d.split(":")
            await msg.answer(f"{title_of(who)} · {report.period(kind)[2]}. Как показать?",
                             reply_markup=views_kb(who, kind))
        elif d.startswith("v:"):
            _, who, kind, view = d.split(":")
            if state["loading"]:
                return await msg.answer("Бот загружает историю заказов Ozon за 40 дней (первый запуск, ~20 минут). "
                                        "Нажмите кнопку ещё раз чуть позже.", reply_markup=views_kb(who, kind))
            if not fresh():
                await msg.answer("Докачиваю свежие заказы Ozon…")
                await asyncio.to_thread(ensure_fresh)
            await send_report(msg, who, kind, view)
        elif d == "add":
            waiting[uid] = ("add",)
            await msg.answer("Пришлите ID покупателя и имя одной строкой, например:\n47327265 Анна 1\n\n"
                             "Можно вставить номер заказа целиком: 47327265-0623-1 Анна 1 — ID возьму из начала.")
        elif d.startswith("ren:"):
            waiting[uid] = ("ren", int(d[4:]))
            await msg.answer(f"Новое имя для ID {d[4:]}:")
        elif d.startswith("del:"):
            bid = d[4:]
            await msg.answer(f"Убрать {title_of(bid)} (ID {bid}) из списка? История заказов останется.",
                             reply_markup=kb([[("🗑 Да, убрать", f"delok:{bid}"), ("Отмена", f"b:{bid}")]]))
        elif d.startswith("delok:"):
            store.del_buyer(db, int(d[6:]))
            await msg.answer("Убрала из списка.", reply_markup=buyers_kb() if store.buyers(db) else main_menu(uid))
        elif d.startswith("u") and is_admin(uid):
            await users_button(msg, d)
    except Exception:
        log.exception("кнопка %s", d)
        await msg.answer("Не получилось собрать отчёт, попробуйте ещё раз через пару минут.", reply_markup=main_menu(uid))


async def users_button(msg, d):
    if d == "u":
        rows = [[(f"❌ {name}", f"u:del:{i}")] for i, name in users().items()]
        await msg.answer("Пользователи бота. Нажмите на имя, чтобы закрыть доступ.\n"
                         "Проще всего добавить так: человек нажимает /start в боте, вам приходит кнопка «Открыть».",
                         reply_markup=kb(rows + [[("➕ Добавить по Telegram ID", "u:add")], [("⬅️ Меню", "menu")]]))
    elif d == "u:add":
        waiting[msg.chat.id] = ("user",)
        await msg.answer("Пришлите Telegram ID нового пользователя (цифрами).")
    elif d.startswith("u:ok:"):
        i = int(d[5:])
        db.execute("insert or replace into users values (?, ?, ?)",
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


def parse_buyer(text):
    """«47327265 Анна 1» или «47327265-0623-1 Анна 1» → (47327265, «Анна 1»)."""
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return None, ""
    bid = buyer_of(parts[0])
    return bid, (parts[1].strip() if len(parts) > 1 else "")


@dp.message(F.text)
async def text(msg: Message):
    uid = msg.from_user.id
    if not allowed(uid):
        return
    mode = waiting.pop(uid, None)
    if mode and mode[0] == "add":
        bid, name = parse_buyer(msg.text)
        if not bid:
            waiting[uid] = mode
            return await msg.answer("Не вижу ID. Пришлите так: 47327265 Анна 1")
        store.add_buyer(db, bid, name or str(bid))
        start, end, _ = report.period("month")
        n = len(store.buyer_lines(db, [bid], start - timedelta(days=40), end))
        return await msg.answer(f"Добавила: {name or bid} (ID {bid}). В базе за 40 дней строк заказов: {n}.",
                                reply_markup=periods_kb(str(bid)))
    if mode and mode[0] == "ren":
        store.add_buyer(db, mode[1], msg.text.strip())
        return await msg.answer("Имя изменила.", reply_markup=periods_kb(str(mode[1])))
    if mode and mode[0] == "user" and is_admin(uid) and msg.text.strip().isdigit():
        i = int(msg.text.strip())
        db.execute("insert or replace into users values (?, ?, ?)", (i, str(i), datetime.now(MSK).strftime("%d.%m.%Y")))
        db.commit()
        return await msg.answer("Добавлен. Пусть нажмёт /start в боте.", reply_markup=main_menu(uid))
    bid, _ = parse_buyer(msg.text)
    if bid and bid in dict(store.buyers(db)):
        return await msg.answer(f"{title_of(str(bid))} · ID {bid}\nВыберите период:", reply_markup=periods_kb(str(bid)))
    await msg.answer("Меню:", reply_markup=main_menu(uid))


# --- фон ---

def background():
    """Первый запуск — история за 40 дней; дальше каждые 10 минут докачка, в 4:00 — перепроверка отмен."""
    import time
    while True:
        try:
            if store.get_meta(db, "last_sync") is None:
                state["loading"] = True
            with _lock:
                if not fresh():
                    report.sync(db, oz)
            state["loading"] = False
            now = datetime.now(MSK)
            if now.hour >= RECHECK_HOUR and store.get_meta(db, "last_recheck") != now.date().isoformat():
                with _lock:
                    report.recheck(db, oz)
        except Exception:
            log.exception("фоновая докачка")
        time.sleep(SYNC_EVERY.total_seconds())


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    threading.Thread(target=background, daemon=True).start()
    session = AiohttpSession()
    if os.environ.get("SSL_CERT_FILE"):
        session._connector_init["ssl"] = ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])
    bot = Bot(os.environ["TELEGRAM_BOT_TOKEN"], session=session)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
