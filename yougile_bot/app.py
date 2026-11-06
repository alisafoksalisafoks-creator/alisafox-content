"""Telegram-бот YouGile для AlisaFox: отчёт «кто чем занимался за день» по сотрудникам и постановка задач.

Запуск: python -m yougile_bot.app
Переменные окружения — в env.example.
"""
import asyncio
import calendar
import html
import json
import logging
import os
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup)
from aiohttp import web

from yougile_bot import env  # noqa: F401  (подхватывает .env до чтения ключей)
from yougile_bot import ai, report, store, voice
from yougile_bot.api import YouGile, YouGileError

log = logging.getLogger("yougile_bot")
OWNER = int(os.environ.get("YG_BOT_OWNER", "0"))
USERS = {int(x) for x in os.environ.get("YG_BOT_USERS", "").replace(" ", "").split(",") if x}
TZ = ZoneInfo(os.environ.get("YG_TZ", "Asia/Yekaterinburg"))
EXCLUDE = {w.strip().lower() for w in os.environ.get(
    "YG_EXCLUDE", "Алина,Вера,Яна,Лана,Дана,Андрей,Менеджер,Дарина,Константин").split(",") if w.strip()}
EMPLOYEES_FILE = Path(os.environ.get("YG_EMPLOYEES", Path(__file__).with_name("employees.json")))
WEBHOOK_URL = os.environ.get("YG_WEBHOOK_URL", "")  # https://адрес/yougile/<секрет>
WEBHOOK_PORT = int(os.environ.get("YG_WEBHOOK_PORT", "8090"))
BACKFILL = int(os.environ.get("YG_BACKFILL", "8"))  # сколько логов задач читать за клик по старой дате

report.set_tz(TZ)
yg = YouGile(os.environ.get("YOUGILE_API_KEY", ""))
dp = Dispatcher()
drafts: dict[int, dict] = {}  # черновик новой задачи по чату
pending_logs: dict[str, asyncio.Task] = {}  # отложенное чтение лога задачи после вебхука

DATE_BTN = "📅 Выберите дату"
PERSONAL_BTN = "👤 Личный"
INPUT_BTN = "✍️ Ввод"
COMPANY_BTN = "🏢 Итоги дня компании"
ACCESS_BTN = "👥 Добавить пользователя"
OVERDUE_BTN = "⏰ Просрочки"
WEEK_BTN = "📈 Неделя"
MORNING = os.environ.get("YG_MORNING", "09:00")  # автоотчёт: итоги вчера и просрочки
IDLE_CHECK = os.environ.get("YG_IDLE_CHECK", "11:00")  # сигнал «нет активности»
WORKDAYS = {int(x) for x in os.environ.get("YG_WORKDAYS", "0,1,2,3,4").split(",") if x.strip()}  # 0 — пн
access_requests: dict[int, str] = {}  # кто просил доступ: id → имя
waiting_id: set[int] = set()  # владелец вводит ID нового пользователя


def allowed(uid):
    return uid == OWNER or uid in USERS or uid in store.bot_users()


def full(uid):
    """Полный доступ: ставить задачи и писать в YouGile. Остальным — только отчёты."""
    return uid == OWNER or uid in USERS or store.bot_users().get(uid, ("", ""))[1] == "full"


def menu(uid=0):
    rows = [[KeyboardButton(text=DATE_BTN), KeyboardButton(text=PERSONAL_BTN)],
            [KeyboardButton(text=INPUT_BTN), KeyboardButton(text=COMPANY_BTN)],
            [KeyboardButton(text=OVERDUE_BTN), KeyboardButton(text=WEEK_BTN)]]
    if uid == OWNER:
        rows.append([KeyboardButton(text=ACCESS_BTN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def who(user):
    return user.full_name + (f" (@{user.username})" if user.username else "")


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in r]
                                                 for r in rows if r])


def collected_from():
    """С какого момента журнал полный (идут вебхуки). Без вебхуков — никогда: всё собирается по задачам."""
    v = store.meta("collect_start")
    return int(v) if WEBHOOK_URL and v else 10 ** 15


def today():
    return datetime.now(TZ).date()


# --- справочники ---

def mapping():
    try:
        return json.loads(EMPLOYEES_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


async def employees():
    """Сотрудники для кнопок: все пользователи компании, кроме исключённых по имени. [(id, имя)]."""
    m = mapping()
    out = []
    try:
        me = (await yg.me())["id"]
    except YouGileError:
        me = None
    for u in await yg.users():
        if u.get("deleted") or u["id"] == me:
            continue
        name = m.get(u["id"], {}).get("name") or report.short_name(u.get("realName") or u.get("email"))
        if report.first_word(name) in EXCLUDE:
            continue
        out.append((u["id"], name))
    return sorted(out, key=lambda x: x[1])


async def name_of(user_id):
    m = mapping().get(user_id, {})
    if m.get("name"):
        return m["name"]
    for u in await yg.users():
        if u["id"] == user_id:
            return report.short_name(u.get("realName") or u.get("email"))
    return "Сотрудник"


async def lookups():
    boards = {b["id"]: b for b in await yg.boards()}
    columns = {c["id"]: c for c in await yg.columns()}
    tasks = await yg.tasks()
    by_id = {t["id"]: t for t in tasks}

    def column_title(cid):
        return (columns.get(cid) or {}).get("title") or "колонка"

    def where(t):
        c = columns.get(t.get("columnId") or "")
        if not c:
            return "без доски"
        return f"{(boards.get(c.get('boardId')) or {}).get('title', 'доска')} › {c.get('title')}"

    return tasks, by_id, columns, where, column_title


# --- старт ---

@dp.message(CommandStart())
@dp.message(Command("menu"))
async def start(msg: Message):
    uid = msg.from_user.id
    if not allowed(uid):
        if not OWNER:
            return await msg.answer(f"Бот ещё не настроен. Ваш Telegram ID: {uid}")
        access_requests[uid] = who(msg.from_user)
        await msg.bot.send_message(OWNER, f"Просит доступ к боту YouGile: {html.escape(access_requests[uid])}, ID {uid}",
                                   reply_markup=kb([[("👁 Только отчёты", f"acc:view:{uid}"),
                                                     ("✅ Полный", f"acc:full:{uid}")],
                                                    [("❌ Отказать", f"acc:no:{uid}")]]))
        return await msg.answer("Запрос отправлен владельцу. Как только он откроет доступ, я напишу.")
    await msg.answer("YouGile AlisaFox. Отчёт по сотрудникам — «Выберите дату». Свои задачи — «Личный». "
                     "Любое действие словами («поставь Веронике задачу до пятницы…») — «Ввод».", reply_markup=menu(uid))
    await msg.answer("Что открыть?", reply_markup=kb([[(DATE_BTN, "pick"), (PERSONAL_BTN, "me")],
                                                       [(INPUT_BTN, "in"), (COMPANY_BTN, f"co:{today()}")],
                                                       [(ACCESS_BTN, "acc:list")] if uid == OWNER else []]))


@dp.message(F.text == DATE_BTN)
async def date_btn(msg: Message):
    if allowed(msg.from_user.id):
        await msg.answer("За какой день показать?", reply_markup=calendar_kb("d", today()))


@dp.message(F.text == PERSONAL_BTN)
async def personal_btn(msg: Message):
    if full(msg.from_user.id):
        await personal(msg)
    elif allowed(msg.from_user.id):
        await msg.answer("«Личный» — раздел владельца. Нужен полный доступ.")


# --- доступ (только владелец) ---

ROLE = {"view": "👁 отчёты", "full": "✅ полный"}


@dp.message(F.text == ACCESS_BTN)
async def access_btn(msg: Message):
    if msg.from_user.id == OWNER:
        await access_list(msg)


async def access_list(msg: Message):
    u = store.bot_users()
    text = ("<b>Доступ к боту</b>\nУ вас полный доступ всегда.\n\n"
            "👁 отчёты — смотрит даты, План|Факт, итоги.\n✅ полный — ещё ставит задачи и пишет в YouGile "
            "(от имени владельца ключа).\n\n"
            + ("\n".join(f"• {html.escape(n)} — {ROLE.get(r, r)}, ID {i}" for i, (n, r) in u.items())
               or "Других пользователей пока нет."))
    rows = [[(f"{'👁→✅' if r == 'view' else '✅→👁'} {n[:20]}", f"acc:{'full' if r == 'view' else 'view'}:{i}"),
             ("🗑 Убрать", f"acc:del:{i}")] for i, (n, r) in u.items()]
    rows.append([("➕ Добавить по ID", "acc:add")])
    await msg.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb(rows))


@dp.callback_query(F.data.startswith("acc:"))
async def on_access(cb: CallbackQuery):
    if cb.from_user.id != OWNER:
        return await cb.answer("Только для владельца")
    await cb.answer()
    parts = cb.data.split(":")
    if parts[1] == "list":
        return await access_list(cb.message)
    if parts[1] == "add":
        waiting_id.add(cb.message.chat.id)
        return await cb.message.answer("Пришлите Telegram ID и, через пробел, имя. Например: 123456789 Анна.\n"
                                       "Проще: пусть человек напишет боту /start — вам придёт кнопка «Пустить».")
    uid = int(parts[2])
    if parts[1] in ("view", "full"):
        known = store.bot_users().get(uid)
        name = known[0] if known else access_requests.pop(uid, f"ID {uid}")
        store.set_bot_user(uid, name, parts[1])
        if known:
            return await access_list(cb.message)
        await cb.message.edit_reply_markup(reply_markup=None)
        await cb.message.answer(f"Доступ открыт: {html.escape(name)} — {ROLE[parts[1]]}.")
        try:
            await cb.bot.send_message(uid, "Доступ к боту YouGile открыт. Нажмите /start.")
        except Exception:
            pass
    elif parts[1] == "no":
        access_requests.pop(uid, None)
        await cb.message.edit_reply_markup(reply_markup=None)
        await cb.message.answer("Отказано.")
    elif parts[1] == "del":
        store.remove_bot_user(uid)
        drafts.pop(uid, None)
        await access_list(cb.message)


@dp.message(F.text, lambda m: m.chat.id in waiting_id)
async def on_access_id(msg: Message):
    if msg.from_user.id != OWNER:
        return
    waiting_id.discard(msg.chat.id)
    head, _, name = (msg.text or "").strip().partition(" ")
    if not head.isdigit():
        return await msg.answer("Нужен ID из цифр, например 123456789 Анна. Нажмите «Добавить по ID» ещё раз.")
    store.set_bot_user(int(head), name.strip() or f"ID {head}", "view")
    await msg.answer(f"Добавил: {html.escape(name.strip() or head)} — 👁 только отчёты. Роль меняется в списке.")
    await access_list(msg)


# --- календарь ---

def calendar_kb(prefix, month_day: date, back=None):
    """Месяц кнопками. prefix «d» — отчёт, «dd» — срок задачи."""
    t = today()
    rows = []
    if prefix == "d":
        rows.append([("Сегодня", f"d:{t}"), ("Вчера", f"d:{t - timedelta(days=1)}")])
    else:
        rows.append([("Сегодня", f"dd:{t}"), ("Завтра", f"dd:{t + timedelta(days=1)}")])
    y, m = month_day.year, month_day.month
    prev_m = (date(y, m, 1) - timedelta(days=1)).replace(day=1)
    next_m = (date(y, m, 28) + timedelta(days=4)).replace(day=1)
    rows.append([("◀️", f"cal:{prefix}:{prev_m:%Y-%m}"), (f"{report.MONTHS[m - 1]} {y}", "noop"),
                 ("▶️", f"cal:{prefix}:{next_m:%Y-%m}")])
    rows.append([(w, "noop") for w in report.WEEKDAYS])
    for week in calendar.Calendar().monthdayscalendar(y, m):
        rows.append([(str(dd) if dd else " ", f"{prefix}:{date(y, m, dd)}" if dd else "noop") for dd in week])
    if back:
        rows.append([("◀️ Назад", back)])
    return kb(rows)


@dp.callback_query(F.data.startswith("cal:"))
async def on_cal(cb: CallbackQuery):
    _, prefix, ym = cb.data.split(":")
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=calendar_kb(prefix, date.fromisoformat(ym + "-01")))


@dp.callback_query(F.data == "noop")
async def on_noop(cb: CallbackQuery):
    await cb.answer()


@dp.callback_query(F.data == "pick")
async def on_pick(cb: CallbackQuery):
    if not allowed(cb.from_user.id):
        return await cb.answer("Нет доступа")
    await cb.answer()
    await cb.message.answer("За какой день показать?", reply_markup=calendar_kb("d", today()))


# --- отчёт за день ---

@dp.callback_query(F.data.regexp(r"^d:\d{4}-\d{2}-\d{2}$"))
async def on_day(cb: CallbackQuery):
    if not allowed(cb.from_user.id):
        return await cb.answer("Нет доступа")
    await cb.answer("Собираю…")
    d = date.fromisoformat(cb.data[2:])
    try:
        text, markup = await day_screen(d)
    except YouGileError as e:
        log.exception("YouGile")
        return await cb.message.answer(f"YouGile не ответил: {html.escape(str(e)[:200])}")
    await cb.message.answer(text, reply_markup=markup, parse_mode=ParseMode.HTML)


async def day_screen(d: date):
    start, end = report.day_window(d, TZ)
    tasks, *_ = await lookups()
    events = store.between(start, end)
    rows, line = [], []
    for uid, name in await employees():
        n = report.count(report.collect(uid, events, tasks, start, end))
        line.append((f"{name} · {n}" if n else name, f"e:{d}:{uid}"))
        if len(line) == 2:
            rows.append(line)
            line = []
    rows.append(line)
    rows.append([(COMPANY_BTN, f"co:{d}")])
    rows.append([("◀️ Другая дата", "pick")])
    note = "Цифра — сколько действий нашёл бот." + (
        " День раньше запуска бота: по клику доберу историю задач, это займёт до минуты."
        if start < collected_from() else "")
    return f"<b>{report.day_title(d)}</b>\nВыберите сотрудника. {note}", kb(rows)


@dp.callback_query(F.data.startswith("e:"))
async def on_employee(cb: CallbackQuery):
    """Клик по сотруднику: карточка с кнопкой «План|Факт» за выбранный день."""
    if not allowed(cb.from_user.id):
        return await cb.answer("Нет доступа")
    _, day, uid = cb.data.split(":", 2)
    d = date.fromisoformat(day)
    await cb.answer()
    await cb.message.answer(f"<b>{html.escape(await name_of(uid))}</b> · {report.day_title(d)}",
                            parse_mode=ParseMode.HTML, reply_markup=kb([
                                [("📊 План|Факт", f"pf:{d}:{uid}"), (INPUT_BTN, f"in:{uid}")],
                                [("◀️ К сотрудникам", f"d:{d}"), ("➕ Задача", f"nu:{uid}")]]))


@dp.callback_query(F.data.startswith("pf:"))
async def on_plan_fact(cb: CallbackQuery):
    """Полная сводка за день: план, действия на досках, чаты задач, групповые чаты."""
    if not allowed(cb.from_user.id):
        return await cb.answer("Нет доступа")
    _, day, uid = cb.data.split(":", 2)
    d = date.fromisoformat(day)
    await cb.answer("Собираю…")
    wait = await cb.message.answer("⏳ Собираю сводку…")
    try:
        text = await employee_report(uid, d)
    except YouGileError as e:
        log.exception("YouGile")
        text = f"YouGile не ответил: {html.escape(str(e)[:200])}"
    finally:
        await wait.delete()
    parts = report.chunks(text)
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        await cb.message.answer(part, parse_mode=ParseMode.HTML, reply_markup=kb([
            [("◀️ К сотрудникам", f"d:{d}"), ("➕ Задача", f"nu:{uid}")]]) if last else None)


async def employee_report(uid, d: date):
    start, end = report.day_window(d, TZ)
    tasks, by_id, columns, where, column_title = await lookups()
    partial = start < collected_from()
    if partial:
        await backfill(uid, tasks, columns, start, end)
    groups = report.collect(uid, store.between(start, end), tasks, start, end)
    titles = {c["id"]: c.get("title") or "групповой" for c in await yg.group_chats()}
    return report.render(await name_of(uid), d, groups, by_id, where, column_title,
                         lambda c: titles.get(c, "групповой"), partial, report.plan(uid, tasks, start, end))


async def backfill(uid, tasks, columns, start, end):
    """Для дней до запуска бота: читает логи и сообщения задач сотрудника (не больше BACKFILL штук)."""
    my_board = mapping().get(uid, {}).get("board")
    cand = [t for t in tasks
            if not t.get("deleted")
            and (uid in (t.get("assigned") or []) or (my_board and (columns.get(t.get("columnId") or "") or {})
                                                     .get("boardId") == my_board))
            and (t.get("timestamp") or 0) < end
            and (t.get("timestamp") or 0) > start - 60 * 86400_000
            and not (t.get("completed") and (t.get("completedTimestamp") or 0) < start)]
    cand.sort(key=lambda t: t.get("timestamp") or 0, reverse=True)
    done_key = f"bf:{uid}:{start}"
    if store.meta(done_key):
        return
    for t in cand[:BACKFILL]:
        store.add_log(t["id"], await yg.task_log(t["id"]))
        for m in await yg.messages(t["id"], start, uid):
            if m.get("id") and m["id"] < end and not m.get("deleted"):
                store.add(f"msg:{t['id']}:{m['id']}", m["id"], m.get("fromUserId"), "message",
                          task_id=t["id"], chat_id=t["id"], text=m.get("text"))
    for chat in (await yg.group_chats())[:BACKFILL]:
        for m in await yg.messages(chat["id"], start, uid):
            if m.get("id") and m["id"] < end and not m.get("deleted"):
                store.add(f"msg:{chat['id']}:{m['id']}", m["id"], m.get("fromUserId"), "message",
                          chat_id=chat["id"], text=m.get("text"))
    if end < datetime.now(TZ).timestamp() * 1000:  # прошедший день больше не меняется — второй раз не читаем
        store.set_meta(done_key, 1)


# --- итоги дня компании ---

@dp.message(F.text == COMPANY_BTN)
async def company_btn(msg: Message):
    if allowed(msg.from_user.id):
        await send_company(msg, today())


@dp.callback_query(F.data.startswith("co:"))
async def on_company(cb: CallbackQuery):
    if not allowed(cb.from_user.id):
        return await cb.answer("Нет доступа")
    await cb.answer("Собираю…")
    await send_company(cb.message, date.fromisoformat(cb.data[3:]))


async def send_company(msg: Message, d: date):
    """Все пользователи компании, без исключений: строка на человека."""
    try:
        text = await company_text(d)
    except YouGileError as e:
        log.exception("YouGile")
        return await msg.answer(f"YouGile не ответил: {html.escape(str(e)[:200])}")
    for part in report.chunks(text):
        await msg.answer(part, parse_mode=ParseMode.HTML,
                         reply_markup=kb([[("👥 По сотрудникам", f"d:{d}"), ("◀️ Другая дата", "pick")]]))


# --- просрочки и неделя ---

@dp.message(F.text == OVERDUE_BTN)
async def overdue_btn(msg: Message):
    if allowed(msg.from_user.id):
        for part in report.chunks(await overdue_text()):
            await msg.answer(part, parse_mode=ParseMode.HTML)


async def overdue_text():
    tasks = await yg.tasks()
    now = int(datetime.now(TZ).timestamp() * 1000)
    data = report.overdue(tasks, now)
    if not data:
        return "⏰ <b>Просрочек нет.</b>"
    blocks = []
    for uid, items in sorted(data.items(), key=lambda x: -len(x[1])):
        who_ = "Без исполнителя" if uid == "—" else await name_of(uid)
        lines = [f"• {html.escape(report.clean(t.get('title'), 70))} — {d} дн." for t, d in items[:10]]
        more = f"\n  …и ещё {len(items) - 10}" if len(items) > 10 else ""
        blocks.append(f"<b>{html.escape(who_)}</b>: {len(items)}\n" + "\n".join(lines) + more)
    total = sum(len(v) for v in data.values())
    return f"⏰ <b>Просрочено: {total}</b>\n\n" + "\n\n".join(blocks)


@dp.message(F.text == WEEK_BTN)
async def week_btn(msg: Message):
    if allowed(msg.from_user.id):
        t = today()
        await msg.answer("Какую неделю?", reply_markup=kb([[
            ("Эта неделя", f"wk:{t - timedelta(days=t.weekday())}"),
            ("Прошлая", f"wk:{t - timedelta(days=t.weekday() + 7)}")]]))


@dp.callback_query(F.data.startswith("wk:"))
async def on_week(cb: CallbackQuery):
    if not allowed(cb.from_user.id):
        return await cb.answer("Нет доступа")
    await cb.answer("Собираю…")
    for part in report.chunks(await week_text(date.fromisoformat(cb.data[3:]))):
        await cb.message.answer(part, parse_mode=ParseMode.HTML)


async def week_text(monday: date):
    start = report.day_window(monday, TZ)[0]
    end = report.day_window(monday + timedelta(days=7), TZ)[0]
    now = int(datetime.now(TZ).timestamp() * 1000)
    tasks = await yg.tasks()
    events = store.between(start, end)
    rows = []
    for u in await yg.users():
        if u.get("deleted"):
            continue
        r = report.week_row(u["id"], events, tasks, start, min(end, now), now)
        if r["total"] or r["late"]:
            name = mapping().get(u["id"], {}).get("name") or report.short_name(u.get("realName") or u.get("email"))
            rows.append((r["total"], f"<b>{html.escape(name)}</b>: ✅{r['done']} ➕{r['add']} 💬{r['msg']} "
                                     f"· всего {r['total']}" + (f" · ⏰{r['late']}" if r["late"] else "")))
    head = f"📈 <b>Неделя {monday:%d.%m}–{monday + timedelta(days=6):%d.%m}</b>"
    note = ("\n<i>Часть недели раньше запуска бота: учтены только созданные и выполненные задачи.</i>"
            if start < collected_from() else "")
    body = "\n".join(line for _, line in sorted(rows, reverse=True)) or "Действий не найдено."
    return (f"{head}\n{body}\n\n<i>✅ выполнено ➕ создано 💬 сообщений · всего действий · ⏰ просрочено сейчас</i>"
            + note)


# --- расписание: утренний отчёт, сигнал «нет активности», недельная сводка ---

async def recipients():
    return [OWNER] + [i for i, (_, r) in store.bot_users().items() if r == "full"]


async def company_text(d: date):
    start, end = report.day_window(d, TZ)
    tasks, by_id, *_ = await lookups()
    events = store.between(start, end)
    rows, idle = [], []
    for u in await yg.users():
        if u.get("deleted"):
            continue
        name = mapping().get(u["id"], {}).get("name") or report.short_name(u.get("realName") or u.get("email"))
        groups = report.collect(u["id"], events, tasks, start, end)
        n = report.count(groups)
        if n:
            rows.append((n, report.company_line(name, groups, by_id)))
        else:
            idle.append(name)
    return report.company(d, [line for _, line in sorted(rows, reverse=True)], sorted(idle),
                          start < collected_from())


async def broadcast(bot: Bot, text):
    for chat in await recipients():
        for part in report.chunks(text):
            try:
                await bot.send_message(chat, part, parse_mode=ParseMode.HTML)
            except Exception:
                log.exception("рассылка %s", chat)


async def scheduler(bot: Bot):
    """Раз в минуту смотрит на часы. Что отправлено сегодня — помечает в базе, чтобы не повторять."""
    while True:
        await asyncio.sleep(60 - datetime.now(TZ).second)
        now = datetime.now(TZ)
        hm = f"{now:%H:%M}"
        d = now.date()
        try:
            if hm == MORNING and d.weekday() in WORKDAYS and not store.meta(f"sent:morning:{d}"):
                store.set_meta(f"sent:morning:{d}", 1)
                prev = d - timedelta(days=1)
                while prev.weekday() not in WORKDAYS and prev > d - timedelta(days=7):
                    prev -= timedelta(days=1)  # в понедельник — итоги пятницы
                await broadcast(bot, "☀️ Доброе утро! " + await company_text(prev))
                await broadcast(bot, await overdue_text())
                if d.weekday() == 0:
                    await broadcast(bot, await week_text(d - timedelta(days=7)))
            if hm == IDLE_CHECK and d.weekday() in WORKDAYS and not store.meta(f"sent:idle:{d}"):
                store.set_meta(f"sent:idle:{d}", 1)
                start, end = report.day_window(d, TZ)
                events, tasks = store.between(start, end), await yg.tasks()
                quiet = [name for uid, name in await employees()
                         if not report.count(report.collect(uid, events, tasks, start, end))]
                if quiet and WEBHOOK_URL:
                    await broadcast(bot, f"🔕 До {IDLE_CHECK} нет действий в YouGile: " + html.escape(", ".join(quiet)))
        except Exception:
            log.exception("расписание")


# --- личный раздел ---

async def personal(msg: Message):
    await msg.answer("Личный раздел.", reply_markup=kb([
        [("➕ Поставить задачу", "nt")],
        [("📌 Поставлены мной, открытые", "mine"), ("🙋 Мои задачи", "my")],
        [("📝 Мои действия сегодня", "myday")],
    ]))


@dp.callback_query(F.data == "me")
async def on_me(cb: CallbackQuery):
    if not full(cb.from_user.id):
        return await cb.answer("Нужен полный доступ", show_alert=True)
    await cb.answer()
    await personal(cb.message)


def deadline_text(t):
    dl = (t.get("deadline") or {}).get("deadline")
    if not dl:
        return ""
    d = datetime.fromtimestamp(dl / 1000, TZ).date()
    mark = " ❗" if d < today() else ""
    return f" · до {d:%d.%m}{mark}"


@dp.callback_query(F.data.in_({"mine", "my"}))
async def on_lists(cb: CallbackQuery):
    if not full(cb.from_user.id):
        return await cb.answer("Нужен полный доступ", show_alert=True)
    await cb.answer("Собираю…")
    me = (await yg.me())["id"]
    tasks, by_id, columns, where, _ = await lookups()
    open_tasks = [t for t in tasks if not t.get("deleted") and not t.get("archived") and not t.get("completed")]
    if cb.data == "mine":
        picked = [t for t in open_tasks if t.get("createdBy") == me and me not in (t.get("assigned") or [])]
        head = "📌 <b>Поставлены мной и ещё не выполнены</b>"
        groups = {}
        for t in picked:
            for uid in t.get("assigned") or ["—"]:
                groups.setdefault(uid, []).append(t)
        blocks = []
        for uid, items in groups.items():
            who = "Без исполнителя" if uid == "—" else await name_of(uid)
            lines = [f"• {html.escape(report.clean(t.get('title'), 80))}{deadline_text(t)}" for t in items]
            blocks.append(f"<b>{html.escape(who)}</b> ({len(items)})\n" + "\n".join(lines))
        text = head + "\n\n" + ("\n\n".join(blocks) if blocks else "Открытых задач нет.")
    else:
        picked = [t for t in open_tasks if me in (t.get("assigned") or [])]
        lines = [f"• {html.escape(report.clean(t.get('title'), 80))}{deadline_text(t)}\n  <i>{html.escape(where(t))}</i>"
                 for t in picked]
        text = "🙋 <b>Мои открытые задачи</b>\n\n" + ("\n".join(lines) if lines else "Задач нет.")
    for part in report.chunks(text):
        await cb.message.answer(part, parse_mode=ParseMode.HTML)


@dp.callback_query(F.data == "myday")
async def on_myday(cb: CallbackQuery):
    if not full(cb.from_user.id):
        return await cb.answer("Нужен полный доступ", show_alert=True)
    await cb.answer("Собираю…")
    me = (await yg.me())["id"]
    text = await employee_report(me, today())
    for part in report.chunks(text):
        await cb.message.answer(part, parse_mode=ParseMode.HTML)


# --- новая задача ---

@dp.callback_query(F.data == "nt")
async def on_new_task(cb: CallbackQuery):
    if not full(cb.from_user.id):
        return await cb.answer("Нужен полный доступ: попросите владельца", show_alert=True)
    await cb.answer()
    rows, line = [], []
    for uid, name in await employees():
        line.append((name, f"nu:{uid}"))
        if len(line) == 2:
            rows.append(line)
            line = []
    rows.append(line)
    await cb.message.answer("Кому поставить задачу?", reply_markup=kb(rows))


@dp.callback_query(F.data.startswith("nu:"))
async def on_new_for(cb: CallbackQuery):
    if not full(cb.from_user.id):
        return await cb.answer("Нужен полный доступ: попросите владельца", show_alert=True)
    await cb.answer()
    uid = cb.data[3:]
    drafts[cb.message.chat.id] = {"uid": uid, "step": "text"}
    await cb.message.answer(f"Задача для: <b>{html.escape(await name_of(uid))}</b>.\n"
                            "Напишите задачу одним сообщением: первая строка — название, дальше — описание.",
                            parse_mode=ParseMode.HTML, reply_markup=kb([[("✖️ Отмена", "nx")]]))


@dp.message(F.text, lambda m: drafts.get(m.chat.id, {}).get("step") == "text")
async def on_task_text(msg: Message):
    if not full(msg.from_user.id):
        return
    title, _, desc = msg.text.strip().partition("\n")
    dr = drafts[msg.chat.id]
    dr.update(title=title.strip()[:250], desc=desc.strip(), step="deadline")
    t = today()
    friday = t + timedelta(days=(4 - t.weekday()) % 7 or 7)
    await msg.answer("Срок?", reply_markup=kb([
        [("Сегодня", f"dd:{t}"), ("Завтра", f"dd:{t + timedelta(days=1)}")],
        [(f"Пятница {friday:%d.%m}", f"dd:{friday}"), ("Через неделю", f"dd:{t + timedelta(days=7)}")],
        [("🗓 Другая дата", "ddcal"), ("Без срока", "dd:none")],
        [("✖️ Отмена", "nx")],
    ]))


@dp.callback_query(F.data == "ddcal")
async def on_dd_cal(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=calendar_kb("dd", today(), back="nx"))


@dp.callback_query(F.data.startswith("dd:"))
async def on_deadline(cb: CallbackQuery):
    dr = drafts.get(cb.message.chat.id)
    if not full(cb.from_user.id) or not dr or "title" not in dr:
        return await cb.answer("Черновик не найден, начните заново")
    await cb.answer()
    value = cb.data[3:]
    dr["deadline"] = None if value == "none" else value
    dr["step"] = "confirm"
    col = mapping().get(dr["uid"], {}).get("column")
    cols = {c["id"]: c for c in await yg.columns()}
    boards = {b["id"]: b for b in await yg.boards()}
    c = cols.get(col or "")
    place = f"{boards.get(c['boardId'], {}).get('title', 'доска')} › {c['title']}" if c else "без доски"
    dl = date.fromisoformat(dr["deadline"]).strftime("%d.%m.%Y") if dr["deadline"] else "без срока"
    text = (f"Проверьте:\n\n<b>{html.escape(dr['title'])}</b>\n"
            + (f"{html.escape(report.clean(dr['desc'], 600))}\n" if dr["desc"] else "")
            + f"\nИсполнитель: {html.escape(await name_of(dr['uid']))}\nКуда: {html.escape(place)}\nСрок: {dl}")
    await cb.message.answer(text, parse_mode=ParseMode.HTML,
                            reply_markup=kb([[("✅ Поставить", "nok"), ("✖️ Отмена", "nx")]]))


@dp.callback_query(F.data == "nok")
async def on_confirm(cb: CallbackQuery):
    dr = drafts.get(cb.message.chat.id)
    if not full(cb.from_user.id) or not dr or dr.get("step") != "confirm":
        return await cb.answer("Черновик не найден, начните заново")
    await cb.answer("Ставлю…")
    dr.setdefault("key", str(uuid.uuid4()))  # повторное нажатие не создаст дубль
    dl_ms = None
    if dr["deadline"]:
        dl_ms = report.day_window(date.fromisoformat(dr["deadline"]), TZ)[0] + 18 * 3600_000
    try:
        await yg.create_task(dr["title"], dr["desc"], mapping().get(dr["uid"], {}).get("column"),
                             [dr["uid"]], dl_ms, dr["key"])
    except YouGileError as e:
        log.exception("создание задачи")
        return await cb.message.answer(f"Не получилось поставить задачу: {html.escape(str(e)[:200])}. "
                                       "Нажмите «Поставить» ещё раз — дубля не будет.")
    drafts.pop(cb.message.chat.id, None)
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(f"✅ Задача поставлена: {html.escape(dr['title'])} → {html.escape(await name_of(dr['uid']))}",
                            reply_markup=kb([[("➕ Ещё задачу", "nt"), (PERSONAL_BTN, "me")]]))


@dp.callback_query(F.data == "nx")
async def on_cancel(cb: CallbackQuery):
    drafts.pop(cb.message.chat.id, None)
    await cb.answer("Отменено")
    await cb.message.edit_reply_markup(reply_markup=None)


# --- ввод словами ---

INPUT_HELP = ("Напишите, что сделать, одним сообщением. Например:\n"
              "• поставь Веронике задачу «обновить инфографику OZ565» до пятницы\n"
              "• напиши в чат руководство, отметь Константина: созвон завтра в 11\n"
              "• в задаче «Поставки, оборачиваемость» напиши Анне: проверь транзит\n\n"
              "Перед отправкой покажу, что сделаю, и спрошу подтверждение.")


@dp.message(F.text == INPUT_BTN)
async def input_btn(msg: Message):
    if not full(msg.from_user.id):
        return await msg.answer("«Ввод» доступен с полным доступом. Попросите владельца.")
    if allowed(msg.from_user.id):
        await start_input(msg, None)


@dp.callback_query(F.data.startswith("in"))
async def on_input(cb: CallbackQuery):
    if not full(cb.from_user.id):
        return await cb.answer("Нужен полный доступ: попросите владельца", show_alert=True)
    await cb.answer()
    await start_input(cb.message, cb.data[3:] or None)


async def start_input(msg: Message, about_uid):
    if not ai.client:
        return await msg.answer("Для «Ввода» нужен ключ Claude: ANTHROPIC_API_KEY в .env на сервере.")
    drafts[msg.chat.id] = {"step": "input", "about": about_uid, "history": []}
    head = f"Про сотрудника: <b>{html.escape(await name_of(about_uid))}</b>\n\n" if about_uid else ""
    await msg.answer(head + html.escape(INPUT_HELP), parse_mode=ParseMode.HTML,
                     reply_markup=kb([[("✖️ Отмена", "nx")]]))


@dp.message(F.voice | F.audio)
async def on_voice(msg: Message):
    """Голосовое — тот же «Ввод»: расшифровка, затем разбор."""
    if not full(msg.from_user.id):
        return
    if not voice.enabled():
        return await msg.answer("Голос не настроен на сервере (нужен пакет faster-whisper). Напишите текстом.")
    if not ai.client:
        return await msg.answer("Для «Ввода» нужен ключ Claude: ANTHROPIC_API_KEY в .env на сервере.")
    wait = await msg.answer("🎙 Расшифровываю…")
    try:
        f = await msg.bot.download(msg.voice or msg.audio)
        text = await voice.transcribe(f.read())
    except Exception as e:
        log.exception("голос")
        return await msg.answer(f"Не получилось расшифровать: {html.escape(str(e)[:150])}")
    finally:
        await wait.delete()
    if not text:
        return await msg.answer("Не расслышал. Повторите или напишите текстом.")
    await msg.answer(f"🎙 «{html.escape(text)}»", parse_mode=ParseMode.HTML)
    dr = drafts.get(msg.chat.id)
    if not dr or dr.get("step") != "input":
        drafts[msg.chat.id] = {"step": "input", "about": None, "history": []}
    await handle_input(msg, text)


@dp.message(F.text, lambda m: drafts.get(m.chat.id, {}).get("step") == "input")
async def on_input_text(msg: Message):
    if not full(msg.from_user.id):
        return
    await handle_input(msg, msg.text.strip())


async def handle_input(msg: Message, text: str):
    dr = drafts[msg.chat.id]
    if not dr["history"] and dr.get("about"):
        text = f"(речь о сотруднике {await name_of(dr['about'])}, id {dr['about']}) {text}"
    dr["history"].append(("user", text))
    wait = await msg.answer("⏳ Разбираю…")
    try:
        people = [(u["id"], report.short_name(u.get("realName") or u.get("email")))
                  for u in await yg.users() if not u.get("deleted")]
        chats = [(c["id"], c.get("title") or "") for c in await yg.group_chats() if not c.get("deleted")]
        ctx = ai.context(today(), await employees(), people, chats)
        res = await ai.plan(dr["history"], ctx)
    except Exception as e:  # сеть, ключ, лимиты — человеку коротко, в журнал подробно
        log.exception("ввод")
        dr["history"].pop()
        return await msg.answer(f"Не получилось разобрать: {html.escape(str(e)[:150])}. Попробуйте ещё раз.")
    finally:
        await wait.delete()
    if res.get("question") and not res.get("actions"):
        dr["history"].append(("assistant", res["question"]))
        return await msg.answer("❓ " + res["question"], reply_markup=kb([[("✖️ Отмена", "nx")]]))
    if res.get("unsupported") and not res.get("actions"):
        drafts.pop(msg.chat.id, None)
        return await msg.answer("⚠️ " + res["unsupported"], reply_markup=kb([[(INPUT_BTN, "in")]]))
    tasks = await yg.tasks()
    lines = []
    for a in res["actions"]:
        if a["type"] == "task_message":
            a["task_id"] = find_task(tasks, a["task_query"])
        lines.append(await describe(a, tasks))
    dr.update(step="input_confirm", actions=res["actions"], key=str(uuid.uuid4()))
    note = f"\n\n⚠️ {html.escape(res['unsupported'])}" if res.get("unsupported") else ""
    can = all(a["type"] != "task_message" or a.get("task_id") for a in res["actions"])
    buttons = [("✅ Выполнить", "iok")] if can else []
    await msg.answer("Сделаю так:\n\n" + "\n\n".join(lines) + note, parse_mode=ParseMode.HTML,
                     reply_markup=kb([buttons + [("✖️ Отмена", "nx")], [("✏️ Переписать", "in")]]))


def find_task(tasks, query):
    """Задача по словам из названия: все слова должны встретиться, из подходящих — самая свежая."""
    words = [w for w in report.clean(query).lower().split() if len(w) > 1]
    if not words:
        return None
    hits = [t for t in tasks if not t.get("deleted") and all(w in (t.get("title") or "").lower() for w in words)]
    hits.sort(key=lambda t: (not t.get("completed"), t.get("timestamp") or 0), reverse=True)
    return hits[0]["id"] if hits else None


async def describe(a, tasks):
    e = html.escape
    by_id = {t["id"]: t for t in tasks}
    mentions = ", ".join([await name_of(u) for u in a["mention_ids"]])
    tag = f"\nОтметить: {e(mentions)}" if mentions else ""
    if a["type"] == "create_task":
        who = ", ".join([await name_of(u) for u in a["assignee_ids"]]) or "без исполнителя"
        dl = date.fromisoformat(a["deadline"]).strftime("%d.%m.%Y") if a["deadline"] else "без срока"
        desc = f"\n{e(report.clean(a['description'], 400))}" if a["description"] else ""
        return f"➕ <b>Задача</b>: {e(a['title'])}{desc}\nИсполнитель: {e(who)} · срок: {dl}"
    if a["type"] == "group_chat_message":
        title = next((c.get("title") for c in await yg.group_chats() if c["id"] == a["chat_id"]), None)
        if not title:
            return "⚠️ Чат не найден — уточните название."
        return f"💬 <b>В чат «{e(title)}»</b>{tag}\n«{e(a['text'])}»"
    t = by_id.get(a.get("task_id") or "")
    if not t:
        return f"⚠️ Задача по словам «{e(a['task_query'])}» не найдена — уточните название."
    return f"💬 <b>В чат задачи «{e(report.clean(t.get('title'), 80))}»</b>{tag}\n«{e(a['text'])}»"


@dp.callback_query(F.data == "iok")
async def on_input_ok(cb: CallbackQuery):
    dr = drafts.get(cb.message.chat.id)
    if not full(cb.from_user.id) or not dr or dr.get("step") != "input_confirm":
        return await cb.answer("План не найден, начните заново")
    await cb.answer("Выполняю…")
    await cb.message.edit_reply_markup(reply_markup=None)
    done, errors = [], []
    for i, a in enumerate(dr["actions"]):
        if a.get("done"):
            continue
        try:
            if a["type"] == "create_task":
                col = mapping().get((a["assignee_ids"] or [""])[0], {}).get("column")
                dl = (report.day_window(date.fromisoformat(a["deadline"]), TZ)[0] + 18 * 3600_000
                      if a["deadline"] else None)
                await yg.create_task(a["title"], a["description"], col, a["assignee_ids"], dl, f"{dr['key']}-{i}")
                done.append(f"✅ Задача «{a['title']}» поставлена")
            elif a["type"] == "group_chat_message":
                await yg.send_message(a["chat_id"], a["text"], a["mention_ids"])
                done.append("✅ Сообщение в чат отправлено")
            else:
                await yg.send_message(a["task_id"], a["text"], a["mention_ids"])
                done.append("✅ Сообщение в задачу отправлено")
            a["done"] = True  # повторное «Выполнить» после ошибки не продублирует сделанное
        except (YouGileError, ValueError) as e:
            log.exception("ввод: выполнение")
            errors.append(f"❌ {a['type']}: {str(e)[:150]}")
    if errors:
        await cb.message.answer("\n".join(done + errors), reply_markup=kb([[("🔁 Повторить", "iok"),
                                                                            ("✖️ Отмена", "nx")]]))
    else:
        drafts.pop(cb.message.chat.id, None)
        await cb.message.answer("\n".join(done), reply_markup=kb([[(INPUT_BTN, "in")]]))


@dp.message()
async def other(msg: Message):
    if allowed(msg.from_user.id):
        await msg.answer("Выберите кнопку внизу.", reply_markup=menu())


# --- вебхуки YouGile ---

async def read_log_later(task_id, delay=60):
    """После события по задаче ждём минуту (их часто несколько подряд) и читаем лог один раз."""
    try:
        await asyncio.sleep(delay)
        store.add_log(task_id, await yg.task_log(task_id, limit=30))
    except YouGileError:
        log.exception("лог задачи %s", task_id)
    finally:
        pending_logs.pop(task_id, None)


seen_payloads = 0


async def on_webhook(request: web.Request):
    global seen_payloads
    if request.match_info.get("secret") != WEBHOOK_URL.rstrip("/").rsplit("/", 1)[-1]:
        return web.Response(status=404)
    try:
        body = await request.json()
    except Exception:
        return web.Response(status=400)
    if seen_payloads < 20:  # первые события — в журнал целиком, чтобы сверить формат
        seen_payloads += 1
        log.info("вебхук: %s", json.dumps(body, ensure_ascii=False)[:1500])
    event = body.get("event", "")
    p = body.get("payload") or {}
    if event.startswith("chat_message"):
        chat_id = p.get("chatId") or body.get("chatId") or (p.get("chat") or {}).get("id")
        if p.get("id") and chat_id and not p.get("deleted"):
            task_id = chat_id if chat_id in {t["id"] for t in await yg.tasks()} else None
            store.add(f"msg:{chat_id}:{p['id']}", p["id"], p.get("fromUserId"), "message",
                      task_id=task_id, chat_id=chat_id, text=p.get("text"))
    elif event.startswith("task-") and p.get("id"):
        if p["id"] not in pending_logs:
            pending_logs[p["id"]] = asyncio.create_task(read_log_later(p["id"]))
    return web.Response(text="ok")


async def run_webhooks():
    app = web.Application()
    app.router.add_post("/yougile/{secret}", on_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT).start()
    log.info("приём вебхуков YouGile на порту %s", WEBHOOK_PORT)
    await yg.ensure_webhooks(WEBHOOK_URL)
    store.collect_start()  # запоминаем момент первого запуска приёма событий


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.environ.get("TELEGRAM_YOUGILE_BOT_TOKEN")
    if not token or not yg.key:
        raise SystemExit("Нет TELEGRAM_YOUGILE_BOT_TOKEN или YOUGILE_API_KEY в .env")
    store.cleanup()
    if WEBHOOK_URL:
        await run_webhooks()
    else:
        log.warning("YG_WEBHOOK_URL не задан: сообщения и переносы видны только через историю задач")
    bot = Bot(token)
    asyncio.create_task(scheduler(bot))
    try:
        await dp.start_polling(bot)
    finally:
        await yg.close()


if __name__ == "__main__":
    asyncio.run(main())
