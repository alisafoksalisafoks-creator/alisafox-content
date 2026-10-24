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
    return f"{'▲' if up else '▼'} {delta:+.0f}%", GOOD if up != lower_is_better else BAD


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


def restock_png(title, rows, summary, path, limit=8):
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

    p.table("Грузить: запаса меньше 20 дней", cols,
            [row(r, BAD if r["status"] == "urgent" else WARN, f"везти {r['need']} шт") for r in load]
            or [([("● везде норма", GOOD, True), None, None, None], "")])
    p.table("Перебор: оборачиваемость больше 60 дней", cols,
            [row(r, EXCESS, f"лишние ~{r['excess']} шт") for r in excess]
            or [([("● перебора нет", GOOD, True), None, None, None], "")])
    p.footer("Остаток с учётом товара в пути. Скорость продаж по складу — средняя по данным Ozon.\n"
             "Источник: Ozon Seller API, остатки по кластерам.")
    p.save(path)
