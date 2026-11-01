"""Хранилище бота цен: доступ, каталог и ежедневные снимки (соинвест, место в выдаче). JSON-файлы в папке данных."""
import json
import os
from pathlib import Path

DATA = Path(os.environ.get("PRICE_BOT_DATA", Path(__file__).resolve().parent.parent / "data" / "price_bot"))
USERS = DATA / "users.json"
CATALOG = DATA / "catalog.json"
SNAPSHOTS = DATA / "snapshots.json"
HISTORY = DATA / "history.jsonl"
KEEP_DAYS = 14


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


# --- каталог ---

def catalog():
    """{"updated": "2026-10-27T09:00", "items": [...]}"""
    return _read(CATALOG, {"updated": None, "items": []})


def save_catalog(updated, items):
    _write(CATALOG, {"updated": updated, "items": items})


# --- снимки по дням ---

def snapshot(day):
    """{sku: {"soinvest": 12.5, "position": 7.2}} за день "2026-10-27"."""
    return {int(k): v for k, v in _read(SNAPSHOTS, {}).get(day, {}).items()}


def save_snapshot(day, values):
    """Дописывает значения за день, старше KEEP_DAYS дней — удаляет."""
    s = _read(SNAPSHOTS, {})
    cur = s.setdefault(day, {})
    for sku, v in values.items():
        cur.setdefault(str(sku), {}).update({k: x for k, x in v.items() if x is not None})
    for old in sorted(s)[:-KEEP_DAYS]:
        del s[old]
    _write(SNAPSHOTS, s)


# --- история изменений ---

def add_history(entries):
    """Дописывает записи {"ts", "who", "kind": "price"|"qty", "offer", "old", "new"} — по одной строке JSON."""
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def history():
    """Все записи, новые сверху."""
    try:
        lines = HISTORY.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            pass  # оборванная строка при сбое — пропускаем
    return out
