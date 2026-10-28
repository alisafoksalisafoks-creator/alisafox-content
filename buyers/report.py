"""Периоды, докачка отправлений и сводки по покупателям."""
import logging
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

from buyers import store
from buyers.store import MSK

log = logging.getLogger("buyers.report")
FIRST_LOAD_DAYS = 40     # при первом запуске: хватает на «Месяц» и «7 дней» на стыке месяцев
RECHECK_DAYS = 10        # раз в сутки перечитываем статусы за 10 дней — отмены приходят позже заказа
CANCELLED = {"cancelled"}
MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
          "ноябрь", "декабрь"]


def today():
    return datetime.now(MSK).date()


def period(kind, now: date | None = None):
    """(начало, конец, подпись). 7 дней — без сегодняшнего; месяц — с 1-го числа текущего месяца по сегодня."""
    t = now or today()
    if kind == "today":
        return t, t, f"сегодня, {t:%d.%m}"
    if kind == "7":
        s, e = t - timedelta(days=7), t - timedelta(days=1)
        return s, e, f"7 дней, {s:%d.%m}–{e:%d.%m}"
    s = t.replace(day=1)
    return s, t, f"{MONTHS[t.month - 1]}, {s:%d.%m}–{t:%d.%m}"


def month_label(now: date | None = None):
    return MONTHS[(now or today()).month - 1].capitalize()


# --- докачка ---

def _windows(since, to, step=timedelta(days=1)):
    while since < to:
        yield since, min(since + step, to)
        since += step


def sync(db, oz, full_from: datetime | None = None):
    """Докачивает отправления с прошлой докачки (с запасом 3 часа) до сейчас, окнами по суткам."""
    now = datetime.now(MSK)
    last = store.get_meta(db, "last_sync")
    if full_from:
        since = full_from
    elif last:
        since = datetime.fromisoformat(last) - timedelta(hours=3)
    else:
        since = datetime.combine(now.date() - timedelta(days=FIRST_LOAD_DAYS), datetime.min.time(), MSK)
    total = 0
    for a, b in _windows(since, now):
        total += store.save_lines(db, oz.lines(a, b))
        if not full_from:
            store.set_meta(db, "last_sync", b.isoformat())
    log.info("докачка с %s: %s строк", since, total)
    return total


def recheck(db, oz):
    """Раз в сутки: статусы за последние дни (отмены) и чистка старого."""
    start = datetime.combine(today() - timedelta(days=RECHECK_DAYS), datetime.min.time(), MSK)
    sync(db, oz, full_from=start)
    store.cleanup(db)
    store.set_meta(db, "last_recheck", today().isoformat())


def fill_customer_prices(db, oz, lines):
    """У FBO цену для покупателя докачиваем по одному отправлению — только для выбранных покупателей."""
    todo = sorted({x["posting"] for x in lines if x["scheme"] == "FBO" and x["customer_price"] is None})
    for i, number in enumerate(todo):
        if i:
            time.sleep(0.3)  # по одному заказу подряд Ozon отвечает 429 — небольшая пауза
        try:
            prices = oz.fbo_customer_prices(number)
        except Exception:
            log.exception("цена покупателя %s", number)
            continue
        for sku, cp in prices.items():
            if cp is not None:
                db.execute("update lines set customer_price = ? where posting = ? and sku = ?", (cp, number, sku))
    if todo:
        db.commit()
        for x in lines:
            if x["posting"] in todo:
                r = db.execute("select customer_price from lines where posting = ? and sku = ?",
                               (x["posting"], x["sku"])).fetchone()
                x["customer_price"] = r[0] if r else None


def keys_period(start, end):
    """Аналитика запросов Ozon готова только по позавчера: обрезаем период, «сегодня» → прошлые 7 дней."""
    edge = today() - timedelta(days=2)
    e = min(end, edge)
    s = start if start <= e else e - timedelta(days=6)
    return s, e


def top_keys(oz, skus, start, end, n=5):
    """{sku: [(запрос, заказов, место)]} — топ-n по заказам за период, только запросы с заказами."""
    by = defaultdict(list)
    for i in range(0, len(skus), 1000):
        for q in oz.queries(skus[i:i + 1000], start, end):
            if q.get("order_count"):
                by[q["sku"]].append((q.get("query", ""), int(q["order_count"]), q.get("position")))
    return {s: sorted(v, key=lambda t: -t[1])[:n] for s, v in by.items()}


POS_TTL = timedelta(hours=6)  # вчерашнее место Ozon досчитывает утром — перечитываем раз в 6 часов


def positions_period(kind):
    """Места есть только по вчера: 7 дней — как у отчётов, месяц — с 1-го по вчера."""
    s, e, _ = period(kind)
    y = today() - timedelta(days=1)
    e = min(e, y)
    s = min(s, e)
    return s, e


def sync_positions(db, oz, start, end):
    """Места всех SKU кабинета по дням, в базе. Докачиваем только недостающие дни и последние 2 дня раз в 6 часов."""
    have = {r[0] for r in db.execute("select distinct day from positions where day between ? and ?",
                                     (str(start), str(end)))}
    at = store.get_meta(db, "pos_at")
    stale = not at or datetime.now(MSK) - datetime.fromisoformat(at) > POS_TTL
    need = [start + timedelta(days=i) for i in range((end - start).days + 1)
            if str(start + timedelta(days=i)) not in have or (stale and start + timedelta(days=i) >= end - timedelta(days=1))]
    if not need:
        return
    for sku, day, pos, views in oz.positions(min(need), max(need)):
        db.execute("insert or replace into positions values (?, ?, ?, ?)", (sku, day, pos, views))
    db.commit()
    store.set_meta(db, "pos_at", datetime.now(MSK).isoformat())


def position_matrix(db, skus, start, end):
    """{sku: {день: место или None}}."""
    q = ",".join("?" * len(skus))
    out = {s: {} for s in skus}
    for sku, day, pos in db.execute(f"select sku, day, pos from positions where sku in ({q}) and day between ? and ?",
                                    (*skus, str(start), str(end))):
        out[sku][day] = pos
    return out


# --- сводки ---

def active(lines):
    return [x for x in lines if x["status"] not in CANCELLED]


def cancelled_qty(lines):
    return sum(x["qty"] for x in lines if x["status"] in CANCELLED)


def by_day(lines, start, end):
    """[(день, шт)] за каждый день периода, включая дни без покупок."""
    qty = defaultdict(int)
    for x in active(lines):
        qty[x["day"]] += x["qty"]
    days, d = [], start
    while d <= end:
        days.append((d, qty.get(d.isoformat(), 0)))
        d += timedelta(days=1)
    return days


def by_buyer(lines, ids):
    """{id: шт} — по всем покупателям списка, включая тех, кто ничего не купил."""
    qty = {i: 0 for i in ids}
    for x in active(lines):
        qty[x["buyer"]] = qty.get(x["buyer"], 0) + x["qty"]
    return qty


def details(lines):
    """[{buyer, offer_id, name, qty, sum, paid}] по артикулам: сумма в заказе и сколько заплатил покупатель.
    paid = None, если хоть по одной строке Ozon цену покупателя не отдал."""
    g = {}
    for x in active(lines):
        k = (x["buyer"], x["offer_id"] or str(x["sku"]))
        r = g.setdefault(k, {"buyer": x["buyer"], "sku": x["sku"], "offer_id": k[1], "name": x["name"], "qty": 0,
                             "sum": 0.0,
                             "paid": 0.0})
        r["qty"] += x["qty"]
        r["sum"] += x["price"] * x["qty"]
        if r["paid"] is not None:
            r["paid"] = None if x["customer_price"] is None else r["paid"] + x["customer_price"] * x["qty"]
    return sorted(g.values(), key=lambda r: (r["buyer"], -r["sum"]))
