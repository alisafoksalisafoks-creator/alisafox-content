"""Сигналы по остаткам Ozon: пора отгрузить, пересорт между кластерами, платное хранение.

Аналитика только предупреждает и называет срок. Сколько штук везти в какой кластер, считает бот поставок.
Скорость: заказы за 14 дней (наша история), по кластерам — в долях средней скорости Ozon за 28 дней,
с поправкой на сезонность категории (bot/seasonality.json).
"""
import json
import os
from datetime import date, timedelta

LEAD_DAYS = 7      # от отгрузки до приёмки на складе Ozon, как в боте поставок
SOON_DAYS = 14     # «пора отгрузить», если отгрузить надо в ближайшие 14 дней (запас < 21 дня)
EXCESS_DAYS = 60   # запас дольше 60 дней — перебор, риск платного хранения
RESORT_MIN = 10    # пересорт — если в «лишнем» кластере лежит хотя бы 10 шт
MIN_SPEED = 0.15   # кластер, где продаётся меньше ~3 шт за 20 дней, в «пора отгрузить» не попадает
HORIZON = 180      # дальше полугода прогноз не считаем
SPEED_DAYS = 14

SEASON_FILE = os.path.join(os.path.dirname(__file__), "seasonality.json")
PAID_GRADES = {"GRADES_RED": "платное", "GRADES_CRITICAL": "платное, критично", "GRADES_YELLOW": "скоро платное"}


def load_season(path=SEASON_FILE):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)["categories"]
    except FileNotFoundError:
        return {}


def coef(season, type_name, d: date):
    return (season.get(type_name) or [1.0] * 12)[d.month - 1] or 1.0


def mean_coef(season, type_name, start: date, days):
    return sum(coef(season, type_name, start + timedelta(days=i)) for i in range(days)) / days


def runout_days(have, base, season, type_name, start: date):
    """Через сколько дней кончится запас, если продажи идут по сезону. None — продаж нет или хватит на полгода."""
    if base <= 0:
        return None
    left = have
    for i in range(HORIZON):
        left -= base * coef(season, type_name, start + timedelta(days=i))
        if left < 0:
            return i
    return None


def speeds(db, day: date, skus):
    """{sku: (шт/день за 14 дней, категория)} из нашей истории заказов."""
    start = day - timedelta(days=SPEED_DAYS - 1)
    rows = db.execute(
        "select d.sku, coalesce(sum(d.ordered_units), 0) u, max(s.type_name) t from daily d left join sku s using (sku) "
        "where d.day between ? and ? group by d.sku", (str(start), str(day))).fetchall()
    out = {r[0]: (r[1] / SPEED_DAYS, r[2] or "") for r in rows}
    return {s: out.get(s, (0.0, "")) for s in skus}


def build(db, stocks, day: date, season=None, turnover=None, catalog=None):
    """stocks — restock.cluster_stocks. Возвращает строки по кластерам и сводку по SKU с сигналами."""
    season = load_season() if season is None else season
    today = day + timedelta(days=1)
    sp = speeds(db, day, stocks)
    grades = {str(x["sku"]): x for x in (turnover or [])}
    rows, by_sku = [], {}
    for sku, clusters in stocks.items():
        speed14, tname = sp.get(sku, (0.0, ""))
        ads_total = sum(c["ads"] or 0 for c in clusters.values())
        # базовая скорость без сезона: делим на средний коэффициент того окна, за которое она посчитана
        if speed14 > 0:
            base = speed14 / mean_coef(season, tname, day - timedelta(days=SPEED_DAYS - 1), SPEED_DAYS)
        else:
            base = ads_total / mean_coef(season, tname, day - timedelta(days=27), 28)
        first = next(iter(clusters.values()))
        own = (catalog.get(first["offer_id"]) or {}).get("stock", 0) if catalog else None
        s = {"sku": sku, "offer_id": first["offer_id"], "name": first["name"], "type_name": tname,
             "speed": base * coef(season, tname, today), "speed_next": base * coef(season, tname, today + timedelta(days=30)),
             "stock": 0, "clusters": [], "ship": [], "excess": [], "own": own,
             "grade": (grades.get(sku) or {}).get("turnover_grade", ""),
             "turnover": (grades.get(sku) or {}).get("turnover")}
        for name, c in clusters.items():
            share = (c["ads"] or 0) / ads_total if ads_total else 0
            have = c["stock"] + c["transit"]
            days = runout_days(have, base * share, season, tname, today)
            r = {"sku": sku, "offer_id": c["offer_id"], "cluster": name, "stock": c["stock"], "transit": c["transit"],
                 "speed": base * share * coef(season, tname, today), "days": days, "grade": c["grade"]}
            weak = base * share * coef(season, tname, today) < MIN_SPEED
            if weak and have == 0:
                r["signal"], r["ship_by"] = "none", None
            elif share and c["stock"] == 0 and not c["transit"]:
                r["signal"], r["ship_by"] = "zero", today
            elif days is not None:
                r["ship_by"] = today + timedelta(days=days - LEAD_DAYS)
                r["signal"] = ("excess" if days > EXCESS_DAYS else "slow" if weak else "ok" if days >= LEAD_DAYS + SOON_DAYS
                               else "late" if days < LEAD_DAYS else "ship")
            elif have > 0:
                r["signal"], r["ship_by"] = "excess", None  # продаж нет или запаса больше чем на полгода
            else:
                r["signal"], r["ship_by"] = "none", None
            s["stock"] += have
            rows.append(r)
            s["clusters"].append(r)
            if r["signal"] in ("zero", "late", "ship"):
                s["ship"].append(r)
            elif r["signal"] == "excess":
                s["excess"].append(r)
        s["ship"].sort(key=lambda r: r["ship_by"])
        s["excess"].sort(key=lambda r: -r["stock"])
        s["ship_by"] = s["ship"][0]["ship_by"] if s["ship"] else None
        s["resort"] = bool(s["ship"]) and any(r["stock"] >= RESORT_MIN for r in s["excess"])
        by_sku[sku] = s
    return rows, by_sku


def to_ship(by_sku):
    """Артикулы, которые пора отгрузить: раньше срок — выше, при равном сроке — быстрее продаются."""
    return sorted((s for s in by_sku.values() if s["ship"]), key=lambda s: (s["ship_by"], -s["speed"]))


def resort(by_sku):
    """Пересорт: в одних кластерах перебор, в других кончается."""
    return sorted((s for s in by_sku.values() if s["resort"]), key=lambda s: (s["ship_by"], -s["speed"]))


def paid(by_sku):
    """Платное хранение по оценке Ozon (оборачиваемость SKU), сначала уже платное."""
    order = {"GRADES_CRITICAL": 0, "GRADES_RED": 1, "GRADES_YELLOW": 2}
    return sorted((s for s in by_sku.values() if s["grade"] in order and s["stock"] > 0),
                  key=lambda s: (order[s["grade"]], -(s["turnover"] or 0)))
