"""Цены артикула Ozon: текущая цена, соинвест, пороги акций, скидка от количества. Только чтение.

Логика порогов — как в боте «Цены AlisaFox»: вилка «Эластичного бустинга» (price_min_elastic / price_max_elastic,
нет мин. — max_action_price), у остальных акций порог — max_action_price. Цена на сайте — «Цена с картой»
(website_prices.green_price) из любой акции, где Ozon отдаёт товар. Менять цену — в боте «Цены».
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("bot.prices")
USN = 0.07


def _money(m):
    if isinstance(m, dict):
        m = m.get("amount")
    try:
        return float(m) or None
    except (TypeError, ValueError):
        return None


def _card_price(wp):
    """«Цена с картой»: самая низкая green_price по схемам, нет — black_price, затем price."""
    wp = wp or {}
    by = [x for x in (wp.get("prices_by_schema") or {}).values() if isinstance(x, dict)]
    for key in ("green_price", "black_price"):
        vals = [v for v in (_money(x.get(key)) for x in by) if v]
        if vals:
            return min(vals)
    return _money(wp.get("price"))


def _pages(oz, kind, action_id):
    """Все товары акции: products — участники, candidates — могут участвовать. Больше 100 за раз Ozon не отдаёт."""
    out, last = [], None
    for path in (f"/v2/actions/{kind}", f"/v1/actions/{kind}"):
        try:
            while True:
                body = {"action_id": action_id, "limit": 100}
                if last:
                    body["last_id"] = last
                r = oz.post(path, body)
                r = r.get("result") or {} if "result" in r else r
                items = r.get("products") or []
                out += items
                last = r.get("last_id")
                if not items or not last:
                    return out
        except Exception as e:  # noqa: BLE001 — v2 не ответил, пробуем v1
            log.info("%s %s: %s", path, action_id, e)
            out, last = [], None
    raise RuntimeError(f"акция {action_id} не ответила")


def actions(oz):
    """{"by": {product_id: [строки акций]}, "site": {product_id: цена с картой}, "errors": [...], "at": time}."""
    acts = oz.get("/v1/actions").get("result", [])
    jobs = [(a, kind) for a in acts for kind in ("products", "candidates")]
    errors = []

    def load(job):
        a, kind = job
        try:
            return _pages(oz, kind, int(a["id"]))
        except Exception as e:  # noqa: BLE001 — одна акция не обнуляет остальные
            errors.append(a.get("title") or str(a.get("id")))
            log.warning("акция %s, %s: %r", a.get("title"), kind, e)
            return []

    with ThreadPoolExecutor(4) as pool:
        loaded = list(pool.map(load, jobs))
    by, site = {}, {}
    for (a, kind), products in zip(jobs, loaded):
        title = a.get("title") or f"акция {a.get('id')}"
        elastic = "эластичн" in title.lower() or "ELASTIC" in (a.get("action_type") or "").upper()
        for p in products:
            sp = _card_price(p.get("website_prices"))
            if sp:
                site.setdefault(p["id"], sp)
            lo, hi, cap = (_money(p.get(k)) for k in ("price_min_elastic", "price_max_elastic", "max_action_price"))
            by.setdefault(p["id"], []).append({
                "action": title, "inside": kind == "products", "elastic": elastic,
                "enter": lo or cap, "max_boost": hi if elastic else None, "cap": cap,
                "action_price": _money(p.get("action_price")), "boost": p.get("current_boost"),
                "end": (a.get("date_end") or "")[:10]})
    log.info("акции: %s, товаров в акциях %s, ошибок %s", len(acts), len(by), len(errors))
    return {"by": by, "site": site, "errors": errors, "at": time.time()}


def price_info(oz, offer_id):
    """Цена из /v5/product/info/prices по артикулу продавца."""
    r = oz.post("/v5/product/info/prices", {"filter": {"offer_id": [offer_id], "visibility": "ALL"}, "limit": 10, "cursor": ""})
    it = next(iter(r.get("items") or []), None)
    if not it:
        return None
    p, c = it.get("price", {}), it.get("commissions", {})
    return {"product_id": it["product_id"], "price": _money(p.get("price")), "old_price": _money(p.get("old_price")),
            "min_price": _money(p.get("min_price")),
            "seller_price": _money(p.get("marketing_seller_price")) or _money(p.get("price")),
            "commission": c.get("sales_percent_fbo"),
            "index": (it.get("price_indexes") or {}).get("color_index")}


def stairway(oz, sku):
    """Скидка от количества: [(от шт, %)] или None, если выключена; бета-метод Ozon."""
    r = oz.post("/v1/product/stairway-discount/by-quantity/get", {"skus": [str(sku)]})
    s = next((x for x in r.get("stairways", []) if str(x.get("sku")) == str(sku)), None)
    if not s or not s.get("enabled"):
        return []
    return sorted((int(x["quantity"]), int(x["discount"])) for x in (s.get("stairway") or {}).get("steps", []))


def forecast(unit, price, new):
    """Прибыль на 1 шт при новой цене ≈ прибыль + Δцены × (1 − комиссия − ДРР) × (1 − УСН). Оценка, не факт."""
    if not unit or unit.get("profit") is None or not price or not new:
        return None
    share = 1 - (unit.get("commission") or 0) / 100 - (unit.get("drr") or 0) / 100
    return unit["profit"] + (new - price) * share * (1 - USN)


def summary(p, acts, unit=None):
    """Сводка по цене. p — price_info, acts — actions(), unit — {profit, drr, commission} на 1 шт за 2 недели.

    Строки акций: где участвуем, куда можно войти и на сколько снизить цену. rec — лучшая рекомендация:
    самый маленький шаг вниз, при котором прибыль на 1 шт не уходит в минус.
    """
    price, pid = p["price"], p["product_id"]
    unit = dict(unit or {}, commission=p.get("commission"))
    rows = []
    for a in acts["by"].get(pid, []):
        for what, limit in (("вход", a["enter"]), ("макс. бустинг", a["max_boost"])):
            if not limit or (what != "вход" and not a["elastic"]):
                continue
            need = max(0.0, price - limit) if price else None
            rows.append({**a, "what": what, "limit": limit, "need": need,
                         "need_pct": need / price * 100 if need and price else 0.0,
                         "profit": forecast(unit, price, min(price, limit)) if price else None})
        if not a["enter"] and not a["max_boost"]:
            rows.append({**a, "what": "вход", "limit": None, "need": None, "need_pct": None, "profit": None})
    rows.sort(key=lambda r: (not r["inside"], r["need"] is None, r["need"] or 0, r["what"] != "вход"))
    rec = next((r for r in sorted((r for r in rows if not r["inside"] and r["need"]), key=lambda r: r["need"])
                if r["profit"] is None or r["profit"] >= 0), None)
    site = acts["site"].get(pid)
    seller = p.get("seller_price")
    return {**p, "rows": rows, "rec": rec, "site": site, "unit": unit,
            "coinvest_now": (seller - site) / seller * 100 if seller and site else None,
            "errors": acts["errors"]}
