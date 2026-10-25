"""Оценка чистой прибыли по артикулу за последние два месяца — для пометки «рекомендую к заказу».

Период — с 1-го числа прошлого месяца по вчера. Продажи и комиссия — точно по SKU из отчёта о реализации Ozon:
закрытый прошлый месяц — месячным отчётом, текущий — по дням (по дням Ozon отдаёт только последние 32 дня). Услуги Ozon (логистика, реклама,
хранение, эквайринг) в API есть только суммой по магазину, поэтому делятся: логистика — на штуку,
остальное — пропорционально выручке. Себестоимость — МойСклад, налог — УСН 7% от положительной прибыли.
"""
import time
from collections import defaultdict
from datetime import timedelta

from bot.morning import AD_SERVICES, OVERHEAD, TAX
from bot.sources.moysklad import is_test

PER_UNIT = {"logistics", "reverse_logistics", "courier_client_reinvoice", "delivery_to_handover_place_by_ozon",
            "drop_off_processing", "drop_off_processing_by_partners", "cross_docking"}


def _ensure(db):
    db.execute("create table if not exists realization (day text, sku text, offer_id text, qty real, total real, "
               "ret_qty real, ret_total real, primary key (day, sku))")
    db.execute("create table if not exists realization_days (day text primary key)")


def _aggregate(rows):
    agg = defaultdict(lambda: [None, 0.0, 0.0, 0.0, 0.0])
    for r in rows:
        a = agg[str(r["item"]["sku"])]
        a[0] = r["item"].get("offer_id")
        if r.get("delivery_commission"):
            a[1] += r["delivery_commission"]["quantity"]
            a[2] += r["delivery_commission"]["total"]
        if r.get("return_commission"):
            a[3] += r["return_commission"]["quantity"]
            a[4] += r["return_commission"]["total"]
    return agg


def _store(db, key, rows):
    db.executemany("insert or replace into realization values (?,?,?,?,?,?,?)",
                   [(key, sku, *a) for sku, a in _aggregate(rows).items()])
    db.execute("insert or replace into realization_days values (?)", (key,))
    db.commit()


def period(end):
    """С 1-го числа прошлого месяца по end."""
    return (end.replace(day=1) - timedelta(days=1)).replace(day=1), end


def refresh(db, oz, end, today=None):
    """Докачивает отчёты о реализации за период; возвращает ключи (месяц 'YYYY-MM' или день), из которых считать."""
    _ensure(db)
    today = today or end + timedelta(days=1)
    start, _ = period(end)
    have = {r[0] for r in db.execute("select day from realization_days")}
    month = f"{start:%Y-%m}"
    if month not in have:
        try:
            res = oz.post("/v2/finance/realization", {"month": start.month, "year": start.year}, retries=2)
            _store(db, month, res.get("result", {}).get("rows", []))
            have.add(month)
        except Exception:  # месячный отчёт готов не сразу после конца месяца — тогда берём по дням, что дают
            pass
    first = start if month not in have else (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return ([month] if month in have else []) + refresh_days(db, oz, first, end, today, have)


def refresh_days(db, oz, start, end, today=None, have=None):
    """Отчёты о реализации по дням (Ozon отдаёт только последние 32 дня); возвращает дни, что есть в базе."""
    _ensure(db)
    today = today or end + timedelta(days=1)
    have = have if have is not None else {r[0] for r in db.execute("select day from realization_days")}
    keys, d = [], start
    while d <= end:
        if str(d) not in have and (today - d).days <= 31:
            res = oz.post("/v1/finance/realization/by-day", {"day": d.day, "month": d.month, "year": d.year})
            _store(db, str(d), res.get("rows", []))
            have.add(str(d))
            time.sleep(0.5)
        if str(d) in have:
            keys.append(str(d))
        d += timedelta(days=1)
    return keys


def services(oz, start, end, no_ads=False):
    """Услуги Ozon за период суммой по магазину: (на штуку, на рубль выручки) — обе отрицательные.
    no_ads — без рекламы: её тогда считают по артикулам из Performance API."""
    unit = rub = 0.0
    while start <= end:  # баланс отдаётся не больше чем за месяц — запрашиваем по календарным месяцам
        stop = min(end, (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1))
        d = oz.post("/v1/finance/balance", {"date_from": str(start), "date_to": str(stop)})
        for s in d["cashflows"].get("services", []):
            if no_ads and s["name"] in AD_SERVICES:
                continue
            if s["name"] in PER_UNIT:
                unit += s["amount"]["value"]
            else:
                rub += s["amount"]["value"]
        start = stop + timedelta(days=1)
    return unit, rub


def by_sku(db, oz, catalog, end, start=None, ads=None):
    """{sku: {"profit", "units", "revenue", "cogs", "ads", "offer_id"}}. Без себестоимости — пропуск.
    По умолчанию — с 1-го числа прошлого месяца по end, реклама делится по выручке.
    start — свой период (не старше 32 дней); ads — {sku: расход} из Performance API вместо деления по выручке."""
    if start is None:
        keys, start = refresh(db, oz, end), period(end)[0]
    else:
        keys = refresh_days(db, oz, start, end)
    rows = db.execute(f"select sku, max(offer_id), sum(qty), sum(total), sum(ret_qty), sum(ret_total) from realization "
                      f"where day in ({','.join('?' * len(keys))}) group by sku", keys).fetchall()
    units_all = sum(r[2] - r[4] for r in rows) or 1
    rev_all = sum(r[3] - r[5] for r in rows) or 1
    unit_cost, rub_cost = services(oz, start, end, no_ads=ads is not None)
    out = {}
    for sku, offer, qty, total, rqty, rtotal in rows:
        cost = (catalog.get(offer) or {}).get("cost")
        if is_test(offer) or not cost or cost <= 0:
            continue
        cost *= 1 + OVERHEAD
        units, rev = qty - rqty, total - rtotal
        ad = (ads or {}).get(sku, 0.0)
        pre = rev + unit_cost * units / units_all + rub_cost * rev / rev_all - units * cost - ad
        out[sku] = {"profit": pre - max(0, pre) * TAX, "units": units, "revenue": rev, "cogs": units * cost,
                    "ads": ad, "offer_id": offer}
    sold = {r[0] for r in rows}
    for sku, ad in (ads or {}).items():  # реклама крутилась, а продаж (доставок) за период нет — чистый минус
        if sku not in sold and ad > 0:
            out[sku] = {"profit": -ad, "units": 0, "revenue": 0.0, "cogs": 0.0, "ads": ad, "offer_id": None}
    return out
