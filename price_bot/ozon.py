"""Клиент Ozon Seller API: каталог, цены, акции «Эластичный бустинг», скидка от количества, позиция в выдаче.

Методы сверены со swagger docs.ozon.ru/api/seller (октябрь 2026). Скидка от количества — бета-методы Ozon.
"""
import asyncio
import logging
import os

import aiohttp

log = logging.getLogger("price_bot.ozon")
BASE = "https://api-seller.ozon.ru"


class OzonError(Exception):
    pass


def _money(m):
    """{"amount": "351", "currency": "RUB"} или "351" → 351.0; пусто → None."""
    if isinstance(m, dict):
        m = m.get("amount")
    try:
        v = float(m)
    except (TypeError, ValueError):
        return None
    return v or None


class Ozon:
    def __init__(self, client_id=None, api_key=None):
        self.headers = {"Client-Id": client_id or os.environ["OZON_CLIENT_ID"],
                        "Api-Key": api_key or os.environ["OZON_API_KEY"]}
        self._session = None

    async def session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self.headers, timeout=aiohttp.ClientTimeout(total=60),
                                                  trust_env=True)
        return self._session

    async def call(self, path, body=None, method="POST"):
        """Запрос с повтором при 429 и 5xx (до 4 попыток)."""
        s = await self.session()
        for attempt in range(4):
            async with s.request(method, BASE + path, json=body) as r:
                if r.status == 429 or r.status >= 500:
                    log.warning("%s → %s, повтор", path, r.status)
                    await asyncio.sleep(2 ** attempt * 2)
                    continue
                data = await r.json(content_type=None)
                if r.status != 200:
                    raise OzonError(f"{path}: {r.status} {(data or {}).get('message', '')}".strip())
                return data
        raise OzonError(f"{path}: Ozon не отвечает, попробуйте позже")

    # --- каталог ---

    async def catalog(self):
        """Товары в продаже с остатком на складах Ozon (FBO): [{product_id, offer_id, sku, name, cat, fbo}].

        cat — тип товара Ozon. Без остатка FBO товар в боте не показываем.
        """
        ids, last = [], ""
        while True:
            r = await self.call("/v3/product/list", {"filter": {"visibility": "VISIBLE_WITH_FBO_STOCK"}, "last_id": last,
                                                     "limit": 1000})
            items = r["result"]["items"]
            ids += [str(i["product_id"]) for i in items]
            last = r["result"].get("last_id")
            if not items or not last:
                break
        types = await self.type_names()
        out = []
        for i in range(0, len(ids), 1000):
            r = await self.call("/v3/product/info/list", {"product_id": ids[i:i + 1000]})
            for it in r.get("items", []):
                sku = it.get("sku") or next((s["sku"] for s in it.get("sources", []) if s.get("sku")), None)
                if not sku:
                    continue  # товар без SKU ещё не прошёл модерацию — ни цен, ни скидок
                fbo = sum(x.get("present") or 0 for x in (it.get("stocks") or {}).get("stocks", [])
                          if (x.get("source") or "").lower() == "fbo")
                if not fbo:
                    continue  # фильтр Ozon мог отстать: проверяем остаток FBO ещё раз
                out.append({"product_id": it["id"], "offer_id": it["offer_id"], "sku": int(sku),
                            "name": it.get("name", ""), "fbo": fbo,
                            "cat": types.get(it.get("type_id")) or types.get(("c", it.get("description_category_id")))
                            or "Без категории"})
        return out

    async def stocks(self, product_ids):
        """{product_id: {"fbo": шт, "fbs": шт}} — сейчас на складах (FBS включает rFBS)."""
        out = {}
        ids = [str(i) for i in product_ids]
        for i in range(0, len(ids), 1000):
            r = await self.call("/v3/product/info/list", {"product_id": ids[i:i + 1000]})
            for it in r.get("items", []):
                s = {"fbo": 0, "fbs": 0}
                for x in (it.get("stocks") or {}).get("stocks", []):
                    key = "fbo" if (x.get("source") or "").lower() == "fbo" else "fbs"
                    s[key] += x.get("present") or 0
                out[it["id"]] = s
        return out

    async def type_names(self):
        """{type_id: «FM-трансмиттер»} и {("c", description_category_id): название категории}."""
        r = await self.call("/v1/description-category/tree", {"language": "RU"})
        names = {}

        def walk(nodes):
            for n in nodes:
                if n.get("type_id"):
                    names[n["type_id"]] = n.get("type_name")
                if n.get("description_category_id"):
                    names[("c", n["description_category_id"])] = n.get("category_name")
                walk(n.get("children") or [])
        walk(r.get("result", []))
        return names

    # --- цены ---

    async def prices(self, product_ids):
        """{product_id: {price, old_price, min_price, seller_price, sale_percent, acquiring}}."""
        out = {}
        ids = [str(i) for i in product_ids]
        for i in range(0, len(ids), 1000):
            cursor = ""
            while True:
                r = await self.call("/v5/product/info/prices", {"filter": {"product_id": ids[i:i + 1000],
                                                                           "visibility": "ALL"},
                                                                "cursor": cursor, "limit": 1000})
                for it in r.get("items", []):
                    p, c = it.get("price", {}), it.get("commissions", {})
                    out[it["product_id"]] = {
                        "price": _money(p.get("price")), "old_price": _money(p.get("old_price")),
                        "min_price": _money(p.get("min_price")),
                        "seller_price": _money(p.get("marketing_seller_price")) or _money(p.get("price")),
                        "sale_percent": c.get("sales_percent_fbo"), "acquiring": it.get("acquiring")}
                cursor = r.get("cursor")
                if not cursor or not r.get("items"):
                    break
        return out

    async def set_price(self, offer_id, price):
        """Меняет цену. Возвращает список ошибок Ozon (пустой — успех)."""
        r = await self.call("/v1/product/import/prices", {"prices": [{"offer_id": offer_id, "price": str(price)}]})
        res = (r.get("result") or [{}])[0]
        if res.get("updated"):
            return []
        return [e.get("message") or e.get("code") for e in res.get("errors", [])] or ["Ozon не принял цену"]

    # --- акции: «Эластичный бустинг» и цена на сайте ---

    async def actions(self):
        """{"elastic": {product_id: {...}}, "site": {product_id: цена}, "seen": {product_id: [...]}, "errors": [...]}.

        Вилку берём из любой акции, где Ozon отдаёт price_min_elastic / price_max_elastic (цена для минимального
        и максимального бустинга), — по названию акцию не отсеиваем. Если товар в нескольких таких акциях,
        приоритет у «Эластичного бустинга», затем у той, где товар уже участвует. Цену на сайте (website_prices)
        Ozon отдаёт по товарам любой акции — участникам и кандидатам; подписка не нужна.
        Сбой одной акции не роняет остальные: она попадает в errors. seen — что Ozon вернул по товару, для /check.
        """
        actions = (await self.call("/v1/actions", method="GET")).get("result", [])
        elastic, site, seen, errors = {}, {}, {}, []
        for a in actions:
            title = a.get("title") or f"акция {a.get('id')}"
            elastic_title = "эластичн" in title.lower() or "ELASTIC" in (a.get("action_type") or "").upper()
            for kind, inside in (("products", True), ("candidates", False)):
                try:
                    products = await self._action_products(kind, int(a["id"]))
                except OzonError as e:
                    errors.append(f"{title}: {e}")
                    log.warning("акция %s, %s: %s", title, kind, e)
                    continue
                for p in products:
                    sp = _money((p.get("website_prices") or {}).get("price"))
                    if sp and p["id"] not in site:
                        site[p["id"]] = sp
                    lo, hi = _money(p.get("price_min_elastic")), _money(p.get("price_max_elastic"))
                    seen.setdefault(p["id"], []).append({"action": title, "in_action": inside, "min_el": lo,
                                                         "max_el": hi, "site": sp})
                    if not lo and not hi:
                        continue
                    rank = (elastic_title, inside)
                    prev = elastic.get(p["id"])
                    if prev and prev["rank"] >= rank:
                        continue
                    elastic[p["id"]] = {"action": title, "min_el": lo, "max_el": hi, "in_action": inside,
                                        "boost": p.get("current_boost"), "min_boost": p.get("min_boost"),
                                        "max_boost": p.get("max_boost"),
                                        "action_price": _money(p.get("action_price")), "rank": rank}
        log.info("акции: %s, вилка бустинга у %s товаров, цена на сайте у %s, ошибок %s",
                 len(actions), len(elastic), len(site), len(errors))
        return {"elastic": elastic, "site": site, "seen": seen, "errors": errors}

    async def _action_products(self, kind, action_id):
        """Все товары акции: kind="products" — участники, "candidates" — могут участвовать. v2, при сбое — v1."""
        try:
            return await self._pages(f"/v2/actions/{kind}", action_id)
        except OzonError as e:
            log.info("/v2/actions/%s для %s: %s, пробую v1", kind, action_id, e)
            return await self._pages(f"/v1/actions/{kind}", action_id)

    async def _pages(self, path, action_id):
        out, last = [], None
        while True:
            body = {"action_id": action_id, "limit": 1000}
            if last:
                body["last_id"] = last
            r = await self.call(path, body)
            r = r.get("result") or {} if "result" in r else r  # v1 заворачивает ответ в result
            items = r.get("products") or []
            out += items
            last = r.get("last_id")
            if not items or not last:
                return out

    # --- скидка от количества ---

    async def stairways(self, skus):
        """{sku: {enabled, steps: [(qty, pct)], status}}."""
        out = {}
        skus = [str(s) for s in skus]
        for i in range(0, len(skus), 5000):
            r = await self.call("/v1/product/stairway-discount/by-quantity/get", {"skus": skus[i:i + 5000]})
            for s in r.get("stairways", []):
                steps = sorted((int(x["quantity"]), int(x["discount"])) for x in (s.get("stairway") or {}).get("steps", []))
                out[int(s["sku"])] = {"enabled": bool(s.get("enabled")), "steps": steps, "status": s.get("status")}
        return out

    async def set_stairways(self, items):
        """items: [(sku, enabled, steps)], steps=[(qty, pct)].

        Ozon требует ступени даже при выключении, поэтому для удаления отдаём текущие ступени с enabled=false.
        Возвращает {"accepted": bool, "errors": {sku: [текст]}, "warnings": {sku: [текст]}}.
        """
        def item(sku, enabled, steps):
            return {"sku": int(sku), "enabled": enabled,
                    "stairway": {"steps": [{"step": n, "quantity": q, "discount": d}
                                           for n, (q, d) in enumerate(steps, 1)]}}
        result = {"accepted": True, "errors": {}, "warnings": {}}
        for i in range(0, len(items), 1000):
            r = await self.call("/v1/product/stairway-discount/by-quantity/set",
                                {"stairways": [item(*x) for x in items[i:i + 1000]], "suppress_warnings": True})
            result["accepted"] &= bool(r.get("accepted"))
            for key in ("errors", "warnings"):
                for e in r.get(key) or []:
                    result[key].setdefault(int(e.get("sku") or 0), []).extend(
                        d.get("message") or d.get("code") for d in e.get("data", []))
        return result

    # --- позиция в выдаче ---

    async def positions(self, day):
        """{sku: средняя позиция в поиске и категории за день}. Метод Ozon — не чаще 1 раза в минуту."""
        out, offset = {}, 0
        while True:
            r = await self.call("/v1/analytics/data", {"date_from": day, "date_to": day, "metrics": ["position_category"],
                                                       "dimension": ["sku"], "limit": 1000, "offset": offset})
            data = (r.get("result") or {}).get("data", [])
            for row in data:
                pos = row["metrics"][0]
                if pos:
                    out[int(row["dimensions"][0]["id"])] = pos
            if len(data) < 1000:
                return out
            offset += 1000
            await asyncio.sleep(61)

    async def close(self):
        if self._session:
            await self._session.close()
