"""Журнал действий сотрудников (SQLite): события из вебхуков YouGile и из логов задач."""
import json
import os
import sqlite3
import time
from pathlib import Path

DATA = Path(os.environ.get("YG_BOT_DATA", Path(__file__).resolve().parent.parent / "data" / "yougile_bot"))
DATA.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(DATA / "events.sqlite3", check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS events (
    uniq TEXT PRIMARY KEY,   -- защита от дублей: одно событие из вебхука и из лога пишется один раз
    ts INTEGER NOT NULL,     -- время события, мс
    user_id TEXT,            -- кто сделал
    kind TEXT NOT NULL,      -- move, done, add, message, file, ...
    task_id TEXT,
    chat_id TEXT,
    text TEXT,
    extra TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS subs (tg_chat INTEGER, yg_chat TEXT, PRIMARY KEY (tg_chat, yg_chat));  -- пересылка ответов
CREATE TABLE IF NOT EXISTS bot_users (tg_id INTEGER PRIMARY KEY, name TEXT, role TEXT);  -- role: view | full
""")


def meta(key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(key, value):
    db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))
    db.commit()


def collect_start():
    """С какого момента бот получает вебхуки: раньше этой даты сообщения в чатах видны не полностью."""
    v = meta("collect_start")
    if v is None:
        v = int(time.time() * 1000)
        set_meta("collect_start", v)
    return int(v)


def add(uniq, ts, user_id, kind, task_id=None, chat_id=None, text=None, extra=None):
    db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?)",
               (uniq, int(ts), user_id, kind, task_id, chat_id, (text or "")[:500],
                json.dumps(extra or {}, ensure_ascii=False)))
    db.commit()


def add_log(task_id, entries):
    """Записи лога задачи (/tasks/{id}/log) — в журнал."""
    for e in entries:
        if not e.get("timestamp") or not e.get("type"):
            continue
        extra = {k: e[k] for k in ("from", "to", "oldName", "newName", "users") if e.get(k)}
        add(f"log:{task_id}:{e['timestamp']}:{e['type']}", e["timestamp"], e.get("userId"), e["type"],
            task_id=task_id, extra=extra)


def between(start_ms, end_ms):
    rows = db.execute("SELECT ts, user_id, kind, task_id, chat_id, text, extra FROM events "
                      "WHERE ts >= ? AND ts < ? ORDER BY ts", (start_ms, end_ms)).fetchall()
    return [{"ts": r[0], "user_id": r[1], "kind": r[2], "task_id": r[3], "chat_id": r[4], "text": r[5],
             "extra": json.loads(r[6] or "{}")} for r in rows]


def cleanup(keep_days=400):
    db.execute("DELETE FROM events WHERE ts < ?", (int((time.time() - keep_days * 86400) * 1000),))
    db.commit()


# --- доступ к боту ---

def bot_users():
    """{tg_id: (имя, роль)}. Владелец сюда не входит, у него полный доступ всегда."""
    return {r[0]: (r[1], r[2]) for r in db.execute("SELECT tg_id, name, role FROM bot_users ORDER BY name")}


def set_bot_user(tg_id, name, role="view"):
    db.execute("INSERT OR REPLACE INTO bot_users VALUES (?, ?, ?)", (int(tg_id), name, role))
    db.commit()


def remove_bot_user(tg_id):
    db.execute("DELETE FROM bot_users WHERE tg_id=?", (int(tg_id),))
    db.commit()


# --- подписки на чаты: новые сообщения пересылаются в Telegram ---

def subscribe(tg_chat, yg_chat):
    db.execute("INSERT OR IGNORE INTO subs VALUES (?, ?)", (int(tg_chat), yg_chat))
    db.commit()


def unsubscribe(tg_chat, yg_chat):
    db.execute("DELETE FROM subs WHERE tg_chat=? AND yg_chat=?", (int(tg_chat), yg_chat))
    db.commit()


def subscribers(yg_chat):
    return [r[0] for r in db.execute("SELECT tg_chat FROM subs WHERE yg_chat=?", (yg_chat,))]
