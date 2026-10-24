"""Сценарии 2 и 5: карточка артикула или категории за период и воронка."""
from datetime import date, timedelta

from bot import store

PERIODS = {"day": (1, "вчера"), "week": (7, "неделя"), "month": (30, "месяц")}
FLAT = 5


def periods(kind: str, end: date):
    n, label = PERIODS[kind]
    cur = (end - timedelta(days=n - 1), end)
    prev = (cur[0] - timedelta(days=n), cur[0] - timedelta(days=1))
    return cur, prev, label


def change(cur, old):
    return (cur - old) / old * 100 if old else None


def ratio(a, b):
    return a / b * 100 if b else 0


def kpis(t):
    return {
        "Заказы, шт": t["ordered_units"],
        "Заказы, ₽": t["revenue"],
        "Средний чек, ₽": t["revenue"] / t["ordered_units"] if t["ordered_units"] else 0,
        "Показы": t["hits_view"],
        "Карточка → заказ, %": ratio(t["ordered_units"], t["hits_view_pdp"]),
        "Отмены, %": ratio(t["cancellations"], t["ordered_units"]),
    }


def tiles(cur, prev):
    kc, kp = kpis(cur), kpis(prev)
    return [{"name": k, "value": kc[k], "delta": change(kc[k], kp[k]),
             "lower_is_better": k.startswith("Отмены")} for k in kc]


STAGES = [
    ("Показы", "hits_view"),
    ("Переходы в карточку", "hits_view_pdp"),
    ("Корзина", "hits_tocart"),
    ("Заказ", "ordered_units"),
    ("Доставлено", "delivered_units"),
]


def funnel(cur, prev):
    """Этапы с конверсией из предыдущего этапа и её изменением к прошлому периоду."""
    out = []
    for i, (name, key) in enumerate(STAGES):
        row = {"name": name, "value": cur[key], "delta_value": change(cur[key], prev[key])}
        if i:
            pk = STAGES[i - 1][1]
            c, p = ratio(cur[key], cur[pk]), ratio(prev[key], prev[pk])
            row.update(conv=c, conv_prev=p, delta_conv=(c - p) / p * 100 if p else None)
        out.append(row)
    # «Доставлено» запаздывает к заказам, поэтому в поиск слабого этапа не берём
    steps = [r for r in out[1:-1] if r.get("delta_conv") is not None]
    worst = min(steps, key=lambda r: r["delta_conv"], default=None)
    if worst and worst["delta_conv"] <= -FLAT:
        worst["worst"] = True
    return out


def build(db, end: date, kind="week", sku=None, type_id=None, compare=None):
    """compare = ((start, end), (start, end)) для произвольного сравнения периодов."""
    if compare:
        cur_p, prev_p = compare
        label = f"{cur_p[0]:%d.%m}–{cur_p[1]:%d.%m} к {prev_p[0]:%d.%m}–{prev_p[1]:%d.%m}"
    else:
        cur_p, prev_p, name = periods(kind, end)
        label = (f"{cur_p[1]:%d.%m.%Y} к {prev_p[1]:%d.%m}" if kind == "day" else
                 f"{name} {cur_p[0]:%d.%m}–{cur_p[1]:%d.%m} к {prev_p[0]:%d.%m}–{prev_p[1]:%d.%m}")
    cur = store.totals(db, *cur_p, sku=sku, type_id=type_id)
    prev = store.totals(db, *prev_p, sku=sku, type_id=type_id)
    span = max(28, (cur_p[1] - cur_p[0]).days + 1)
    s_end = cur_p[1]
    s_start = s_end - timedelta(days=span - 1)
    ser = store.series(db, s_start, s_end, sku=sku, type_id=type_id)
    ser_prev = store.series(db, s_start - timedelta(days=span), s_start - timedelta(days=1), sku=sku, type_id=type_id)
    return {"label": label, "tiles": tiles(cur, prev), "funnel": funnel(cur, prev),
            "series": ser, "series_prev": [v for _, v in ser_prev], "span": span,
            "cur_period": cur_p}
