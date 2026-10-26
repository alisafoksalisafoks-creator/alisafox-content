"""Поисковые запросы Ozon по SKU (Premium Plus): позиция, заказы, выручка, конверсия.

Общий источник для «продающих запросов» и отслеживания позиций: срез за каждый
день кладётся в таблицу query_stats, уже загруженные (day, sku) повторно не тянем.
Ozon отдаёт данные с отставанием: самый свежий доступный день — позавчера.
"""
import time
from datetime import date, timedelta

import httpx

from bot.sources.ozon import OzonClient

PATH = "/v1/analytics/product-queries/details"
LIMIT_BY_SKU = 15   # максимум, который принимает Ozon
PAGE_SIZE = 100
SKU_BATCH = 50
LAG_DAYS = 2
HISTORY_DAYS = 35  # глубже Ozon запросы не отдаёт (проверено 2026-10-26: самый ранний день — сегодня − 35)


def ensure_table(db):
    db.executescript("""
        create table if not exists query_stats (
            day text, sku text, query text,
            position real, orders real, gmv real, conv real,
            searches real, views real, query_index integer,
            primary key (day, sku, query));
        create table if not exists query_days (
            day text, sku text, primary key (day, sku));
        create index if not exists query_stats_sku on query_stats (sku, day);
    """)


def latest_day(today: date | None = None) -> date:
    return (today or date.today()) - timedelta(days=LAG_DAYS)


def _fetch_day(oz: OzonClient, skus, day: date):
    """Все запросы по пачке SKU за один день."""
    rows, page = [], 0
    while True:
        try:
            res = oz.post(PATH, {
                "date_from": f"{day}T00:00:00Z", "date_to": f"{day}T23:59:59Z",
                "skus": [str(s) for s in skus],
                "page": page, "page_size": PAGE_SIZE,
                "sort_by": "BY_SEARCHES", "sort_dir": "DESCENDING",
                "limit_by_sku": LIMIT_BY_SKU,
            })
        except httpx.HTTPStatusError as e:
            if "no data for the specified period" in e.response.text:
                return None
            raise
        rows += res.get("queries", [])
        page += 1
        if page >= res.get("page_count", 0):
            return rows
        time.sleep(1)


def fetch(db, skus, day: date, oz: OzonClient | None = None):
    """Догружает срез за день по SKU, которых ещё нет в базе. Возвращает число новых SKU."""
    ensure_table(db)
    have = {r[0] for r in db.execute("select sku from query_days where day = ?", (str(day),))}
    todo = [str(s) for s in skus if str(s) not in have]
    if not todo:
        return 0
    oz = oz or OzonClient()
    done = 0
    for i in range(0, len(todo), SKU_BATCH):
        batch = todo[i:i + SKU_BATCH]
        rows = _fetch_day(oz, batch, day)
        if rows is None:   # Ozon ещё не посчитал этот день
            break
        db.executemany(
            "insert or replace into query_stats values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(str(day), str(r["sku"]), r["query"], r.get("position"), r.get("order_count"),
              r.get("gmv"), r.get("view_conversion"), r.get("unique_search_users"),
              r.get("unique_view_users"), r.get("query_index")) for r in rows])
        db.executemany("insert or ignore into query_days values (?, ?)",
                       [(str(day), s) for s in batch])
        db.commit()
        done += len(batch)
        time.sleep(1)
    return done


def fetch_range(db, skus, start: date, end: date, oz: OzonClient | None = None):
    oz = oz or OzonClient()
    d = start
    while d <= end:
        fetch(db, skus, d, oz)
        d += timedelta(days=1)


def top_queries(db, sku, start: date, end: date, limit=15):
    """Запросы SKU за период по заказам: позиция — средняя по дням, взвешенная по ищущим."""
    return db.execute(
        "select query, sum(orders) orders, sum(gmv) gmv, sum(searches) searches, sum(views) views, "
        "sum(position * searches) / nullif(sum(searches), 0) position, count(*) days "
        "from query_stats where sku = ? and day between ? and ? "
        "group by query order by orders desc, searches desc limit ?",
        (str(sku), str(start), str(end), limit)).fetchall()


def positions(db, sku, start: date, end: date):
    """Дневной ряд позиций SKU: [(day, query, position)]."""
    return db.execute(
        "select day, query, position from query_stats where sku = ? and day between ? and ? "
        "order by day, query", (str(sku), str(start), str(end))).fetchall()


# Срез за период целиком (кнопка «Ключи»): Ozon сам сводит период и отдаёт топ-15 запросов SKU
# со средней позицией. Кэш по (start, end, sku): прошлые периоды не меняются.

def ensure_period_table(db):
    db.executescript("""
        create table if not exists query_period (
            start text, end text, sku text, query text,
            position real, orders real, gmv real, conv real, searches real, views real,
            primary key (start, end, sku, query));
        create table if not exists query_period_done (
            start text, end text, sku text, primary key (start, end, sku));
    """)


def _fetch_period(oz: OzonClient, skus, start: date, end: date):
    rows, page = [], 0
    while True:
        try:
            res = oz.post(PATH, {
                "date_from": f"{start}T00:00:00Z", "date_to": f"{end}T23:59:59Z",
                "skus": [str(s) for s in skus],
                "page": page, "page_size": PAGE_SIZE,
                "sort_by": "BY_SEARCHES", "sort_dir": "DESCENDING",
                "limit_by_sku": LIMIT_BY_SKU,
            })
        except httpx.HTTPStatusError as e:
            if "no data for the specified period" in e.response.text:
                return None
            raise
        rows += res.get("queries", [])
        page += 1
        if page >= res.get("page_count", 0):
            return rows
        time.sleep(1)


def last_ready_day(oz: OzonClient, sku, today: date | None = None) -> date:
    """Вчера, если Ozon уже посчитал запросы за вчера, иначе позавчера."""
    y = (today or date.today()) - timedelta(days=1)
    return y if _fetch_period(oz, [sku], y, y) is not None else y - timedelta(days=1)


def period(db, skus, start: date, end: date, oz: OzonClient | None = None):
    """Запросы SKU за период: {sku: [row, ...]}; недостающие SKU догружаются из Ozon."""
    ensure_period_table(db)
    key = (str(start), str(end))
    skus = [str(s) for s in skus]
    have = {r[0] for r in db.execute("select sku from query_period_done where start = ? and end = ?", key)}
    todo = [s for s in skus if s not in have]
    if todo and oz is None:
        oz = OzonClient()
    for i in range(0, len(todo), SKU_BATCH):
        batch = todo[i:i + SKU_BATCH]
        rows = _fetch_period(oz, batch, start, end)
        if rows is None:
            break
        db.executemany(
            "insert or replace into query_period values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(*key, str(r["sku"]), r["query"], r.get("position"), r.get("order_count"), r.get("gmv"),
              r.get("view_conversion"), r.get("unique_search_users"), r.get("unique_view_users")) for r in rows])
        db.executemany("insert or ignore into query_period_done values (?, ?, ?)", [(*key, s) for s in batch])
        db.commit()
        time.sleep(1)
    out = {s: [] for s in skus}
    marks = ",".join("?" * len(skus))
    for r in db.execute(f"select * from query_period where start = ? and end = ? and sku in ({marks})",
                        (*key, *skus)):
        out[r["sku"]].append(dict(r))
    return out


def from_days(db, skus, start: date, end: date):
    """Период из сохранённых дневных срезов (когда Ozon уже не отдаёт период целиком):
    {sku: [row]} с местом, средним по показам, и суммами заказов. Только если покрыто ≥ половины дней."""
    ensure_table(db)
    skus = [str(s) for s in skus]
    out = {s: [] for s in skus}
    if not skus:
        return out
    marks = ",".join("?" * len(skus))
    covered = {r[0]: r[1] for r in db.execute(
        f"select sku, count(*) from query_days where day between ? and ? and sku in ({marks}) group by sku",
        (str(start), str(end), *skus))}
    need = ((end - start).days + 1) / 2
    for r in db.execute(
            f"select sku, query, sum(position * views) / nullif(sum(views), 0) position, sum(orders) orders, "
            f"sum(gmv) gmv, sum(searches) searches, sum(views) views from query_stats "
            f"where day between ? and ? and sku in ({marks}) group by sku, query", (str(start), str(end), *skus)):
        if covered.get(r["sku"], 0) >= need:
            out[r["sku"]].append(dict(r))
    return out


def period_or_days(db, skus, start: date, end: date, oz: OzonClient | None = None, today: date | None = None):
    """Период из Ozon, а если он уже старше окна истории Ozon — из дневных срезов."""
    if start >= (today or date.today()) - timedelta(days=HISTORY_DAYS - 1):
        return period(db, skus, start, end, oz)
    return from_days(db, skus, start, end)
