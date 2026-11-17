"""Проверка правил расчёта на маленьком выдуманном магазине. Запуск: python -m pytest supply_bot/tests"""
from datetime import date, timedelta

from supply_bot import calc, config
from supply_bot.sources.sheets import parse_paid

TODAY = date(2026, 10, 24)   # суббота → отгрузка в понедельник 26.10
MSK1, MSK2, SPB, SPB2, KZN = "ВАТУТИНКИ_РФЦ", "НОГИНСК_РФЦ", "СПБ_ШУШАРЫ_РФЦ", "СПБ_БУГРЫ_РФЦ", "КАЗАНЬ_РФЦ_НОВЫЙ"
CLUSTERS = {MSK1: "Москва, МО и Дальние регионы", MSK2: "Москва, МО и Дальние регионы",
            SPB: "Санкт-Петербург и СЗО", SPB2: "Санкт-Петербург и СЗО", KZN: "Казань", "АЛМАТЫ_2_РФЦ": "Алматы"}


def sales(offer, wh, per_day, days=14):
    """Обычные заказы по 1 шт: per_day заказов в день."""
    return [(str(TODAY - timedelta(days=i)), offer, "1", wh, 1) for i in range(1, days + 1) for _ in range(per_day)]


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


def test_ship_date_is_next_mon_or_thu():
    assert calc.next_ship_date(TODAY) == date(2026, 10, 26)
    assert calc.next_ship_date(date(2026, 10, 26)) == date(2026, 10, 29)


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
    msk = by["Москва, МО и Дальние регионы"]
    # Москва — хит (140 шт за 14 дн): запас 5 дн → до 40 дн: 400 − 50 = 350 → 7 коробов по 50
    assert msk.need == 350 and msk.qty == 350 and msk.box_count == 7 and msk.ship_to == MSK1
    # Казань: 28 шт за 14 дн → до 45 дн: 90 − 10 = 80 → 80 шт (короб 50 далеко)
    assert by["Казань"].need == 80 and by["Казань"].qty == 80 and by["Казань"].ship_to == KZN


def test_box_near_need_goes_as_exact_box():
    # нужно 1000 (25 шт/дн × 40 дн), короб 900 в пределах 25% — везём ровно короб
    s = snap(sales("OZ1", MSK1, 25) + sales("OZ1", SPB, 25), ms={"OZ1": 10000},
             units={"OZ1": unit("OZ1")}, boxes={"OZ1": [("61×40×33", 900)]})
    plan = calc.build(s)
    for ln in plan.lines:
        assert ln.need == 1000 and ln.qty == 900 and ln.boxes == [("61×40×33", 900, 1)]


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
    by = {ln.offer: ln for ln in plan.lines}
    assert set(by) == {"OZ3", "OZ4"}                  # выкуп < 85% и платное — грузим (решение 02.11.2026)
    assert "OZ1" not in reasons                       # «не грузим» — молча не везём
    assert "новинка" in reasons["OZ2"]
    assert by["OZ3"].warn == "выкуп" and "85%" in by["OZ3"].flags[0]
    assert by["OZ4"].warn == "платное" and "платное хранение с" in by["OZ4"].flags[0] and plan.paid[0]["days"] == 10


def test_empty_cluster_signal():
    post = sales("OZ1", MSK1, 10) + sales("OZ2", MSK2, 3) + sales("OZ1", KZN, 1)
    s = snap(post, stocks=[("OZ1", MSK1, 0, 0), ("OZ2", MSK2, 500, 0), ("OZ1", KZN, 5, 0)],
             ms={"OZ1": 5000, "OZ2": 5000}, units={"OZ1": unit("OZ1"), "OZ2": unit("OZ2")})
    plan = calc.build(s)
    assert {x["cluster"] for x in plan.empty_whole} == {"Санкт-Петербург и СЗО"}


def test_manual_override_rounds_to_nearest_full_box():
    s = snap(sales("OZ1", MSK1, 10), ms={"OZ1": 10000}, units={"OZ1": unit("OZ1")},
             boxes={"OZ1": [("43×29×35", 200), ("31×23×19,5", 50)]})
    for typed, expect in ((237, 250), (220, 200), (0, 0)):
        cl = CLUSTERS[MSK1]
        ln = next(x for x in calc.build(s, {("OZ1", cl): typed}).lines if x.warehouse == cl)
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


def test_cluster_is_one_direction_outside_moscow():
    # два склада Питера — одна строка «кластер», спрос и остатки складываются
    s = snap(sales("OZ1", SPB, 6) + sales("OZ1", SPB2, 4), stocks=[("OZ1", SPB, 30, 0), ("OZ1", SPB2, 10, 0)],
             ms={"OZ1": 10000}, units={"OZ1": unit("OZ1")})
    plan = calc.build(s)
    spb = [ln for ln in plan.lines if ln.cluster == "Санкт-Петербург и СЗО"]
    assert len(spb) == 1 and spb[0].warehouse == "Санкт-Петербург и СЗО" and spb[0].ship_to == SPB
    assert spb[0].need == 10 * 40 - 40 and spb[0].qty == 350   # хит: до 40 дн, больше 100 — до 50


def test_moscow_is_one_cluster():
    s = snap(sales("OZ1", MSK1, 6) + sales("OZ1", MSK2, 2), stocks=[("OZ1", MSK2, 100, 0)],
             ms={"OZ1": 10000}, units={"OZ1": unit("OZ1")})
    plan = calc.build(s)
    msk = [ln for ln in plan.lines if ln.cluster.startswith("Москва")]
    assert len(msk) == 1 and msk[0].need == 8 * 40 - 100


def test_order_only_profitable_items_missing_on_our_stock():
    units = {o: unit(o) for o in ("OZ1", "OZ2", "OZ3", "OZ4")}
    post = sales("OZ1", MSK1, 5) + sales("OZ2", MSK1, 5) + sales("OZ3", MSK1, 5) + sales("OZ4", MSK1, 5)
    s = snap(post, ms={"OZ1": 0, "OZ2": 0, "OZ3": 100, "OZ4": 0}, units=units)
    s.profit = {"OZ1": {"per_unit": 50}, "OZ2": {"per_unit": -10}, "OZ3": {"per_unit": 30}}
    plan = calc.build(s)
    got = {x["offer"]: x for x in plan.order}
    assert set(got) == {"OZ1", "OZ3"}                     # минусовой OZ2 не заказываем
    assert got["OZ1"]["order"] == 5 * 40 and got["OZ3"]["order"] == 5 * 40 - got["OZ3"]["ship"]
    assert all(ln.offer != "OZ1" for ln in plan.lines)  # в распределении только то, что есть у нас
    why = {x["offer"]: x["why"] for x in plan.order_skipped}
    assert why["OZ2"] == "минусовой артикул" and why["OZ4"].startswith("прибыль не посчитать")


def test_profit_per_unit_from_ozon_reports():
    from supply_bot import profit
    # OZ1: 10 шт по 1000 ₽ после комиссии, 1 возврат; OZ2NEW (корень OZ2): 10 шт по 500 ₽
    real = {"OZ1": [10, 10000, 1, 1000], "OZ2NEW": [10, 5000, 0, 0], "OZ9": [5, 100, 0, 0]}
    got = profit.by_offer(real, {"OZ1": 600, "OZ2": 450}, unit_cost=-190, rub_cost=-1400)
    # услуги делятся на все проданные 24 шт и 14 100 ₽ выручки
    # OZ1: 9000 − 190×9/24 − 1400×9000/14100 − 9×600 = 2635.1; налог 7% → 2450.7
    assert round(got["OZ1"]["profit"], 1) == 2450.7 and round(got["OZ1"]["per_unit"], 1) == 272.3
    # OZ2: 5000 − 79.2 − 496.5 − 4500 = −75.6 — минус, налога нет
    assert round(got["OZ2NEW"]["profit"], 1) == -75.6
    assert "OZ9" not in got                                # нет себестоимости — не считаем


def test_box_quants_found_by_root_article():
    boxes = {"OZ378VBLUE": [("A", 300)], "OZ299NEW2": [("A", 200)], "OZ657-P10": [("A", 80)], "OZ378": [("A", 9)]}
    assert calc._sizes("OZ378VBLUENEW", "OZ378", boxes) == [("A", 300)]   # не строка OZ378
    assert calc._sizes("OZ299NEW", "OZ299", boxes) == [("A", 200)]
    from supply_bot.sources.sheets import norm
    assert calc._sizes(norm("OZ657-Р10"), "", boxes) == [("A", 80)]       # кириллическая Р в таблице


def test_box_rule_over_box_and_partial():
    sizes = [("A", 40), ("B", 15)]
    assert calc.pack(70, sizes) == ([(40, 1), (15, 2)], 0, 0)          # 40 + 2×15, ровно
    assert calc.pack(52, sizes) == ([(40, 1), (15, 1)], 0, 0)          # сверх короба 12 ≥ 7.5 → +15
    assert calc.pack(45, sizes) == ([(40, 1)], 5, 0)                   # сверх короба 5 < 7.5 → не везём
    assert calc.pack(6, [("C", 10), ("D", 7)]) == ([(7, 1)], 0, 0)     # 6 ≥ половины 7 → полный короб
    assert calc.pack(8, [("E", 40)]) == ([], 0, 10)                    # неполный короб, вверх до 10
    assert calc.pack(6, [("F", 15)]) == ([], 0, 10)                    # от 5 шт — неполный короб
    assert calc.pack(4, [("G", 40)]) == ([], 4, 0)                     # меньше 5 шт — не везём


def test_season_file_shipped_with_code():
    from supply_bot import data
    season = data.load_season()
    assert season["Кабель для мобильных устройств"][11] == 1.08
    assert 7 not in season["Камера обзорная для автомобиля"]   # «нет данных» → коэф. 1
    assert len(season) == 43


def test_wholesale_and_spikes():
    day = lambda i: str(TODAY - timedelta(days=i))
    usual = sales("OZ1", MSK1, 10)
    # опт: один заказ 40 шт — в спрос не идёт
    plan = calc.build(snap(usual + [(day(3), "OZ1", "1", MSK1, 40)], ms={"OZ1": 10000}, units={"OZ1": unit("OZ1")}))
    ln = plan.lines[0]
    assert ln.sales14 == 140 and ln.demand == "опт" and "опт: 1 заказ(а) 40 шт" in ln.flags[0]
    # разовый всплеск: +30 шт за день (40 при обычных 10) — с 05.11.2026 считаем по факту, подсвечиваем
    spike = [(day(9), "OZ1", "1", MSK1, 1)] * 30
    plan = calc.build(snap(usual + spike, ms={"OZ1": 10000}, units={"OZ1": unit("OZ1")}))
    ln = plan.lines[0]
    assert ln.sales14 == 170 and ln.demand == "всплеск" and plan.demand_notes[0]["kind"] == "всплеск"
    # два всплеска за последнюю неделю — пошёл спрос: считаем по факту
    grow = [(day(2), "OZ1", "1", MSK1, 1)] * 30 + [(day(5), "OZ1", "1", MSK1, 1)] * 30
    plan = calc.build(snap(usual + grow, ms={"OZ1": 10000}, units={"OZ1": unit("OZ1")}))
    ln = plan.lines[0]
    assert ln.sales14 == 200 and ln.demand == "рост" and "спрос растёт" in ln.flags[0]


def test_shortage_cluster_order_spb_moscow_then_rostov_kazan():
    s = snap(sales("OZ1", MSK1, 20) + sales("OZ1", KZN, 10) + sales("OZ1", SPB, 5),
             stocks=[("OZ1", MSK1, 100, 0), ("OZ1", SPB, 50, 0)], ms={"OZ1": 600}, units={"OZ1": unit("OZ1")})
    plan = calc.build(s)
    by = {ln.warehouse: ln.qty for ln in plan.lines}
    # потребность (все хиты, до 40 дн): Москва 700, СПб 150, Казань 400; на ФФ 600 — СПб и Москва первыми,
    # на обоих не хватает (850) — делим пропорционально; Казани не остаётся
    assert sum(by.values()) <= 600 and not by.get("Казань")
    assert by["Москва, МО и Дальние регионы"] > by["Санкт-Петербург и СЗО"] > 0
    assert all("не хватает ФФ: потребность всех кластеров 1250 шт больше свободного на ФФ 600 шт" in ln.flags[0]
               for ln in plan.lines)
    # хватает на СПб и Москву — остаток Казани (Ростова нет)
    s = snap(sales("OZ1", MSK1, 20) + sales("OZ1", KZN, 10) + sales("OZ1", SPB, 5),
             stocks=[("OZ1", MSK1, 100, 0), ("OZ1", SPB, 50, 0)], ms={"OZ1": 900}, units={"OZ1": unit("OZ1")})
    by = {ln.warehouse: ln.qty for ln in calc.build(s).lines}
    assert by == {"Москва, МО и Дальние регионы": 700, "Санкт-Петербург и СЗО": 150, "Казань": 50}


def test_need_owner_logic_target_days():
    # запас от 30 дн — не везём: OZ446-AC6 02.11 — 254 шт при 109 за 14 дн = 33 дн
    assert config.need(109, 254) == 0
    assert config.need(77, 86) == 134       # хит: до 40 дн — 40 × 5,5 − 86
    assert config.need(15, 31) == 23        # 29 дн — ещё везём; медленный: до 50 дн
    assert config.need(5, 8) == 10
    assert config.need(0, 0) == 0


def test_round_qty_owner_rules():
    assert config.round_qty(134, [300, 200, 150, 70]) == (150, 150)   # короб рядом (±25%) — ровно короб
    assert config.round_qty(80, [50, 20, 10]) == (80, 20)               # 4 короба по 20
    assert config.round_qty(80, [300, 200]) == (80, None)               # до 100 — до 10
    assert config.round_qty(230, [50]) == (250, 50)                     # 5 коробов по 50
    assert config.round_qty(230, [300]) == (250, None)                  # больше 100 — до 50
    assert config.round_qty(11, [100, 40]) == (30, None)                # минимум 30


def test_slow_item_orange_and_skip_over_80_days():
    # 9 шт за 14 дн, на Ozon 10 → нужно 23 → минимум 30 → после поставки 62 дн > 60 — оранжевый, решает владелец
    s = snap(sales("OZ1", MSK1, 1, days=9), stocks=[("OZ1", MSK1, 10, 0)], ms={"OZ1": 1000},
             units={"OZ1": unit("OZ1")}, boxes={"OZ1": [("40×30×30", 100)]})
    ln = calc.build(s).lines[0]
    assert ln.qty == 30 and ln.warn == "короб" and any("больше 60" in f for f in ln.flags)
    # 5 шт за 14 дн, на Ozon 8 → 30 шт дали бы 106 дн > 80 — не везём
    s = snap(sales("OZ1", MSK1, 1, days=5), stocks=[("OZ1", MSK1, 8, 0)], ms={"OZ1": 1000},
             units={"OZ1": unit("OZ1")}, boxes={"OZ1": [("40×30×30", 100)]})
    plan = calc.build(s)
    assert not plan.lines and "не везём" in plan.small[0]["why"] and "больше 80" in plan.small[0]["why"]


def test_empty_ozon_stock_gets_nearest_full_box():
    # на Ozon 0, 10 шт за 14 дн → нужно 36, короба 40 и 100 → полный короб 40 (56 дн)
    s = snap(sales("OZ1", MSK1, 1, days=10), ms={"OZ1": 1000},
             units={"OZ1": unit("OZ1")}, boxes={"OZ1": [("A", 100), ("B", 40)]})
    ln = calc.build(s).lines[0]
    assert ln.need == 36 and ln.qty == 40 and ln.boxes == [("B", 40, 1)]
    # 5 шт за 14 дн: короб 40 хватит на 112 дн (> 80) — не короб, а минимум 30; товар в кластере всё равно будет (05.11.2026)
    s = snap(sales("OZ1", MSK1, 1, days=5), ms={"OZ1": 1000},
             units={"OZ1": unit("OZ1")}, boxes={"OZ1": [("A", 100), ("B", 40)]})
    ln = calc.build(s).lines[0]
    assert ln.need == 18 and ln.qty == 30


def test_closed_warehouse_stock_and_supply_not_counted():
    # 05.11.2026: Адыгейск закрыт — поставка туда «в пути» потеряна, склад приёмки — другой склад кластера
    KRD, NVR = "АДЫГЕЙСК_РФЦ", "НОВОРОССИЙСК_РФЦ"
    CLUSTERS.update({KRD: "Краснодар", NVR: "Краснодар"})
    s = snap(sales("OZ1", KRD, 3) + sales("OZ1", NVR, 1), stocks=[("OZ1", KRD, 0, 100), ("OZ1", NVR, 5, 0)],
             ms={"OZ1": 1000}, units={"OZ1": unit("OZ1")})
    s.not_accepted = [{"order": "1", "warehouse": KRD, "cluster": "Краснодар", "slot": "2026-10-23T10:00:00Z",
                       "items": {"OZ1": 100}}]
    plan = calc.build(s)
    ln = next(x for x in plan.lines if x.cluster == "Краснодар")
    assert ln.transit == 0 and ln.ship_to == NVR and ln.qty > 0 and plan.stuck[0]["order"] == "1"


def test_ff_leftover_below_minimum_not_shipped():
    # Невинномысск 02.11, OZ522-HB1B: нужно 24, на нашем складе 2 шт — меньше минимума 30, не везём
    s = snap(sales("OZ1", KZN, 1, days=10), stocks=[("OZ1", KZN, 12, 0)], ms={"OZ1": 2},
             units={"OZ1": unit("OZ1")})
    plan = calc.build(s)
    assert not plan.lines and "меньше минимума 30" in plan.small[0]["why"]


def test_orange_with_25_days_waits_for_next_week():
    # Невинномысск 02.11, OZ462: 11 шт за 14 дн, на Ozon 23 (29 дн) → 30 шт дали бы 67 дн — не везём, ждём неделю
    s = snap(sales("OZ1", KZN, 1, days=11), stocks=[("OZ1", KZN, 23, 0)], ms={"OZ1": 1000},
             units={"OZ1": unit("OZ1")}, boxes={"OZ1": [("40×30×30", 100)]})
    plan = calc.build(s)
    assert not plan.lines and "следующей поставкой" in plan.small[0]["why"]
    # запас 20 дн (OZ159NEW2) — оранжевый, но везём
    s = snap(sales("OZ1", KZN, 1, days=10), stocks=[("OZ1", KZN, 14, 0)], ms={"OZ1": 1000},
             units={"OZ1": unit("OZ1")}, boxes={"OZ1": [("40×30×30", 100)]})
    ln = calc.build(s).lines[0]
    assert ln.qty == 30 and ln.warn == "короб"


def test_low_buyout_speed_by_buyouts():
    # 28 шт за 14 дн, выкуп 50% → скорость 1 шт/дн; на Ozon 10 → до 45 дн (14 выкупов — «до 20» → 50 дн): 50 − 10 = 40
    s = snap(sales("OZ1", MSK1, 2), stocks=[("OZ1", MSK1, 10, 0)], ms={"OZ1": 1000},
             units={"OZ1": unit("OZ1", buyout=0.5)})
    ln = calc.build(s).lines[0]
    # с 05.11.2026 выкуп только подсвечиваем: 28 заказов → 2 шт/дн, до 45 дн: 90 − 10 = 80
    assert ln.need == 80 and ln.warn == "выкуп" and not any("скорость по выкупу" in f for f in ln.flags)



def test_moscow_hits_to_37_days_others_below_25():
    # Москва 05.11: OZ555-UA43TC 113 шт за 14 дн, запас 36 дн — хит, везём; OZ622-Z69A 35 шт, запас 27 дн — ждём пн/чт
    assert config.need(113, 287, cluster=config.MOSCOW_CLUSTER) > 0
    assert config.need(35, 68, cluster=config.MOSCOW_CLUSTER) == 0
    assert config.need(35, 68, cluster="Казань") > 0
    assert config.need(113, 287, cluster="Казань") == 0


def test_moscow_hit_30_37_days_is_yellow():
    # 50 шт за 14 дн, в Москве 120 (34 дн) — везём, но жёлтым: можно подождать до пн/чт
    s = snap(sales("OZ1", MSK1, 5, days=10), stocks=[("OZ1", MSK1, 120, 0)], ms={"OZ1": 1000}, units={"OZ1": unit("OZ1")})
    ln = calc.build(s).lines[0]
    assert ln.qty > 0 and ln.warn == "ждать"


def test_stuck_supply_not_counted_in_transit():
    # Москва 05.11: поставка 2000065989644 со слотом 10.09 висит на приёмке — её «в пути» не считаем
    s = snap(sales("OZ1", KZN, 3), stocks=[("OZ1", KZN, 20, 100)], ms={"OZ1": 1000}, units={"OZ1": unit("OZ1")})
    s.not_accepted = [{"order": "200", "warehouse": "", "cluster": "Казань", "state": "ACCEPTANCE_AT_STORAGE_WAREHOUSE",
                       "slot": str(TODAY - timedelta(days=40)) + "T14:00:00Z", "items": {"OZ1": 100}}]
    plan = calc.build(s)
    ln = plan.lines[0]
    assert ln.transit == 0 and ln.qty > 0 and any("не принята" in f for f in ln.flags)
    assert plan.stuck[0]["order"] == "200"
    # свежая поставка (слот 5 дней назад) — считаем «в пути» как обычно
    s.not_accepted[0]["slot"] = str(TODAY - timedelta(days=5)) + "T14:00:00Z"
    assert not calc.build(s).lines


def test_several_big_boxes_preferred():
    # Москва 05.11, OZ470-MD60: нужно 81, короба 50/20/10 → 2×50, а не 8×10
    assert config.round_qty(81, [50, 20, 10, 50]) == (100, 50)


def test_smaller_box_ok_up_to_35_percent():
    # СПб 05.11, OZ522-HB1A: нужно 27, короб 20 — везём 20, довезём в понедельник
    assert config.round_qty(27, [200, 100, 60, 20]) == (20, 20)


def test_partial_status_target_35_days():
    # СПб 05.11, OZ560-GA3 «частично грузим»: 18 шт за 14 дн, на Ozon 22 → до 35 дн: 23 → короб 25
    assert config.need(18, 22, partial=True) == 23
    assert config.round_qty(23, [25]) == (25, 25)


def test_small_need_takes_one_small_box():
    assert config.round_qty(9, [20]) == (20, 20)
    assert config.round_qty(9, []) == (30, None)


def test_hit_absent_in_cluster_is_shipped_by_estimate():
    # OZ1 продаётся в Москве и СПб, в Казани его нет совсем; Казань — 25% продаж магазина (05.11.2026)
    base = sales("OZ1", MSK1, 2) + sales("OZ1", SPB, 2) + sales("OZ2", KZN, 2) + sales("OZ2", MSK1, 2)
    units = {"OZ1": unit("OZ1"), "OZ2": unit("OZ2")}
    s = snap(base, stocks=[("OZ1", MSK1, 100, 0), ("OZ1", SPB, 100, 0), ("OZ2", KZN, 100, 0), ("OZ2", MSK1, 100, 0)],
             ms={"OZ1": 1000, "OZ2": 1000}, units=units)
    kzn = next(ln for ln in calc.build(s).lines if ln.cluster == "Казань" and ln.offer == "OZ1")
    # оценка 56 × 0,25 / 0,75 ≈ 19 шт за 14 дн → до 50 дн: 67 → 70
    assert kzn.absent and kzn.need == 67 and kzn.qty == 70
    # та же модель другой карточкой уже есть в Казани — «нет в кластере» не считаем
    units["OZ1NEW"] = unit("OZ1NEW")
    s2 = snap(base, stocks=[("OZ1", MSK1, 100, 0), ("OZ1", SPB, 100, 0), ("OZ2", KZN, 100, 0),
                            ("OZ2", MSK1, 100, 0), ("OZ1NEW", KZN, 20, 0)],
              ms={"OZ1": 1000, "OZ2": 1000}, units=units)
    assert not any(ln.absent and ln.cluster == "Казань" for ln in calc.build(s2).lines)


def test_far_clusters_wait_longer_before_supply_is_lost():
    assert config.stuck_days("Дальний Восток") == 45 and config.stuck_days("Екатеринбург") == 30
    assert config.stuck_days("Казань") == 21
