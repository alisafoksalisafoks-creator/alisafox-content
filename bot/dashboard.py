"""PNG-дашборды для Telegram: плитки, график, таблица товаров, воронка, подгрузка."""
from datetime import timedelta

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

FLAT = 5
INK, INK2, MUTED, GRID = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e1"
SURFACE, TILE = "#fcfcfb", "#f3f2ef"
BLUE, PREV = "#2a78d6", "#b5b4ae"
GOOD, WARN, BAD, EXCESS = "#0ca30c", "#eda100", "#d03b3b", "#2a78d6"
W_IN = 8

for f in ("Inter", "DejaVu Sans"):
    if any(f in x.name for x in font_manager.fontManager.ttflist):
        plt.rcParams["font.family"] = f
        break


def num(x):
    return f"{x:,.0f}".replace(",", " ")


def fmt_value(name, v):
    return f"{v:.1f}" if "%" in name else num(v)


def short(name, n=58):
    return name if len(name) <= n else name[: n - 1] + "…"


def delta_style(delta, lower_is_better=False):
    if delta is None:
        return "нет базы", MUTED
    if abs(delta) < FLAT:
        return f"≈ {delta:+.0f}%", MUTED
    up = delta > 0
    return f"{'▲' if up else '▼'} {delta:+.0f}%", GOOD if up != bool(lower_is_better) else BAD


class Page:
    """Вертикальная страница: блоки кладутся сверху вниз, высота в дюймах считается заранее."""

    def __init__(self, height, width=W_IN):
        self.h = height
        self.fig = plt.figure(figsize=(width, height), dpi=200 if width < W_IN else 160, facecolor=SURFACE)
        self.y = height - 0.35  # курсор сверху, в дюймах

    def box(self, x, h, w=None):
        """Оси под блок высотой h дюймов, x и w в долях ширины."""
        self.y -= h
        return self.fig.add_axes([x, self.y / self.h, w if w is not None else 0.9, h / self.h])

    def text(self, x, dy, s, **kw):
        self.y -= dy
        self.fig.text(x, self.y / self.h, s, **kw)

    def header(self, title, subtitle):
        self.text(0.05, 0.3, title, fontsize=17, fontweight="bold", color=INK)
        self.text(0.05, 0.28, subtitle, fontsize=10, color=INK2)
        self.y -= 0.2

    def tiles(self, tiles, per_row=3):
        gx, th, gy = 0.02, 0.85, 0.15
        w = (0.9 - gx * (per_row - 1)) / per_row
        for row in range(0, len(tiles), per_row):
            self.y -= th
            for i, t in enumerate(tiles[row:row + per_row]):
                ax = self.fig.add_axes([0.05 + i * (w + gx), self.y / self.h, w, th / self.h])
                ax.set_facecolor(TILE)
                ax.set_xticks([]), ax.set_yticks([])
                for s in ax.spines.values():
                    s.set_visible(False)
                label, color = ("", INK) if t.get("plain") else delta_style(t["delta"], t.get("lower_is_better"))
                ax.text(0.06, 0.76, t["name"], fontsize=8.5, color=INK2, transform=ax.transAxes, va="center")
                ax.text(0.06, 0.34, fmt_value(t["name"], t["value"]), fontsize=15, fontweight="bold",
                        color=INK, transform=ax.transAxes, va="center")
                ax.text(0.94, 0.34, label, fontsize=10, fontweight="bold", color=color,
                        transform=ax.transAxes, va="center", ha="right")
            self.y -= gy

    def line(self, series, prev, span, title="Заказы по дням, шт"):
        self.text(0.05, 0.35, title, fontsize=11, fontweight="bold", color=INK)
        self.y -= 0.32
        ax = self.box(0.08, 2.0, 0.87)
        self.y -= 0.35
        xs = list(range(len(series)))
        cur = [v for _, v in series]
        ax.plot(xs, prev, color=PREV, lw=1.6, ls=(0, (4, 3)), label=f"предыдущие {span} дн")
        ax.plot(xs, cur, color=BLUE, lw=2, label=f"последние {span} дн")
        ax.scatter([xs[-1]], [cur[-1]], s=28, color=BLUE, zorder=3, edgecolor=SURFACE, linewidth=1.5)
        ax.annotate(f"{cur[-1]:.0f}", (xs[-1], cur[-1]), xytext=(6, 0), textcoords="offset points",
                    fontsize=9, color=INK, va="center")
        step = max(1, len(xs) // 4)
        ticks = sorted(set(list(range(0, len(xs), step))[:4] + [xs[-1]]))
        ax.set_xticks(ticks, [f"{series[i][0]:%d.%m}" for i in ticks], fontsize=8.5, color=INK2)
        ax.tick_params(axis="y", labelsize=8.5, colors=INK2, length=0)
        ax.tick_params(axis="x", length=0)
        ax.set_ylim(bottom=0)
        ax.grid(axis="y", color=GRID, lw=0.8)
        ax.set_facecolor(SURFACE)
        for s in ("top", "right", "left"):
            ax.spines[s].set_visible(False)
        ax.spines["bottom"].set_color(GRID)
        ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=2, frameon=False, fontsize=8.5,
                  labelcolor=INK2, handlelength=2.2, borderaxespad=0.2)

    def table(self, title, cols, rows):
        """cols: [(x, заголовок, ha)], rows: [[(text, color, bold) | None, ...], подпись]."""
        self.text(0.05, 0.4, title, fontsize=11, fontweight="bold", color=INK)
        self.text(0.05, 0.3, "", fontsize=1)
        hy = self.y
        for x, name, ha in cols:
            self.fig.text(x, hy / self.h, name, fontsize=8.5, color=MUTED, ha=ha)
        self.fig.add_artist(plt.Line2D([0.05, 0.95], [(hy - 0.08) / self.h] * 2, color=GRID, lw=0.8))
        for cells, sub in rows:
            self.y -= 0.42
            y = self.y / self.h
            for (x, _, ha), cell in zip(cols, cells):
                if cell:
                    t, c, b = cell
                    self.fig.text(x, y + (0.05 / self.h if sub else 0), t, fontsize=9.5, color=c, ha=ha,
                                  fontweight="bold" if b else "normal")
            if sub:
                self.fig.text(0.05, y - 0.13 / self.h, sub, fontsize=7.5, color=INK2)
            self.fig.add_artist(plt.Line2D([0.05, 0.95], [y - 0.2 / self.h] * 2, color=GRID, lw=0.5))
        self.y -= 0.3

    def funnel(self, stages):
        self.text(0.05, 0.4, "Воронка: этап и конверсия из предыдущего", fontsize=11, fontweight="bold", color=INK)
        self.y += 0.1
        for r in stages:
            self.y -= 0.62
            y = self.y / self.h
            hi = r.get("worst")
            if hi:
                ax = self.fig.add_axes([0.04, y - 0.2 / self.h, 0.92, 0.58 / self.h])
                ax.set_facecolor("#fbe9e9")
                ax.set_xticks([]), ax.set_yticks([])
                for s in ax.spines.values():
                    s.set_visible(False)
            self.fig.text(0.06, y + 0.12 / self.h, r["name"], fontsize=10.5, fontweight="bold", color=INK)
            lab, col = delta_style(r["delta_value"])
            self.fig.text(0.06, y - 0.06 / self.h, f"{num(r['value'])} шт  {lab}", fontsize=9, color=INK2)
            if "conv" in r:
                self.fig.text(0.62, y + 0.12 / self.h, f"{r['conv']:.2f}%", fontsize=13, fontweight="bold",
                              color=INK, ha="right")
                self.fig.text(0.62, y - 0.06 / self.h, f"было {r['conv_prev']:.2f}%", fontsize=8.5,
                              color=MUTED, ha="right")
                lab, col = delta_style(r["delta_conv"])
                self.fig.text(0.94, y + 0.03 / self.h, lab, fontsize=11, fontweight="bold", color=col, ha="right")
            if hi:
                self.fig.text(0.94, y - 0.12 / self.h, "сильнее всего просел", fontsize=8, color=BAD, ha="right")
        self.y -= 0.2

    def footer(self, s):
        self.fig.text(0.05, 0.12 / self.h, s, fontsize=7.5, color=MUTED, wrap=True, linespacing=1.4)

    def save(self, path):
        self.fig.savefig(path, facecolor=SURFACE)
        plt.close(self.fig)


SHEET_W = 6.2  # уже обычной страницы: в телефоне шрифт выходит крупнее


def _save(p, prefix, paths, note):
    p.footer(note)
    path = f"{prefix}_{len(paths) + 1}.png"
    p.save(path)
    paths.append(path)


def card_png(title, data, prefix, top_rows=None):
    """Сценарий 2 и карточка категории: листы «показатели», «топ товаров», «воронка». Возвращает пути PNG."""
    paths, n = [], 3 if top_rows else 2
    head, _, rest = title.partition(" · ")  # у артикула: «OZ… · название» — название уходит в подзаголовок
    sub = short(f"{rest} · {data['label']}", 64) if rest else data["label"]
    legend = "▲ лучше · ▼ хуже · ≈ изменение меньше 5%. Источник: Ozon Seller API."
    p = Page(0.35 + 0.78 + 3 * 1.0 + 3.0 + 0.7, SHEET_W)
    p.header(f"1/{n} · {short(head, 30)}", f"Ozon · {sub}")
    p.tiles(data["tiles"], per_row=2)
    p.line(data["series"], data["series_prev"], data["span"])
    _save(p, prefix, paths, legend)
    if top_rows:
        p = Page(0.35 + 0.78 + 1.0 + 0.42 * len(top_rows) + 0.7, SHEET_W)
        p.header(f"2/{n} · Топ-{len(top_rows)} товаров", f"{short(head, 30)} · {sub}")
        cols = [(0.05, "Товар", "left"), (0.60, "шт", "right"), (0.78, "к прошл.", "right"),
                (0.95, "остаток", "right")]
        p.table("Заказы за период и запас", cols, top_rows)
        _save(p, prefix, paths, "Остаток в днях — по скорости продаж за 14 дней.\n"
              "● красный < 20 дн · зелёный — норма · синий > 60 дн.")
    p = Page(0.35 + 0.78 + 0.7 + 0.62 * len(data["funnel"]) + 0.7, SHEET_W)
    p.header(f"{n}/{n} · Воронка", f"{short(head, 30)} · {sub}")
    p.funnel(data["funnel"])
    _save(p, prefix, paths, legend + "\n«Доставлено» — доставки за период, в том числе по прошлым заказам.")
    return paths


ORDER_HINT = "рекомендую к заказу"
SIGNAL_NOTE = ("Скорость — заказы за 14 дней с поправкой на сезонность категории. До приёмки — 7 дней.\n"
               "Сколько штук везти в какой кластер, считает бот поставок.")
SIGNAL_COLOR = {"zero": BAD, "late": BAD, "ship": WARN, "ok": GOOD, "slow": MUTED, "excess": EXCESS, "none": MUTED}


def ship_date(d, today):
    return "срочно" if d is None or d <= today else f"{d:%d.%m}"


def _zero_in(r):
    return "сейчас" if r["signal"] == "zero" or not r["days"] else f"{r['days']} дн"


def _names(rs, n=3):
    names = [short(r["cluster"].split(",")[0], 16) for r in rs[:n]]
    return ", ".join(names) + (f" +{len(rs) - n}" if len(rs) > n else "")


SHIP_COLS = [(0.05, "Товар", "left"), (0.45, "в день", "right"), (0.62, "ноль через", "right"),
             (0.80, "отгрузить до", "right"), (0.95, "свой", "right")]
PAID_COLS = [(0.05, "Товар", "left"), (0.48, "на Ozon", "right"), (0.62, "в день", "right"),
             (0.78, "оборач.", "right"), (0.95, "статус", "right")]
GRADE = {"GRADES_CRITICAL": ("критично", BAD), "GRADES_RED": ("платное", BAD), "GRADES_YELLOW": ("скоро", WARN)}


def ship_row(s, today):
    first = s["ship"][0]
    red = any(r["signal"] in ("zero", "late") for r in s["ship"])
    own = s.get("own")
    own_cell = ("—", MUTED, False) if own is None else (num(own), INK, True) if own > 0 else ("0", BAD, True)
    sub = f"{len(s['ship'])} кл.: {_names(s['ship'])}"
    if own is not None and own <= 0 and (s.get("profit") or 0) > 0:
        sub += f" · {ORDER_HINT}"
    return ([(s["offer_id"], BAD if red else WARN, True), (f"{s['speed']:.1f}", INK, False),
             (_zero_in(first), BAD if red else WARN, True), (ship_date(s["ship_by"], today), INK, True), own_cell],
            short(sub, 70))


def paid_row(s):
    return ([(s["offer_id"], INK, True), (num(s["stock"]), INK, False), (f"{s['speed']:.1f}", INK, False),
             (f"{s['turnover']:.0f} дн" if s.get("turnover") else "—", INK, False), (*GRADE[s["grade"]], True)],
            short(s["name"], 60))


def _sheets(title, subtitle, table, cols, rows, empty, prefix, note, per_sheet=14, max_sheets=3, tiles=None):
    chunks = [rows[i:i + per_sheet] for i in range(0, len(rows), per_sheet)][:max_sheets] or [[]]
    paths, n = [], len(chunks)
    for i, part in enumerate(chunks):
        p = Page(0.35 + 0.78 + (1.0 if tiles and i == 0 else 0) + 1.0 + 0.42 * max(1, len(part)) + 0.75, SHEET_W)
        p.header(f"{i + 1}/{n} · {title}" if n > 1 else title, subtitle)
        if tiles and i == 0:
            p.tiles(tiles)
        p.table(table, cols, part or [([(empty, GOOD, True)] + [None] * (len(cols) - 1), "")])
        more = len(rows) - per_sheet * max_sheets
        _save(p, prefix, paths, note + (f"\nЕщё {more} строк — в списке кнопок ниже." if i == n - 1 and more > 0 else ""))
    return paths


def signals_png(kind, items, today, prefix, title=None):
    """Сигналы по остаткам: kind = ship | resort | paid. items — сводки по SKU из signals.build."""
    if kind == "ship":
        cols = SHIP_COLS
        rows = [ship_row(x, today) for x in items]
        return _sheets(title or "Пора отгрузить", f"Ozon · запас кончится в ближайшие 3 недели · {len(items)} SKU",
                       "Кластеры, где кончается товар", cols, rows, "● отгружать пока рано", prefix,
                       SIGNAL_NOTE + "\nСвой — остаток МоегоСклада.")
    if kind == "resort":
        cols = [(0.05, "Товар", "left"), (0.50, "в день", "right"), (0.72, "лишнее, кл.", "right"),
                (0.95, "кончается, кл.", "right")]
        rows = [([(s["offer_id"], WARN, True), (f"{s['speed']:.1f}", INK, False),
                  (str(len(s["excess"])), EXCESS, True), (str(len(s["ship"])), BAD, True)],
                 short(f"лишнее: {_names(s['excess'], 2)} · кончается: {_names(s['ship'], 2)}", 72)) for s in items]
        return _sheets(title or "Пересорт", f"Ozon · в одних кластерах перебор, в других кончается · {len(items)} SKU",
                       "Перекос остатков между кластерами", cols, rows, "● перекосов нет", prefix,
                       "Лишнее — запаса больше чем на 60 дней (или нет продаж). Кончается — меньше 3 недель.\n"
                       "Новые поставки — туда, где кончается; в «лишние» кластеры пока не везти.\n"
                       "Сколько штук и куда, считает бот поставок.")
    cols = PAID_COLS
    rows = [paid_row(x) for x in items]
    return _sheets(title or "Платное хранение", f"Ozon · оценка оборачиваемости Ozon · {len(items)} SKU",
                   "Товары с долгой оборачиваемостью", cols, rows, "● платного хранения нет", prefix,
                   "Статус — оценка Ozon по оборачиваемости SKU. Что делать: не везти, акция или вывоз.\n"
                   "Источник: Ozon Seller API.")


def sku_signals_png(s, today, prefix):
    """Сигналы одного артикула по всем кластерам."""
    cols = [(0.05, "Кластер", "left"), (0.45, "есть", "right"), (0.60, "в день", "right"), (0.78, "хватит", "right"),
            (0.95, "отгрузить до", "right")]
    order = {"zero": 0, "late": 1, "ship": 2, "ok": 3, "excess": 4, "slow": 5, "none": 6}
    cl = sorted((r for r in s["clusters"] if r["signal"] != "none"), key=lambda r: (order[r["signal"]], r["days"] or 999))
    rows = []
    for r in cl:
        col = SIGNAL_COLOR[r["signal"]]
        days = "ноль" if r["signal"] == "zero" else f"{r['days']} дн" if r["days"] is not None else "> полгода"
        when = ship_date(r["ship_by"], today) if r["signal"] in ("zero", "late", "ship") else "—"
        rows.append(([(f"● {short(r['cluster'], 22)}", col, True), (num(r["stock"] + r["transit"]), INK, False),
                      (f"{r['speed']:.1f}", INK, False), (days, col, True), (when, INK, True)],
                     f"в пути {num(r['transit'])} шт" if r["transit"] else ""))
    tiles = [{"name": "Продажи в день, шт", "value": s["speed"], "plain": True},
             {"name": "Через месяц в день", "value": s["speed_next"], "plain": True},
             {"name": "Пора отгрузить, кл.", "value": len(s["ship"]), "plain": True}]
    sub = short(s["name"], 60) if not s["resort"] else "Пересорт: в одних кластерах перебор, в других кончается"
    return _sheets(short(s["offer_id"], 28), sub, "Запас по кластерам с учётом сезона", cols, rows,
                   "● нет остатков и продаж", prefix, SIGNAL_NOTE, per_sheet=16, tiles=tiles)


LOSS_NOTE = ("«От Ozon» — продажи по отчёту о реализации за вычетом комиссии и возвратов. Прибыль = от Ozon − логистика\n"
             "и прочие услуги Ozon (доля артикула) − реклама артикула − себестоимость +3% − УСН 7%.\n"
             "Реклама: «Оплата за заказ» — точно по заказам, кампании за клики — по выручке их товаров.")


def losses_png(title, subtitle, items, prefix):
    """Минусовые артикулы категории: выручка, реклама, себестоимость, прибыль за период."""
    cols = [(0.05, "Товар", "left"), (0.45, "от Ozon", "right"), (0.61, "реклама", "right"),
            (0.78, "себест.", "right"), (0.95, "прибыль", "right")]
    rows = [([(x["offer_id"] or x["sku"], INK, True), (num(x["revenue"]), INK, False), (num(x["ads"]), INK, False),
              (num(x["cogs"]), INK, False), (num(x["profit"]), BAD, True)],
             short(f"{num(x['units'])} шт · {x.get('name') or ''}", 64)) for x in items]
    return _sheets(title, subtitle, "Артикулы в минусе", cols, rows, "● минусовых нет", prefix, LOSS_NOTE)


def _chg(cur, old):
    return (cur - old) / abs(old) * 100 if old else None


def morning_png(m, prefix):
    """Сценарий 1: утренняя сводка — 4 листа по темам. Возвращает пути PNG."""
    o, ow = m["orders"], m["orders_week"]
    fin, finp = m["fin"], m["fin_prev"]
    groups = ["Поиск", "Поиск и рекомендации", "ОЗЗ"]
    services = sorted(((k, v) for k, v in fin["services"].items() if v), key=lambda x: x[1])[:10]
    ending, excess = m["ending"][:8], m["excess"][:6]
    drops = m["rating_drops"]
    bad = list(m["ms"].anomalies().items())[:8]
    day = f"{m['day']:%d.%m.%Y}"
    paths = []

    def sheet(n, title, n_tables, n_rows, tiles=0):
        h = 0.35 + 0.78 + tiles * 1.0 + n_tables * 1.0 + 0.42 * n_rows + 0.7
        p = Page(h, SHEET_W)
        p.header(f"{n}/4 · {title}", f"Ozon за {day}")
        return p

    def done(p, note):
        _save(p, prefix, paths, note)

    # 1. Главные цифры и категории
    p = sheet(1, "Главные цифры", 1, len(m["cats"]) + 1, tiles=3)
    p.tiles([
        {"name": "Заказы, ₽", "value": o["revenue"], "delta": _chg(o["revenue"], ow["revenue"])},
        {"name": "Заказы, шт", "value": o["ordered_units"], "delta": _chg(o["ordered_units"], ow["ordered_units"])},
        {"name": "Чистая прибыль, ₽", "value": m["profit"], "delta": _chg(m["profit"], m.get("profit_prev"))},
        {"name": "Общий ДРР, %", "value": m["drr"], "delta": _chg(m["drr"], m["drr_prev"]), "lower_is_better": True},
        {"name": "Реклама, ₽", "value": m["spend"], "delta": _chg(m["spend"], m["spend_prev"]), "lower_is_better": True},
        {"name": "Выкуп 30 дн, %", "value": m["buyout"], "delta": _chg(m["buyout"], m["buyout_prev"])},
    ], per_row=2)
    cols = [(0.05, "Категория", "left"), (0.62, "шт", "right"), (0.80, "₽", "right"), (0.95, "к нед.", "right")]
    rows = []
    for c in m["cats"]:
        lab, col = delta_style(_chg(c["cur"] or 0, c["wk"] or 0))
        rows.append(([(short(c["type_name"], 30), INK, True), (num(c["units"] or 0), INK, False),
                      (num(c["cur"] or 0), INK, False), (lab, col, True)], ""))
    p.table("Категории: заказы за день", cols, rows)
    done(p, f"Прибыль = начислено Ozon {num(fin['accrued'])} − себестоимость {num(m['cogs'])} (+3% косвенных) − УСН 7%."
         + (f" Нет себестоимости: {', '.join(m['no_cost'][:4])}{'…' if len(m['no_cost']) > 4 else ''}." if m["no_cost"] else "")
         + "\nЗаказы и выкуп — к тому же дню прошлой недели, прибыль и реклама — к позавчера.")

    # 2. Реклама и удержания
    p = sheet(2, "Реклама и удержания", 2, len(groups) + 1 + len(services) + 1)
    cols = [(0.05, "Группа РК", "left"), (0.52, "расход, ₽", "right"), (0.74, "заказы, ₽", "right"),
            (0.84, "ДРР", "right"), (0.95, "", "right")]
    rows = []
    for g in groups:
        a = m["ads"].get(g, {"spend": 0, "orders_money": 0})
        b = m["ads_prev"].get(g, {"spend": 0, "orders_money": 0})
        drr = a["spend"] / a["orders_money"] * 100 if a["orders_money"] else 0
        drr_p = b["spend"] / b["orders_money"] * 100 if b["orders_money"] else 0
        lab, col = delta_style(_chg(drr, drr_p), lower_is_better=True)
        rows.append(([(short(g, 20), INK, True), (num(a["spend"]), INK, False), (num(a["orders_money"]), INK, False),
                      (f"{drr:.1f}%", INK, True), (lab, col, True)], ""))
    lab, col = delta_style(_chg(m["drr"], m["drr_prev"]), lower_is_better=True)
    rows.append(([("Итого", INK, True), (num(m["spend"]), INK, True),
                  (num(o["revenue"]), INK, False), (f"{m['drr']:.1f}%", INK, True), (lab, col, True)], ""))
    p.table("Реклама и ДРР (к позавчера)", cols, rows)
    cols = [(0.05, "Статья", "left"), (0.74, "за день, ₽", "right"), (0.95, "к позавч.", "right")]
    rows = [([(short(service_label_short(k), 32), BAD if "fine" in k else INK, "fine" in k), (num(v), INK, False),
              delta_style(_chg(-v, -finp["services"].get(k, 0)), lower_is_better=True) + (True,)], "")
            for k, v in services]
    rows.insert(0, ([("Комиссия Ozon", INK, True), (num(fin["commission"]), INK, False),
                     delta_style(_chg(-fin["commission"], -finp["commission"]), lower_is_better=True) + (True,)], ""))
    p.table("Удержания Ozon (штрафы — красным)", cols, rows)
    done(p, "Итого ДРР — весь расход на рекламу ко всем заказам за день.\nИсточники: Ozon Performance API, Ozon Seller API.")

    # 3. Остатки: сигналы
    ship = m.get("ship")
    p = sheet(3, "Остатки и хранение", 2, max(1, len((ship or [])[:8])) + max(1, len(excess)))
    if ship is None:
        rows = [([("● сигналы не посчитались, см. кнопку «Сигналы»", MUTED, True), None, None, None, None], "")]
    else:
        today = m["day"] + timedelta(days=1)
        rows = [ship_row(x, today) for x in ship[:8]] or [([("● отгружать пока рано", GOOD, True), None, None, None, None], "")]
    resort = f", пересорт у {len(m['resort'])}" if m.get("resort") else ""
    p.table(f"Пора отгрузить: {len(ship or [])} SKU{resort}", SHIP_COLS, rows)
    cols = [(0.05, "Товар", "left"), (0.58, "остаток", "right"), (0.76, "в день", "right"), (0.95, "оборач.", "right")]
    p.table(f"Платное хранение: красных {len(m['excess'])}, скоро {len(m['soon'])} SKU", cols,
            [([(x["offer_id"], INK, True), (num(x["current_stock"]), INK, False), (f"{x['ads']:.1f}", INK, False),
               (f"{x['turnover']:.0f} дн", EXCESS, True)], short(x["name"], 48)) for x in excess]
            or [([("● перебора нет", GOOD, True), None, None, None], "")])
    done(p, "Ноль через — по скорости за 14 дней с поправкой на сезон. Отгрузить до — с учётом 7 дней до приёмки.\n"
         "Списки и пересорт — кнопка «Сигналы». Штуки по кластерам считает бот поставок.")

    # 4. Рейтинг и МойСклад
    n_drops = 1 if not drops else len(drops[:6])
    p = sheet(4, "Рейтинг и проверки", 1 + bool(bad), n_drops + len(bad))
    cols = [(0.05, "Товар", "left"), (0.78, "было", "right"), (0.95, "стало", "right")]
    if drops is None:
        rows = [([("База собирается, сравнение — с завтра", MUTED, False), None, None], "")]
    else:
        rows = [([(r["offer_id"] or r["sku"], INK, True), (f"{r['old']:.0f}", INK, False), (f"{r['new']:.0f}", BAD, True)],
                 short(r["name"] or "", 48)) for r in drops[:6]] or [([("● падений нет", GOOD, True), None, None], "")]
    p.table("Контент-рейтинг упал ко вчера", cols, rows)
    if bad:
        cols = [(0.05, "Артикул", "left"), (0.66, "остаток", "right"), (0.95, "себестоим.", "right")]
        p.table("МойСклад: минус в остатке или себестоимости", cols,
                [([(k, BAD, True), (num(v["stock"]), BAD if v["stock"] < 0 else INK, False),
                   (f"{v.get('avg_cost', v['cost']):.2f} ₽", BAD if v.get("bad_cost") else INK, False)], "")
                 for k, v in bad])
    done(p, "Налог — УСН 7% от (доходы − расходы).\nИсточники: Ozon Seller API, Ozon Performance API, МойСклад.")
    return paths


def service_label_short(name):
    from bot.morning import service_label
    return short(service_label(name), 44)


POS_COLOR = {"up": GOOD, "down": BAD, "flat": MUTED, "none": MUTED}
POS_NOTE = ("Средняя позиция в поиске и каталоге Ozon за день: чем меньше число, тем выше товар.\n"
            "Зелёная ячейка — поднялся к предыдущему дню, красная — опустился. Справа — на сколько мест\n"
            "изменилась позиция за период (▲ выше, ▼ ниже). Полные 28 дней — в Excel. Источник: Ozon Seller API.")


def _pos(p):
    return "—" if p is None else num(p)


POS_UP, POS_DOWN = "#e3f4e3", "#fbe4e4"  # фон ячейки: поднялся / опустился к предыдущему дню


def _cell(fig, x, y, w, h, color):
    ax = fig.add_axes([x, y, w, h])
    ax.set_facecolor(color)
    ax.set_xticks([]), ax.set_yticks([])
    for s in ax.spines.values():
        s.set_visible(False)


def _bg(cur, old, move):
    kind = move(cur, old)[1]
    return POS_UP if kind == "up" else POS_DOWN if kind == "down" else None


def positions_matrix_png(title, subtitle, rows, days, move, prefix, per_sheet=16):
    """Таблица-картинка: строка — артикул, столбцы — дни, в ячейке место.
    rows: [{"offer_id", "name", "cells": [позиция за день до первого дня, затем по дням]}]."""
    n = len(days)
    x0, x1 = 0.05, 0.95
    first = 0.30  # ширина столбца с артикулом
    last = 0.10  # столбец «за период»
    cw = (x1 - x0 - first - last) / n
    chunks = [rows[i:i + per_sheet] for i in range(0, len(rows), per_sheet)][:4] or [[]]
    paths = []
    for k, part in enumerate(chunks):
        rh = 0.46
        p = Page(0.35 + 0.78 + 0.5 + rh * max(1, len(part)) + 0.75, SHEET_W)
        p.header(f"{k + 1}/{len(chunks)} · {title}" if len(chunks) > 1 else title, subtitle)
        hy = p.y - 0.2
        p.fig.text(x0, hy / p.h, "Артикул", fontsize=8.5, color=MUTED)
        for j, d in enumerate(days):
            p.fig.text(x0 + first + cw * (j + 0.5), hy / p.h, f"{d:%d.%m}", fontsize=8, color=MUTED, ha="center")
        p.fig.text(x1, hy / p.h, f"за {n} дн", fontsize=8, color=MUTED, ha="right")
        p.y = hy - 0.12
        for r in part:
            p.y -= rh
            y = p.y / p.h
            cells = r["cells"]
            for j in range(n):
                cur, old = cells[j + 1], cells[j]
                bg = _bg(cur, old, move)
                if bg:
                    _cell(p.fig, x0 + first + cw * j + 0.002, y + 0.03 / p.h, cw - 0.004, (rh - 0.06) / p.h, bg)
                p.fig.text(x0 + first + cw * (j + 0.5), (p.y + rh / 2) / p.h, _pos(cur), fontsize=9,
                           color=INK if cur is not None else MUTED, ha="center", va="center",
                           fontweight="bold" if j == n - 1 else "normal")
            p.fig.text(x0, (p.y + rh * 0.62) / p.h, short(r["offer_id"], 20), fontsize=9, fontweight="bold", color=INK,
                       va="center")
            p.fig.text(x0, (p.y + rh * 0.24) / p.h, short(r.get("name") or "", 34), fontsize=6.5, color=INK2,
                       va="center")
            lab, kind = move(cells[-1], next((c for c in cells[1:] if c is not None), None))
            p.fig.text(x1, (p.y + rh / 2) / p.h, lab if kind != "none" else "—", fontsize=9, fontweight="bold",
                       color=POS_COLOR[kind], ha="right", va="center")
            p.fig.add_artist(plt.Line2D([x0, x1], [y] * 2, color=GRID, lw=0.5))
        if not part:
            p.y -= rh
            p.fig.text(x0, p.y / p.h, "● нет данных о позициях", fontsize=9.5, color=MUTED)
        path = f"{prefix}_{len(paths) + 1}.png"
        p.footer(POS_NOTE)
        p.save(path)
        paths.append(path)
    return paths


def position_calendar_png(title, subtitle, cells, days, move, prefix):
    """Артикул: таблица-календарь за 28 дней — неделя в строке, в ячейке дата и место."""
    p = Page(0.35 + 0.78 + 0.45 + 4 * 0.72 + 0.75, SHEET_W)
    p.header(short(title, 30), subtitle)
    x0, x1 = 0.05, 0.95
    cw = (x1 - x0) / 7
    rh = 0.72
    p.y -= 0.2
    for w in range(len(days) // 7):
        p.y -= rh
        y = p.y / p.h
        for j in range(7):
            i = w * 7 + j
            cur, old = cells[i + 1], cells[i]
            _cell(p.fig, x0 + cw * j + 0.004, y + 0.04 / p.h, cw - 0.008, (rh - 0.08) / p.h,
                  _bg(cur, old, move) or TILE)
            cx = x0 + cw * (j + 0.5)
            p.fig.text(cx, (p.y + rh * 0.72) / p.h, f"{days[i]:%d.%m}", fontsize=7.5, color=INK2, ha="center",
                       va="center")
            p.fig.text(cx, (p.y + rh * 0.36) / p.h, _pos(cur), fontsize=12, fontweight="bold",
                       color=INK if cur is not None else MUTED, ha="center", va="center")
    path = f"{prefix}_1.png"
    p.footer("Средняя позиция в поиске и каталоге Ozon за день: чем меньше число, тем выше товар.\n"
             "Зелёный — поднялся к предыдущему дню, красный — опустился, серый — изменение меньше 5%. "
             "Источник: Ozon Seller API.")
    p.save(path)
    return [path]


KW_NOTE = ("Место — средняя позиция в выдаче за период (Ozon, аналитика запросов), ▲ — поднялись выше к прошлому\n"
           "такому же периоду. Заказы по запросу — реклама и органика вместе: Ozon их не делит. «РК» — запрос есть\n"
           "в активной кампании «Вывод в топ».")
KW_AD_NOTE = ("Только активные кампании. По фразам Ozon отдаёт показы и клики, расхода и заказов по фразе нет.\n"
              "Заказы с рекламы — атрибуция Ozon по SKU, органика = все заказы − рекламные.")


def _place(cur, prev, has_prev=True):
    if cur is None:
        return ("—", MUTED), ("", MUTED)
    place = (f"{cur:.0f}", INK)
    if prev is None and not has_prev:
        return place, ("", MUTED)
    if prev is None:
        return place, ("нов.", MUTED)
    d = round(prev) - round(cur)
    if d == 0:
        return place, ("≈", MUTED)
    return place, (f"{'▲' if d > 0 else '▼'}{abs(d)}", GOOD if d > 0 else BAD)


def keywords_png(data, prefix):
    """«Ключи»: лист топ-15 запросов, для артикула ещё лист рекламных фраз. Возвращает пути PNG."""
    period = f"Ozon · {data['start']:%d.%m}–{data['end']:%d.%m} · {data['days']} дн"
    if data["kind"] == "s":
        title = f"{data['offer_id']} · {short(data['name'] or '', 26)}"
        tiles = [{"name": "Заказы всего, шт", "value": data["total_orders"], "plain": True},
                 {"name": "С рекламы, шт", "value": data["ad_orders"], "plain": True},
                 {"name": "Органика, шт", "value": data["organic_orders"], "plain": True}]
    else:
        title = short(data["name"], 30)
        q_orders = sum(r["orders"] or 0 for r in data["rows"])
        tiles = [{"name": "Заказы категории, шт", "value": data["total_orders"], "plain": True},
                 {"name": "С топ-15 запросов, шт", "value": q_orders, "plain": True},
                 {"name": "Артикулов", "value": data["n_skus"], "plain": True}]
    cols = [(0.05, "Запрос", "left"), (0.62, "место", "right"), (0.72, "", "right"), (0.84, "заказы", "right"),
            (0.95, "искали", "right")]
    rows = []
    has_prev = any(r["prev_position"] is not None for r in data["rows"])
    for r in data["rows"]:
        place, delta = _place(r["position"], r["prev_position"], has_prev)
        bits = [f"{num(r['gmv'] or 0)} ₽"]
        if data["kind"] == "s":
            bits.append(f"конв. {r['conv'] or 0:.1f}%")
            if not r["in_title"]:
                bits.append("нет в названии")
        else:
            bits.append(f"лучше всех {r['best_offer']} (место {r['best_position']:.0f})" if r["best_position"] else "")
            bits.append(f"{r['n_skus']} арт.")
        if r.get("ad"):
            bits.append("РК")
        rows.append(([(short(r["query"], 30), INK, True), (place[0], place[1], True), (delta[0], delta[1], True),
                      (num(r["orders"] or 0), INK, False), (num(r["searches"] or 0), INK2, False)],
                     " · ".join(b for b in bits if b)))
    paths = _sheets(title, period, "Топ-15 запросов", cols, rows, "● запросов за период нет", prefix + "_q",
                    KW_NOTE + ("" if has_prev or not data["rows"] else
                               f"\nСравнения с прошлыми {data['days']} дн пока нет: Ozon хранит запросы 35 дней, бот копит историю сам."),
                    per_sheet=15, max_sheets=1, tiles=tiles)
    if data["kind"] != "s":
        return paths
    ad_tiles = [{"name": "Расход РК, ₽", "value": data["ad_spent"], "plain": True},
                {"name": "ДРР артикула, %", "value": data["drr"] or 0, "plain": True},
                {"name": "Доля органики, %", "plain": True,
                 "value": data["organic_orders"] / data["total_orders"] * 100 if data["total_orders"] else 0}]
    cols = [(0.05, "Фраза в рекламе", "left"), (0.66, "показы", "right"), (0.81, "клики", "right"),
            (0.95, "CTR", "right")]
    rows = [([(short(x["query"], 32), INK, True), (num(x["views"]), INK, False), (num(x["clicks"]), INK, False),
              (f"{x['clicks'] / x['views'] * 100:.1f}%" if x["views"] else "—", INK2, False)], "")
            for x in data["phrases"][:15]]
    camps = ", ".join(c[2] for c in data["campaigns"] if c[1] == "PLACEMENT_TOP_PROMOTION")
    empty = "● нет активной кампании «Вывод в топ»" if not camps else "● фраз за период нет"
    return paths + _sheets(title, period + (f" · {short(camps, 30)}" if camps else ""), "Запросы РК", cols, rows,
                           empty, prefix + "_a", KW_AD_NOTE, per_sheet=15, max_sheets=1, tiles=ad_tiles)
