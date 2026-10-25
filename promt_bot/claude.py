"""Запрос к Claude: системный промпт промпт-инженера + история диалога + инструмент «тест с вопросами»."""
import os
from pathlib import Path

import anthropic

MODEL = os.environ.get("PROMT_BOT_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("PROMT_BOT_EFFORT", "high")
SYSTEM = (Path(__file__).resolve().parent / "system_prompt.md").read_text(encoding="utf-8")

ASK = "ask_questions"
TOOLS = [{
    "name": ASK,
    "description": (
        "Задать человеку уточняющие вопросы тестом: каждый вопрос придёт отдельным сообщением с кнопками "
        "вариантов, человек выбирает кликом или пишет свой ответ. Все уточняющие вопросы задавай только "
        "через этот инструмент, не текстом. Ответы вернутся результатом инструмента."),
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["intro", "questions"],
        "properties": {
            "intro": {"type": "string",
                      "description": "Одна строка перед тестом, например «Пара вопросов, чтобы сделать точно»."},
            "questions": {
                "type": "array",
                "description": "От 1 до 5 вопросов.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["question", "options", "recommended", "reason"],
                    "properties": {
                        "question": {"type": "string", "description": "Короткий вопрос."},
                        "options": {"type": "array", "items": {"type": "string"},
                                    "description": "2–4 варианта, каждый до 40 знаков, без букв А/Б в начале."},
                        "recommended": {"type": "integer",
                                        "description": "Номер рекомендуемого варианта, с 0."},
                        "reason": {"type": "string",
                                   "description": "Почему рекомендуешь, 5–10 слов."},
                    },
                },
            },
        },
    },
}]

client = anthropic.AsyncAnthropic(max_retries=3)


async def ask(messages):
    """Один ход модели. Ответ длинный, поэтому через поток; ждём итоговое сообщение целиком."""
    async with client.beta.messages.stream(
        model=MODEL,
        max_tokens=64000,
        system=SYSTEM,
        messages=messages,
        tools=TOOLS,
        tool_choice={"type": "auto"},
        thinking={"type": "adaptive"},
        output_config={"effort": EFFORT},
        cache_control={"type": "ephemeral"},  # системный промпт и история кэшируются между ходами
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # если модель откажет по ошибке фильтра, ответит запасная
    ) as stream:
        return await stream.get_final_message()


def echo(content):
    """Ответ модели в историю — без изменений, кроме правила для смены модели посреди ответа:
    до последнего блока fallback выкидываем thinking и tool_use, остальное оставляем как есть."""
    blocks = [b.to_dict() for b in content]
    last = max((i for i, b in enumerate(blocks) if b["type"] == "fallback"), default=-1)
    drop = {"thinking", "redacted_thinking", "tool_use"}
    return [b for i, b in enumerate(blocks) if i > last or b["type"] not in drop and b["type"] != "fallback"]


def text_of(content):
    return "\n\n".join(b["text"] for b in content if b["type"] == "text" and b["text"].strip())


def questions_of(content):
    """Вызов теста, если модель его сделала: (id вызова, вопросы, вступление)."""
    for b in content:
        if b["type"] == "tool_use" and b["name"] == ASK:
            return b["id"], b["input"]["questions"], b["input"].get("intro", "")
    return None
