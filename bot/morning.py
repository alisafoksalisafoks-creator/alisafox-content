"""Сценарий 1: утренняя сводка Ozon за вчера."""
import logging
import time
from datetime import date, timedelta

from bot import restock, signals, store
from bot.sources import moysklad
from bot.sources.performance import PerformanceClient

log = logging.getLogger(__name__)
TAX = 0.07  # УСН «доходы минус расходы»
OVERHEAD = 0.03  # косвенные расходы — 3% сверху на себестоимость (владелец 2026-10-25)

SERVICE_NAMES = {
    "logistics": "Логистика",
    "reverse_logistics": "Обратная логистика",
    "courier_client_reinvoice": "Доставка курьером",
    "cross_docking": "Кросс-докинг",
    "product_placement_in_ozon_warehouses": "Размещение (хранение)",
    "acquiring": "Эквайринг",
    "pay_per_click": "Реклама: оплата за клики",
    "promotion_with_cost_per_order": "Реклама: оплата за заказ",
    "partner_returns_cancellations_processing": "Обработка возвратов и отмен",
    "delivery_to_handover_place_by_ozon": "Доставка до места передачи",
    "drop_off_processing": "Обработка отгрузки (drop-off)",
    "drop_off_processing_by_partners": "Drop-off у партнёров",
    "packing_by_agents": "Упаковка агентами",
    "packing_package": "Упаковка",
    "booking_space_and_staff_for_partial_shipment": "Бронь места при неполной поставке",
    "product_disposal": "Утилизация",
    "label_brand_verified": "Подписка «Оригинал»",
    "ozon_warehouse_pickup": "Вывоз со склада Ozon",
    "ozon_warehouse_pickup_assortment": "Сборка вывоза",
    "processing_of_identified_surpluses_in_shipment": "Обработка излишков в поставке",
    "defect_fine_shipment_delay_rated": "Штраф: опоздание отгрузки",
}
AD_SERVICES = {"pay_per_click", "promotion_with_cost_per_order"}


def service_label(name):
    if name in SERVICE_NAMES:
        return SERVICE_NAMES[name]
    return ("Штраф: " if "fine" in name else "") + name.replace("_", " ")


def balance(oz, day):
    d = oz.post("/v1/finance/balance", {"date_from": str(day), "date_to": str(day)})
    cf = d["cashflows"]
    services = {s["name"]: s["amount"]["value"] for s in cf.get("services", [])}
    return {
        "accrued": d["total"]["accrued"]["value"],
        "sales": cf["sales"]["amount"]["value"],
        "commission": cf["sales"]["fee"]["value"] + cf["returns"]["fee"]["value"],
        "returns": cf["returns"]["amount"]["value"],
        "services": services,
    }


def turnover(oz):
    items, offset = [], 0
    while True:
        res = oz.post("/v1/analytics/turnover/stocks", {"limit": 1000, "offset": offset})
        items += res.get("items", [])
        if len(res.get("items", [])) < 1000:
            return items
        offset += 1000
        time.sleep(2)


def content_ratings(db, oz, day, skus):
    db.execute("create table if not exists rating (sku text, day text, rating real, primary key (sku, day))")
    for i in range(0, len(skus), 100):
        res = oz.post("/v1/product/rating-by-sku", {"skus": [int(s) for s in skus[i:i + 100]]})
        db.executemany("insert or replace into rating values (?, ?, ?)",
                       [(str(p["sku"]), str(day), p["rating"]) for p in res.get("products", [])])
    db.commit()
    prev = db.execute("select max(day) from rating where day < ?", (str(day),)).fetchone()[0]
    if not prev:
        return None
    return db.execute(
        "select n.sku, s.offer_id, s.name, o.rating old, n.rating new from rating n join rating o "
        "on o.sku = n.sku and o.day = ? left join sku s on s.sku = n.sku "
        "where n.day = ? and n.rating < o.rating order by n.rating - o.rating", (prev, str(day))).fetchall()


def build(db, oz, day: date):
    t = store.totals(db, day, day)
    t_prev = store.totals(db, day - timedelta(days=1), day - timedelta(days=1))
    t_week = store.totals(db, day - timedelta(days=7), day - timedelta(days=7))
    # выкуп по заказам, которые уже успели доехать: окно 37–8 дней назад
    b = store.totals(db, day - timedelta(days=37), day - timedelta(days=8))
    b_prev = store.totals(db, day - timedelta(days=44), day - timedelta(days=15))

    def buyout(x):
        return (x["delivered_units"] - x["returns"]) / x["ordered_units"] * 100 if x["ordered_units"] else 0

    fin, fin_prev = balance(oz, day), balance(oz, day - timedelta(days=1))
    perf = PerformanceClient()
    ads, ads_prev = perf.spend_by_group(day, day), perf.spend_by_group(day - timedelta(days=1), day - timedelta(days=1))
    spend = sum(g["spend"] for g in ads.values())
    spend_prev = sum(g["spend"] for g in ads_prev.values())

    turn = [x for x in turnover(oz) if not moysklad.is_test(x.get("offer_id"))]
    ending = sorted([x for x in turn if x["ads"] >= 0.5 and x["idc"] < 20], key=lambda x: -x["ads"])
    excess = sorted([x for x in turn if x["turnover_grade"] in ("GRADES_RED", "GRADES_CRITICAL") and x["current_stock"] > 0],
                    key=lambda x: -x["current_stock"])
    soon = [x for x in turn if x["turnover_grade"] == "GRADES_YELLOW" and x["current_stock"] > 0]

    top = [r[0] for r in db.execute(
        "select sku from daily where day >= ? group by sku order by sum(revenue) desc limit 300",
        (str(day - timedelta(days=29)),))]
    rating_drops = content_ratings(db, oz, day, top)
    if rating_drops:
        rating_drops = [r for r in rating_drops if not moysklad.is_test(r["offer_id"])]

    # себестоимость проданного: доставленные минус возвраты за день × себестоимость из МоегоСклада
    ms = moysklad.Catalog()

    def cogs_of(d):
        total, missing = 0.0, []
        for r in db.execute("select d.sku, s.offer_id, d.delivered_units - d.returns n from daily d "
                            "join sku s using (sku) where d.day = ? and d.delivered_units - d.returns != 0", (str(d),)):
            if moysklad.is_test(r["offer_id"]):
                continue
            c = (ms.get(r["offer_id"]) or {}).get("cost", 0)
            if c > 0:
                total += c * (1 + OVERHEAD) * r["n"]
            else:
                missing.append(r["offer_id"] or r["sku"])
        return total, missing

    def net(accrued, cogs):
        before_tax = accrued - cogs
        return before_tax - max(0, before_tax) * TAX

    cogs, no_cost = cogs_of(day)
    profit = net(fin["accrued"], cogs)
    profit_prev = net(fin_prev["accrued"], cogs_of(day - timedelta(days=1))[0])
    try:  # прибыль по артикулам за 2 месяца — для пометки «к заказу»
        from bot import profit as sku_profit
        profits = sku_profit.by_sku(db, oz, ms, day)
    except Exception:
        log.exception("прибыль по артикулам не посчиталась")
        profits = {}

    try:  # сигналы по кластерам: что пора отгрузить
        skus = [r[0] for r in db.execute("select sku from daily where day >= ? group by sku having sum(ordered_units) > 0",
                                         (str(day - timedelta(days=29)),))]
        _, sig = signals.build(db, restock.cluster_stocks(oz, skus), day, turnover=turn, catalog=ms)
        for k, v in sig.items():
            v["profit"] = (profits.get(k) or {}).get("profit")
        ship, resort = signals.to_ship(sig), signals.resort(sig)
    except Exception:
        log.exception("сигналы по остаткам не посчитались")
        ship, resort = None, []

    cats = db.execute(
        "select s.type_name, sum(case when d.day = ? then d.revenue end) cur, "
        "sum(case when d.day = ? then d.revenue end) wk, sum(case when d.day = ? then d.ordered_units end) units "
        "from daily d join sku s using (sku) where d.day in (?, ?) group by s.type_id order by cur desc limit 8",
        (str(day), str(day - timedelta(days=7)), str(day), str(day), str(day - timedelta(days=7)))).fetchall()

    return {
        "day": day, "orders": t, "cogs": cogs, "profit": profit, "profit_prev": profit_prev, "no_cost": no_cost, "orders_prev": t_prev, "orders_week": t_week,
        "buyout": buyout(b), "buyout_prev": buyout(b_prev),
        "fin": fin, "fin_prev": fin_prev, "ads": ads, "ads_prev": ads_prev,
        "spend": spend, "spend_prev": spend_prev,
        "drr": spend / t["revenue"] * 100 if t["revenue"] else 0,
        "drr_prev": spend_prev / t_prev["revenue"] * 100 if t_prev["revenue"] else 0,
        "ms": ms, "profits": profits, "ending": ending, "ship": ship, "resort": resort, "excess": excess, "soon": soon, "rating_drops": rating_drops, "cats": cats,
    }
