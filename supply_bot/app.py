"""Telegram-бот поставок. Старт: FBO / FBS. Под FBO — распределение, поиск по артикулу и складу, правки.

Бот читает Ozon, «Мой склад» и таблицы. Если на сервере есть ключ поставок (OZON_SUPPLY_API_KEY), владелец может
создать черновик и заявку на поставку из экрана направления — только после своего «да» на конкретный план.
Запуск: python -m supply_bot.app (переменные — в env.example).
"""
import asyncio
import re
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
from .sources import ozon_supply
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
        self.drafts = {}  # chat_id → черновик поставки в Ozon (живёт 30 минут)
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
    [("📥 Сформировать отчёт Excel", "file"), ("🛒 Заказать", "order")],
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
        name = _wh_by_index(plan, int(d[3:]))
        rows = [[("🏬 Другое направление", "whs:0"), ("⬅️ Меню FBO", "fbo")]]
        if any(x.warehouse == name and x.qty > 0 for x in plan.lines):
            rows.insert(0, [("✅ Всё верно, скачать файл направления", f"whf:{d[3:]}")])
            if ozon_supply.enabled() and m.chat.id in admins():
                rows.insert(1, [("📝 Создать черновик в Ozon", f"drf:{d[3:]}")])
        try:
            carry = json.loads(CARRY.read_text(encoding="utf-8")).get(name)
        except (OSError, ValueError):
            carry = None
        note = [f"↪️ Остаток с отгрузки {carry['date']} (склад не принял): " +
                ", ".join(f"{escape(o)} {q}" for o, q in carry["items"].items())] if carry and carry.get("items") else []
        await send(m, note + render.warehouse(plan, name), kb(rows))
    elif d.startswith("whf:"):
        await m.answer("Формирую файл направления…")
        await send_direction_file(m, plan, _wh_by_index(plan, int(d[4:])))
    elif d.startswith("dr"):
        await on_draft(m, plan, d)
    elif d == "empty":
        await send(m, render.empty_clusters(plan), FBO)
    elif d == "decide":
        await send(m, render.decide(plan), FBO)
    elif d == "order":
        await send(m, render.order(plan), FBO)
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
        await m.answer("Введите название кластера, как в Ozon, например <code>Казань</code>. ")
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
            return await m.answer("Такого кластера нет. Проверьте название.")
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


async def send_direction_file(m, plan, name):
    safe = re.sub(r"[^\w\- ]+", "", name).strip().replace(" ", "_") or "направление"
    path = Path(tempfile.gettempdir()) / f"Поставка_{safe}_{plan.ship_date:%d.%m}.xlsx"
    await asyncio.to_thread(excel.build_direction, plan, name, path, S.snap.taken_at)
    await m.answer_document(FSInputFile(path), caption=f"{name}: распределение на {plan.ship_date:%d.%m}. "
                                                       f"Данные на {S.snap.taken_at}.")


DRAFT_TTL = 28 * 60   # Ozon хранит черновик 30 минут — после 28 просим создать заново


def _slot_text(slot):
    f, t = slot
    try:
        dt = datetime.fromisoformat(f.replace("Z", "+00:00"))
        dt2 = datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return f"{f} – {t}"
    wd = "пн вт ср чт пт сб вс".split()[dt.weekday()]
    star = "⭐ " if dt.weekday() in config.SHIP_WEEKDAYS else ""
    return f"{star}{wd} {dt:%d.%m} {dt:%H:%M}–{dt2:%H:%M}"


async def on_draft(m, plan, d):
    """Черновик → что примут склады → слот → «да» владельца → заявка. Только для того, кто управляет доступом."""
    if m.chat.id not in admins():
        return await m.answer("Создавать поставки может только владелец.")
    if not ozon_supply.enabled():
        return await m.answer("На сервере нет ключа поставок Ozon — черновик создать нельзя.")
    dr = S.drafts.get(m.chat.id)
    if d == "drx":
        S.drafts.pop(m.chat.id, None)
        return await m.answer("Черновик отменён. В кабинете ничего не создано.", reply_markup=FBO)
    if d.startswith("drf:"):
        name = _wh_by_index(plan, int(d[4:]))
        lines = [x for x in plan.lines if x.warehouse == name and x.qty > 0]
        sku = {norm(r["offer_id"]): s for s, r in S.snap.product_stocks.items()}
        asked = {x.offer: x.qty for x in lines}
        items = [(sku[norm(o)], q) for o, q in asked.items() if norm(o) in sku]
        missing = [o for o in asked if norm(o) not in sku]
        await m.answer(f"Создаю черновик в Ozon: <b>{escape(name)}</b>, {sum(asked.values())} шт, {len(asked)} арт. "
                       "Ozon считает до 3 минут…")
        cl = ozon_supply.SupplyClient()
        macro = (await asyncio.to_thread(cl.macro_ids)).get(name)
        if not macro:
            return await m.answer(f"Не нашёл кластер «{escape(name)}» в Ozon.")
        draft_id, errs = await asyncio.to_thread(cl.create_draft, macro, items, config.SUPPLY_DROP_OFF_ID)
        if not draft_id:
            return await m.answer("Ozon не создал черновик: " + escape(", ".join(str(e.get("error_message")) for e in errs)
                                                                       or "без причины"))
        info = await asyncio.to_thread(cl.draft_info, draft_id)
        whs, errors = ozon_supply.parse_info(info)
        for w in whs:
            w["accepted"] = await asyncio.to_thread(cl.bundle, w["bundle_id"]) if w["state"] != "NOT_AVAILABLE" else {}
        S.drafts[m.chat.id] = {"cluster": name, "draft_id": draft_id, "macro": macro, "asked": asked, "whs": whs,
                               "created": datetime.now().timestamp()}
        rows, text = [], [f"📝 <b>Черновик {escape(name)}</b> (живёт 30 минут, в кабинете не виден)", ""]
        for j, w in enumerate(whs[:8]):
            take, rest = ozon_supply.split(asked, w["accepted"])
            why = ozon_supply.REASON_RU.get(w["reason"], "")
            text.append(f"<b>{escape(w['name'])}</b> — {ozon_supply.STATE_RU.get(w['state'], w['state'])}"
                        + (f", примет {sum(take.values())} из {sum(asked.values())} шт" if w["state"] != "NOT_AVAILABLE" else "")
                        + (f" ({why})" if why else ""))
            cut = [f"   {escape(o)}: {take.get(o, 0)} из {asked[o]}" for o in asked if o in rest]
            text += cut[:12] + (["   …"] if len(cut) > 12 else [])
            if w["state"] != "NOT_AVAILABLE" and take:
                rows.append([(f"✅ {w['name'][:28]} · {sum(take.values())} шт", f"drw:{j}")])
        if missing:
            text.append("Нет SKU Ozon, не вошли: " + ", ".join(escape(x) for x in missing))
        if errors:
            text.append("Ozon отклонил: " + escape("; ".join(errors[:8])))
        rows.append([("❌ Отмена", "drx")])
        text.append("" if rows[:-1] else "Ни один склад не примет поставку сейчас.")
        return await send(m, render.chunks(text), kb(rows))
    if not dr:
        return await m.answer("Черновика нет — создайте его на экране направления.", reply_markup=FBO)
    if datetime.now().timestamp() - dr["created"] > DRAFT_TTL:
        S.drafts.pop(m.chat.id, None)
        return await m.answer("Черновик устарел (Ozon хранит его 30 минут). Создайте заново.", reply_markup=FBO)
    cl = ozon_supply.SupplyClient()
    if d.startswith("drw:"):
        w = dr["whs"][int(d[4:])]
        dr["wh"] = w
        slots = await asyncio.to_thread(cl.timeslots, dr["draft_id"], dr["macro"], w["id"])
        dr["slots"] = slots[:12]
        if not slots:
            return await m.answer(f"У {escape(w['name'])} нет свободных слотов на 2 недели. Выберите другой склад.",
                                  reply_markup=kb([[("❌ Отмена", "drx")]]))
        rows = [[(_slot_text(sl), f"drs:{k}")] for k, sl in enumerate(dr["slots"])] + [[("❌ Отмена", "drx")]]
        return await m.answer(f"<b>{escape(w['name'])}</b>: выберите слот отгрузки (⭐ — пн и чт).", reply_markup=kb(rows))
    if d.startswith("drs:"):
        dr["slot"] = dr["slots"][int(d[4:])]
        take, rest = ozon_supply.split(dr["asked"], dr["wh"]["accepted"])
        dr["take"], dr["rest"] = take, rest
        text = [f"Записать поставку в Ozon?", "",
                f"Кластер: <b>{escape(dr['cluster'])}</b>", f"Склад: <b>{escape(dr['wh']['name'])}</b>",
                f"Слот: <b>{_slot_text(dr['slot'])}</b>", f"Везём: <b>{sum(take.values())} шт</b>, {len(take)} арт.",
                (f"Остаток на следующую отгрузку: {sum(rest.values())} шт" if rest else "Остатка нет."), "",
                "Это создаст заявку в кабинете Ozon."]
        return await m.answer("\n".join(text), reply_markup=kb([[("✅ Да, записать поставку", "dry")],
                                                                [("❌ Отмена", "drx")]]))
    if d == "dry":
        if "slot" not in dr:
            return await m.answer("Сначала выберите склад и слот.")
        await m.answer("Создаю заявку в Ozon…")
        errs = await asyncio.to_thread(cl.create_supply, dr["draft_id"], dr["macro"], dr["wh"]["id"], dr["slot"])
        st = await asyncio.to_thread(cl.supply_status, dr["draft_id"]) if not errs else {"error_reasons": errs}
        S.drafts.pop(m.chat.id, None)
        if st.get("status") != "SUCCESS":
            return await m.answer("⚠️ Ozon не создал заявку: " + escape(", ".join(st.get("error_reasons") or
                                                                                  [str(st.get("status"))])), reply_markup=FBO)
        save_carryover(dr["cluster"], dr["rest"])
        path = Path(tempfile.gettempdir()) / f"Заявка_{st.get('order_id')}.xlsx"
        await asyncio.to_thread(excel.build_supply, dr["take"], dr["rest"], path)
        await m.answer(f"✅ Заявка создана: <b>{st.get('order_id')}</b>, {escape(dr['cluster'])} → "
                       f"{escape(dr['wh']['name'])}, {_slot_text(dr['slot'])}, {sum(dr['take'].values())} шт.")
        return await m.answer_document(FSInputFile(path), caption="Что везём и остаток на следующую отгрузку.")


CARRY = config.DATA_DIR / "carryover.json"


def save_carryover(cluster, rest):
    """Что склад не принял — на следующую отгрузку в этот кластер."""
    try:
        data = json.loads(CARRY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data[cluster] = {"date": datetime.now(MSK).strftime("%d.%m.%Y"), "items": rest}
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    CARRY.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


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


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    bot = Bot(os.environ["TELEGRAM_SUPPLY_BOT_TOKEN"].strip(), default=DefaultBotProperties(parse_mode="HTML"))
    # сам бот ничего не присылает: расчёт только по кнопке (решение владельца 26.10.2026)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
