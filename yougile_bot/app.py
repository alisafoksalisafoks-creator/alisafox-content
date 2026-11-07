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
BOT: Bot | None = None  # для пересылки ответов из вебхуков
drafts: dict[int, dict] = {}  # черновик новой задачи по чату
pending_logs: dict[str, asyncio.Task] = {}  # отложенное чтение лога задачи после вебхука

DATE_BTN = "📅 По дате"
PERSONAL_BTN = "👤 Личный"
INPUT_BTN = "✍️ Ввод"
COMPANY_BTN = "🏢 Итоги дня"
ACCESS_BTN = "👥 Доступ"
OVERDUE_BTN = "⏰ Просрочки"
CHAT_BTN = "💬 Чаты"
# старые подписи кнопок — у кого ещё висит прежняя клавиатура
OLD_BTNS = {"📅 Выберите дату": "📅 По дате", "🏢 Итоги дня компании": "🏢 Итоги дня",
            "👥 Добавить пользователя": "👥 Доступ", "💬 Написать в чат": "💬 Чаты"}
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
            ([KeyboardButton(text=INPUT_BTN)] if ai.client else []) + [KeyboardButton(text=COMPANY_BTN)],
            [KeyboardButton(text=OVERDUE_BTN), KeyboardButton(text=WEEK_BTN)],
            [KeyboardButton(text=CHAT_BTN)]]
    if uid == OWNER:
        rows.append([KeyboardButton(text=ACCESS_BTN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def who(user):
    return user.full_name + (f" (@{user.username})" if user.username else "")


HOME = ("🏠 Домой", "home")


def kb(rows, home=True):
    """Кнопки под сообщением. Внизу всегда «🏠 Домой», кроме самого стартового меню."""
    rows = [r for r in rows if r]
    if home and not any(d == "home" for r in rows for _, d in r):
        rows.append([HOME])
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in r]
                                                 for r in rows])


def start_kb(uid):
    return kb([[(DATE_BTN, "pick"), (PERSONAL_BTN, "me")],
               ([(INPUT_BTN, "in")] if ai.client else []) + [(COMPANY_BTN, f"co:{today()}")],
               [(CHAT_BTN, "wch"), (OVERDUE_BTN, "od")],
               [(WEEK_BTN, "wkm")],
               [(ACCESS_BTN, "acc:list")] if uid == OWNER else []], home=False)


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
                                   reply_markup=kb([[("👁 Отчёты", f"acc:view:{uid}"),
                                                     ("✅ Полный", f"acc:full:{uid}")],
                                                    [("❌ Отказать", f"acc:no:{uid}")]]))
        return await msg.answer("Запрос отправлен владельцу. Как только он откроет доступ, я напишу.")
    await msg.answer("YouGile AlisaFox. Отчёт по сотрудникам — «По дате». Свои задачи — «Личный». "
                     + ("Любое действие словами («поставь Веронике задачу до пятницы…») — «Ввод»." if ai.client
                        else "Задачи и сообщения — кнопками «➕ Задача» и «💬 Чаты»."), reply_markup=menu(uid))
    await msg.answer("Что открыть?", reply_markup=start_kb(uid))


@dp.callback_query(F.data == "home")
async def on_home(cb: CallbackQuery):
    """Домой: сбросить начатое и показать стартовое меню."""
    if not allowed(cb.from_user.id):
        return await cb.answer("Нет доступа")
    drafts.pop(cb.message.chat.id, None)
    await cb.answer()
    await cb.message.answer("🏠 Что открыть?", reply_markup=start_kb(cb.from_user.id))


@dp.callback_query(F.data == "od")
async def on_overdue(cb: CallbackQuery):
    if not allowed(cb.from_user.id):
        return await cb.answer("Нет доступа")
    await cb.answer("Собираю…")
    for part in report.chunks(await overdue_text()):
        await cb.message.answer(part, parse_mode=ParseMode.HTML, reply_markup=kb([]))


@dp.callback_query(F.data == "wkm")
async def on_week_menu(cb: CallbackQuery):
    if not allowed(cb.from_user.id):
        return await cb.answer("Нет доступа")
    await cb.answer()
    await week_btn(cb.message, cb.from_user.id)


@dp.message(F.text.in_({DATE_BTN, "📅 Выберите дату"}))
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


@dp.message(F.text.in_({ACCESS_BTN, "👥 Добавить пользователя"}))
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
    rows = [[(f"{'👁→✅' if r == 'view' else '✅→👁'} {n[:12]}", f"acc:{'full' if r == 'view' else 'view'}:{i}"),
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
        if len(line) == 1:
            rows.append(line)
            line = []
    rows.append(line)
    rows.append([(COMPANY_BTN, f"co:{d}")])
    rows.append([("📅 Другой день", "pick")])
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
                                [("📊 План|Факт", f"pf:{d}:{uid}")] + ([(INPUT_BTN, f"in:{uid}")] if ai.client else []),
                                [("👥 Сотрудники", f"d:{d}"), ("➕ Задача", f"nu:{uid}")]]))


@dp.callback_query(F.data.startswith("pf:") | F.data.startswith("pfd:"))
async def on_plan_fact(cb: CallbackQuery):
    """План|Факт: короткий конспект, по кнопке «Подробно» — полный разбор."""
    if not allowed(cb.from_user.id):
        return await cb.answer("Нет доступа")
    mode, day, uid = cb.data.split(":", 2)
    d = date.fromisoformat(day)
    await cb.answer("Собираю…")
    wait = await cb.message.answer("⏳ Собираю…")
    try:
        r = await employee_data(uid, d)
        text = r["detail"] if mode == "pfd" else await employee_brief(r, uid, d)
    except YouGileError as e:
        log.exception("YouGile")
        text = f"YouGile не ответил: {html.escape(str(e)[:200])}"
    finally:
        await wait.delete()
    nav = [("👥 Сотрудники", f"d:{d}"), ("➕ Задача", f"nu:{uid}")]
    more = [("📋 Подробно", f"pfd:{d}:{uid}")] if mode == "pf" else [("📝 Коротко", f"pf:{d}:{uid}")]
    parts = report.chunks(text)
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        await cb.message.answer(part, parse_mode=ParseMode.HTML, reply_markup=kb([more, nav]) if last else None)


briefs: dict[tuple, str] = {}  # конспекты: (вид, день, кто, число действий) → текст; новые действия — новый конспект


async def employee_data(uid, d: date):
    start, end = report.day_window(d, TZ)
    tasks, by_id, columns, where, column_title = await lookups()
    partial = start < collected_from()
    if partial:
        await backfill(uid, tasks, columns, start, end)
    groups = report.collect(uid, store.between(start, end), tasks, start, end)
    titles = {c["id"]: c.get("title") or "групповой" for c in await yg.group_chats()}
    plan = report.plan(uid, tasks, start, end)
    name = await name_of(uid)
    chat = lambda c: titles.get(c, "групповой")  # noqa: E731
    return {"name": name, "partial": partial, "groups": groups, "plan": plan,
            "detail": report.render(name, d, groups, by_id, where, column_title, chat, partial, plan),
            "facts": report.digest(groups, by_id, column_title, chat, plan)}


async def employee_report(uid, d: date):
    return (await employee_data(uid, d))["detail"]


async def employee_brief(r, uid, d: date):
    """Конспект за день от Claude. Без ключа или при ошибке — подробный разбор."""
    n = report.count(r["groups"])
    head = f"📝 <b>{html.escape(r['name'])}</b> · {report.day_title(d)}"
    if not n and not r["plan"]:
        return head + "\n\nДействий в YouGile за этот день нет."
    done = sum(1 for _, s in r["plan"] if s == "done")
    stat = f"Действий: {n}" + (f" · план {done} из {len(r['plan'])}" if r["plan"] else "")
    if not ai.client:
        return r["detail"]
    key = ("e", d, uid, n, done)
    if len(briefs) > 500:
        briefs.clear()
    if key not in briefs:
        try:
            briefs[key] = await ai.brief_employee(r["name"], report.day_title(d), r["facts"], r["partial"])
        except Exception:
            log.exception("конспект")
            return r["detail"]
    note = "\n\n<i>День раньше запуска бота — данные неполные.</i>" if r["partial"] else ""
    return f"{head}\n{stat}\n\n{html.escape(briefs[key])}{note}"


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

@dp.message(F.text.in_({COMPANY_BTN, "🏢 Итоги дня компании"}))
async def company_btn(msg: Message):
    if allowed(msg.from_user.id):
        await send_company(msg, today())


@dp.callback_query(F.data.startswith("co:"))
async def on_company(cb: CallbackQuery):
    if not allowed(cb.from_user.id):
        return await cb.answer("Нет доступа")
    await cb.answer("Собираю…")
    await send_company(cb.message, date.fromisoformat(cb.data[3:]))


async def send_company(msg: Message, d: date, detail=False):
    """Итоги компании: конспект, по кнопке — подробные строки."""
    wait = await msg.answer("⏳ Собираю…")
    try:
        text = await company_text(d, detail)
    except YouGileError as e:
        log.exception("YouGile")
        return await msg.answer(f"YouGile не ответил: {html.escape(str(e)[:200])}")
    finally:
        await wait.delete()
    switch = ("📝 Коротко", f"co:{d}") if detail else ("📋 Подробно", f"cod:{d}")
    parts = report.chunks(text)
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        await msg.answer(part, parse_mode=ParseMode.HTML, reply_markup=kb([
            [switch], [("👥 Сотрудники", f"d:{d}"), ("📅 Дата", "pick")]]) if last else None)


@dp.callback_query(F.data.startswith("cod:"))
async def on_company_detail(cb: CallbackQuery):
    if not allowed(cb.from_user.id):
        return await cb.answer("Нет доступа")
    await cb.answer("Собираю…")
    await send_company(cb.message, date.fromisoformat(cb.data[4:]), detail=True)


# --- просрочки и неделя ---

@dp.message(F.text == OVERDUE_BTN)
async def overdue_btn(msg: Message):
    if allowed(msg.from_user.id):
        for part in report.chunks(await overdue_text()):
            await msg.answer(part, parse_mode=ParseMode.HTML, reply_markup=kb([]))


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
async def week_btn_msg(msg: Message):
    await week_btn(msg, msg.from_user.id)


async def week_btn(msg: Message, uid):
    if allowed(uid):
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
        await cb.message.answer(part, parse_mode=ParseMode.HTML, reply_markup=kb([]))


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


async def company_text(d: date, detail=False):
    start, end = report.day_window(d, TZ)
    tasks, by_id, columns, where, column_title = await lookups()
    titles = {c["id"]: c.get("title") or "групповой" for c in await yg.group_chats()}
    chat = lambda c: titles.get(c, "групповой")  # noqa: E731
    events = store.between(start, end)
    rows, idle = [], []
    for u in await yg.users():
        if u.get("deleted"):
            continue
        name = mapping().get(u["id"], {}).get("name") or report.short_name(u.get("realName") or u.get("email"))
        groups = report.collect(u["id"], events, tasks, start, end)
        n = report.count(groups)
        if n:
            rows.append((n, name, groups))
        else:
            idle.append(name)
    rows.sort(key=lambda r: -r[0])
    partial = start < collected_from()
    idle.sort()
    if detail or not ai.client or not rows:
        return report.company(d, [report.company_line(n, g, by_id) for _, n, g in rows], idle, partial)
    total = sum(r[0] for r in rows)
    key = ("c", d, total)
    if key not in briefs:
        people = [(name, report.digest(g, by_id, column_title, chat)) for _, name, g in rows]
        try:
            briefs[key] = await ai.brief_company(report.day_title(d), people, idle, partial)
        except Exception:
            log.exception("конспект компании")
            return report.company(d, [report.company_line(n, g, by_id) for _, n, g in rows], idle, partial)
    lines = []
    for line in briefs[key].splitlines():
        line = line.strip().lstrip("•-– ").strip()
        if not line:
            continue
        who_, sep, rest = line.partition(" — ")
        lines.append(f"• <b>{html.escape(who_)}</b> — {html.escape(rest)}" if sep and len(who_) < 40
                     else html.escape(line))
    text = [f"🏢 <b>Итоги дня компании</b> · {report.day_title(d)}", f"Работали в YouGile: {len(rows)}", ""] + lines
    if idle:
        text += ["", "Без действий: " + html.escape(", ".join(idle))]
    if partial:
        text.append("<i>День раньше запуска бота — данные неполные.</i>")
    return "\n".join(text)


async def broadcast(bot: Bot, text):
    for chat in await recipients():
        for part in report.chunks(text):
            try:
                await bot.send_message(chat, part, parse_mode=ParseMode.HTML, reply_markup=kb([]))
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
        [("📌 Поставил я", "mine"), ("🙋 Мои задачи", "my")],
        [("📝 Мой день", "myday")],
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
        await cb.message.answer(part, parse_mode=ParseMode.HTML, reply_markup=kb([]))


@dp.callback_query(F.data == "myday")
async def on_myday(cb: CallbackQuery):
    if not full(cb.from_user.id):
        return await cb.answer("Нужен полный доступ", show_alert=True)
    await cb.answer("Собираю…")
    me = (await yg.me())["id"]
    text = await employee_report(me, today())
    for part in report.chunks(text):
        await cb.message.answer(part, parse_mode=ParseMode.HTML, reply_markup=kb([]))


# --- новая задача ---

@dp.callback_query(F.data == "nt")
async def on_new_task(cb: CallbackQuery):
    if not full(cb.from_user.id):
        return await cb.answer("Нужен полный доступ: попросите владельца", show_alert=True)
    await cb.answer()
    rows, line = [], []
    for uid, name in await employees():
        line.append((name, f"nu:{uid}"))
        if len(line) == 1:
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
                            "Напишите <b>название</b> задачи.",
                            parse_mode=ParseMode.HTML, reply_markup=kb([[("✖️ Отмена", "nx")]]))


@dp.message(F.text, lambda m: drafts.get(m.chat.id, {}).get("step") == "text")
async def on_task_text(msg: Message):
    """Шаг 1: название. Всё сообщение — название задачи."""
    if not full(msg.from_user.id):
        return
    dr = drafts[msg.chat.id]
    dr.update(title=" ".join(msg.text.split())[:250], step="desc")
    await msg.answer("Теперь <b>описание</b>: что сделать, ссылки, детали. Оно уйдёт первым сообщением "
                     "в чат задачи с отметкой исполнителя.", parse_mode=ParseMode.HTML,
                     reply_markup=kb([[("Без описания", "nd")], [("✖️ Отмена", "nx")]]))


@dp.message(F.text, lambda m: drafts.get(m.chat.id, {}).get("step") == "desc")
async def on_task_desc(msg: Message):
    """Шаг 2: описание — пойдёт в чат задачи."""
    if not full(msg.from_user.id):
        return
    drafts[msg.chat.id]["desc"] = msg.text.strip()
    await ask_deadline(msg)


@dp.callback_query(F.data == "nd")
async def on_no_desc(cb: CallbackQuery):
    dr = drafts.get(cb.message.chat.id)
    if not full(cb.from_user.id) or not dr or dr.get("step") != "desc":
        return await cb.answer("Черновик не найден, начните заново")
    await cb.answer()
    dr["desc"] = ""
    await ask_deadline(cb.message)


async def ask_deadline(msg: Message):
    drafts[msg.chat.id]["step"] = "deadline"
    t = today()
    friday = t + timedelta(days=(4 - t.weekday()) % 7 or 7)
    await msg.answer("Срок?", reply_markup=kb([
        [("Сегодня", f"dd:{t}"), ("Завтра", f"dd:{t + timedelta(days=1)}")],
        [(f"Пт {friday:%d.%m}", f"dd:{friday}"), ("+7 дней", f"dd:{t + timedelta(days=7)}")],
        [("🗓 Дата", "ddcal"), ("Без срока", "dd:none")],
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
            + (f"\n💬 В чат задачи: {html.escape(report.clean(dr['desc'], 600))}\n" if dr["desc"] else "")
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
        if not dr.get("task_id"):
            res = await yg.create_task(dr["title"], "", mapping().get(dr["uid"], {}).get("column"),
                                       [dr["uid"]], dl_ms, dr["key"])
            dr["task_id"] = (res or {}).get("id")
        if dr["desc"] and dr.get("task_id") and not dr.get("desc_sent"):
            await yg.send_message(dr["task_id"], dr["desc"], [dr["uid"]])
            dr["desc_sent"] = True
    except YouGileError as e:
        log.exception("создание задачи")
        what = "отправить описание в чат задачи" if dr.get("task_id") else "поставить задачу"
        return await cb.message.answer(f"Не получилось {what}: {html.escape(str(e)[:200])}. "
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


# --- написать в чат ---

DM_PREFIX = "Личное: "


@dp.message(F.text.in_({CHAT_BTN, "💬 Написать в чат"}))
async def chat_btn(msg: Message):
    if not full(msg.from_user.id):
        return await msg.answer("Писать в чаты можно с полным доступом. Попросите владельца.")
    await chat_menu(msg)


@dp.callback_query(F.data == "wch")
async def on_chat_menu(cb: CallbackQuery):
    if not full(cb.from_user.id):
        return await cb.answer("Нужен полный доступ", show_alert=True)
    await cb.answer()
    await chat_menu(cb.message)


async def chat_menu(msg: Message):
    """Групповые чаты, где есть владелец ключа, и люди для «личного» сообщения."""
    me = (await yg.me())["id"]
    chats = [c for c in await yg.group_chats()
             if not c.get("deleted") and me in (c.get("users") or {me: 1}) and not (c.get("title") or "").startswith(DM_PREFIX)]
    rows = [[(f"👥 {report.clean(c.get('title'), 30)}", f"wc:{c['id']}")] for c in chats]
    people = [(u["id"], mapping().get(u["id"], {}).get("name") or report.short_name(u.get("realName") or u.get("email")))
              for u in await yg.users() if not u.get("deleted") and u["id"] != me]
    people.sort(key=lambda x: x[1])
    line = []
    for uid, name in people:
        line.append((f"👤 {name}", f"wp:{uid}"))
        if len(line) == 1:
            rows.append(line)
            line = []
    rows.append(line)
    await msg.answer("Куда написать? 👥 — групповые чаты, 👤 — лично человеку.", reply_markup=kb(rows))


@dp.callback_query(F.data.startswith("wc:") | F.data.startswith("wp:"))
async def on_chat_pick(cb: CallbackQuery):
    if not full(cb.from_user.id):
        return await cb.answer("Нужен полный доступ", show_alert=True)
    await cb.answer()
    kind, target = cb.data.split(":", 1)
    history = ""
    if kind == "wc":
        where = await chat_title(target)
        drafts[cb.message.chat.id] = {"step": "chatmsg", "chat_id": target, "where": where}
        history = await chat_history(target)
        note = ""
    else:
        name = await name_of(target)
        me = (await yg.me())["id"]
        dm = next((c["id"] for c in await yg.group_chats() if not c.get("deleted")
                   and c.get("title") == DM_PREFIX + name and {me, target} <= set(c.get("users") or {})), None)
        drafts[cb.message.chat.id] = {"step": "chatmsg", "dm": target, "chat_id": dm, "where": f"лично: {name}"}
        if dm:
            history = await chat_history(dm)
        note = (f"\n\n<i>Личные сообщения YouGile через API недоступны, поэтому пишу в чат на двоих "
                f"«{DM_PREFIX}{html.escape(name)}». Его создам при первом сообщении, дальше — туда же.</i>")
    if history:
        for part in report.chunks(f"🕘 <b>Последние сообщения</b>\n{history}"):
            await cb.message.answer(part, parse_mode=ParseMode.HTML, reply_markup=kb([]))
    await cb.message.answer(f"Пишем в {html.escape(drafts[cb.message.chat.id]['where'])}. Напишите сообщение. "
                            f"Отметить человека — через @: «@Константин посмотри отчёт».{note}",
                            parse_mode=ParseMode.HTML, reply_markup=kb([[("✖️ Отмена", "nx")]]))


async def chat_title(chat_id):
    """Название чата: групповой — по названию, чат задачи — по названию задачи."""
    for c in await yg.group_chats():
        if c["id"] == chat_id:
            return f"чат «{c.get('title') or 'без названия'}»"
    for t in await yg.tasks():
        if t["id"] == chat_id:
            return f"задача «{report.clean(t.get('title'), 60)}»"
    return "чат"


async def readable(text):
    """Текст сообщения YouGile для Telegram: без HTML, упоминания — @Имя."""
    import re
    text = text or ""
    for uid in set(re.findall(r"\[!mention:([\w-]+)!\]", text)):
        text = text.replace(f"[!mention:{uid}!]", "@" + await name_of(uid))
    return report.clean(text, 600)


async def chat_history(chat_id, n=10):
    """Последние сообщения чата за неделю: «Имя, 12:30: текст»."""
    since = int((datetime.now(TZ) - timedelta(days=7)).timestamp() * 1000)
    try:
        msgs = [m for m in await yg.messages(chat_id, since) if not m.get("deleted") and m.get("id")]
    except YouGileError:
        log.exception("история чата")
        return "<i>Историю чата получить не удалось.</i>"
    msgs = sorted(msgs, key=lambda m: m["id"])[-n:]
    if not msgs:
        return "<i>За неделю сообщений нет.</i>"
    lines = []
    for m in msgs:
        when = datetime.fromtimestamp(m["id"] / 1000, TZ).strftime("%d.%m %H:%M")
        lines.append(f"<b>{html.escape(await name_of(m.get('fromUserId')) if m.get('fromUserId') else 'Система')}</b>, "
                     f"{when}: {html.escape(await readable(m.get('text')))}")
    return "\n".join(lines)


async def with_mentions(text):
    """«@Константин», «@Костя», «@Константина» → упоминание YouGile по первым 4 буквам имени."""
    people = [(u["id"], report.first_word(mapping().get(u["id"], {}).get("name")
                                           or report.short_name(u.get("realName") or u.get("email"))))
              for u in await yg.users() if not u.get("deleted")]
    found = []

    def repl(m):
        word = m.group(1).lower()
        hits = [uid for uid, first in people if first and (first.startswith(word[:4]) or word.startswith(first))]
        if len(hits) == 1:
            found.append(hits[0])
            return f"[!mention:{hits[0]}!]"
        return m.group(0)

    import re
    return re.sub(r"@([А-Яа-яЁёA-Za-z]+)", repl, text), found


@dp.message(F.text, lambda m: drafts.get(m.chat.id, {}).get("step") == "chatmsg")
async def on_chat_text(msg: Message):
    if not full(msg.from_user.id):
        return
    dr = drafts[msg.chat.id]
    dr["text"], found = await with_mentions(msg.text.strip())
    dr["step"] = "chatmsg_confirm"
    tags = ", ".join([await name_of(u) for u in found])
    await msg.answer(f"Отправить в {html.escape(dr['where'])}?\n\n«{html.escape(msg.text.strip())}»"
                     + (f"\n\nОтмечу: {html.escape(tags)}" if tags else ""),
                     parse_mode=ParseMode.HTML, reply_markup=kb([[("✅ Отправить", "wok"), ("✖️ Отмена", "nx")]]))


@dp.callback_query(F.data == "wok")
async def on_chat_send(cb: CallbackQuery):
    dr = drafts.get(cb.message.chat.id)
    if not full(cb.from_user.id) or not dr or dr.get("step") != "chatmsg_confirm":
        return await cb.answer("Черновик не найден, начните заново")
    await cb.answer("Отправляю…")
    try:
        if dr.get("dm") and not dr.get("chat_id"):
            me = (await yg.me())["id"]
            title = DM_PREFIX + await name_of(dr["dm"])
            dr["chat_id"] = next((c["id"] for c in await yg.group_chats()
                                  if not c.get("deleted") and c.get("title") == title
                                  and {me, dr["dm"]} <= set(c.get("users") or {})), None) \
                or await yg.create_dm_chat(me, dr["dm"], title)
        await yg.send_message(dr["chat_id"], dr["text"])
    except YouGileError as e:
        log.exception("сообщение в чат")
        return await cb.message.answer(f"Не отправилось: {html.escape(str(e)[:200])}. Нажмите «Отправить» ещё раз.")
    drafts.pop(cb.message.chat.id, None)
    store.subscribe(cb.message.chat.id, dr["chat_id"])
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(f"✅ Отправлено в {html.escape(dr['where'])}. Ответы из этого чата пришлю сюда.",
                            reply_markup=kb([[("🔕 Без ответов", f"un:{dr['chat_id']}"), (CHAT_BTN, "wch")]]))


@dp.callback_query(F.data.startswith("un:"))
async def on_unsub(cb: CallbackQuery):
    store.unsubscribe(cb.message.chat.id, cb.data[3:])
    await cb.answer("Больше не присылаю сообщения из этого чата")
    await cb.message.edit_reply_markup(reply_markup=None)


async def forward_reply(chat_id, msg_id, from_user, text):
    """Новое сообщение в чате, куда владелец писал через бота, — в Telegram с кнопкой «Ответить»."""
    subs = store.subscribers(chat_id)
    if not subs or not BOT:
        return
    try:
        if from_user == (await yg.me())["id"]:
            return  # своё же сообщение (в том числе отправленное ботом) не пересылаем
        body = (f"💬 <b>{html.escape(await chat_title(chat_id))}</b>\n"
                f"<b>{html.escape(await name_of(from_user))}</b>: {html.escape(await readable(text))}")
    except YouGileError:
        log.exception("пересылка")
        return
    for tg in subs:
        try:
            await BOT.send_message(tg, body, parse_mode=ParseMode.HTML, reply_markup=kb([
                [("↩️ Ответить", f"wc:{chat_id}"), ("🔕 Без ответов", f"un:{chat_id}")]]))
        except Exception:
            log.exception("пересылка в %s", tg)


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
        return await msg.answer("Голосом бот пока не управляет. Задачу — «➕ Задача», сообщение — «💬 Чаты».")
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
                if not a.get("task_id"):
                    res = await yg.create_task(a["title"], "", col, a["assignee_ids"], dl, f"{dr['key']}-{i}")
                    a["task_id"] = (res or {}).get("id")
                if a["description"] and a.get("task_id"):
                    await yg.send_message(a["task_id"], a["description"], a["assignee_ids"])
                done.append(f"✅ Задача «{a['title']}» поставлена" + (", описание в чате задачи" if a["description"] else ""))
            elif a["type"] == "group_chat_message":
                await yg.send_message(a["chat_id"], a["text"], a["mention_ids"])
                store.subscribe(cb.message.chat.id, a["chat_id"])
                done.append("✅ Сообщение в чат отправлено, ответы пришлю сюда")
            else:
                await yg.send_message(a["task_id"], a["text"], a["mention_ids"])
                store.subscribe(cb.message.chat.id, a["task_id"])
                done.append("✅ Сообщение в задачу отправлено, ответы пришлю сюда")
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
            asyncio.create_task(forward_reply(chat_id, p["id"], p.get("fromUserId"), p.get("text")))
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
    global BOT
    bot = BOT = Bot(token)
    asyncio.create_task(scheduler(bot))
    try:
        await dp.start_polling(bot)
    finally:
        await yg.close()


if __name__ == "__main__":
    asyncio.run(main())
