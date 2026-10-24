"""Ozon Seller API, только чтение. Ключи — из переменных окружения."""
import os
import time
from datetime import datetime, timedelta, timezone

import httpx

BASE = "https://api-seller.ozon.ru"
UNSHIPPED_STATES = ["DATA_FILLING", "READY_TO_SUPPLY"]


class OzonClient:
    def __init__(self, client_id=None, api_key=None):
        self.http = httpx.Client(base_url=BASE, timeout=90, headers={
            "Client-Id": (client_id or os.environ["OZON_CLIENT_ID"]).strip(),
            "Api-Key": (api_key or os.environ["OZON_API_KEY"]).strip(),
        })

    def post(self, path, body, retries=5):
        for attempt in range(retries):
            r = self.http.post(path, json=body)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt * 3)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()

    def product_stocks(self):
        """{sku: {offer_id, fbo, fbs}} по всем товарам: свободный остаток FBO и FBS."""
        out, cursor = {}, ""
        while True:
            res = self.post("/v4/product/info/stocks", {
                "filter": {"visibility": "ALL"}, "limit": 1000, "cursor": cursor})
            for item in res["items"]:
                for s in item["stocks"]:
                    rec = out.setdefault(str(s["sku"]), {"offer_id": item["offer_id"], "fbo": 0, "fbs": 0})
                    rec["fbo" if s["type"] == "fbo" else "fbs"] += s["present"] - s["reserved"]
            cursor = res.get("cursor")
            if not res["items"] or not cursor:
                return out

    def fbo_postings(self, since, to):
        """Отправления FBO без отмен: [(date, offer_id, sku, warehouse, qty)]."""
        rows, cursor = [], ""
        while True:
            res = self.post("/v3/posting/fbo/list", {
                "filter": {"since": since.strftime("%Y-%m-%dT00:00:00Z"),
                           "to": to.strftime("%Y-%m-%dT00:00:00Z")},
                "limit": 100, "cursor": cursor, "with": {"analytics_data": True}})
            for p in res.get("postings", []):
                if p.get("status") == "cancelled":
                    continue
                wh = (p.get("analytics_data") or {}).get("warehouse_name") or ""
                day = p["created_at"][:10]
                for pr in p["products"]:
                    rows.append((day, pr["offer_id"], str(pr["sku"]), wh, pr["quantity"]))
            cursor = res.get("cursor") or ""
            if not res.get("has_next") or not cursor:
                return rows

    def warehouse_stocks(self, skus):
        """Остатки по складам Ozon из /v1/analytics/stocks, по 100 SKU за запрос."""
        rows, skus = [], list(skus)
        for i in range(0, len(skus), 100):
            res = self.post("/v1/analytics/stocks", {"skus": skus[i:i + 100]})
            for it in res.get("items", []):
                rows.append({
                    "sku": str(it["sku"]), "offer_id": it.get("offer_id", ""),
                    "name": it.get("name", ""),
                    "warehouse": it.get("warehouse_name", ""),
                    "cluster": it.get("cluster_name", ""),
                    "available": it.get("available_stock_count") or 0,
                    "transit": (it.get("transit_stock_count") or 0) + (it.get("requested_stock_count") or 0),
                    "ads": it.get("ads") or 0,
                    "ads_cluster": it.get("ads_cluster") or 0,
                    "idc": it.get("idc") or 0,
                })
            time.sleep(0.5)
        return rows

    def clusters(self):
        """{склад: кластер} для складов хранения Ozon (тип FULL_FILLMENT)."""
        out = {}
        res = self.post("/v1/cluster/list", {"cluster_type": "CLUSTER_TYPE_OZON"})
        for c in res.get("clusters", []):
            for lc in c.get("logistic_clusters", []):
                for w in lc.get("warehouses", []):
                    if w.get("type") == "FULL_FILLMENT":
                        out[w["name"]] = c.get("name", "")
        return out

    def unshipped_supplies(self):
        """Заявки, которые ещё не уехали с нашего склада: [{order, warehouse, state, slot, items}]."""
        ids, last = [], ""
        while True:
            res = self.post("/v3/supply-order/list", {
                "filter": {"states": UNSHIPPED_STATES}, "limit": 100,
                "sort_by": 1, "sort_dir": "DESC", "last_id": last})
            ids += res.get("order_ids", [])
            last = res.get("last_id") or ""
            if not last:
                break
        out = []
        for i in range(0, len(ids), 50):
            res = self.post("/v3/supply-order/get", {"order_ids": ids[i:i + 50]})
            for o in res.get("orders", []):
                slot = ((o.get("timeslot") or {}).get("timeslot") or {}).get("from")
                for s in o.get("supplies", []):
                    out.append({
                        "order": o.get("order_number"), "state": o.get("state"),
                        "warehouse": (s.get("storage_warehouse") or {}).get("name", ""),
                        "slot": slot, "items": self._bundle(s.get("bundle_id")),
                    })
        return out

    def _bundle(self, bundle_id):
        """{offer_id: qty} в составе заявки."""
        items, last = {}, ""
        if not bundle_id:
            return items
        while True:
            res = self.post("/v1/supply-order/bundle", {
                "bundle_ids": [bundle_id], "limit": 100, "last_id": last})
            for it in res.get("items", []):
                items[it.get("offer_id", "")] = items.get(it.get("offer_id", ""), 0) + it.get("quantity", 0)
            last = res.get("last_id") or ""
            if not res.get("has_next") or not last:
                return items


def full_days(n, today=None):
    """Период из n полных дней, заканчивая вчера: (since, to) для фильтров Ozon."""
    today = today or datetime.now(timezone(timedelta(hours=3))).date()
    return today - timedelta(days=n), today
