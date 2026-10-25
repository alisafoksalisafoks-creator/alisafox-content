"""Telegram-бот «Промт инженер AlisaFox»: делает промпты, инструкции и скилы для магазина.

Запуск: python -m promt_bot.app
Переменные окружения: TELEGRAM_PROMT_BOT_TOKEN, ANTHROPIC_API_KEY, PROMT_BOT_OWNER (Telegram ID владельца);
необязательно PROMT_BOT_MODEL, PROMT_BOT_EFFORT, PROMT_BOT_DATA.
"""
import asyncio
import base64
import logging
import os
import ssl

import anthropic
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)

from promt_bot import env  # noqa: F401  (подхватывает .env до чтения ключей)
from promt_bot import claude, render, store

log = logging.getLogger("promt_bot")
OWNER = int(os.environ.get("PROMT_BOT_OWNER", "0"))

dp = Dispatcher()
busy: set[int] = set()  # чаты, где модель ещё думает
waiting: dict[int, str] = {}  # владелец вводит ID нового пользователя
requests: dict[int, str] = {}  # кто просил доступ: id → имя

MODES = {
    "🧩 Скил": ("Скил", "Опишите процедуру, которую скил должен выполнять одинаково каждый раз. "
                      "Например: «ответ на вопрос покупателя „подойдёт ли к моему…“»."),
    "📋 Инструкция": ("Инструкция", "Для какого проекта, агента или бота нужна инструкция и что он должен делать?"),
    "✍️ Промпт": ("Промпт", "Для какой задачи нужен промпт? Например: «разобрать выгрузку MPStats по FM-трансмиттерам»."),
    "💬 Консультация": ("Консультация", "Расскажите своими словами, что хотите сделать или улучшить. "
                                       "Помогу выбрать: промпт, инструкция, скил или бот — и сделаю."),
}
NEW = "🆕 Новый диалог"
ACCESS = "👥 Доступ"
LETTERS = "АБВГДЕ"
TEXT_FILES = (".md", ".txt", ".csv", ".json", ".py", ".html", ".xml", ".yaml", ".yml")
MAX_FILE = 2_000_000


def allowed(user_id):
    return user_id == OWNER or user_id in store.users()


def menu(user_id):
    rows = [[KeyboardButton(text="🧩 Скил"), KeyboardButton(text="📋 Инструкция")],
            [KeyboardButton(text="✍️ Промпт"), KeyboardButton(text="💬 Консультация")],
            [KeyboardButton(text=NEW)] + ([KeyboardButton(text=ACCESS)] if user_id == OWNER else [])]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in r]
                                                 for r in rows])


def who(user):
    return user.full_name + (f" (@{user.username})" if user.username else "")


# --- доступ ---

@dp.message(CommandStart())
@dp.message(Command("menu"))
async def start(msg: Message):
    uid = msg.from_user.id
    if not allowed(uid):
        if not OWNER:
            return await msg.answer(f"Бот ещё не настроен. Ваш ID: {uid}")
        requests[uid] = who(msg.from_user)
        await msg.bot.send_message(OWNER, f"Просит доступ к боту: {requests[uid]}, ID {uid}",
                                   reply_markup=kb([[("✅ Пустить", f"acc:y:{uid}"), ("❌ Отказать", f"acc:n:{uid}")]]))
        return await msg.answer("Запрос отправлен владельцу. Как только он откроет доступ, я напишу.")
    await msg.answer("Я промпт-инженер магазина AlisaFox. Выберите, что нужно. "
                     "Не знаете — жмите «Консультация», помогу разобраться.", reply_markup=menu(uid))


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
        waiting[OWNER] = "add"
        return await cb.message.answer("Пришлите Telegram ID человека и, через пробел, его имя. "
                                       "Например: 123456789 Анна. Свой ID человек увидит, если напишет боту /start.")
    uid = int(parts[2])
    if parts[1] == "y":
        store.add_user(uid, requests.pop(uid, f"ID {uid}"))
        await cb.message.edit_text(cb.message.text + "\n\n✅ Доступ открыт")
        await cb.bot.send_message(uid, "Доступ открыт. Выберите, что нужно.", reply_markup=menu(uid))
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
        await msg.bot.send_message(int(head), "Вам открыли доступ к боту «Промт инженер AlisaFox». Нажмите /start.")
    except Exception:
        pass  # человек ещё не писал боту — Telegram не даст написать первым


# --- разговор с моделью ---

async def keep_typing(bot: Bot, chat_id):
    while True:
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
        await asyncio.sleep(4)


async def turn(msg: Message, chat_id, user_content):
    """Ход: добавить сообщение человека, получить ответ модели, показать текст, файлы и тест."""
    if chat_id in busy:
        return await msg.answer("Ещё думаю над прошлым сообщением, подождите немного.")
    busy.add(chat_id)
    c = store.chat(chat_id)
    before = len(c["messages"])
    c["messages"].append({"role": "user", "content": user_content})
    wait = await msg.bot.send_message(chat_id, "Думаю… Обычно от 20 секунд до 2 минут.")
    typing = asyncio.create_task(keep_typing(msg.bot, chat_id))
    try:
        resp = await claude.ask(c["messages"])
    except anthropic.APIStatusError as e:
        log.exception("ошибка API")
        return await fail(msg, chat_id, c, before, user_content, f"сервис Claude ответил ошибкой {e.status_code}")
    except anthropic.APIConnectionError:
        log.exception("нет связи с API")
        return await fail(msg, chat_id, c, before, user_content, "нет связи с сервисом Claude")
    finally:
        typing.cancel()
        busy.discard(chat_id)
        await wait.delete()
    if resp.stop_reason == "refusal":
        del c["messages"][before:]
        store.save_chat(chat_id, c)
        return await msg.bot.send_message(chat_id, "Модель отказалась отвечать на этот запрос. "
                                                   "Переформулируйте, пожалуйста.")
    content = claude.echo(resp.content)
    c["messages"].append({"role": "assistant", "content": content})
    text = claude.text_of(content)
    for part in render.chunks(render.chat_text(text)):
        await msg.bot.send_message(chat_id, part)
    for name, body, caption in render.files(text):
        await msg.bot.send_document(chat_id, BufferedInputFile(body.encode("utf-8"), name), caption=caption)
    q = claude.questions_of(content)
    if q:
        tool_id, questions, intro = q
        c["pending"] = {"tool_id": tool_id, "questions": questions, "answers": [], "custom": False}
        store.save_chat(chat_id, c)
        if intro:
            await msg.bot.send_message(chat_id, intro)
        return await send_question(msg.bot, chat_id, c["pending"])
    store.save_chat(chat_id, c)
    if resp.stop_reason == "max_tokens":
        await msg.bot.send_message(chat_id, "Ответ оборвался по длине. Напишите «продолжи».")
    elif not text:
        await msg.bot.send_message(chat_id, "Пустой ответ. Напишите сообщение ещё раз.")


async def fail(msg, chat_id, c, before, user_content, reason):
    del c["messages"][before:]  # история как до сообщения, чтобы можно было повторить
    c["retry"] = user_content
    store.save_chat(chat_id, c)
    await msg.bot.send_message(chat_id, f"Не получилось: {reason}. Попробуйте ещё раз через минуту.",
                               reply_markup=kb([[("🔁 Повторить", "retry")]]))


async def send_question(bot: Bot, chat_id, p):
    i = len(p["answers"])
    q = p["questions"][i]
    rec = q["recommended"] if 0 <= q["recommended"] < len(q["options"]) else -1
    lines = [f"Вопрос {i + 1} из {len(p['questions'])}", q["question"], ""]
    lines += [f"{LETTERS[j]}) {o}{' ⭐' if j == rec else ''}" for j, o in enumerate(q["options"][:len(LETTERS)])]
    if rec >= 0:
        lines += ["", f"Рекомендую {LETTERS[rec]}: {q['reason']}"]
    buttons = [[(f"{LETTERS[j]}) {o[:40]}{' ⭐' if j == rec else ''}", f"qa:{i}:{j}")]
               for j, o in enumerate(q["options"][:len(LETTERS)])]
    await bot.send_message(chat_id, "\n".join(lines), reply_markup=kb(buttons + [[("✍️ Свой ответ", f"qa:{i}:c")]]))


async def answer(msg: Message, chat_id, value):
    """Записать ответ на текущий вопрос теста; на последнем — отдать все ответы модели."""
    c = store.chat(chat_id)
    p = c["pending"]
    p["answers"].append(value)
    p["custom"] = False
    if len(p["answers"]) < len(p["questions"]):
        store.save_chat(chat_id, c)
        return await send_question(msg.bot, chat_id, p)
    c["pending"] = None
    store.save_chat(chat_id, c)
    result = "\n".join(f"{n}. {q['question']} — {a}" for n, (q, a) in enumerate(zip(p["questions"], p["answers"]), 1))
    await turn(msg, chat_id, [{"type": "tool_result", "tool_use_id": p["tool_id"], "content": result}])


async def on_question(cb: CallbackQuery):
    chat_id = cb.message.chat.id
    c = store.chat(chat_id)
    p = c["pending"]
    _, i, choice = cb.data.split(":")
    if not p or int(i) != len(p["answers"]):
        return await cb.answer("Этот вопрос уже закрыт")
    await cb.answer()
    q = p["questions"][int(i)]
    if choice == "c":
        p["custom"] = True
        store.save_chat(chat_id, c)
        await cb.message.edit_reply_markup(reply_markup=None)
        return await cb.message.answer("Напишите свой ответ одним сообщением.")
    value = q["options"][int(choice)]
    await cb.message.edit_text(f"{cb.message.text}\n\n✅ Ваш ответ: {value}")
    await answer(cb.message, chat_id, value)


async def user_content(msg: Message):
    """Текст, файл .md/.txt/.pdf или скриншот → блоки для модели. None — если формат не подходит."""
    text = msg.text or msg.caption or ""
    blocks = []
    if msg.photo:
        f = await msg.bot.download(msg.photo[-1])
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                   "data": base64.b64encode(f.read()).decode()}})
    elif msg.document:
        d = msg.document
        name = (d.file_name or "файл").lower()
        if d.file_size and d.file_size > MAX_FILE:
            await msg.answer("Файл больше 2 МБ. Пришлите часть или текстом.")
            return None
        if name.endswith(".pdf"):
            f = await msg.bot.download(d)
            blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                          "data": base64.b64encode(f.read()).decode()}})
        elif name.endswith(TEXT_FILES):
            f = await msg.bot.download(d)
            body = f.read().decode("utf-8", errors="replace")
            text = f"{text}\n\nФайл «{d.file_name}»:\n<<<\n{body}\n>>>".strip()
        else:
            await msg.answer("Этот формат я не читаю. Пришлите .md, .txt, .pdf, скриншот или текстом.")
            return None
    if text:
        blocks.append({"type": "text", "text": text})
    return blocks or None


@dp.message(F.text.in_(MODES))
async def on_mode(msg: Message):
    if not allowed(msg.from_user.id):
        return
    mode, hint = MODES[msg.text]
    store.save_chat(msg.chat.id, store.empty_chat(mode))
    await msg.answer(f"Режим «{mode}», начинаем с чистого листа.\n{hint}")


@dp.message(F.text == NEW)
async def on_new(msg: Message):
    if not allowed(msg.from_user.id):
        return
    store.save_chat(msg.chat.id, store.empty_chat())
    await msg.answer("Начали заново. Выберите режим или просто напишите задачу.", reply_markup=menu(msg.from_user.id))


@dp.message(F.text == ACCESS)
async def on_access_menu(msg: Message):
    if msg.from_user.id == OWNER:
        await access_menu(msg)


@dp.message()
async def on_message(msg: Message):
    uid, chat_id = msg.from_user.id, msg.chat.id
    if not allowed(uid):
        return await msg.answer("Нет доступа. Нажмите /start, чтобы отправить запрос владельцу.")
    if waiting.get(uid) == "add":
        return await add_by_id(msg)
    c = store.chat(chat_id)
    if c["pending"]:
        if not msg.text:
            return await msg.answer("Сейчас идёт тест: выберите вариант кнопкой или напишите ответ текстом.")
        return await answer(msg, chat_id, msg.text)  # текст во время теста — свой ответ на текущий вопрос
    content = await user_content(msg)
    if not content:
        return
    if not c["messages"] and c["mode"]:
        content.insert(0, {"type": "text", "text": f"[Режим: {c['mode']}]"})
    await turn(msg, chat_id, content)


@dp.callback_query()
async def on_button(cb: CallbackQuery):
    if cb.data.startswith("acc:"):
        return await on_access(cb)
    if not allowed(cb.from_user.id):
        return await cb.answer("Нет доступа")
    if cb.data.startswith("qa:"):
        return await on_question(cb)
    if cb.data == "retry":
        await cb.answer()
        c = store.chat(cb.message.chat.id)
        if not c.get("retry"):
            return await cb.message.answer("Повторять нечего. Напишите сообщение ещё раз.")
        content = c.pop("retry")
        store.save_chat(cb.message.chat.id, c)
        await cb.message.edit_reply_markup(reply_markup=None)
        await turn(cb.message, cb.message.chat.id, content)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    session = AiohttpSession()
    if os.environ.get("SSL_CERT_FILE"):  # свой корневой сертификат (прокси облачной сессии)
        session._connector_init["ssl"] = ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])
    bot = Bot(os.environ["TELEGRAM_PROMT_BOT_TOKEN"], session=session)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
