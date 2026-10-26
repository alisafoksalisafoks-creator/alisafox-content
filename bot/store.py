"""Локальное хранилище истории Ozon (SQLite): продажи и воронка по SKU и дням.

Первый запуск тянет 120 дней, дальше каждый раз обновляются последние 5 дней:
Ozon дописывает заказы и доставки задним числом.
"""
import os
import sqlite3
from datetime import date, timedelta

from bot.sources.ozon import OzonClient

METRICS = ("ordered_units", "revenue", "hits_view", "hits_view_pdp", "hits_tocart",
           "cancellations", "delivered_units", "returns", "position_category")
DB_PATH = os.environ.get("BOT_DB", os.path.join(os.path.dirname(__file__), "data.sqlite"))
FIRST_LOAD_DAYS = 120
REFRESH_DAYS = 5


def connect(path=DB_PATH):
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(f"""
        create table if not exists daily (
            sku text, day text, type_id text,
            {", ".join(f"{m} real" for m in METRICS)},
            primary key (sku, day));
        create table if not exists sku (
            sku text primary key, name text, type_id text, type_name text, offer_id text);
        create index if not exists daily_type on daily (type_id, day);
        create index if not exists daily_day on daily (day);
    """)
    cols = {r[1] for r in db.execute("pragma table_info(daily)")}
    for m in METRICS:  # новая метрика в старой базе — колонка добавится, история догрузится в refresh
        if m not in cols:
            db.execute(f"alter table daily add column {m} real")
    return db


def last_day(db):
    row = db.execute("select max(day) from daily").fetchone()
    return date.fromisoformat(row[0]) if row[0] else None


def no_positions(db):
    return not db.execute("select 1 from daily where position_category > 0 limit 1").fetchone()


def refresh(db, until: date, oz: OzonClient | None = None, need_positions=False):
    oz = oz or OzonClient()
    have = None if need_positions and no_positions(db) else last_day(db)  # позиций ещё нет — перекачиваем всё

    start = until - timedelta(days=FIRST_LOAD_DAYS - 1) if not have else min(have, until) - timedelta(days=REFRESH_DAYS - 1)
    # кусками по 30 дней, чтобы не упираться в размер ответа
    d = start
    while d <= until:
        end = min(d + timedelta(days=29), until)
        rows = oz.analytics(d, end, ["sku", "descriptionType", "day"], METRICS)
        db.execute("delete from daily where day between ? and ?", (str(d), str(end)))
        db.executemany(
            f"insert or replace into daily values (?, ?, ?, {', '.join('?' * len(METRICS))})",
            [(r["sku"][0], r["day"][0], r["descriptionType"][0], *(r[m] for m in METRICS)) for r in rows])
        db.executemany(
            "insert into sku (sku, name, type_id, type_name) values (?, ?, ?, ?) "
            "on conflict(sku) do update set name = excluded.name, type_id = excluded.type_id, "
            "type_name = excluded.type_name",
            {(r["sku"][0], r["sku"][1], r["descriptionType"][0], r["descriptionType"][1]) for r in rows})
        db.commit()
        d = end + timedelta(days=1)


def set_offer_ids(db, stocks):
    db.executemany("update sku set offer_id = ? where sku = ?",
                   [(v["offer_id"], k) for k, v in stocks.items()])
    db.commit()


def find_sku(db, text):
    """Артикул продавца (OZ353NEW2) или SKU Ozon → строка из таблицы sku."""
    text = text.strip()
    return db.execute("select * from sku where sku = ? or upper(offer_id) = upper(?)", (text, text)).fetchone()


def categories(db, since: date):
    return db.execute(
        "select s.type_id, s.type_name, sum(d.revenue) revenue from daily d join sku s using (sku) "
        "where d.day >= ? group by s.type_id order by revenue desc", (str(since),)).fetchall()


def totals(db, start: date, end: date, sku=None, type_id=None):
    where, args = "day between ? and ?", [str(start), str(end)]
    if sku:
        where += " and sku = ?"
        args.append(sku)
    if type_id:
        where += " and type_id = ?"
        args.append(type_id)
    row = db.execute(f"select {', '.join(f'coalesce(sum({m}), 0) {m}' for m in METRICS)} "
                     f"from daily where {where}", args).fetchone()
    return dict(row)


def series(db, start: date, end: date, sku=None, type_id=None):
    where, args = "day between ? and ?", [str(start), str(end)]
    if sku:
        where += " and sku = ?"
        args.append(sku)
    if type_id:
        where += " and type_id = ?"
        args.append(type_id)
    got = {r[0]: r[1] for r in db.execute(
        f"select day, sum(ordered_units) from daily where {where} group by day", args)}
    n = (end - start).days + 1
    return [(start + timedelta(days=i), got.get(str(start + timedelta(days=i)), 0)) for i in range(n)]


def top_skus(db, start: date, end: date, type_id, limit=8):
    return db.execute(
        "select d.sku, s.name, s.offer_id, sum(d.ordered_units) units, sum(d.revenue) revenue "
        "from daily d join sku s using (sku) where d.day between ? and ? and d.type_id = ? "
        "group by d.sku order by revenue desc limit ?", (str(start), str(end), type_id, limit)).fetchall()
