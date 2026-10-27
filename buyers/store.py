"""База бота: все отправления Ozon за последние ~100 дней, список покупателей, пользователи бота.

Отправления храним все, а не только по списку: если добавить новый ID, его история уже есть.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))
KEEP_DAYS = 100

SCHEMA = """
create table if not exists lines (
  posting text, sku integer, buyer integer, ts text, day text, scheme text, status text,
  offer_id text, name text, qty integer, price real, old_price real, customer_price real,
  actions text, city text, primary key (posting, sku));
create index if not exists lines_buyer on lines (buyer, day);
create table if not exists buyers (id integer primary key, name text, added text);
create table if not exists users (id integer primary key, name text, added text);
create table if not exists meta (key text primary key, value text);
"""


def connect(path):
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("pragma journal_mode=wal")  # отчёты читают, пока фон пишет
    db.executescript(SCHEMA)
    return db


def msk_day(ts):
    t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return t.astimezone(MSK).date().isoformat()


def save_lines(db, lines):
    """Новые строки добавляет, у старых обновляет статус и цены. Цену покупателя FBO, если уже скачана, не затирает."""
    lines = list(lines)  # сначала скачать всё окно, потом писать — запись не держит базу во время запросов к Ozon
    n = 0
    for x in lines:
        db.execute(
            "insert into lines values (:posting,:sku,:buyer,:ts,:day,:scheme,:status,:offer_id,:name,:qty,:price,"
            ":old_price,:customer_price,:actions,:city) on conflict (posting, sku) do update set status=excluded.status,"
            " qty=excluded.qty, price=excluded.price, old_price=excluded.old_price, actions=excluded.actions,"
            " customer_price=coalesce(excluded.customer_price, lines.customer_price)",
            {**x, "day": msk_day(x["ts"])})
        n += 1
    db.commit()
    return n


def cleanup(db):
    edge = (datetime.now(MSK).date() - timedelta(days=KEEP_DAYS)).isoformat()
    db.execute("delete from lines where day < ?", (edge,))
    db.commit()


def get_meta(db, key):
    r = db.execute("select value from meta where key = ?", (key,)).fetchone()
    return r[0] if r else None


def set_meta(db, key, value):
    db.execute("insert or replace into meta values (?, ?)", (key, str(value)))
    db.commit()


# --- покупатели ---

def buyers(db):
    """[(id, имя)] по алфавиту имён."""
    return [(r[0], r[1]) for r in db.execute("select id, name from buyers order by name collate nocase, id")]


def buyer_name(db, bid):
    r = db.execute("select name from buyers where id = ?", (bid,)).fetchone()
    return r[0] if r else str(bid)


def add_buyer(db, bid, name):
    db.execute("insert into buyers values (?, ?, ?) on conflict (id) do update set name = excluded.name",
               (bid, name, datetime.now(MSK).strftime("%d.%m.%Y")))
    db.commit()


def del_buyer(db, bid):
    db.execute("delete from buyers where id = ?", (bid,))
    db.commit()


def buyer_lines(db, ids, start, end):
    q = ",".join("?" * len(ids))
    return [dict(r) for r in db.execute(
        f"select * from lines where buyer in ({q}) and day between ? and ? order by ts",
        (*ids, start.isoformat(), end.isoformat()))]
