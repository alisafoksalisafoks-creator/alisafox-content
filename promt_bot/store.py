"""Хранилище бота: кто имеет доступ и история диалогов. Обычные JSON-файлы в папке данных."""
import json
import os
from pathlib import Path

DATA = Path(os.environ.get("PROMT_BOT_DATA", Path(__file__).resolve().parent.parent / "data" / "promt_bot"))
USERS = DATA / "users.json"
CHATS = DATA / "chats"


def _read(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)  # атомарно: при сбое старый файл цел


# --- доступ ---

def users():
    """{id: имя} — кому владелец открыл бота. Сам владелец сюда не входит, у него доступ всегда."""
    return {int(k): v for k, v in _read(USERS, {}).items()}


def add_user(user_id, name):
    u = users()
    u[int(user_id)] = name
    _write(USERS, u)


def remove_user(user_id):
    u = users()
    u.pop(int(user_id), None)
    _write(USERS, u)


# --- диалоги ---

def empty_chat(mode=None):
    # pending — открытый тест: вопросы от модели и ответы, которые человек уже выбрал
    return {"mode": mode, "messages": [], "pending": None}


def chat(chat_id):
    return _read(CHATS / f"{chat_id}.json", None) or empty_chat()


def save_chat(chat_id, data):
    _write(CHATS / f"{chat_id}.json", data)
