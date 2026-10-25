"""Ozon Performance API: расходы рекламы по кампаниям. Только чтение."""
import os
import time
from datetime import timedelta

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

    def post(self, path, body):
        self._auth()
        for attempt in range(6):
            r = self.http.post(path, json=body)
            if r.status_code != 429:
                break
            time.sleep(10 * (attempt + 1))
        r.raise_for_status()
        return r.json()

    def cpo_by_sku(self, day_from, day_to, timeout=600):
        """{sku: расход} «Оплаты за заказ» по каждому заказу — асинхронный отчёт Ozon (в очереди максимум один)."""
        body = {"from": f"{day_from - timedelta(days=1)}T21:00:00Z", "to": f"{day_to}T20:59:59Z"}  # сутки по МСК
        self._auth()
        deadline = time.time() + timeout
        while True:
            r = self.http.post("/api/client/statistic/orders/generate/json", json=body)
            if r.status_code != 429 or time.time() > deadline:
                break
            time.sleep(15)  # занят другой отчёт
        r.raise_for_status()
        uuid = r.json()["UUID"]
        while self.get(f"/api/client/statistics/{uuid}").get("state") != "OK":
            if time.time() > deadline:
                raise TimeoutError("отчёт по оплате за заказ не собрался")
            time.sleep(5)
        rows = self.get("/api/client/statistics/report", UUID=uuid).get("rows", [])
        out = {}
        for x in rows:
            out[str(x["sku"])] = out.get(str(x["sku"]), 0.0) + float(x["moneySpent"].replace(",", ".").replace(" ", "") or 0)
        return out

    def spend_by_sku(self, day_from, day_to, revenue):
        """{sku: расход, ₽} за период. Кампании за клики делятся между своими товарами пропорционально выручке
        (revenue — {sku: выручка за тот же период}); «Оплата за заказ» — точно по заказам из отчёта Ozon."""
        if not hasattr(self, "camps"):
            self.camps = {c["id"]: c for c in self.get("/api/client/campaign")["list"]}
        spend = {}
        for r in self.get("/api/client/statistics/daily/json", dateFrom=str(day_from), dateTo=str(day_to))["rows"]:
            spend[r["id"]] = spend.get(r["id"], 0.0) + float(r["moneySpent"].replace(",", "."))
        out, other = {}, 0.0

        def split(money, skus):
            base = sum(revenue.get(s, 0) for s in skus)
            for s in skus:
                out[s] = out.get(s, 0.0) + money * (revenue.get(s, 0) / base if base else 1 / len(skus))

        for cid, money in spend.items():
            if money <= 0:
                continue
            if self.camps.get(cid, {}).get("advObjectType") == "SKU":
                skus = [str(p["sku"]) for p in self.get(f"/api/client/campaign/{cid}/v2/products").get("products", [])]
                split(money, skus or list(revenue))
            else:
                other += money
        if other > 0:
            cpo = self.cpo_by_sku(day_from, day_to)
            for s, v in cpo.items():
                out[s] = out.get(s, 0.0) + v
            if other - sum(cpo.values()) > 1:  # чего нет в отчёте по заказам — по выручке
                split(other - sum(cpo.values()), list(revenue))
        return out
