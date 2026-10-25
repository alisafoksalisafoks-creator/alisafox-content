"""Telegram-бот поставок. Старт: FBO / FBS. Под FBO — распределение, поиск по артикулу и складу, правки.

Бот только читает Ozon, «Мой склад» и таблицы. В кабинете ничего не создаёт.
Запуск: python -m supply_bot.app (переменные — в env.example).
"""
import asyncio
import json
import logging
import os
import tempfile
from html import escape
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, ErrorEvent, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import calc, config, excel, render
from .cli import snapshot
from .data import MSK
from .sources.sheets import norm

log = logging.getLogger("supply_bot")
FRESH_HOURS = 3          # снимок старше — пересобираем перед расчётом
WH_PAGE = 12             # складов на одной странице кнопок
OVERRIDES = config.DATA_DIR / "overrides.json"
USERS = config.DATA_DIR / "users.json"   # кого пустили кнопкой «Доступ»: {chat_id: имя}


def _ids(name):
    return [int(x.strip()) for x in os.environ.get(name, "").split(",") if x.strip()]


def env_chats():
    return set(_ids("SUPPLY_BOT_CHATS"))


def admins():
    """Кто управляет доступом: SUPPLY_BOT_ADMINS, а если пусто — первый id из SUPPLY_BOT_CHATS (владелец)."""
    return set(_ids("SUPPLY_BOT_ADMINS")) or set(_ids("SUPPLY_BOT_CHATS")[:1])


def load_users():
    try:
        return {int(k): v for k, v in json.loads(USERS.read_text(encoding="utf-8")).items()}
    except (OSError, ValueError):
        return {}


def save_users(users):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    USERS.write_text(json.dumps({str(k): v for k, v in users.items()}, ensure_ascii=False), encoding="utf-8")


def allowed_chats():
    return env_chats() | set(load_users())


class State:
    """Один магазин — одно общее состояние для всех разрешённых чатов."""

    def __init__(self):
        self.snap = None
        self.plan = None
        self.lock = asyncio.Lock()
        self.wait = {}   # chat_id → {"step": ..., ...} — чего ждём от владельца текстом
        self.names = {}  # chat_id → имя того, кто просил доступ
        try:
            raw = json.loads(OVERRIDES.read_text(encoding="utf-8"))
            self.overrides = {(o, w): q for o, w, q in raw}
        except (OSError, ValueError):
            self.overrides = {}

    def save_overrides(self):
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        OVERRIDES.write_text(json.dumps([[o, w, q] for (o, w), q in self.overrides.items()], ensure_ascii=False),
                             encoding="utf-8")

    def stale(self):
        if not self.snap:
            return True
        taken = datetime.strptime(self.snap.taken_at, "%d.%m.%Y %H:%M").replace(tzinfo=MSK)
        return datetime.now(MSK) - taken > timedelta(hours=FRESH_HOURS)

    async def ensure(self, msg, refresh=False):
        """Свежий снимок и план. Пока качаем — показываем, что делаем."""
        async with self.lock:
            if refresh or self.stale():
                note = await msg.answer("Собираю данные: Ozon, «Мой склад», таблицы. Это 1–3 минуты…")
                loop = asyncio.get_running_loop()
                steps = []

                def progress(text):
                    steps.append(text)
                    asyncio.run_coroutine_threadsafe(_edit(note, list(steps)), loop)

                self.snap = await asyncio.to_thread(snapshot, False, progress)
                await _edit(note, steps + ["Готово."])
            if refresh or self.plan is None or self.plan.today != self.snap.today:
                self.plan = calc.build(self.snap, self.overrides)
        return self.plan

    def rebuild(self):
        self.plan = calc.build(self.snap, self.overrides)
        return self.plan


async def _edit(note, steps):
    try:
        await note.edit_text("Собираю данные:\n" + "\n".join(f"• {s}" for s in steps[-6:]))
    except Exception:  # noqa: BLE001 — текст не изменился или сообщение уже удалено
        pass


S = State()
dp = Dispatcher()


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row]
                                                 for row in rows])


START = kb([[("📦 FBO", "fbo"), ("🚚 FBS", "fbs")]])
START_ADMIN = kb([[("📦 FBO", "fbo"), ("🚚 FBS", "fbs")], [("👥 Доступ", "acc")]])


def start_kb(chat_id):
    return START_ADMIN if chat_id in admins() else START
FBO = kb([
    [("📊 Распределение по магазину", "plan")],
    [("📥 Сформировать отчёт Excel", "file")],
    [("🔎 По артикулу", "ask_offer"), ("🏬 По направлению", "whs:0")],
    [("🗺 Пустые кластеры", "empty"), ("⛔ Решить / стоп", "decide")],
    [("💰 Скоро платно", "paid"), ("🧾 Сверка с «Моим складом»", "reconcile")],
    [("🔄 Обновить данные", "refresh"), ("⬅️ Назад", "start")],
])
SCREEN = kb([
    [("🏬 Показать по направлениям", "all_wh")],
    [("✅ Всё верно, прислать файлом", "file")],
    [("✏️ Исправить кол-во", "fix"), ("🔁 Пересчитать", "refresh")],
    [("⬅️ Меню FBO", "fbo")],
])


async def send(msg, texts, markup=None):
    for i, t in enumerate(texts):
        await msg.answer(t, reply_markup=markup if i == len(texts) - 1 else None)


def _guard(chat_id):
    """Пустой SUPPLY_BOT_CHATS — доступа нет ни у кого, бот только подскажет chat id."""
    return chat_id in allowed_chats()


@dp.errors()
async def on_error(event: ErrorEvent):
    """Сбой источника (Ozon, «Мой склад», таблицы) — говорим владельцу, а не молчим."""
    log.exception("ошибка обработки", exc_info=event.exception)
    upd = event.update
    msg = upd.message or (upd.callback_query.message if upd.callback_query else None)
    if msg:
        await msg.answer(f"⚠️ Не получилось: {escape(str(event.exception))[:300]}\n"
                         "Попробуйте ещё раз через пару минут. Если повторяется — пришлите это сообщение в проект.",
                         reply_markup=START)


ASKED = set()   # кто уже просил доступ с момента запуска — владельцу пишем один раз


def _who(user):
    name = " ".join(x for x in (user.first_name, user.last_name) if x) or "без имени"
    return name + (f" (@{user.username})" if user.username else "")


@dp.message(CommandStart())
async def on_start(m: Message):
    if not _guard(m.chat.id):
        await m.answer(f"Нет доступа. Ваш chat id: <code>{m.chat.id}</code>.\n"
                       "Я отправил владельцу запрос — когда он разрешит, нажмите /start ещё раз.")
        if m.chat.id not in ASKED:
            ASKED.add(m.chat.id)
            who = escape(_who(m.from_user))
            for admin in admins():
                await m.bot.send_message(admin, f"🔐 Просит доступ к боту поставок: <b>{who}</b>, id <code>{m.chat.id}</code>",
                                         reply_markup=kb([[("✅ Пустить", f"accok:{m.chat.id}"),
                                                           ("❌ Отказать", f"accno:{m.chat.id}")]]))
            S.names[m.chat.id] = _who(m.from_user)
        return
    S.wait.pop(m.chat.id, None)
    await m.answer("Бот поставок AlisaFox. Выберите схему:", reply_markup=start_kb(m.chat.id))


def access_screen():
    users = load_users()
    lines = ["👥 <b>Доступ к боту</b>", "", "Из настроек сервера (кнопкой не убрать):"]
    lines += [f"• <code>{x}</code>" + (" — управляет доступом" if x in admins() else "") for x in sorted(env_chats())]
    lines += ["", "Добавлены кнопкой:" if users else "Кнопкой пока никого не добавляли."]
    lines += [f"• {escape(n)} — <code>{x}</code>" for x, n in users.items()]
    lines += ["", "Новый человек пишет боту /start — вам придёт запрос с кнопкой «Пустить». "
                  "Или нажмите «Добавить по ID»."]
    rows = [[("➕ Добавить по ID", "accadd")]]
    rows += [[(f"❌ Убрать {n[:30]}", f"accdel:{x}")] for x, n in users.items()]
    rows.append([("⬅️ Назад", "start")])
    return "\n".join(lines), kb(rows)


def grant(chat_id, name):
    users = load_users()
    users[chat_id] = name
    save_users(users)


@dp.callback_query()
async def on_button(c: CallbackQuery):
    if not _guard(c.message.chat.id):
        return await c.answer("Нет доступа", show_alert=True)
    await c.answer()
    m, d = c.message, c.data
    S.wait.pop(m.chat.id, None)
    if d == "start":
        return await m.answer("Выберите схему:", reply_markup=start_kb(m.chat.id))
    if d.startswith("acc"):
        return await on_access(c, d)
    if d == "fbs":
        return await m.answer("FBS: правила расчёта пропишем отдельно. Пока здесь пусто.",
                              reply_markup=kb([[("⬅️ Назад", "start")]]))
    if d == "fbo":
        return await m.answer("FBO — что считаем?", reply_markup=FBO)
    if d == "refresh":
        plan = await S.ensure(m, refresh=True)
        note = [f"Правок вручную сохранено: {len(S.overrides)}. Сбросить — «Исправить кол-во»."] if S.overrides else []
        return await send(m, note + render.summary(plan, S.snap.taken_at), SCREEN)

    plan = await S.ensure(m)
    if d == "plan":
        await send(m, render.summary(plan, S.snap.taken_at), SCREEN)
    elif d == "all_wh":
        await send(m, render.all_warehouses(plan), SCREEN)
    elif d == "file":
        await m.answer("Формирую отчёт Excel…")
        await send_file(m, plan)
    elif d == "ask_offer":
        S.wait[m.chat.id] = {"step": "offer"}
        await m.answer("Введите артикул Ozon, например <code>OZ299NEW</code>.")
    elif d.startswith("whs:"):
        await m.answer("Выберите направление:", reply_markup=wh_keyboard(plan, int(d[4:]), "wh"))
    elif d.startswith("wh:"):
        await send(m, render.warehouse(plan, _wh_by_index(plan, int(d[3:]))),
                   kb([[("🏬 Другое направление", "whs:0"), ("⬅️ Меню FBO", "fbo")]]))
    elif d == "empty":
        await send(m, render.empty_clusters(plan), FBO)
    elif d == "decide":
        await send(m, render.decide(plan), FBO)
    elif d == "paid":
        await send(m, render.paid(plan), FBO)
    elif d == "reconcile":
        await send(m, render.reconcile(plan), FBO)
    elif d == "fix":
        rows = [[("➕ Направление не из списка", "fixwh:other")]]
        if S.overrides:
            rows.append([("🧹 Сбросить все правки", "fixreset")])
        await m.answer("Исправить количество. Выберите направление:",
                       reply_markup=_merge(wh_keyboard(plan, 0, "fixwh"), rows))
    elif d.startswith("fixwhs:"):
        await m.answer("Выберите направление:", reply_markup=wh_keyboard(plan, int(d[7:]), "fixwh"))
    elif d == "fixwh:other":
        S.wait[m.chat.id] = {"step": "fix_wh_name"}
        await m.answer("Введите название кластера, как в Ozon, например <code>Казань</code>. Для Москвы — склад, например <code>НОГИНСК_РФЦ</code>.")
    elif d.startswith("fixwh:"):
        wh = _wh_by_index(plan, int(d[6:]))
        S.wait[m.chat.id] = {"step": "fix_offer", "wh": wh}
        await m.answer(f"Направление <b>{wh}</b>. Введите артикул.")
    elif d == "fixreset":
        S.overrides = {}
        S.save_overrides()
        await send(m, ["Правки сброшены."] + render.summary(S.rebuild(), S.snap.taken_at), SCREEN)


@dp.message(F.text)
async def on_text(m: Message):
    if not _guard(m.chat.id):
        return
    w = S.wait.get(m.chat.id)
    if not w:
        return await m.answer("Выберите действие:", reply_markup=start_kb(m.chat.id))
    text = m.text.strip()
    if w["step"] == "acc_id":
        try:
            new = int(text.replace(" ", ""))
        except ValueError:
            return await m.answer("Нужен chat id — только цифры, например <code>923121806</code>. "
                                  "Человек увидит его, если напишет боту /start.")
        S.wait.pop(m.chat.id)
        grant(new, S.names.get(new, f"id {new}"))
        body, markup = access_screen()
        return await m.answer(f"✅ Добавил <code>{new}</code>.\n\n" + body, reply_markup=markup)
    plan = await S.ensure(m)
    if w["step"] == "offer":
        S.wait.pop(m.chat.id)
        return await send(m, render.offer(plan, text, S.snap.unit),
                          kb([[("🔎 Ещё артикул", "ask_offer"), ("⬅️ Меню FBO", "fbo")]]))
    if w["step"] == "fix_wh_name":
        known = {norm(x): x for x in calc.directions_of(plan, S.snap.wh_cluster)}
        wh = known.get(norm(text)) or next((v for k, v in known.items() if norm(text) in k), None)
        if not wh:
            return await m.answer("Такого направления нет: нужен кластер Ozon, а для Москвы — один из 5 основных складов.")
        S.wait[m.chat.id] = {"step": "fix_offer", "wh": wh}
        return await m.answer(f"Направление <b>{wh}</b>. Введите артикул.")
    if w["step"] == "fix_offer":
        line = next((x for x in plan.lines if x.warehouse == w["wh"] and norm(x.offer) == norm(text)), None)
        now = f"сейчас в плане {line.qty} шт ({render._boxes(line)})" if line else "сейчас в плане нет"
        S.wait[m.chat.id] = {"step": "fix_qty", "wh": w["wh"], "offer": text.upper()}
        return await m.answer(f"<code>{text}</code> → {w['wh']}: {now}.\nВведите нужное количество, шт (0 — убрать).")
    if w["step"] == "fix_qty":
        try:
            qty = int(text.replace(" ", ""))
        except ValueError:
            return await m.answer("Нужно целое число, например 150.")
        S.wait.pop(m.chat.id)
        await apply_fix(m, w["offer"], w["wh"], qty)


async def on_access(c, d):
    """Кнопки «Доступ»: только для тех, кто управляет доступом."""
    m = c.message
    if m.chat.id not in admins():
        return await m.answer("Управлять доступом может только владелец.", reply_markup=START)
    if d == "acc":
        body, markup = access_screen()
        return await m.answer(body, reply_markup=markup)
    if d == "accadd":
        S.wait[m.chat.id] = {"step": "acc_id"}
        return await m.answer("Введите chat id человека (цифры). Он увидит свой id, если напишет боту /start.")
    who = int(d.split(":")[1])
    if d.startswith("accok:"):
        grant(who, S.names.get(who, f"id {who}"))
        await m.edit_reply_markup(reply_markup=None)
        await m.answer(f"✅ Пустил <code>{who}</code>.")
        try:
            await m.bot.send_message(who, "Доступ к боту поставок открыт. Нажмите /start.")
        except Exception:  # noqa: BLE001 — человек мог заблокировать бота
            pass
    elif d.startswith("accno:"):
        await m.edit_reply_markup(reply_markup=None)
        await m.answer(f"Отказано: <code>{who}</code>.")
    elif d.startswith("accdel:"):
        users = load_users()
        users.pop(who, None)
        save_users(users)
        S.wait.pop(who, None)
        body, markup = access_screen()
        await m.answer(f"Убрал <code>{who}</code>.\n\n" + body, reply_markup=markup)


async def apply_fix(m, offer, wh, qty):
    if qty < 0:
        return await m.answer("Количество не может быть меньше нуля.")
    S.overrides[(offer, wh)] = qty
    S.save_overrides()
    plan = S.rebuild()
    line = next((x for x in plan.lines if x.warehouse == wh and norm(x.offer) == norm(offer)), None)
    if line is None and qty:
        S.overrides.pop((offer, wh))
        S.save_overrides()
        S.rebuild()
        why = render.offer(plan, offer, S.snap.unit)[0]
        return await m.answer(f"Не применил правку: артикул не проходит проверки.\n{why}", reply_markup=SCREEN)
    if not line or not line.qty:
        text = f"✏️ <code>{offer}</code> → {wh}: убрано из плана (вручную)."
    else:
        text = (f"✏️ <code>{line.offer}</code> → {wh}: <b>{line.qty}</b> шт ({render._boxes(line)}), вручную.")
        if line.qty != qty:
            text += f"\nВы ввели {qty}, подогнал под полные короба и свободный остаток нашего склада."
        warn = [f for f in line.flags if f != "вручную"]
        if warn:
            text += "\n⚠️ " + "; ".join(warn)
    await send(m, [text] + render.summary(plan, S.snap.taken_at), SCREEN)


async def send_file(m, plan):
    path = Path(tempfile.gettempdir()) / f"Поставка_FBO_{plan.ship_date:%d.%m}.xlsx"
    await asyncio.to_thread(excel.build, plan, path, S.snap.taken_at)
    await m.answer_document(FSInputFile(path), caption=f"Лист распределения FBO на {plan.ship_date:%d.%m}. "
                                                       f"Данные на {S.snap.taken_at}.")


def _merge(markup, rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in r]
                                                 for r in rows] + markup.inline_keyboard)


def _wh_list(plan):
    return list(plan.by_warehouse())


def _wh_by_index(plan, i):
    names = _wh_list(plan)
    return names[i] if 0 <= i < len(names) else ""


def wh_keyboard(plan, page, prefix):
    """Склады из плана, по убыванию объёма. Кнопка несёт номер склада — в callback_data не больше 64 байт."""
    names = _wh_list(plan)
    chunk = names[page * WH_PAGE:(page + 1) * WH_PAGE]
    rows = [[(f"{n} · {sum(x.qty for x in plan.by_warehouse()[n])} шт", f"{prefix}:{page * WH_PAGE + i}")]
            for i, n in enumerate(chunk)]
    nav = []
    list_prefix = "whs" if prefix == "wh" else "fixwhs"
    if page:
        nav.append(("◀️", f"{list_prefix}:{page - 1}"))
    if (page + 1) * WH_PAGE < len(names):
        nav.append(("▶️", f"{list_prefix}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([("⬅️ Меню FBO", "fbo")])
    return kb(rows)


async def scheduler(bot):
    """В дни отгрузки (пн, ср, пт) в 9:00 МСК — сводка в разрешённые чаты. Файл — только по кнопке."""
    while True:
        now = datetime.now(MSK)
        nxt = now.replace(hour=9, minute=0, second=0, microsecond=0)
        while nxt <= now or nxt.weekday() not in config.SHIP_WEEKDAYS:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            async with S.lock:
                S.snap = await asyncio.to_thread(snapshot, False, log.info)
                S.overrides = {}
                S.save_overrides()
                S.plan = calc.build(S.snap)
            texts = render.summary(S.plan, S.snap.taken_at)
            for chat in allowed_chats():
                for i, t in enumerate(texts):
                    await bot.send_message(chat, t, reply_markup=SCREEN if i == len(texts) - 1 else None)
        except Exception:  # noqa: BLE001 — сбой одного запуска не роняет бота
            log.exception("плановый расчёт не удался")


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    bot = Bot(os.environ["TELEGRAM_SUPPLY_BOT_TOKEN"].strip(), default=DefaultBotProperties(parse_mode="HTML"))
    if os.environ.get("SUPPLY_BOT_SCHEDULE", "1") == "1":
        asyncio.create_task(scheduler(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
