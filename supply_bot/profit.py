"""Чистая прибыль на 1 шт по артикулу — бот считает сам, по фактическим отчётам Ozon. Для кнопки «Заказать».

Тот же расчёт, что у бота аналитики («Минусовые артикулы»), чтобы цифры совпадали.
Период — с 1-го числа прошлого месяца по вчера. Продажи и комиссия — по артикулу из отчёта о реализации Ozon:
закрытый прошлый месяц — месячным отчётом, текущий — по дням (по дням Ozon отдаёт только последние 32 дня).
Услуги Ozon (логистика, реклама, хранение, эквайринг) в API есть только суммой по магазину, поэтому делятся:
логистика — на штуку, остальное — пропорционально выручке. Себестоимость — «Мой склад», налог — УСН 7%
от положительной прибыли.
"""
import json
import re
import time
from collections import defaultdict
from datetime import timedelta

from . import config

TAX = 0.07
PER_UNIT = {"logistics", "reverse_logistics", "courier_client_reinvoice", "delivery_to_handover_place_by_ozon",
            "drop_off_processing", "drop_off_processing_by_partners", "cross_docking"}
# учёт в «Моём складе» по корневому артикулу: OZ204NEW2 на Ozon = OZ204; «ДБ OZ378V» — дубль карточки
RELAUNCH = re.compile(r"NEW\d*$")
DOUBLE = re.compile(r"^ДБ\s*")


def root(offer):
    return RELAUNCH.sub("", DOUBLE.sub("", (offer or "").strip().upper()))


def period(end):
    """С 1-го числа прошлого месяца по end."""
    return (end.replace(day=1) - timedelta(days=1)).replace(day=1), end


def _aggregate(rows):
    """Строки отчёта о реализации → {offer: [шт, ₽, шт возвратов, ₽ возвратов]}."""
    agg = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for r in rows:
        a = agg[r["item"].get("offer_id") or str(r["item"]["sku"])]
        if r.get("delivery_commission"):
            a[0] += r["delivery_commission"]["quantity"]
            a[1] += r["delivery_commission"]["total"]
        if r.get("return_commission"):
            a[2] += r["return_commission"]["quantity"]
            a[3] += r["return_commission"]["total"]
    return agg


def realization(oz, end, log=print):
    """{offer: [шт, ₽, возвр. шт, возвр. ₽]} за период. Отчёты кешируются в DATA_DIR/realization."""
    cache = config.DATA_DIR / "realization"
    cache.mkdir(parents=True, exist_ok=True)
    today = end + timedelta(days=1)
    start, _ = period(end)

    def load(key, fetch):
        path = cache / f"{key}.json"
        if not path.exists():
            path.write_text(json.dumps(_aggregate(fetch()), ensure_ascii=False))
        return json.loads(path.read_text())

    parts = []
    month = None
    try:
        month = load(f"{start:%Y-%m}", lambda: oz.post(
            "/v2/finance/realization", {"month": start.month, "year": start.year}, retries=2)["result"]["rows"])
        parts.append(month)
    except Exception:  # noqa: BLE001 — месячный отчёт готов не сразу после конца месяца — тогда по дням
        pass
    d = start
    while d <= end:
        if month is None or d.month != start.month:
            if (cache / f"{d}.json").exists() or (today - d).days <= 31:
                if not (cache / f"{d}.json").exists():
                    log(f"Ozon: отчёт о реализации за {d:%d.%m}")
                    time.sleep(0.5)
                parts.append(load(str(d), lambda d=d: oz.post(
                    "/v1/finance/realization/by-day", {"day": d.day, "month": d.month, "year": d.year}).get("rows", [])))
        d += timedelta(days=1)
    total = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for p in parts:
        for offer, a in p.items():
            total[offer] = [x + y for x, y in zip(total[offer], a)]
    return total


def services(oz, start, end):
    """Услуги Ozon за период суммой по магазину: (на штуку, на рубль выручки) — обе отрицательные."""
    unit = rub = 0.0
    while start <= end:  # баланс отдаётся не больше чем за месяц — по календарным месяцам
        stop = min(end, (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1))
        d = oz.post("/v1/finance/balance", {"date_from": str(start), "date_to": str(stop)})
        for s in d["cashflows"].get("services", []):
            if s["name"] in PER_UNIT:
                unit += s["amount"]["value"]
            else:
                rub += s["amount"]["value"]
        start = stop + timedelta(days=1)
    return unit, rub


def by_offer(real, costs, unit_cost, rub_cost):
    """{offer: {"per_unit", "profit", "units", "revenue"}}; без себестоимости или без продаж — пропуск."""
    units_all = sum(a[0] - a[2] for a in real.values()) or 1
    rev_all = sum(a[1] - a[3] for a in real.values()) or 1
    out = {}
    for offer, (qty, total, rqty, rtotal) in real.items():
        cost = costs.get(root(offer))
        units, rev = qty - rqty, total - rtotal
        if not cost or cost <= 0 or units <= 0:
            continue
        pre = rev + unit_cost * units / units_all + rub_cost * rev / rev_all - units * cost
        net = pre - max(0, pre) * TAX
        out[offer] = {"per_unit": net / units, "profit": net, "units": units, "revenue": rev}
    return out


def collect(oz, ms, end, log=print):
    """Прибыль по артикулам с 1-го числа прошлого месяца по end (вчера)."""
    real = realization(oz, end, log)
    log("Ozon: услуги за период (логистика, реклама, хранение)")
    unit_cost, rub_cost = services(oz, period(end)[0], end)
    log("«Мой склад»: себестоимость")
    return by_offer(real, ms.costs(), unit_cost, rub_cost)
