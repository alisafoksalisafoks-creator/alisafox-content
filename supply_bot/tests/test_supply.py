"""Черновик и заявка на поставку: разбор ответов Ozon без сети."""
from datetime import date

from supply_bot.sources import ozon_supply


class Fake(ozon_supply.SupplyClient):
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def post(self, path, body, retries=5):
        self.calls.append((path, body))
        a = self.answers[path]
        return a.pop(0) if isinstance(a, list) else a


INFO = {"status": "SUCCESS", "clusters": [{"macrolocal_cluster_id": 7, "warehouses": [
    {"availability_status": {"state": "NOT_AVAILABLE", "invalid_reason": "NOT_AVAILABLE_ROUTE"},
     "storage_warehouse": {"warehouse_id": 1, "name": "АДЫГЕЙСК_РФЦ"}, "bundle_id": ""},
    {"availability_status": {"state": "PARTIAL_AVAILABLE", "invalid_reason": "PARTIAL_MATRIX_AVAILABLE"},
     "storage_warehouse": {"warehouse_id": 2, "name": "НОВОРОССИЙСК_РФЦ"}, "bundle_id": "b2", "total_score": 5}]}]}


def test_draft_body_and_polling():
    c = Fake({"/v1/draft/crossdock/create": {"draft_id": 55},
              "/v2/draft/create/info": [{"status": "IN_PROGRESS"}, INFO]})
    draft, errs = c.create_draft(7, [("424936924", 102)], 1020001649204000)
    body = c.calls[0][1]
    assert draft == 55 and not errs
    assert body["cluster_info"] == {"macrolocal_cluster_id": 7, "items": [{"sku": 424936924, "quantity": 102}]}
    assert body["delivery_info"]["drop_off_warehouse"] == {"warehouse_id": 1020001649204000, "warehouse_type": "CROSS_DOCK"}
    assert c.draft_info(55, sleep=lambda s: None)["status"] == "SUCCESS"


def test_parse_info_puts_available_first_and_split_keeps_rest():
    whs, errors = ozon_supply.parse_info(INFO)
    assert [w["name"] for w in whs] == ["НОВОРОССИЙСК_РФЦ", "АДЫГЕЙСК_РФЦ"] and not errors
    take, rest = ozon_supply.split({"OZ214": 102, "OZ299NEW": 50, "OZ632-L25": 20}, {"OZ214": 74, "OZ299NEW": 50})
    assert take == {"OZ214": 74, "OZ299NEW": 50} and rest == {"OZ214": 28, "OZ632-L25": 20}


def test_timeslots_and_supply_request():
    c = Fake({"/v2/draft/timeslot/info": {"result": {"drop_off_warehouse_timeslots": {"days": [
        {"date_in_timezone": "2026-11-09", "timeslots": [{"from_in_timezone": "2026-11-09T09:00:00",
                                                          "to_in_timezone": "2026-11-09T10:00:00"}]}]}}},
              "/v2/draft/supply/create": {"draft_id": 55},
              "/v2/draft/supply/create/status": {"status": "SUCCESS", "order_id": 999}})
    slots = c.timeslots(55, 7, 2, today=date(2026, 11, 5))
    req = c.calls[0][1]
    assert slots == [("2026-11-09T09:00:00", "2026-11-09T10:00:00")]
    assert req["date_from"] == "2026-11-06" and req["selected_cluster_warehouses"] == [
        {"macrolocal_cluster_id": 7, "storage_warehouse_id": 2}]
    assert c.create_supply(55, 7, 2, slots[0]) == []
    assert c.supply_status(55, sleep=lambda s: None)["order_id"] == 999
