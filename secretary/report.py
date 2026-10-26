"""Недельный отчёт менеджера: цифры неделя к неделе, минусовые, что отгрузить и выводы по правилам.

Неделя — последняя полная Пн–Вс по данным Ozon, сравнение с предыдущей. Все выводы — короткие строки
из правил ниже, без нейросети. Бот только читает API.
"""
import time
from datetime import date, timedelta

from bot import morning, profit, restock, signals, store
from bot.sources.moysklad import Catalog, is_test

FLAT = 5          # изменение меньше 5% — «на том же уровне»
DROP = 10         # падение показателя на 10% и больше — версия причины
POS_MOVE = 3      # позиция сдвинулась на 3 места и больше
RATING_LOW = 80   # контент-рейтинг ниже — карточку стоит доработать
SIGNAL_TTL = 3600


def week(day: date):
    """Последняя полная неделя Пн–Вс, которая кончается не позже day, и неделя перед ней."""
    end = day - timedelta(days=(day.weekday() + 1) % 7)
    cur = (end - timedelta(days=6), end)
    return cur, (cur[0] - timedelta(days=7), cur[0] - timedelta(days=1))


def period(day: date, n: int):
    """Последние n дней по day и столько же дней перед ними."""
    cur = (day - timedelta(days=n - 1), day)
    return cur, (cur[0] - timedelta(days=n), cur[0] - timedelta(days=1))


def change(cur, old):
    return (cur - old) / old * 100 if old else None


def pct(a, b):
    return a / b * 100 if b else 0.0


def _in(skus):
    return f"({','.join('?' * len(skus))})"


def totals(db, skus, start, end):
    skus = list(skus) or [""]
    row = db.execute(f"select {', '.join(f'coalesce(sum({m}), 0) {m}' for m in store.METRICS if m != 'position_category')} "
                     f"from daily where day between ? and ? and sku in {_in(skus)}",
                     [str(start), str(end), *skus]).fetchone()
    t = dict(row)
    pos = db.execute(f"select avg(position_category) from daily where day between ? and ? and sku in {_in(skus)} "
                     "and position_category > 0", [str(start), str(end), *skus]).fetchone()[0]
    t["position"] = pos
    return t


def kpis(t):
    """Показатели для сводки и выводов."""
    return {
        "orders": t["ordered_units"], "revenue": t["revenue"],
        "check": t["revenue"] / t["ordered_units"] if t["ordered_units"] else 0,
        "views": t["hits_view"], "ctr": pct(t["hits_view_pdp"], t["hits_view"]),
        "cart": pct(t["hits_tocart"], t["hits_view_pdp"]), "order_conv": pct(t["ordered_units"], t["hits_tocart"]),
        "cancel": pct(t["cancellations"], t["ordered_units"]), "returns": pct(t["returns"], t["delivered_units"]),
        # выкуп: доставили и не вернули — из всего, что дошло до развязки (доставка или отмена)
        "buyout": max(0.0, min(100.0, pct(t["delivered_units"] - t["returns"], t["delivered_units"] + t["cancellations"]))),
        "position": t["position"],
    }


def categories(db, skus):
    """{type_id: (название, [sku])} среди артикулов менеджера, по выручке за 4 недели."""
    skus = list(skus) or [""]
    rows = db.execute(f"select s.type_id, s.type_name, s.sku, coalesce(sum(d.revenue), 0) r from sku s "
                      f"left join daily d on d.sku = s.sku and d.day >= ? where s.sku in {_in(skus)} "
                      "group by s.sku", [str(store.last_day(db) - timedelta(days=27)), *skus]).fetchall()
    out = {}
    for tid, name, sku, rev in rows:
        c = out.setdefault(tid or "-", [name or "Без категории", [], 0.0])
        c[1].append(sku)
        c[2] += rev
    return {k: (v[0], v[1]) for k, v in sorted(out.items(), key=lambda kv: -kv[1][2])}


def sku_rows(db, skus):
    skus = list(skus) or [""]
    return db.execute(f"select * from sku where sku in {_in(skus)}", skus).fetchall()


# --- снимки цены и контент-рейтинга: история копится сама, чтобы видеть, что меняли в кабинете ---

def ensure(db):
    db.executescript("""
        create table if not exists prices (day text, offer_id text, price real, marketing_price real,
            min_price real, color text, ozon_min real, actions integer, primary key (day, offer_id));
        create table if not exists stocks (day text, sku text, fbo real, primary key (day, sku));
        create table if not exists ratings (day text, sku text, rating real, primary key (day, sku));
        create table if not exists notes (id integer primary key, day text, user_id integer, author text,
            sku text, offer_id text, text text);
        create table if not exists users (id integer primary key, name text, manager text, added text);
        create table if not exists sent (week text, chat integer, primary key (week, chat));
    """)


def snapshot(db, oz, day: date):
    """Цена, участие в акциях, индекс цен Ozon и контент-рейтинг на сегодня. Раз в день."""
    ensure(db)
    if db.execute("select 1 from prices where day = ? limit 1", (str(day),)).fetchone():
        return
    cursor, rows = "", []
    while True:
        res = oz.post("/v5/product/info/prices", {"filter": {"visibility": "ALL"}, "limit": 1000, "cursor": cursor})
        for it in res.get("items", []):
            p, idx = it.get("price") or {}, it.get("price_indexes") or {}
            rows.append((str(day), it["offer_id"], p.get("price"), p.get("marketing_seller_price"), p.get("min_price"),
                         idx.get("color_index"), (idx.get("ozon_index_data") or {}).get("min_price"),
                         int(bool(p.get("auto_action_enabled")))))
        cursor = res.get("cursor")
        if not res.get("items") or not cursor:
            break
    db.executemany("insert or replace into prices values (?,?,?,?,?,?,?,?)", rows)
    db.executemany("insert or replace into stocks values (?,?,?)",
                   [(str(day), sku, v["fbo"]) for sku, v in oz.stocks().items()])
    skus = [r[0] for r in db.execute("select sku from sku")]
    for i in range(0, len(skus), 100):
        res = oz.post("/v1/product/rating-by-sku", {"skus": [int(s) for s in skus[i:i + 100]]})
        db.executemany("insert or replace into ratings values (?,?,?)",
                       [(str(day), str(p["sku"]), p.get("rating")) for p in res.get("products", [])])
        time.sleep(1)
    db.commit()


def fbo(db, skus, day: date):
    """Остаток FBO суммой по артикулам на последний снимок не позже day; None — снимков ещё нет."""
    skus = list(skus) or [""]
    snap = db.execute("select max(day) from stocks where day <= ?", (str(day),)).fetchone()[0]
    if not snap or (day - date.fromisoformat(snap)).days > 3:
        return None
    return db.execute(f"select coalesce(sum(fbo), 0) from stocks where day = ? and sku in {_in(skus)}",
                      [snap, *skus]).fetchone()[0]


def price_change(db, offer_id, start, end):
    """(цена в начале, цена в конце) по снимкам, если они есть за период."""
    rows = db.execute("select price from prices where offer_id = ? and day between ? and ? order by day",
                      (offer_id, str(start - timedelta(days=7)), str(end))).fetchall()
    return (rows[0][0], rows[-1][0]) if len(rows) > 1 else None


def rating(db, sku):
    rows = db.execute("select rating from ratings where sku = ? order by day desc limit 8", (sku,)).fetchall()
    return (rows[0][0], rows[-1][0]) if rows else None


def price_index(db, offer_id):
    return db.execute("select color, ozon_min, price from prices where offer_id = ? order by day desc limit 1",
                      (offer_id,)).fetchone()


# --- минусовые и отгрузка: те же расчёты, что в «Аналитике», только за неделю ---

def losses(db, oz, perf, period, skus=None):
    """Прибыль по артикулам за неделю: {sku: {...}}; skus — только эти. Формула как в «Аналитике»."""
    start, end = period
    have = {r[0] for r in db.execute("select day from realization_days")} if db.execute(
        "select name from sqlite_master where name = 'realization_days'").fetchone() else set()
    if (date.today() - start).days > 31 and any(str(start + timedelta(days=i)) not in have
                                                for i in range((end - start).days + 1)):
        return None  # отчёт о реализации по дням Ozon отдаёт только за 32 дня, старее — только если накопили
    revenue = {r[0]: r[1] for r in db.execute("select sku, sum(revenue) from daily where day between ? and ? group by sku",
                                              (str(start), str(end)))}
    ads = perf.spend_by_sku(start, end, revenue)
    rows = profit.by_sku(db, oz, Catalog(), end, start=start, ads=ads)
    return {k: v for k, v in rows.items() if (skus is None or k in skus) and not is_test(v.get("offer_id") or "")}


def stock_signals(db, oz, day, skus):
    """Сигналы по остаткам (пора отгрузить / пересорт / платное) для артикулов — как в «Аналитике», без штук."""
    skus = [s for s in skus if db.execute("select 1 from daily where sku = ? and day >= ? and ordered_units > 0",
                                          (s, str(day - timedelta(days=29)))).fetchone()]
    if not skus:
        return {}
    _, by = signals.build(db, restock.cluster_stocks(oz, skus), day, turnover=morning.turnover(oz), catalog=Catalog())
    return by


# --- выводы по правилам ---

def fmt_pct(d):
    return "—" if d is None else f"{d:+.0f}%"


def trend(d, flat=FLAT):
    return "на том же уровне" if d is None or abs(d) < flat else ("рост " if d > 0 else "падение ") + f"{abs(d):.0f}%"


def reasons(cur, prev, extra):
    """Версии, почему изменились заказы. Каждая: (подтверждается, текст). Сначала подтверждённые.
    cur/prev — kpis(), extra — {"ads": (сейчас, было), "price": (было, стало), "rating": (сейчас, было),
    "index": (цвет, мин. цена Ozon, наша), "zero_clusters": n, "notes": [..]}."""
    out = []

    def add(ok, text):
        out.append((ok, text))

    a = extra.get("ads")
    if a:
        d = change(a[0], a[1])
        add(d is not None and d <= -DROP or a[1] > 0 and a[0] == 0,
            f"Реклама: расход {a[0]:,.0f} ₽ против {a[1]:,.0f} ₽ ({fmt_pct(d)})".replace(",", " "))
    d = change(cur["views"], prev["views"])
    add(d is not None and d <= -DROP, f"Показы: {cur['views']:,.0f} ({fmt_pct(d)})".replace(",", " "))
    if cur["position"] and prev["position"]:
        move = cur["position"] - prev["position"]
        add(move >= POS_MOVE, f"Позиция в поиске и каталоге: {prev['position']:.0f} → {cur['position']:.0f}")
    d = cur["ctr"] - prev["ctr"]
    add(prev["ctr"] and d / prev["ctr"] * 100 <= -DROP, f"CTR (показ → карточка): {prev['ctr']:.1f}% → {cur['ctr']:.1f}%")
    d = cur["cart"] - prev["cart"]
    add(prev["cart"] and d / prev["cart"] * 100 <= -DROP,
        f"Конверсия в корзину: {prev['cart']:.1f}% → {cur['cart']:.1f}%")
    add(cur["cancel"] - prev["cancel"] >= 5 or cur["returns"] - prev["returns"] >= 5,
        f"Отмены {prev['cancel']:.0f}% → {cur['cancel']:.0f}%, возвраты {prev['returns']:.0f}% → {cur['returns']:.0f}%")
    p = extra.get("price")
    if p and p[0]:
        dp = change(p[1], p[0])
        add(dp is not None and dp >= FLAT, f"Цена: {p[0]:.0f} → {p[1]:.0f} ₽ ({fmt_pct(dp)})")
    else:
        dc = change(cur["check"], prev["check"])
        add(dc is not None and dc >= FLAT, f"Средняя цена продажи: {prev['check']:.0f} → {cur['check']:.0f} ₽ ({fmt_pct(dc)})")
    idx = extra.get("index")
    if idx and idx[0] and idx[0] not in ("WITHOUT_INDEX", "GREEN", "SUPER"):
        cheaper = f", мин. цена у других на Ozon {idx[1]:.0f} ₽" if idx[1] else ""
        add(True, f"Индекс цен Ozon: {INDEX.get(idx[0], idx[0])}{cheaper}")
    z = extra.get("zero_clusters")
    if z is not None:
        add(z > 0, f"Остатки: {'обнулились в ' + str(z) + ' кл.' if z else 'на складах есть'}")
    r = extra.get("rating")
    if r:
        add(r[0] < RATING_LOW or r[0] < r[1] - 2, f"Контент-рейтинг карточки: {r[0]:.0f}" +
            (f" (было {r[1]:.0f})" if r[1] != r[0] else ""))
    for n in extra.get("notes", []):
        add(True, f"Менеджер отметил: {n}")
    return sorted(out, key=lambda x: not x[0])


INDEX = {"RED": "невыгодный", "YELLOW": "умеренный", "GREEN": "выгодный", "SUPER": "супер"}


def category_line(name, cur, prev, n_loss=0, n_ship=0):
    """Одна строка вывода по категории."""
    d = change(cur["orders"], prev["orders"])
    parts = [f"{name}: заказы {trend(d)}"]
    if d is not None and d <= -FLAT:
        why = [t for ok, t in reasons(cur, prev, {}) if ok][:2]
        if why:
            parts.append("вероятно: " + "; ".join(w.split(":")[0].lower() for w in why))
    if n_loss:
        parts.append(f"в минусе {n_loss} арт.")
    if n_ship:
        parts.append(f"пора отгрузить {n_ship} арт.")
    return ", ".join(parts) + "."


RUNNING = "CAMPAIGN_STATE_RUNNING"


def active_ads(perf, start, end, revenue):
    """{sku: расход ₽} только по активным сейчас кампаниям — для раздела «Реклама» (решение владельца 2026-10-26).
    Деление как в PerformanceClient.spend_by_sku: «Оплата за заказ» — по заказам, кампании за клики — по выручке."""
    camps = {c["id"]: c for c in perf.get("/api/client/campaign")["list"] if c.get("state") == RUNNING}
    spend = {}
    for r in perf.get("/api/client/statistics/daily/json", dateFrom=str(start), dateTo=str(end))["rows"]:
        if r["id"] in camps:
            spend[r["id"]] = spend.get(r["id"], 0.0) + float(r["moneySpent"].replace(",", "."))
    out, cpo = {}, False

    def split(money, skus):
        base = sum(revenue.get(s, 0) for s in skus)
        for s in skus:
            out[s] = out.get(s, 0.0) + money * (revenue.get(s, 0) / base if base else 1 / len(skus))

    for cid, money in spend.items():
        if money <= 0:
            continue
        if camps[cid].get("advObjectType") == "SKU":
            skus = [str(p["sku"]) for p in perf.get(f"/api/client/campaign/{cid}/v2/products").get("products", [])]
            split(money, skus or list(revenue))
        else:
            cpo = True
    if cpo:
        for s, v in perf.cpo_by_sku(start, end).items():
            out[s] = out.get(s, 0.0) + v
    return out


# --- отчёт по исключениям: пишем только о том, что скачет (владелец 2026-10-26) ---
ALERT = {
    "orders": 15,       # заказы упали на 15% и больше (или выросли на 50% — скачок вверх)
    "orders_up": 50,
    "views": 20,        # показы −20%
    "position": 20,     # средняя позиция хуже на 20% и минимум на 3 места …
    "position_top": 60,  # … и только если товар был в первых 60 (глубже места мало что значат)
    "drr": 15,          # ДРР выше 15% …
    "drr_up": 3,        # … и вырос на 3 п.п.
    "ads_up": 30,       # расход рекламы +30% при заказах, которые не выросли хотя бы вдвое меньше
    "profit": 15,       # чистая прибыль −15% или ушла в минус
    "cancel": 5,        # отмены +5 п.п.
    "buyout": 5,        # выкуп −5 п.п.
}
MIN_ORDERS = {"sku": 10, "category": 20}  # меньше заказов за оба периода — проценты не считаем, это шум
MIN_RUB = 500       # изменения прибыли и рекламы меньше 500 ₽ — не тревога
MAX_ALERTS = 3      # у артикула не больше трёх главных тревог


def alerts(c, p, level="sku"):
    """Короткие тревоги по показателям c/p (summary из бота), самые важные первыми. Пусто — писать не о чем."""
    out = []  # (важность, текст): меньше — важнее
    big = max(c["orders"], p["orders"]) >= MIN_ORDERS[level]
    cp, pp = c.get("profit"), p.get("profit")
    if cp is not None and pp is not None and abs(cp - pp) >= MIN_RUB:
        if cp < 0 <= pp:
            out.append((0, f"ЧП ушла в минус: {cp:,.0f} ₽".replace(",", " ")))
        elif pp > 0 and change(cp, pp) <= -ALERT["profit"]:
            out.append((1, f"ЧП −{pp - cp:,.0f} ₽ ({change(cp, pp):+.0f}%)".replace(",", " ")))
    d = change(c["orders"], p["orders"])
    ads, ads_p = c.get("ads") or 0, p.get("ads") or 0
    if max(ads, ads_p) >= MIN_RUB:
        da = change(ads, ads_p)
        if c["drr"] >= ALERT["drr"] and c["drr"] - p.get("drr", 0) >= ALERT["drr_up"]:
            out.append((2, f"ДРР {p.get('drr', 0):.0f}% → {c['drr']:.0f}%"))
        elif da is not None and da >= ALERT["ads_up"] and (d is None or d < da / 2):
            out.append((2, f"перерасход рекламы {da:+.0f}% при заказах {fmt_pct(d)}"))
    if big and d is not None and d <= -ALERT["orders"]:
        out.append((3, f"заказы {d:+.0f}%"))
    elif big and d is not None and d >= ALERT["orders_up"]:
        out.append((6, f"скачок заказов {d:+.0f}%"))
    pos, pos_p = c.get("position"), p.get("position")
    if pos and pos_p and pos_p <= ALERT["position_top"] and pos - pos_p >= max(3, pos_p * ALERT["position"] / 100):
        out.append((4, f"место {pos_p:.0f} → {pos:.0f}"))
    dv = change(c["views"], p["views"])
    if big and dv is not None and dv <= -ALERT["views"]:
        out.append((5, f"показы {dv:+.0f}%"))
    if big and c["cancel"] - p["cancel"] >= ALERT["cancel"]:
        out.append((7, f"отмены {p['cancel']:.0f}% → {c['cancel']:.0f}%"))
    if big and p["buyout"] and p["buyout"] - c["buyout"] >= ALERT["buyout"]:
        out.append((8, f"выкуп {p['buyout']:.0f}% → {c['buyout']:.0f}%"))
    out.sort()
    return [t for _, t in (out[:MAX_ALERTS] if level == "sku" else out)]


MIN_IMPACT = 1000   # артикул попадает в текст, если на кону хотя бы 1000 ₽ за период
TOP_CATS = 6        # в тексте не больше 6 категорий, остальное — в картинке и Excel
TOP_SKUS = 3


def impact(c, p):
    """Сколько рублей на кону: потеря ЧП + лишняя реклама + 10% от потерянных заказов ₽ (грубая маржа)."""
    lost_profit = max(0.0, (p.get("profit") or 0) - (c.get("profit") or 0)) if c.get("profit") is not None else 0.0
    extra_ads = max(0.0, (c.get("ads") or 0) - (p.get("ads") or 0) * max(1.0, c["revenue"] / p["revenue"] if p["revenue"] else 1.0))
    lost_orders = max(0.0, p["revenue"] - c["revenue"]) * 0.1
    return lost_profit + extra_ads + lost_orders
