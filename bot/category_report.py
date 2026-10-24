"""Сводка по категориям Ozon за день: заказы, сравнение, дни остатка.

Запуск: python -m bot.category_report [YYYY-MM-DD]  (по умолчанию вчера по МСК)
"""
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from bot.sources.ozon import OzonClient

MSK = timezone(timedelta(hours=3))
AVG_DAYS = 14
LOW_STOCK_DAYS = 14


def build(day: date, client: OzonClient | None = None):
    oz = client or OzonClient()
    start = day - timedelta(days=AVG_DAYS - 1)
    by_day = oz.analytics(min(start, day - timedelta(days=7)), day, ["descriptionType", "day"])
    by_sku = oz.analytics(start, day, ["sku", "descriptionType"])
    stocks = oz.stocks()

    cats = defaultdict(lambda: {"units": 0, "revenue": 0, "prev": 0, "week_ago": 0,
                                "units_avg": 0, "stock": 0, "low_skus": 0})
    for r in by_day:
        name, d = r["descriptionType"][1], date.fromisoformat(r["day"][0])
        c = cats[name]
        if d == day:
            c["units"] += r["ordered_units"]
            c["revenue"] += r["revenue"]
        if d == day - timedelta(days=1):
            c["prev"] += r["ordered_units"]
        if d == day - timedelta(days=7):
            c["week_ago"] += r["ordered_units"]
        if d >= start:
            c["units_avg"] += r["ordered_units"] / AVG_DAYS

    for r in by_sku:
        sku, name = r["sku"][0], r["descriptionType"][1]
        st = stocks.get(sku, {})
        free = st.get("fbo", 0) + st.get("fbs", 0)
        cats[name]["stock"] += free
        per_day = r["ordered_units"] / AVG_DAYS
        if per_day >= 1 and free / per_day < LOW_STOCK_DAYS:
            cats[name]["low_skus"] += 1

    rows = []
    for name, c in cats.items():
        c["name"] = name
        c["stock_days"] = round(c["stock"] / c["units_avg"]) if c["units_avg"] else None
        rows.append(c)
    rows.sort(key=lambda c: c["revenue"], reverse=True)
    return rows


def num(x):
    return f"{x:,.0f}".replace(",", " ")


def pct(a, b):
    return f"{(a - b) / b * 100:+.0f}%" if b else "—"


def render(day: date, rows, top=10):
    total_u = sum(r["units"] for r in rows)
    total_r = sum(r["revenue"] for r in rows)
    prev = sum(r["prev"] for r in rows)
    week = sum(r["week_ago"] for r in rows)
    lines = [
        f"Ozon, {day:%d.%m.%Y}: {num(total_u)} заказов на {num(total_r)} ₽",
        f"к вчера {pct(total_u, prev)}, к тому же дню недели {pct(total_u, week)}",
        "",
        "Топ категорий по выручке (шт / ₽ / к дню недели / дней остатка):",
    ]
    for i, r in enumerate(rows[:top], 1):
        days = r["stock_days"] if r["stock_days"] is not None else "—"
        lines.append(f"{i}. {r['name']}: {r['units']} / {num(r['revenue'])} / "
                     f"{pct(r['units'], r['week_ago'])} / {days}")
    alerts = [r for r in rows if r["low_skus"]]
    if alerts:
        lines += ["", f"Остаток меньше {LOW_STOCK_DAYS} дней продаж:"]
        lines += [f"• {r['name']}: {r['low_skus']} SKU" for r in alerts[:10]]
    return "\n".join(lines)


if __name__ == "__main__":
    d = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else (
        datetime.now(MSK).date() - timedelta(days=1))
    print(render(d, build(d)))
