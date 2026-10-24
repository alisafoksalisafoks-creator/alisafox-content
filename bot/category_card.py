"""Карточка категории для кнопки в боте: текст + PNG-дашборд.

Запуск: python -m bot.category_card "Сетевое зарядное устройство" [YYYY-MM-DD]
Неделя = 7 дней по выбранную дату включительно, сравнение с 7 днями до неё.
"""
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from bot.category_report import MSK, num
from bot.sources.ozon import OzonClient

METRICS = ("ordered_units", "revenue", "hits_view", "hits_view_pdp", "hits_tocart", "cancellations")
LOW_DAYS, OK_DAYS = 14, 28
FLAT = 5  # изменение в пределах ±5% считаем «без изменений»


def fetch(category: str, day: date, oz: OzonClient):
    d0 = day - timedelta(days=55)
    days = [r for r in oz.analytics(d0, day, ["descriptionType", "day"], METRICS)
            if r["descriptionType"][1] == category]
    week = oz.analytics(day - timedelta(days=6), day, ["sku", "descriptionType"], METRICS)
    prev = oz.analytics(day - timedelta(days=13), day - timedelta(days=7), ["sku", "descriptionType"], METRICS)
    stocks = oz.stocks()
    return {
        "days": [{"day": r["day"][0], **{m: r[m] for m in METRICS}} for r in days],
        "week": [{"sku": r["sku"][0], "name": r["sku"][1], **{m: r[m] for m in METRICS}}
                 for r in week if r["descriptionType"][1] == category],
        "prev": [{"sku": r["sku"][0], **{m: r[m] for m in METRICS}}
                 for r in prev if r["descriptionType"][1] == category],
        "stocks": stocks,
    }


def change(cur, old):
    return (cur - old) / old * 100 if old else None


def compute(raw, day: date):
    by_day = {date.fromisoformat(r["day"]): r for r in raw["days"]}

    def total(start, end):
        t = defaultdict(float)
        d = start
        while d <= end:
            for m in METRICS:
                t[m] += by_day.get(d, {}).get(m, 0)
            d += timedelta(days=1)
        return t

    w = total(day - timedelta(days=6), day)
    p = total(day - timedelta(days=13), day - timedelta(days=7))

    def kpis(t):
        return {
            "Заказы, шт": t["ordered_units"],
            "Выручка, ₽": t["revenue"],
            "Средний чек, ₽": t["revenue"] / t["ordered_units"] if t["ordered_units"] else 0,
            "Показы": t["hits_view"],
            "Карточка → заказ, %": t["ordered_units"] / t["hits_view_pdp"] * 100 if t["hits_view_pdp"] else 0,
            "Отмены, %": t["cancellations"] / t["ordered_units"] * 100 if t["ordered_units"] else 0,
        }

    kw, kp = kpis(w), kpis(p)
    tiles = [{"name": k, "value": kw[k], "delta": change(kw[k], kp[k]),
              "lower_is_better": k.startswith("Отмены")} for k in kw]

    series = [(day - timedelta(days=27 - i), by_day.get(day - timedelta(days=27 - i), {}).get("ordered_units", 0))
              for i in range(28)]
    series_prev = [by_day.get(day - timedelta(days=55 - i), {}).get("ordered_units", 0) for i in range(28)]

    prev_units = {r["sku"]: r["ordered_units"] for r in raw["prev"]}
    skus = []
    for r in sorted(raw["week"], key=lambda r: r["revenue"], reverse=True):
        st = raw["stocks"].get(r["sku"], {})
        free = st.get("fbo", 0) + st.get("fbs", 0)
        per_day = r["ordered_units"] / 7
        skus.append({
            "sku": r["sku"], "offer_id": st.get("offer_id", ""), "name": r["name"],
            "units": r["ordered_units"], "revenue": r["revenue"],
            "delta": change(r["ordered_units"], prev_units.get(r["sku"], 0)),
            "stock": free, "stock_days": free / per_day if per_day else None,
        })
    return {"tiles": tiles, "series": series, "series_prev": series_prev, "skus": skus}


def arrow(delta, lower_is_better=False):
    if delta is None:
        return "▫️ новинка"
    if abs(delta) < FLAT:
        return f"▫️ {delta:+.0f}%"
    good = (delta > 0) != lower_is_better
    return f"{'🟢▲' if delta > 0 else '🔴▼'} {delta:+.0f}%" if good else f"{'🔴▲' if delta > 0 else '🔴▼'} {delta:+.0f}%"


def stock_mark(days):
    if days is None:
        return "⚪ нет продаж"
    if days < LOW_DAYS:
        return f"🔴 {days:.0f} дн"
    if days < OK_DAYS:
        return f"🟡 {days:.0f} дн"
    return f"🟢 {days:.0f} дн"


def fmt_value(name, v):
    return f"{v:.1f}" if "%" in name else num(v)


def short(name, n=42):
    return name if len(name) <= n else name[: n - 1] + "…"


def render_text(category, day, data, top=5):
    start = day - timedelta(days=6)
    lines = [f"📦 {category} · Ozon",
             f"Неделя {start:%d.%m}–{day:%d.%m} к прошлой неделе", ""]
    for t in data["tiles"]:
        lines.append(f"{t['name']}: {fmt_value(t['name'], t['value'])}  {arrow(t['delta'], t['lower_is_better'])}")
    lines += ["", f"Топ-{top} товаров (шт за неделю · к прошлой · остаток):"]
    for i, s in enumerate(data["skus"][:top], 1):
        lines.append(f"{i}. {s['offer_id']} {short(s['name'], 34)}\n"
                     f"   {s['units']} шт · {arrow(s['delta'])} · {stock_mark(s['stock_days'])}")
    lines += ["", "Что видно:"] + insights(data)
    return "\n".join(lines)


def insights(data, limit=3):
    """Правила без ИИ: самые сильные сигналы недели."""
    out = []
    t = {x["name"]: x for x in data["tiles"]}
    units, views = t["Заказы, шт"]["delta"], t["Показы"]["delta"]
    conv = t["Карточка → заказ, %"]["delta"]
    if units is not None and units <= -FLAT:
        if views is not None and views <= -FLAT:
            out.append(f"• Заказы {units:+.0f}% вместе с показами {views:+.0f}%: товар реже видят. Проверьте рекламу, цену и индекс цен.")
        elif conv is not None and conv <= -FLAT:
            out.append(f"• Заказы {units:+.0f}% при конверсии {conv:+.0f}%: видят, но не покупают. Проверьте цену и новые отзывы.")
    if views is not None and views >= FLAT and conv is not None and conv <= -FLAT:
        out.append(f"• Показы {views:+.0f}%, а конверсия карточки {conv:+.0f}%: трафик есть, покупают хуже. Сравните цену с конкурентами и проверьте свежие отзывы.")
    low = [s for s in data["skus"] if s["stock_days"] is not None and s["stock_days"] < LOW_DAYS]
    if low:
        names = ", ".join(s["offer_id"] or s["sku"] for s in low[:4])
        out.append(f"• Остатка меньше {LOW_DAYS} дней: {names}. Нужна подгрузка.")
    falls = [s for s in data["skus"][:10] if s["delta"] is not None and s["delta"] <= -30 and s["units"] >= 5]
    if falls:
        s = falls[0]
        out.append(f"• Сильнее всего просел {s['offer_id']}: {s['delta']:+.0f}% за неделю.")
    return out[:limit] or ["• Существенных отклонений нет."]


if __name__ == "__main__":
    cat = sys.argv[1]
    d = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else datetime.now(MSK).date() - timedelta(days=1)
    cache = Path(f"/tmp/category_card_{d}_{cat}.json")
    raw = json.loads(cache.read_text()) if cache.exists() else fetch(cat, d, OzonClient())
    cache.write_text(json.dumps(raw, ensure_ascii=False))
    data = compute(raw, d)
    print(render_text(cat, d, data))
    if len(sys.argv) > 3:
        from bot.dashboard import category_png
        category_png(cat, d, data, sys.argv[3])
