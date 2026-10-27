"""Соинвест Ozon: сколько процентов цены доплачивает Ozon (скидка покупателю за его счёт).

Источник — отчёт о реализации по дням: по каждой продаже наша цена (seller_price_per_instance)
и цена для покупателя (delivery_commission.price_per_instance). Соинвест = 1 − цена покупателя / наша цена.
Отчёт бывает только за закрытый день и только за последние 32 дня; день — день продажи (доставки), не заказа.
"""
import time
from datetime import timedelta

from bot.sources.moysklad import is_test

KEEP_DAYS = 15  # вчера, позавчера и две недели для «7 дней к прошлым 7»


def _ensure(db):
    db.execute("create table if not exists coinvest (day text, sku text, offer_id text, qty real, seller real, buyer real, "
               "primary key (day, sku))")
    db.execute("create table if not exists coinvest_days (day text primary key)")


def refresh(db, oz, end):
    """Докачивает дни по end включительно. Пустой отчёт за вчера не запоминаем: Ozon мог ещё не собрать его."""
    _ensure(db)
    have = {r[0] for r in db.execute("select day from coinvest_days")}
    for i in range(KEEP_DAYS - 1, -1, -1):
        d = end - timedelta(days=i)
        if str(d) in have:
            continue
        try:
            rows = oz.post("/v1/finance/realization/by-day", {"day": d.day, "month": d.month, "year": d.year}).get("rows", [])
        except Exception:
            continue  # отчёта за день ещё нет — попробуем в следующий раз
        agg = {}
        for r in rows:
            dc = r.get("delivery_commission")
            if not dc or not r.get("seller_price_per_instance"):
                continue
            a = agg.setdefault(str(r["item"]["sku"]), [r["item"].get("offer_id"), 0.0, 0.0, 0.0])
            a[1] += dc["quantity"]
            a[2] += r["seller_price_per_instance"] * dc["quantity"]
            a[3] += dc["price_per_instance"] * dc["quantity"]
        db.execute("delete from coinvest where day = ?", (str(d),))
        db.executemany("insert into coinvest values (?,?,?,?,?,?)", [(str(d), k, *v) for k, v in agg.items()])
        if agg:
            db.execute("insert or replace into coinvest_days values (?)", (str(d),))
        db.commit()
        time.sleep(0.5)


def pct(seller, buyer):
    return (1 - buyer / seller) * 100 if seller else None


def periods(end):
    """(название, (с, по), (с, по) для сравнения)."""
    return [("Вчера", (end, end), (end - timedelta(days=1),) * 2),
            ("7 дней", (end - timedelta(days=6), end), (end - timedelta(days=13), end - timedelta(days=7)))]


def _sums(db, start, stop, by):
    """{ключ: [шт, наша цена, цена покупателя]} за период; by = 'type' (категория) или 'sku'."""
    key = "s.type_id" if by == "type" else "c.sku"
    out = {}
    for k, offer, qty, seller, buyer in db.execute(
            f"select {key}, c.offer_id, c.qty, c.seller, c.buyer from coinvest c left join sku s on s.sku = c.sku "
            "where c.day between ? and ?", (str(start), str(stop))):
        if is_test(offer):
            continue
        a = out.setdefault(k or "-", [0.0, 0.0, 0.0])
        a[0] += qty
        a[1] += seller
        a[2] += buyer
    return out


def table(db, end, by="type", type_id=None, period="7 дней"):
    """Строки: ключ, продажи, соинвест и изменение в п.п. по периодам. Для by='sku' — только товары категории type_id."""
    rows = {}
    for name, cur, prev in periods(end):
        now, before = _sums(db, *cur, by), _sums(db, *prev, by)
        for k, (qty, seller, buyer) in now.items():
            r = rows.setdefault(k, {"key": k, "qty": 0.0, "seller": 0.0})
            p, b = pct(seller, buyer), before.get(k)
            r[name] = {"pct": p, "qty": qty, "seller": seller, "buyer": buyer,
                       "delta": p - pct(b[1], b[2]) if b and b[1] and p is not None else None}
            if name == period:
                r["qty"], r["seller"] = qty, seller
    out = list(rows.values())
    if by == "sku":
        skus = {r[0] for r in db.execute("select sku from sku where type_id = ?", (type_id,))}
        out = [r for r in out if r["key"] in skus]
    for r in out:
        s = db.execute("select * from sku where " + ("type_id" if by == "type" else "sku") + " = ? limit 1",
                       (r["key"],)).fetchone()
        r["name"] = (s["type_name"] if by == "type" else s["name"]) if s else "Без категории"
        r["offer_id"] = s["offer_id"] if s and by == "sku" else None
    return sorted((r for r in out if r.get(period)), key=lambda r: -r["seller"])


def daily(db, sku, end, days=14):
    """По дням для артикула: [(день, шт, наша цена за шт, цена покупателя за шт, соинвест %)]."""
    return [(r[0], r[1], r[2] / r[1], r[3] / r[1], pct(r[2], r[3])) for r in db.execute(
        "select day, sum(qty), sum(seller), sum(buyer) from coinvest where sku = ? and day > ? and day <= ? and qty > 0 "
        "group by day order by day desc", (sku, str(end - timedelta(days=days)), str(end)))]
