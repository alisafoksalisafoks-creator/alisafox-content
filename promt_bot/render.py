"""Как ответ модели уходит в Telegram: куски до 4 000 знаков и блоки ``` отдельными файлами .md."""
import re

LIMIT = 4000  # у Telegram предел 4 096 знаков на сообщение
FENCE = re.compile(r"```[^\n`]*\n(.*?)```", re.S)
MIN_FILE = 300  # короткие блоки (пример строки, команда) файлом не шлём
INLINE = 3500  # блок длиннее этого в чат не дублируем: на телефоне удобнее открыть файл


def chunks(text, limit=LIMIT):
    """Режем по абзацам, длинный абзац — по строкам, длинную строку — как есть по лимиту."""
    out, cur = [], ""
    for part in re.split(r"(\n\n)", text):
        if len(cur) + len(part) <= limit:
            cur += part
            continue
        if cur.strip():
            out.append(cur.strip())
        cur = ""
        while len(part) > limit:
            cut = part.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            out.append(part[:cut].strip())
            part = part[cut:]
        cur = part
    if cur.strip():
        out.append(cur.strip())
    return out


def chat_text(text):
    """Текст для чата: очень длинные блоки ``` заменены пометкой, что они во вложенном файле."""
    return FENCE.sub(lambda m: "📎 Полный текст — во вложенном файле ниже."
                     if len(m.group(1).strip()) > INLINE else m.group(0), text)


def files(text):
    """[(имя файла, содержимое, подпись)] для каждого длинного блока ```. Скил — SKILL.md, остальное — promt_N.md."""
    out, n = [], 0
    for body in FENCE.findall(text):
        body = body.strip()
        if len(body) < MIN_FILE:
            continue
        if body.startswith("---") and re.search(r"^name:\s*\S", body, re.M):
            name = re.search(r"^name:\s*(\S+)", body, re.M).group(1)
            out.append(("SKILL.md", body + "\n", f"Скил {name}: загрузите в Claude в папке с именем {name}"))
        else:
            n += 1
            out.append((f"promt_{n}.md", body + "\n", "Тот же текст файлом"))
    return out
