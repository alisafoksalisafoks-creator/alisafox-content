"""Кнопка «Ввод»: Claude разбирает фразу владельца в план действий в YouGile. Сам ничего не делает —
бот показывает план, и только после «✅ Выполнить» отправляет запросы."""
import os

import anthropic

MODEL = os.environ.get("YG_BOT_MODEL", "claude-sonnet-5-5")
client = anthropic.AsyncAnthropic(max_retries=3) if os.environ.get("ANTHROPIC_API_KEY") else None

TOOL = {
    "name": "propose",
    "description": "Вернуть план действий в YouGile по просьбе владельца. Бот покажет план и выполнит его "
                   "только после подтверждения.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["actions", "question", "unsupported"],
        "properties": {
            "actions": {
                "type": "array",
                "description": "Действия по порядку. Пусто, если нужен уточняющий вопрос или действие невозможно.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["type", "assignee_ids", "title", "description", "deadline", "chat_id",
                                 "task_query", "text", "mention_ids"],
                    "properties": {
                        "type": {"type": "string", "enum": ["create_task", "group_chat_message", "task_message"],
                                 "description": "create_task — поставить задачу на доску сотрудника; "
                                                "group_chat_message — написать в групповой (тематический) чат; "
                                                "task_message — написать в чат существующей задачи."},
                        "assignee_ids": {"type": "array", "items": {"type": "string"},
                                         "description": "create_task: id исполнителей из списка сотрудников."},
                        "title": {"type": "string", "description": "create_task: короткое название, до 100 знаков."},
                        "description": {"type": "string", "description": "create_task: описание или пусто."},
                        "deadline": {"type": "string",
                                     "description": "create_task: срок YYYY-MM-DD или пусто, если срок не назван."},
                        "chat_id": {"type": "string", "description": "group_chat_message: id чата из списка."},
                        "task_query": {"type": "string",
                                       "description": "task_message: слова из названия задачи для поиска."},
                        "text": {"type": "string",
                                 "description": "group_chat_message и task_message: текст сообщения без упоминаний, "
                                                "как его написал бы владелец."},
                        "mention_ids": {"type": "array", "items": {"type": "string"},
                                        "description": "Кого отметить (@) в сообщении: id пользователей."},
                    },
                },
            },
            "question": {"type": "string",
                         "description": "Один короткий уточняющий вопрос, если без него нельзя (например, не ясно, "
                                        "кому или в какой чат). Иначе пусто."},
            "unsupported": {"type": "string",
                            "description": "Если просьбу нельзя выполнить через API YouGile — коротко почему "
                                           "и что можно вместо этого. Иначе пусто."},
        },
    },
}

SYSTEM = """Ты — помощник владельца компании в YouGile. Владелец пишет просьбу своими словами, ты превращаешь её в план действий инструментом propose.

Что можно сделать:
- поставить задачу сотруднику на его доску (create_task), со сроком, если он назван;
- написать сообщение в групповой (тематический) чат (group_chat_message), с отметкой людей;
- написать в чат существующей задачи (task_message), с отметкой людей.

Чего нельзя: писать в личные сообщения (API YouGile их не даёт) — предложи вместо этого чат задачи или групповой чат. Удалять, переносить, менять чужие задачи — тоже нельзя, ответь в unsupported.

Правила:
- Людей и чаты бери только из списков ниже, по id. Имена в просьбе могут быть в другом падеже или уменьшительные: «Веронике», «Насте», «Костю».
- Сроки: «сегодня», «завтра», «в пятницу», «до 10-го» — переводи в дату YYYY-MM-DD от сегодняшней даты. Срок в прошлом — уточни.
- Текст сообщения пиши от лица владельца, без приветствия от бота и без подписи. Упоминания не вставляй в текст, клади в mention_ids.
- Если не ясно, кому, в какой чат или какая задача — задай один вопрос в question, actions пустой.
- Ничего не выдумывай: нет человека или чата в списке — спроси."""


def context(today, employees, people, chats):
    lines = [f"Сегодня: {today:%Y-%m-%d} ({['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'][today.weekday()]})",
             "", "Сотрудники (можно ставить задачи):"]
    lines += [f"- {name}: {uid}" for uid, name in employees]
    lines += ["", "Все пользователи компании (можно отмечать):"]
    lines += [f"- {name}: {uid}" for uid, name in people]
    lines += ["", "Групповые чаты:"]
    lines += [f"- {title}: {cid}" for cid, title in chats] or ["- нет"]
    return "\n".join(lines)


async def plan(history, ctx):
    """history — реплики [(role, text)]: просьба, вопрос бота, ответ владельца. Возвращает ввод propose."""
    messages = []
    for role, text in history:
        messages.append({"role": role, "content": text})
    messages[0] = {"role": "user", "content": f"{ctx}\n\nПросьба владельца:\n{history[0][1]}"}
    resp = await client.messages.create(
        model=MODEL, max_tokens=4000, system=SYSTEM, messages=messages,
        tools=[TOOL], tool_choice={"type": "tool", "name": "propose"})
    for b in resp.content:
        if b.type == "tool_use" and b.name == "propose":
            return b.input
    return {"actions": [], "question": "", "unsupported": "Не понял просьбу, переформулируйте."}


BRIEF_SYSTEM = """Ты делаешь для владельца компании короткие конспекты работы сотрудников по данным из YouGile.
Пиши по-русски, просто, как опытный руководитель пишет себе заметку. Без воды, без времени по минутам, без эмодзи.
Не перечисляй все действия подряд: собери смысл — над чем работал человек, что довёл до результата, что зависло.
Не выдумывай: только то, что есть в данных. Нет данных — так и скажи одной фразой.
Выдержки из переписки используй, чтобы понять суть работы (о чём договаривались, что проверяли), но не цитируй их целиком."""


async def brief_employee(name, day_title, facts, partial):
    prompt = (f"Сотрудник: {name}. День: {day_title}.\n"
              + ("Данные неполные: день раньше запуска бота.\n" if partial else "")
              + f"Факты:\n{facts or '- действий нет'}\n\n"
              "Сделай конспект в 3–5 строк, каждая начинается с «• »:\n"
              "• Главное за день — 1 строка.\n"
              "• Сделано — что закрыто или доведено до результата.\n"
              "• В работе — над чем шла работа, о чём переписка.\n"
              "• План — сколько из плана выполнено и что не сделано (если план есть).\n"
              "• Внимание — риск или хвост, только если он есть.\n"
              "Каждая строка до 25 слов. Без заголовка.")
    return await _text(prompt, 900)


async def brief_company(day_title, people, idle, partial):
    facts = "\n\n".join(f"## {name}\n{f}" for name, f in people)
    prompt = (f"День: {day_title}. Итоги дня компании.\n"
              + ("Данные неполные: день раньше запуска бота.\n" if partial else "")
              + f"Факты по людям:\n{facts}\n\nБез действий в YouGile: {', '.join(idle) or 'нет'}\n\n"
              "Напиши:\n1) Одну строку «Итог: …» — главное по компании за день, до 25 слов.\n"
              "2) По строке на каждого человека с действиями, в том же порядке: «Имя — конспект», до 15 слов: "
              "чем занимался и что сделал.\n"
              "Людей без действий не перечисляй, их бот добавит сам. Без заголовков и пустых строк.")
    return await _text(prompt, 1500)


async def _text(prompt, max_tokens):
    resp = await client.messages.create(model=MODEL, max_tokens=max_tokens, system=BRIEF_SYSTEM,
                                        messages=[{"role": "user", "content": prompt}])
    return "\n".join(b.text for b in resp.content if b.type == "text").strip()
