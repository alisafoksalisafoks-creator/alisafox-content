"""Проверка правил расчёта на маленьком выдуманном магазине. Запуск: python -m pytest supply_bot/tests"""
from datetime import date, timedelta

from supply_bot import calc
from supply_bot.sources.sheets import parse_paid

TODAY = date(2026, 10, 24)   # суббота → отгрузка в понедельник 26.10
MSK1, MSK2, SPB, KZN = "ВАТУТИНКИ_РФЦ", "НОГИНСК_РФЦ", "СПБ_ШУШАРЫ_РФЦ", "КАЗАНЬ_РФЦ_НОВЫЙ"
CLUSTERS = {MSK1: "Москва, МО и Дальние регионы", MSK2: "Москва, МО и Дальние регионы",
            SPB: "Санкт-Петербург и СЗО", KZN: "Казань", "АЛМАТЫ_2_РФЦ": "Алматы"}


def sales(offer, wh, per_day, days=14):
    return [(str(TODAY - timedelta(days=i)), offer, "1", wh, per_day) for i in range(1, days + 1)]


def unit(art, status="грузим", buyout=0.9, rating=4.9):
    return {"article": art, "root": art.replace("NEW", ""), "status": status, "rating": rating,
            "category": "", "volume": None, "buyout": buyout}


def snap(postings, stocks=(), ms=None, units=None, paid=None, boxes=None):
    return calc.Snapshot(
        today=TODAY, postings=postings,
        wh_stocks=[{"offer_id": o, "warehouse": w, "cluster": CLUSTERS[w], "available": a, "transit": t}
                   for o, w, a, t in stocks],
        wh_cluster=CLUSTERS, product_stocks={},
        ms=[{"article": a, "stock": q, "reserve": 0} for a, q in (ms or {}).items()],
        unshipped=[], unit=units or {}, paid=paid or {}, boxes=boxes or {})


def test_ship_date_is_next_mon_wed_fri():
    assert calc.next_ship_date(TODAY) == date(2026, 10, 26)
    assert calc.next_ship_date(date(2026, 10, 26)) == date(2026, 10, 28)


def test_pick_boxes_full_mono_boxes_only():
    sizes = [("61×40×33", 300), ("31×23×19,5", 50)]
    assert calc.pick_boxes(330, sizes, cap=360) == [(300, 1), (50, 1)]   # остаток — коробом поменьше вверх
    assert calc.pick_boxes(330, sizes, cap=330) == [(300, 1)]            # вверх нельзя — вниз
    assert calc.pick_boxes(20, sizes, cap=20) == []


def test_need_per_warehouse_rounded_to_boxes_and_limited_by_our_stock():
    s = snap(sales("OZ1", MSK1, 10) + sales("OZ1", KZN, 2),
             stocks=[("OZ1", MSK1, 50, 0), ("OZ1", KZN, 10, 0)],
             ms={"OZ1": 10000}, units={"OZ1": unit("OZ1")}, boxes={"OZ1": [("31×23×19,5", 50)]})
    plan = calc.build(s)
    by = {ln.warehouse: ln for ln in plan.lines}
    # Москва: 10/дн × (7 + 30) − 50 = 320 → полные короба по 50
    assert by[MSK1].need == 320 and by[MSK1].qty % 50 == 0 and by[MSK1].box_count == by[MSK1].qty // 50
    # Казань: 2/дн × (9 + 30) − 10 = 68 → 50 или 100, только целые короба
    assert by[KZN].qty in (50, 100)


def test_box_never_split_between_directions():
    s = snap(sales("OZ1", MSK1, 10) + sales("OZ1", SPB, 10), ms={"OZ1": 10000},
             units={"OZ1": unit("OZ1")}, boxes={"OZ1": [("61×40×33", 600)]})
    plan = calc.build(s)
    for ln in plan.lines:
        assert all(size == 600 for _, size, _ in ln.boxes)
        assert ln.qty % 600 == 0


def test_ff_shortage_goes_to_purchase_and_never_exceeds_free_stock():
    s = snap(sales("OZ1", MSK1, 10) + sales("OZ1", SPB, 10), ms={"OZ1": 300},
             units={"OZ1": unit("OZ1")}, boxes={"OZ1": [("31×23×19,5", 50)]})
    plan = calc.build(s)
    assert sum(ln.qty for ln in plan.lines) <= 300
    assert plan.purchase and plan.purchase[0]["short"] > 0


def test_empty_our_stock_means_nothing_to_ship():
    s = snap(sales("OZ1", MSK1, 10), ms={"OZ1": 0}, units={"OZ1": unit("OZ1")})
    plan = calc.build(s)
    assert not plan.lines and plan.no_stock[0]["offer"] == "OZ1"


def test_statuses_buyout_and_paid_storage():
    units = {"OZ1": unit("OZ1", "не грузим"), "OZ2": unit("OZ2", "новинки"),
             "OZ3": unit("OZ3", buyout=0.80), "OZ4": unit("OZ4", "частично грузим")}
    paid = {"OZ4": [{"qty": 20, "date": TODAY + timedelta(days=10)}]}
    post = sum((sales(o, MSK1, 5) for o in units), [])
    plan = calc.build(snap(post, ms={o: 1000 for o in units}, units=units, paid=paid))
    reasons = {x["offer"]: x["reason"] for x in plan.decide}
    assert not plan.lines
    assert "OZ1" not in reasons                       # «не грузим» — молча не везём
    assert "новинка" in reasons["OZ2"]
    assert "85%" in reasons["OZ3"]
    assert "не отгружать" in reasons["OZ4"] and plan.paid[0]["days"] == 10


def test_mandatory_moscow_warehouse_gets_start_line_and_empty_cluster_signal():
    post = sales("OZ1", MSK1, 10) + sales("OZ2", MSK2, 3) + sales("OZ1", KZN, 1)
    s = snap(post, stocks=[("OZ1", MSK1, 0, 0), ("OZ2", MSK2, 500, 0), ("OZ1", KZN, 5, 0)],
             ms={"OZ1": 5000, "OZ2": 5000}, units={"OZ1": unit("OZ1"), "OZ2": unit("OZ2")})
    plan = calc.build(s)
    assert set(plan.mandatory["Москва, МО и Дальние регионы"]) == {MSK1, MSK2}
    start = [ln for ln in plan.lines if ln.offer == "OZ1" and ln.warehouse == MSK2]
    assert start and start[0].priority == "срочно"
    assert {x["cluster"] for x in plan.empty_whole} == {"Санкт-Петербург и СЗО"}


def test_manual_override_rounds_to_nearest_full_box():
    s = snap(sales("OZ1", MSK1, 10), ms={"OZ1": 10000}, units={"OZ1": unit("OZ1")},
             boxes={"OZ1": [("43×29×35", 200), ("31×23×19,5", 50)]})
    for typed, expect in ((237, 250), (220, 200), (0, 0)):
        ln = next(x for x in calc.build(s, {("OZ1", MSK1): typed}).lines if x.warehouse == MSK1)
        assert ln.manual and ln.qty == expect and "вручную" in ln.flags


def test_paid_sheet_takes_only_latest_block():
    rows = [["СКОРО СТАНЕТ"], ["OZ1", "20", "11.10"], [], ["20.10"],
            ["OZ1", "114", "01.11"], ["OZ2", "13", "уже"]]
    got = parse_paid(rows, TODAY)
    assert got["OZ1"] == [{"qty": 114, "date": date(2026, 11, 1)}]
    assert got["OZ2"][0]["date"] == TODAY


def test_foreign_warehouses_excluded():
    s = snap(sales("OZ1", "МИНСК_МПСЦ", 10) + sales("OZ1", MSK1, 1),
             stocks=[("OZ1", "АЛМАТЫ_2_РФЦ", 0, 0)], ms={"OZ1": 10000}, units={"OZ1": unit("OZ1")})
    s.wh_stocks.append({"offer_id": "OZ1", "warehouse": "МИНСК_МПСЦ", "cluster": "Беларусь",
                        "available": 0, "transit": 0})
    plan = calc.build(s)
    assert all(ln.warehouse not in ("МИНСК_МПСЦ", "АЛМАТЫ_2_РФЦ") for ln in plan.lines)
    assert "Алматы" not in {x["cluster"] for x in plan.empty_whole + plan.empty_clusters}
