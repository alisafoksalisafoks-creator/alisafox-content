"""PNG-дашборды для Telegram: плитки, график, таблица товаров, воронка, подгрузка."""
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

    def __init__(self, height):
        self.h = height
        self.fig = plt.figure(figsize=(W_IN, height), dpi=160, facecolor=SURFACE)
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

    def tiles(self, tiles):
        w, gx, th, gy = 0.29, 0.02, 0.85, 0.15
        for row in range(0, len(tiles), 3):
            self.y -= th
            for i, t in enumerate(tiles[row:row + 3]):
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


def card_png(title, data, path, top_rows=None):
    """Сценарий 2 и карточка категории: плитки, график, (топ товаров), воронка."""
    h = 0.35 + 0.78 + 2 * 1.0 + 3.0 + (0.9 + 0.42 * len(top_rows) if top_rows else 0) + 0.7 + 0.62 * 5 + 0.5
    p = Page(h)
    p.header(title, f"Ozon · {data['label']}")
    p.tiles(data["tiles"])
    p.line(data["series"], data["series_prev"], data["span"])
    if top_rows:
        cols = [(0.05, "Товар", "left"), (0.66, "шт", "right"), (0.80, "к прошлому", "right"),
                (0.95, "остаток", "right")]
        p.table(f"Топ-{len(top_rows)} товаров", cols, top_rows)
    p.funnel(data["funnel"])
    p.footer("▲ лучше · ▼ хуже · ≈ изменение меньше 5%. Источник: Ozon Seller API.\n"
             "«Доставлено» — доставки за период, в том числе по заказам прошлых периодов.")
    p.save(path)


def restock_png(title, rows, summary, path, limit=8, catalog=None):
    load = [r for r in rows if r["status"] in ("urgent", "low")][:limit]
    excess = sorted([r for r in rows if r["status"] == "excess"], key=lambda r: -r["excess"])[:limit]
    h = 0.35 + 0.78 + 1.0 + 2 * 0.9 + 0.42 * (max(1, len(load)) + max(1, len(excess))) + 0.7
    p = Page(h)
    p.header(f"Подгрузка: {title}", "Ozon · норма на складе ≥ 20 дн, перебор > 60 дн")
    p.tiles([
        {"name": "Всего на Ozon, шт", "value": summary["stock"], "plain": True},
        {"name": "Продажи в день, шт", "value": summary["ads"], "plain": True},
        {"name": "Хватит на, дн", "value": summary["days"] or 0, "plain": True},
    ])
    cols = [(0.05, "Склад · товар", "left"), (0.58, "остаток", "right"), (0.71, "дней", "right"),
            (0.95, "что сделать", "right")]

    def row(r, col, act):
        d = f"{r['days']:.0f}" if r["days"] is not None else "—"
        return ([(f"● {r['cluster']}", col, True), (num(r["stock"] + r["transit"]), INK, False),
                 (d, col, True), (act, INK, True)], f"{r['offer_id']} · {short(r['name'], 60)}")

    def load_act(r):
        if catalog is None:
            return f"везти {r['need']} шт"
        own = (catalog.get(r["offer_id"]) or {}).get("stock", 0)
        return f"везти {min(r['need'], own)} шт" if own > 0 else "своего 0 — заказать"

    p.table("Грузить: запаса меньше 20 дней", cols,
            [row(r, BAD if r["status"] == "urgent" else WARN, load_act(r)) for r in load]
            or [([("● везде норма", GOOD, True), None, None, None], "")])
    p.table("Перебор: оборачиваемость больше 60 дней", cols,
            [row(r, EXCESS, f"лишние ~{r['excess']} шт") for r in excess]
            or [([("● перебора нет", GOOD, True), None, None, None], "")])
    p.footer("Остаток с учётом товара в пути. Скорость по складу — средняя по данным Ozon. Свой склад — МойСклад.\n"
             "Источник: Ozon Seller API, остатки по кластерам.")
    p.save(path)


def _chg(cur, old):
    return (cur - old) / abs(old) * 100 if old else None


def morning_png(m, path):
    """Сценарий 1: утренняя сводка."""
    o, op, ow = m["orders"], m["orders_prev"], m["orders_week"]
    fin, finp = m["fin"], m["fin_prev"]
    groups = ["Поиск", "Поиск и рекомендации", "ОЗЗ"]
    services = sorted(((k, v) for k, v in fin["services"].items() if v), key=lambda x: x[1])[:10]
    ending, excess = m["ending"][:8], m["excess"][:6]
    drops = m["rating_drops"]
    n_rows = min(8, len(m["ms"].anomalies())) + 1 + len(m["cats"]) + len(groups) + 1 + len(services) + 2 + max(1, len(ending)) + max(1, len(excess)) + max(1, len(drops or [])[:6] if drops else 1)
    h = 0.35 + 0.78 + 3 * 1.0 + 6 * 0.9 + 0.42 * n_rows + 0.8
    p = Page(h)
    p.header("Утренняя сводка Ozon", f"за {m['day']:%d.%m.%Y} · к позавчера и к тому же дню прошлой недели")
    p.tiles([
        {"name": "Заказы, ₽", "value": o["revenue"], "delta": _chg(o["revenue"], ow["revenue"])},
        {"name": "Заказы, шт", "value": o["ordered_units"], "delta": _chg(o["ordered_units"], ow["ordered_units"])},
        {"name": "Чистая прибыль, ₽", "value": m["profit"], "plain": True},
        {"name": "Общий ДРР, %", "value": m["drr"], "delta": _chg(m["drr"], m["drr_prev"]), "lower_is_better": True},
        {"name": "Реклама, ₽", "value": m["spend"], "delta": _chg(m["spend"], m["spend_prev"]), "lower_is_better": True},
        {"name": "Выкуп 30 дн, %", "value": m["buyout"], "delta": _chg(m["buyout"], m["buyout_prev"])},
    ])
    p.fig.text(0.05, (p.y - 0.05) / p.h,
               f"Прибыль = начислено Ozon {num(fin['accrued'])} − себестоимость проданного {num(m['cogs'])} − УСН 7%."
               + (f" Нет себестоимости: {', '.join(m['no_cost'][:4])}{'…' if len(m['no_cost']) > 4 else ''}" if m["no_cost"] else ""),
               fontsize=7.5, color=MUTED)
    p.fig.text(0.05, (p.y - 0.22) / p.h, "Заказы и выкуп — к тому же дню прошлой недели; реклама — к позавчера.",
               fontsize=7.5, color=MUTED)
    p.y -= 0.3

    cols = [(0.05, "Категория", "left"), (0.62, "шт", "right"), (0.80, "₽", "right"), (0.95, "к нед. назад", "right")]
    rows = []
    for c in m["cats"]:
        lab, col = delta_style(_chg(c["cur"] or 0, c["wk"] or 0))
        rows.append(([(short(c["type_name"], 40), INK, True), (num(c["units"] or 0), INK, False),
                      (num(c["cur"] or 0), INK, False), (lab, col, True)], ""))
    p.table("Категории: заказы за день", cols, rows)

    cols = [(0.05, "Группа РК", "left"), (0.50, "расход, ₽", "right"), (0.70, "заказы по РК, ₽", "right"),
            (0.83, "ДРР", "right"), (0.95, "к позавч.", "right")]
    rows = []
    for g in groups:
        a = m["ads"].get(g, {"spend": 0, "orders_money": 0})
        b = m["ads_prev"].get(g, {"spend": 0, "orders_money": 0})
        drr = a["spend"] / a["orders_money"] * 100 if a["orders_money"] else 0
        drr_p = b["spend"] / b["orders_money"] * 100 if b["orders_money"] else 0
        lab, col = delta_style(_chg(drr, drr_p), lower_is_better=True)
        rows.append(([(g, INK, True), (num(a["spend"]), INK, False), (num(a["orders_money"]), INK, False),
                      (f"{drr:.1f}%", INK, True), (lab, col, True)], ""))
    lab, col = delta_style(_chg(m["drr"], m["drr_prev"]), lower_is_better=True)
    rows.append(([("Итого ко всем заказам", INK, True), (num(m["spend"]), INK, True),
                  (num(o["revenue"]), INK, False), (f"{m['drr']:.1f}%", INK, True), (lab, col, True)], ""))
    p.table("Реклама и ДРР", cols, rows)

    cols = [(0.05, "Статья", "left"), (0.72, "за день, ₽", "right"), (0.95, "к позавч.", "right")]
    rows = [([(service_label_short(k), BAD if "fine" in k else INK, "fine" in k), (num(v), INK, False),
              delta_style(_chg(-v, -finp["services"].get(k, 0)), lower_is_better=True) + (True,)], "")
            for k, v in services]
    rows.insert(0, ([("Комиссия Ozon", INK, True), (num(fin["commission"]), INK, False),
                     delta_style(_chg(-fin["commission"], -finp["commission"]), lower_is_better=True) + (True,)], ""))
    p.table("Удержания Ozon (штрафы — красным)", cols, rows)

    cols = [(0.05, "Товар", "left"), (0.50, "на Ozon", "right"), (0.63, "в день", "right"), (0.75, "хватит", "right"),
            (0.95, "свой склад", "right")]
    rows = []
    for x in ending:
        own = (m["ms"].get(x["offer_id"]) or {}).get("stock", 0)
        rows.append(([(x["offer_id"], INK, True), (num(x["current_stock"]), INK, False), (f"{x['ads']:.1f}", INK, False),
                      (f"{x['idc']:.0f} дн", BAD, True),
                      (f"{num(own)} шт" if own > 0 else "0 — заказать", INK if own > 0 else BAD, True)],
                     short(x["name"], 62)))
    p.table(f"Скоро закончится: запаса меньше 20 дней ({len(m['ending'])} SKU)", cols,
            rows or [([("● всё в норме", GOOD, True), None, None, None], "")])

    cols = [(0.05, "Товар", "left"), (0.62, "остаток", "right"), (0.78, "в день", "right"), (0.95, "оборач.", "right")]
    rows = [([(x["offer_id"], INK, True), (num(x["current_stock"]), INK, False), (f"{x['ads']:.1f}", INK, False),
              (f"{x['turnover']:.0f} дн", EXCESS, True)], short(x["name"], 62)) for x in excess]
    p.table(f"Платное хранение: красная зона {len(m['excess'])} SKU, жёлтая (скоро) {len(m['soon'])} SKU", cols,
            rows or [([("● перебора нет", GOOD, True), None, None, None], "")])

    cols = [(0.05, "Товар", "left"), (0.78, "было", "right"), (0.95, "стало", "right")]
    if drops is None:
        rows = [([("База рейтинга собирается с сегодняшнего дня, сравнение — с завтра", MUTED, False), None, None], "")]
    else:
        rows = [([(r["offer_id"] or r["sku"], INK, True), (f"{r['old']:.0f}", INK, False), (f"{r['new']:.0f}", BAD, True)],
                 short(r["name"] or "", 62)) for r in drops[:6]] or [([("● падений нет", GOOD, True), None, None], "")]
    p.table("Контент-рейтинг упал ко вчера", cols, rows)
    bad = m["ms"].anomalies()
    if bad:
        cols = [(0.05, "Артикул МоегоСклада", "left"), (0.72, "остаток", "right"), (0.95, "себестоимость", "right")]
        p.table("Проверьте в МоёмСкладе: минус в остатке или себестоимости", cols,
                [([(k, BAD, True), (num(v["stock"]), BAD if v["stock"] < 0 else INK, False),
                   (f"{v.get('avg_cost', v['cost']):.2f} ₽", BAD if v.get("bad_cost") else INK, False)], "")
                 for k, v in list(bad.items())[:8]])
    p.footer("Себестоимость и свой склад — МойСклад. Налог — УСН 7% от (доходы − расходы).\n"
             "Источники: Ozon Seller API, Ozon Performance API, МойСклад.")
    p.save(path)


def service_label_short(name):
    from bot.morning import service_label
    return short(service_label(name), 44)
