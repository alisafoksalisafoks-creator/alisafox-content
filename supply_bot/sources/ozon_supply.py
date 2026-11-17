"""Ozon Seller API: черновик и заявка на поставку FBO (кросс-докинг через наш пункт отгрузки).

Нужен отдельный ключ с доступом к поставкам: OZON_SUPPLY_API_KEY (и OZON_SUPPLY_CLIENT_ID, если отличается).
Черновик в кабинете не виден и живёт 30 минут. Заявку бот создаёт только после «да» владельца на конкретный план
(решение владельца 05.11.2026). Лимиты Ozon: черновик и заявка — 2 раза в минуту, 50 в час.
"""
import os
import time
from datetime import date, timedelta

from .ozon import OzonClient

SUPPLY_TYPE = "CROSSDOCK"
POLL_SECONDS = 10
POLL_TRIES = 18          # до 3 минут ждём расчёт черновика


def enabled():
    return bool(os.environ.get("OZON_SUPPLY_API_KEY", "").strip())


class SupplyClient(OzonClient):
    def __init__(self):
        super().__init__(os.environ.get("OZON_SUPPLY_CLIENT_ID") or os.environ["OZON_CLIENT_ID"],
                         os.environ["OZON_SUPPLY_API_KEY"])

    # --- справочники ---
    def macro_ids(self):
        """{название кластера: macrolocal_cluster_id}."""
        res = self.post("/v1/cluster/list", {"cluster_type": "CLUSTER_TYPE_OZON"})
        return {c.get("name", ""): int(c.get("macrolocal_cluster_id") or 0) for c in res.get("clusters", [])}

    # --- черновик ---
    def create_draft(self, macro_id, items, drop_off_id):
        """items: [(sku, qty)]. Возвращает (draft_id, errors)."""
        body = {
            "cluster_info": {"macrolocal_cluster_id": macro_id,
                             "items": [{"sku": int(sku), "quantity": int(q)} for sku, q in items]},
            "deletion_sku_mode": "PARTIAL",
            "delivery_info": {"type": "DROPOFF",
                              "drop_off_warehouse": {"warehouse_id": int(drop_off_id), "warehouse_type": "CROSS_DOCK"}},
        }
        res = self.post("/v1/draft/crossdock/create", body)
        return res.get("draft_id"), res.get("errors") or []

    def draft_info(self, draft_id, sleep=time.sleep):
        """Ждём, пока Ozon посчитает черновик. Возвращает ответ /v2/draft/create/info."""
        res = {}
        for _ in range(POLL_TRIES):
            res = self.post("/v2/draft/create/info", {"draft_id": int(draft_id)})
            if res.get("status") in ("SUCCESS", "FAILED"):
                return res
            sleep(POLL_SECONDS)
        return res

    def bundle(self, bundle_id):
        return self._bundle(bundle_id)

    def timeslots(self, draft_id, macro_id, warehouse_id=None, days=14, today=None):
        """[(from, to)] — свободные слоты на нашем пункте отгрузки, ближайшие days дней."""
        today = today or date.today()
        sel = {"macrolocal_cluster_id": macro_id}
        if warehouse_id:
            sel["storage_warehouse_id"] = int(warehouse_id)
        res = self.post("/v2/draft/timeslot/info", {
            "draft_id": int(draft_id), "supply_type": SUPPLY_TYPE, "selected_cluster_warehouses": [sel],
            "date_from": str(today + timedelta(days=1)), "date_to": str(today + timedelta(days=days))})
        days_ = (((res.get("result") or {}).get("drop_off_warehouse_timeslots") or {}).get("days")) or []
        return [(t["from_in_timezone"], t["to_in_timezone"]) for d in days_ for t in d.get("timeslots", [])]

    # --- заявка: только после «да» владельца ---
    def create_supply(self, draft_id, macro_id, warehouse_id, slot):
        sel = {"macrolocal_cluster_id": macro_id}
        if warehouse_id:
            sel["storage_warehouse_id"] = int(warehouse_id)
        res = self.post("/v2/draft/supply/create", {
            "draft_id": int(draft_id), "supply_type": SUPPLY_TYPE, "selected_cluster_warehouses": [sel],
            "timeslot": {"from_in_timezone": slot[0], "to_in_timezone": slot[1]}})
        return res.get("error_reasons") or []

    def supply_status(self, draft_id, sleep=time.sleep):
        res = {}
        for _ in range(POLL_TRIES):
            res = self.post("/v2/draft/supply/create/status", {"draft_id": int(draft_id)})
            if res.get("status") in ("SUCCESS", "FAILED"):
                return res
            sleep(POLL_SECONDS)
        return res


def parse_info(info):
    """Склады черновика: [{id, name, state, reason, bundle_id, restricted_bundle_id}] и ошибки текстом."""
    whs = []
    for cl in info.get("clusters") or []:
        for w in cl.get("warehouses") or []:
            st = w.get("availability_status") or {}
            sw = w.get("storage_warehouse") or {}
            whs.append({"id": sw.get("warehouse_id"), "name": sw.get("name", ""), "state": st.get("state", ""),
                        "reason": st.get("invalid_reason", ""), "bundle_id": w.get("bundle_id"),
                        "restricted_bundle_id": w.get("restricted_bundle_id"), "score": w.get("total_score") or 0})
    order = {"FULL_AVAILABLE": 0, "PARTIAL_AVAILABLE": 1, "NOT_AVAILABLE": 2}
    whs.sort(key=lambda w: (order.get(w["state"], 3), -w["score"]))
    errors = []
    for e in info.get("errors") or []:
        for v in e.get("items_validation") or []:
            for r in v.get("rejected_items") or []:
                errors.append(f"SKU {r.get('sku')}: " + ", ".join(r.get("reasons") or []))
        if e.get("error_message") and e["error_message"] not in ("UNSPECIFIED", "ITEMS_VALIDATION"):
            errors.append(e["error_message"])
    return whs, errors


STATE_RU = {"FULL_AVAILABLE": "примет всё", "PARTIAL_AVAILABLE": "примет часть", "NOT_AVAILABLE": "не примет"}
REASON_RU = {
    "PARTIAL_MATRIX_AVAILABLE": "не может принять часть товаров",
    "NOT_AVAILABLE_MATRIX": "не может принять эти товары",
    "NOT_AVAILABLE_RANK": "недоступен из-за рейтинга",
    "NOT_AVAILABLE_ROUTE": "нет маршрута",
    "NOT_AVAILABLE_TIMESLOT_FOR_DROP_OFF_POINT": "нет слотов на нашем пункте отгрузки",
    "NOT_AVAILABLE_TIMESLOT_FOR_STORAGE_WAREHOUSE": "нет слотов на складе",
    "NOT_AVAILABLE_TIMESLOT_FOR_BOTH_WAREHOUSES": "нет слотов",
    "NOT_AVAILABLE_TIMESLOT_NO_REASON": "нет слотов",
}


def split(asked, accepted):
    """asked/accepted: {артикул: шт}. Возвращает (берут {арт: шт}, остаток {арт: шт})."""
    take = {o: min(q, accepted.get(o, 0)) for o, q in asked.items() if accepted.get(o, 0) > 0}
    rest = {o: q - take.get(o, 0) for o, q in asked.items() if q - take.get(o, 0) > 0}
    return take, rest
