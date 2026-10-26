"""Кнопка «🔑 Ключи»: топ-15 поисковых запросов артикула или категории Ozon за 7 или 30 дней.

Источник — аналитика запросов Premium Plus (bot/sources/ozon_queries.period). Ozon отдаёт по каждому
SKU 15 самых частых запросов за период со средней позицией, заказами и выручкой.
Место сравниваем с таким же предыдущим периодом. Реклама — только активные кампании (bot/sources/ads_queries):
заказы по запросу Ozon не делит на рекламу и органику, поэтому делим на уровне артикула,
а запросы, по которым крутится «Вывод в топ», помечаем 📢.
"""
import re
from datetime import date, timedelta

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from bot import store
from bot.sources import ads_queries as aq
from bot.sources import ozon_queries as oq
from bot.sources.moysklad import is_test

TOP = 15
DAYS = (7, 28)
MAX_CAT_SKUS = 100  # в категории берём самые продаваемые SKU, остальные почти не дают запросов
_ready = {"checked": None, "day": None}


def ready_day(oz, db, today: date):
    """Последний день, который Ozon уже посчитал; проверяем не чаще раза в час на день."""
    if _ready["checked"] != today or _ready["day"] is None or _ready["day"] < today - timedelta(days=1):
        probe = db.execute("select sku from daily where day >= ? group by sku order by sum(ordered_units) desc limit 1",
                           (str(today - timedelta(days=7)),)).fetchone()
        _ready.update(checked=today, day=oq.last_ready_day(oz, probe[0], today))
    return _ready["day"]


def periods(end: date, days: int):
    start = end - timedelta(days=days - 1)
    return (start, end), (start - timedelta(days=days), start - timedelta(days=1))


def _stem(w):
    return w[:max(4, len(w) - 2)] if len(w) > 4 else w


def in_title(query, name):
    """Все значимые слова запроса (по основе) есть в названии карточки."""
    name = (name or "").lower().replace("ё", "е")
    words = [w for w in re.findall(r"[\wё]+", query.lower().replace("ё", "е")) if len(w) > 2]
    return all(_stem(w) in name for w in words)


def _wavg(rows):
    w = sum(r["views"] or 0 for r in rows)
    if w:
        return sum((r["position"] or 0) * (r["views"] or 0) for r in rows) / w
    vals = [r["position"] for r in rows if r["position"]]
    return sum(vals) / len(vals) if vals else None


def _sort(rows):
    return sorted(rows, key=lambda r: (-(r["orders"] or 0), -(r["searches"] or 0)))


def _norm(q):
    return " ".join(q.lower().replace("ё", "е").split())


def ad_queries(db, skus, start, end):
    """Запросы из кэша фраз «Вывод в топ» за период: {запрос: (показы, клики)}."""
    if not skus:
        return {}
    aq.ensure_tables(db)
    out = {}
    for r in db.execute(f"select query, sum(views), sum(clicks) from ad_phrases where start = ? and end = ? "
                        f"and sku in ({','.join('?' * len(skus))}) group by query",
                        (str(start), str(end), *[str(s) for s in skus])):
        v, c = out.get(_norm(r[0]), (0, 0))
        out[_norm(r[0])] = (v + r[1], c + r[2])
    return out


def article(db, oz, perf, sku, end: date, days: int, today: date):
    (start, end), prev = periods(end, days)
    s = store.find_sku(db, sku)
    cur = oq.period(db, [sku], start, end, oz)[str(sku)]
    old = {r["query"]: r["position"] for r in oq.period_or_days(db, [sku], *prev, oz, today)[str(sku)]}
    ads = aq.for_sku(db, perf, sku, start, end, today)
    in_ads = ad_queries(db, [sku], start, end)
    rows = []
    for r in _sort(cur)[:TOP]:
        rows.append({**r, "prev_position": old.get(r["query"]), "in_title": in_title(r["query"], s["name"]),
                     "ad": in_ads.get(_norm(r["query"]))})
    t = store.totals(db, start, end, sku=str(sku))
    ad_orders = min(ads["totals"]["orders"], t["ordered_units"])
    return {"kind": "s", "sku": str(sku), "offer_id": s["offer_id"] or s["sku"], "name": s["name"],
            "start": start, "end": end, "days": days, "rows": rows,
            "total_orders": t["ordered_units"], "total_revenue": t["revenue"],
            "ad_orders": ad_orders, "organic_orders": t["ordered_units"] - ad_orders,
            "ad_spent": ads["totals"]["spent"], "ad_money": ads["totals"]["orders_money"],
            "drr": ads["totals"]["spent"] / t["revenue"] * 100 if t["revenue"] else None,
            "ad_raw_orders": ads["totals"]["orders"], "phrases": ads["phrases"],
            "campaigns": sorted({(c[0], c[1], c[2]) for c in ads["campaigns"]})}


def _cat_skus(db, type_id, start, end):
    return [r["sku"] for r in store.top_skus(db, start, end, type_id, MAX_CAT_SKUS * 2)
            if not is_test(r["offer_id"]) and (r["units"] or r["revenue"])][:MAX_CAT_SKUS]


def _merge(by_sku, names):
    """Запросы всех SKU категории → один запрос: заказы и выручка суммой, место — среднее по показам."""
    groups = {}
    for sku, rows in by_sku.items():
        for r in rows:
            groups.setdefault(r["query"].strip().lower(), []).append({**r, "offer_id": names.get(sku, sku)})
    out = []
    for q, rs in groups.items():
        best = min(rs, key=lambda r: r["position"] or 1e9)
        out.append({"query": q, "orders": sum(r["orders"] or 0 for r in rs), "gmv": sum(r["gmv"] or 0 for r in rs),
                    "searches": max(r["searches"] or 0 for r in rs), "views": sum(r["views"] or 0 for r in rs),
                    "position": _wavg(rs), "best_position": best["position"], "best_offer": best["offer_id"],
                    "n_skus": len(rs), "items": rs})
    return out


def category(db, oz, type_id, end: date, days: int, today: date | None = None):
    (start, end), prev = periods(end, days)
    skus = _cat_skus(db, type_id, start, end)
    names = {r["sku"]: r["offer_id"] or r["sku"] for r in db.execute(
        f"select sku, offer_id from sku where sku in ({','.join('?' * len(skus))})", skus)} if skus else {}
    cur = _merge(oq.period(db, skus, start, end, oz), names)
    old = {r["query"]: r["position"] for r in _merge(oq.period_or_days(db, skus, *prev, oz, today), names)}
    in_ads = ad_queries(db, skus, start, end)  # только из ночной предзагрузки: днём очередь отчётов не трогаем
    rows = [{**r, "prev_position": old.get(r["query"]), "ad": in_ads.get(_norm(r["query"]))} for r in _sort(cur)[:TOP]]
    name = db.execute("select type_name from sku where type_id = ? limit 1", (type_id,)).fetchone()[0]
    total = store.totals(db, start, end, type_id=type_id)["ordered_units"]
    return {"kind": "c", "type_id": type_id, "name": name, "start": start, "end": end, "days": days,
            "rows": rows, "all": _sort(cur), "total_orders": total, "n_skus": len(skus)}


def place_delta(cur, prev):
    """Сколько мест выиграли: >0 — поднялись выше (номер места уменьшился)."""
    if cur is None or prev is None:
        return None
    return round(prev) - round(cur)


def to_xlsx(data, path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Все запросы"
    period = f"{data['start']:%d.%m.%Y}–{data['end']:%d.%m.%Y}"
    title = f"{data['offer_id']} · {data['name']}" if data["kind"] == "s" else f"Категория: {data['name']}"
    _title(ws, title, f"Ozon, аналитика запросов, {period} ({data['days']} дн). Место — средняя позиция за период. "
                      "Заказы по запросу — реклама и органика вместе (Ozon их не делит).")
    head = ["Запрос", "Место", "Место пред. период", "Изменение мест", "Заказы, шт", "Выручка, ₽",
            "Конверсия в заказ, %", "Искали, чел", "Видели товар, чел", "В РК: показы", "В РК: клики"]
    head += ["Есть в названии"] if data["kind"] == "s" else ["Лучшее место", "Лучший артикул", "Артикулов в запросе"]
    _head(ws, head)
    for r in data["rows"]:
        ad = r.get("ad") or (None, None)
        line = [r["query"], _r(r["position"]), _r(r["prev_position"]), place_delta(r["position"], r["prev_position"]),
                r["orders"], r["gmv"], r.get("conv"), r["searches"], r["views"], *ad]
        if data["kind"] == "s":
            line.append("да" if r["in_title"] else "нет")
        else:
            line += [_r(r["best_position"]), r["best_offer"], r["n_skus"]]
        ws.append(line)
    _widths(ws, [42, 8, 12, 12, 10, 12, 14, 12, 14, 12, 12, 16, 18, 14])
    if data["kind"] == "s":
        ws2 = wb.create_sheet("Запросы РК")
        _title(ws2, title, f"Активные кампании «Вывод в топ», {period}. Расхода и заказов по фразе Ozon не отдаёт.")
        ws2.append(["Заказы всего, шт", data["total_orders"]])
        ws2.append(["Заказы с рекламы, шт (атрибуция Ozon)", data["ad_orders"]])
        ws2.append(["Органика, шт", data["organic_orders"]])
        ws2.append(["Расход рекламы, ₽", round(data["ad_spent"])])
        ws2.append(["ДРР артикула, %", _r(data["drr"])])
        ws2.append(["Кампании", "; ".join(f"{c[2]} ({c[0]})" for c in data["campaigns"]) or "нет активных"])
        ws2.append([])
        _head(ws2, ["Фраза", "Показы", "Клики", "CTR, %"])
        for x in data["phrases"]:
            ws2.append([x["query"], x["views"], x["clicks"], _r(x["clicks"] / x["views"] * 100) if x["views"] else None])
        _widths(ws2, [44, 12, 10, 10])
    else:
        ws2 = wb.create_sheet("По артикулам")
        _head(ws2, ["Запрос", "Артикул", "SKU", "Место", "Заказы, шт", "Выручка, ₽", "Конверсия, %", "Искали, чел",
                    "Видели товар, чел"])
        for r in data["all"]:
            for x in sorted(r["items"], key=lambda x: -(x["orders"] or 0)):
                ws2.append([r["query"], x["offer_id"], x["sku"], _r(x["position"]), x["orders"], x["gmv"], x["conv"],
                            x["searches"], x["views"]])
        _widths(ws2, [38, 18, 14, 8, 10, 12, 12, 12, 14])
    wb.save(path)
    return path


def _title(ws, title, note):
    ws.append([title])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([note])
    ws.append([])


def _head(ws, cols):
    ws.append(cols)
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)


def _r(x):
    return round(x, 1) if x is not None else None


def _widths(ws, widths):
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
