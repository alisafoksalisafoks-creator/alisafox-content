"""Отправления Ozon FBO и FBS — только чтение.

ID покупателя = первая часть номера отправления: 47327265-0623-1 → 47327265 (сравниваем как число,
бывают ведущие нули). Отдельного поля с ID покупателя в API нет, поэтому качаем все отправления
за период и раскладываем по префиксу.
Цена, которую заплатил покупатель (customer_price, со скидками за счёт Ozon), в списке FBS есть,
а в списке FBO нет — её даёт только /v2/posting/fbo/get, по одному отправлению.
"""
import logging
import os
import time
from datetime import datetime, timezone

import httpx

log = logging.getLogger("buyers.ozon")
HOST = "https://api-seller.ozon.ru"


def buyer_of(posting_number):
    head = str(posting_number).split("-")[0]
    return int(head) if head.isdigit() else None


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(x):
    if isinstance(x, dict):
        x = x.get("amount")
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


class Ozon:
    def __init__(self):
        cid = os.environ.get("OZON_BOT_CLIENT_ID") or os.environ.get("OZON_CLIENT_ID", "")
        key = os.environ.get("OZON_BOT_API_KEY") or os.environ.get("OZON_API_KEY", "")
        self.http = httpx.Client(base_url=HOST, timeout=90,
                                 headers={"Client-Id": cid, "Api-Key": key, "Content-Type": "application/json"})

    def post(self, path, body):
        """Прокси и Ozon иногда рвут соединение, на 429 надо подождать — повторяем."""
        for attempt in range(8):
            try:
                r = self.http.post(path, json=body)
                if r.status_code == 429 or r.status_code >= 500:
                    raise httpx.HTTPStatusError(str(r.status_code), request=r.request, response=r)
                r.raise_for_status()
                return r.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                if isinstance(e, httpx.HTTPStatusError) and e.response.status_code < 500 \
                        and e.response.status_code != 429:
                    raise
                wait = min(60, 2 ** attempt)
                log.warning("%s: %s, повтор через %s с", path, e, wait)
                time.sleep(wait)
        raise RuntimeError(f"Ozon {path} не ответил")

    def _pages(self, path, since, to):
        cursor = ""
        while True:
            body = {"filter": {"since": _iso(since), "to": _iso(to)}, "limit": 100,
                    "with": {"financial_data": True, "analytics_data": True}}
            if cursor:
                body["cursor"] = cursor
            d = self.post(path, body)
            yield from d.get("postings") or d.get("result", {}).get("postings") or []
            cursor = d.get("cursor") or ""
            if not d.get("has_next") or not cursor:
                return

    def lines(self, since: datetime, to: datetime):
        """Строки отправлений FBO+FBS, оформленных в [since, to): одна строка = один товар в отправлении."""
        for scheme, path in (("FBO", "/v3/posting/fbo/list"), ("FBS", "/v4/posting/fbs/list")):
            for p in self._pages(path, since, to):
                yield from _lines(p, scheme)

    def queries(self, skus, date_from, date_to):
        """Поисковые запросы по артикулам за период — по всему кабинету, не по покупателю.
        Ozon отдаёт до 15 запросов на артикул (самые частые по ищущим); сортировка по заказам не работает."""
        out, page = [], 0
        while True:
            d = self.post("/v1/analytics/product-queries/details", {
                "date_from": f"{date_from}T00:00:00Z", "date_to": f"{date_to}T23:59:59Z",
                "skus": [str(s) for s in skus], "page": page, "page_size": 100,
                "sort_by": "BY_SEARCHES", "sort_dir": "DESC", "limit_by_sku": 15})
            out += d.get("queries") or []
            page += 1
            if page >= (d.get("page_count") or 0):
                return out
            time.sleep(1.1)  # лимит аналитики ~1 запрос в секунду

    def fbo_customer_prices(self, posting_number):
        """{sku: цена для покупателя за 1 шт.} для отправления FBO."""
        d = self.post("/v2/posting/fbo/get", {"posting_number": posting_number, "with": {"financial_data": True}})
        fin = (d.get("result") or {}).get("financial_data") or {}
        return {fp.get("product_id"): _num(fp.get("customer_price")) for fp in fin.get("products", [])}


def _lines(p, scheme):
    number = p.get("posting_number", "")
    buyer = buyer_of(number)
    ts = p.get("in_process_at") or p.get("created_at")
    if buyer is None or not ts:
        return
    fin = {fp.get("product_id"): fp for fp in (p.get("financial_data") or {}).get("products", [])}
    city = (p.get("analytics_data") or {}).get("city") or ""
    for pr in p.get("products", []):
        f = fin.get(pr.get("sku"), {})
        yield {
            "posting": number, "buyer": buyer, "ts": ts, "scheme": scheme, "status": p.get("status", ""),
            "sku": pr.get("sku"), "offer_id": pr.get("offer_id", ""), "name": pr.get("name", ""),
            "qty": int(pr.get("quantity") or 0),
            "price": _num(f.get("price")) or _num(pr.get("price")) or 0,
            "old_price": _num(f.get("old_price")),
            "customer_price": _num(f.get("customer_price")),
            "actions": "; ".join(f.get("actions") or []), "city": city,
        }
