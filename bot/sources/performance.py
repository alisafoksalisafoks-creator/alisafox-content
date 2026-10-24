"""Ozon Performance API: расходы рекламы по кампаниям. Только чтение."""
import os
import time

import httpx

BASE = "https://api-performance.ozon.ru"

# группы как в «Аналитике продвижения» Ozon
GROUPS = {
    ("SKU", "PLACEMENT_TOP_PROMOTION"): "Поиск",
    ("SKU", "PLACEMENT_SEARCH_AND_CATEGORY"): "Поиск и рекомендации",
    ("SEARCH_PROMO", ""): "ОЗЗ",
}


class PerformanceClient:
    def __init__(self):
        self.http = httpx.Client(base_url=BASE, timeout=60)
        self.expires = 0

    def _auth(self):
        if time.time() < self.expires:
            return
        r = self.http.post("/api/client/token", json={
            "client_id": os.environ["OZON_PERF_CLIENT_ID"],
            "client_secret": os.environ["OZON_PERF_CLIENT_SECRET"],
            "grant_type": "client_credentials"})
        r.raise_for_status()
        d = r.json()
        self.http.headers["Authorization"] = f"Bearer {d['access_token']}"
        self.expires = time.time() + d.get("expires_in", 1800) - 60

    def get(self, path, **params):
        self._auth()
        for attempt in range(6):
            r = self.http.get(path, params=params)
            if r.status_code != 429:
                break
            time.sleep(10 * (attempt + 1))
        r.raise_for_status()
        return r.json()

    def spend_by_group(self, day_from, day_to):
        """{группа: {"spend", "orders", "orders_money"}} за период."""
        if not hasattr(self, "camps"):
            self.camps = {c["id"]: c for c in self.get("/api/client/campaign")["list"]}
        camps = self.camps
        rows = self.get("/api/client/statistics/daily/json", dateFrom=str(day_from), dateTo=str(day_to))["rows"]
        out = {}
        for r in rows:
            c = camps.get(r["id"], {})
            key = (c.get("advObjectType"), (c.get("placement") or [""])[0])
            g = out.setdefault(GROUPS.get(key, "Другое"), {"spend": 0.0, "orders": 0, "orders_money": 0.0})
            g["spend"] += float(r["moneySpent"].replace(",", "."))
            g["orders"] += int(r["orders"])
            g["orders_money"] += float(r["ordersMoney"].replace(",", "."))
        return out
