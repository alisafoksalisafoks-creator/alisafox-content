"""Ozon Seller API: только чтение. Ключи берутся из переменных окружения."""
import os
import time

import httpx

BASE = "https://api-seller.ozon.ru"


class OzonClient:
    def __init__(self, client_id=None, api_key=None):
        self.http = httpx.Client(
            base_url=BASE,
            timeout=60,
            headers={
                "Client-Id": client_id or os.environ.get("OZON_BOT_CLIENT_ID") or os.environ["OZON_CLIENT_ID"],
                # у бота свой ключ (OZON_BOT_API_KEY), чтобы не делить его с другими интеграциями; нет — общий
                "Api-Key": api_key or os.environ.get("OZON_BOT_API_KEY") or os.environ["OZON_API_KEY"],
            },
        )

    def post(self, path, body, retries=5):
        for attempt in range(retries):
            r = self.http.post(path, json=body)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt * 5)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()

    def analytics(self, date_from, date_to, dimensions, metrics=("ordered_units", "revenue")):
        """Строки /v1/analytics/data: [{dim_id: (id, name), ..., metric: value}]."""
        rows, offset = [], 0
        while True:
            data = self.post("/v1/analytics/data", {
                "date_from": str(date_from),
                "date_to": str(date_to),
                "metrics": list(metrics),
                "dimension": list(dimensions),
                "limit": 1000,
                "offset": offset,
            })["result"]["data"]
            for item in data:
                row = {d: (x["id"], x["name"]) for d, x in zip(dimensions, item["dimensions"])}
                row.update(zip(metrics, item["metrics"]))
                rows.append(row)
            if len(data) < 1000:
                return rows
            offset += 1000

    def stocks(self):
        """Остатки по SKU: {sku: {"offer_id", "fbo", "fbs", "reserved"}}."""
        out, cursor = {}, ""
        while True:
            res = self.post("/v4/product/info/stocks", {
                "filter": {"visibility": "ALL"}, "limit": 1000, "cursor": cursor,
            })
            for item in res["items"]:
                for s in item["stocks"]:
                    rec = out.setdefault(str(s["sku"]), {
                        "offer_id": item["offer_id"], "fbo": 0, "fbs": 0, "reserved": 0,
                    })
                    kind = "fbo" if s["type"] == "fbo" else "fbs"
                    rec[kind] += s["present"] - s["reserved"]
                    rec["reserved"] += s["reserved"]
            cursor = res.get("cursor")
            if not res["items"] or not cursor:
                return out
