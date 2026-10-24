"""Сценарий 3: нужна ли подгрузка. Остатки и скорость продаж по кластерам Ozon."""
import time
from collections import defaultdict

MIN_DAYS = 20      # запаса на складе должно хватать минимум на 20 дней
TARGET_DAYS = 30   # грузим до этого запаса
EXCESS_DAYS = 60   # дольше — перебор, долгая оборачиваемость
MIN_NEED = 3       # потребность меньше 3 шт на склад не везём: поставка дороже продажи


def cluster_stocks(oz, skus):
    """{sku: {cluster: {...}}} из /v1/analytics/stocks, по 100 SKU за запрос."""
    out = defaultdict(dict)
    skus = list(skus)
    for i in range(0, len(skus), 100):
        res = oz.post("/v1/analytics/stocks", {"skus": skus[i:i + 100]})
        for it in res.get("items", []):
            c = out[str(it["sku"])].setdefault(it["cluster_name"], {
                "stock": 0, "transit": 0, "ads": it.get("ads_cluster") or 0,
                "days_no_sales": it.get("days_without_sales_cluster") or 0,
                "grade": it.get("turnover_grade_cluster", ""), "offer_id": it.get("offer_id", ""),
                "name": it.get("name", "")})
            c["stock"] += it.get("available_stock_count") or 0
            c["transit"] += (it.get("transit_stock_count") or 0) + (it.get("requested_stock_count") or 0)
        time.sleep(1)
    return out


def plan(stocks):
    """Строки по кластерам: дни запаса, сколько везти, перебор."""
    rows = []
    for sku, clusters in stocks.items():
        for name, c in clusters.items():
            have = c["stock"] + c["transit"]
            days = have / c["ads"] if c["ads"] else None
            need = max(0, round(c["ads"] * TARGET_DAYS - have)) if c["ads"] else 0
            need = need if need >= MIN_NEED else 0
            if days is not None and days < MIN_DAYS:
                status = "urgent" if c["stock"] == 0 else "low"
            elif have and (days is None or days > EXCESS_DAYS):
                status = "excess"
            else:
                status = "ok"
            excess = round(have - c["ads"] * EXCESS_DAYS) if status == "excess" and c["ads"] else (have if status == "excess" else 0)
            rows.append({"sku": sku, "offer_id": c["offer_id"], "name": c["name"], "cluster": name,
                         "stock": c["stock"], "transit": c["transit"], "ads": c["ads"],
                         "days": days, "need": need, "excess": excess, "status": status})
    order = {"urgent": 0, "low": 1, "excess": 2, "ok": 3}
    rows.sort(key=lambda r: (order[r["status"]], -(r["ads"] or 0)))
    return rows


def summary(rows):
    tot = defaultdict(float)
    for r in rows:
        tot["stock"] += r["stock"] + r["transit"]
        tot["ads"] += r["ads"]
        tot["need"] += r["need"]
        tot["excess"] += r["excess"]
    tot["days"] = tot["stock"] / tot["ads"] if tot["ads"] else None
    return dict(tot)


def render_text(title, rows, limit=12):
    s = summary(rows)
    days = f"{s['days']:.0f} дн" if s["days"] is not None else "нет продаж"
    lines = [f"🚚 Подгрузка: {title}",
             f"Всего на Ozon с учётом пути: {s['stock']:.0f} шт, продажи {s['ads']:.1f} шт/день, хватит на {days}",
             f"Норма: на каждом складе ≥ {MIN_DAYS} дн, перебор > {EXCESS_DAYS} дн", ""]
    mark = {"urgent": "🔴 ноль, грузить срочно", "low": "🟠 мало", "excess": "🔵 перебор", "ok": "🟢 норма"}
    shown = [r for r in rows if r["status"] != "ok"][:limit]
    for r in shown:
        d = f"{r['days']:.0f} дн" if r["days"] is not None else "нет продаж"
        transit = f" (+{r['transit']:.0f} в пути)" if r["transit"] else ""
        act = (f"везти {r['need']} шт" if r["status"] in ("urgent", "low")
               else f"вывезти/переместить ~{r['excess']} шт")
        lines.append(f"{mark[r['status']]} · {r['cluster']} · {r['offer_id']}: {r['stock']:.0f} шт"
                     f"{transit}, {d} → {act}")
    if not shown:
        lines.append("🟢 На всех складах запас в норме.")
    return "\n".join(lines)


def allocate(rows, catalog):
    """Делит свой остаток (МойСклад, корневой артикул) между складами Ozon: сначала туда, где запас кончится раньше.
    need — полная потребность склада до 30 дней, ship — сколько везти из своего, order — сколько не хватает своего."""
    groups = defaultdict(list)
    for r in rows:
        r["ship"], r["order"] = 0, 0
        if r["status"] in ("urgent", "low") and r["need"] > 0:
            groups[catalog.root(r["offer_id"])].append(r)
    for root, rs in groups.items():
        left = int(max(0, (catalog.get(root) or {}).get("stock", 0)))
        for r in sorted(rs, key=lambda r: (r["days"] or 0, -r["ads"])):
            r["own"] = left
            r["ship"] = min(r["need"], left)
            r["order"] = r["need"] - r["ship"]
            left -= r["ship"]
    return rows


def by_cluster(rows):
    """Склады, куда пора везти: [{cluster, skus, need, ship}] по убыванию потребности."""
    out = defaultdict(lambda: {"skus": 0, "need": 0, "ship": 0, "urgent": 0})
    for r in rows:
        if r["status"] in ("urgent", "low") and r["need"] > 0:
            c = out[r["cluster"]]
            c["skus"] += 1
            c["need"] += r["need"]
            c["ship"] += r["ship"]
            c["urgent"] += r["status"] == "urgent"
    return sorted(({"cluster": k, **v} for k, v in out.items()), key=lambda c: -c["need"])


def by_sku(rows):
    """Артикулы, которым нужна отгрузка: [{sku, offer_id, clusters, need, ship, days}].
    Сначала те, что есть на своём складе (самые срочные выше), потом те, что надо заказывать у поставщика."""
    out = {}
    for r in rows:
        if r["status"] in ("urgent", "low") and r["need"] > 0:
            s = out.setdefault(r["sku"], {"sku": r["sku"], "offer_id": r["offer_id"], "name": r["name"],
                                          "clusters": 0, "need": 0, "ship": 0, "days": 999, "profit": r.get("profit")})
            s["clusters"] += 1
            s["need"] += r["need"]
            s["ship"] += r["ship"]
            s["days"] = min(s["days"], r["days"] or 0)
    return sorted(out.values(), key=lambda s: (not s["ship"], s["days"], -s["ship"], -s["need"]))
