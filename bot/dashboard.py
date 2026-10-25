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
SUPPLY_COLS = [(0.05, "", "left"), (0.50, "есть", "right"), (0.62, "дней", "right"), (0.77, "нужно", "right"),
               (0.95, "везти", "right")]


def supply_row(r, label, sub, hint=True):
    """Строка «грузить»: полная потребность склада и сколько везти из своего остатка."""
    col = BAD if r["status"] == "urgent" else WARN
    d = f"{r['days']:.0f}" if r["days"] is not None else "—"
    ship = r.get("ship", r["need"])
    act = (f"{num(ship)} шт", INK, True) if ship else ("своего нет", MUTED, True)
    if hint and r.get("order") and (r.get("profit") or 0) > 0:
        sub = f"{sub} · {ORDER_HINT}"
    return ([(label, col, True), (num(r["stock"] + r["transit"]), INK, False), (d, col, True),
             (num(r["need"]), INK, False), act], sub)


def excess_row(r, label, sub, hint=True):
    d = f"{r['days']:.0f}" if r["days"] is not None else "—"
    return ([(label, EXCESS, True), (num(r["stock"] + r["transit"]), INK, False), (d, EXCESS, True),
             None, (f"лишние ~{num(r['excess'])}", EXCESS, True)], sub)


def supply_png(title, subtitle, rows, prefix, by="offer", tiles=None, per_sheet=12, max_sheets=4):
    """Поставки: по складу (строки — артикулы) или по артикулу (строки — склады). Возвращает пути PNG."""
    load = [r for r in rows if r["status"] in ("urgent", "low") and r["need"] > 0]
    excess = [r for r in rows if r["status"] == "excess"] if by == "cluster" else []
    first = "Склад" if by == "cluster" else "Артикул"
    cols = [(SUPPLY_COLS[0][0], first, "left")] + SUPPLY_COLS[1:]

    def fmt(r, fn):
        if by == "cluster":
            return fn(r, f"● {short(r['cluster'], 22)}", f"продаёт {r['ads']:.1f} шт/день", hint=False)
        return fn(r, r["offer_id"], short(r["name"], 48))

    chunks = [load[i:i + per_sheet] for i in range(0, len(load), per_sheet)][:max_sheets] or [[]]
    n, paths = len(chunks), []
    for i, part in enumerate(chunks):
        last = i == n - 1
        n_rows = max(1, len(part)) + (max(1, len(excess)) if last and excess else 0)
        p = Page(0.35 + 0.78 + (1.0 if tiles and i == 0 else 0) + 1.0 * (1 + (last and bool(excess)))
                 + 0.42 * n_rows + 0.7, SHEET_W)
        p.header(f"{i + 1}/{n} · {short(title, 28)}" if n > 1 else short(title, 32), subtitle)
        if tiles and i == 0:
            p.tiles(tiles)
        p.table("Грузить: запаса меньше 20 дней", cols,
                [fmt(r, supply_row) for r in part] or [([("● везти не нужно", GOOD, True), None, None, None, None], "")])
        if last and excess:
            p.table("Перебор: больше 60 дней", cols, [fmt(r, excess_row) for r in excess])
        more = len(load) - per_sheet * max_sheets
        _save(p, prefix, paths, ("Есть — на складе Ozon с учётом пути. Нужно — до 30 дней продаж. "
                                 "Везти — из своего остатка\n(МойСклад), сначала туда, где кончится раньше. "
                                 f"«{ORDER_HINT}» — своего мало,\nа товар в чистой прибыли за 2 месяца.")
              + (f"\nЕщё {more} строк не поместились — смотрите по артикулу." if last and more > 0 else ""))
    return paths


def restock_png(title, rows, summary, prefix, limit=8):
    """Сценарий 3 из карточки категории или артикула: листы «грузить» и «перебор». Возвращает пути PNG."""
    load = [r for r in rows if r["status"] in ("urgent", "low") and r["need"] > 0][:limit]
    excess = sorted([r for r in rows if r["status"] == "excess"], key=lambda r: -r["excess"])[:limit]
    cols = [(0.05, "Склад · товар", "left")] + SUPPLY_COLS[1:]

    def label(r):
        return f"● {short(r['cluster'], 20)}", f"{r['offer_id']} · {short(r['name'], 40)}"

    paths = []
    p = Page(0.35 + 0.78 + 1.0 + 1.0 + 0.42 * max(1, len(load)) + 0.7, SHEET_W)
    p.header("1/2 · Грузить", f"Подгрузка: {title} · норма ≥ 20 дн")
    p.tiles([
        {"name": "Всего на Ozon, шт", "value": summary["stock"], "plain": True},
        {"name": "Продажи в день, шт", "value": summary["ads"], "plain": True},
        {"name": "Хватит на, дн", "value": summary["days"] or 0, "plain": True},
    ])
    p.table("Запаса меньше 20 дней", cols, [supply_row(r, *label(r)) for r in load]
            or [([("● везде норма", GOOD, True), None, None, None, None], "")])
    _save(p, prefix, paths, "Нужно — до 30 дней продаж. Везти — из своего остатка (МойСклад), сначала туда,\n"
          f"где кончится раньше. «{ORDER_HINT}» — своего мало, а товар в прибыли за 2 мес.")
    p = Page(0.35 + 0.78 + 1.0 + 0.42 * max(1, len(excess)) + 0.7, SHEET_W)
    p.header("2/2 · Перебор", f"Подгрузка: {title} · перебор > 60 дн")
    p.table("Оборачиваемость больше 60 дней", cols, [excess_row(r, *label(r)) for r in excess]
            or [([("● перебора нет", GOOD, True), None, None, None, None], "")])
    _save(p, prefix, paths, "Лишнее — сверх 60 дней продаж: вывезти или переместить.\n"
          "Скорость по складу — средняя по данным Ozon.")
    return paths


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
    done(p, f"Прибыль = начислено Ozon {num(fin['accrued'])} − себестоимость {num(m['cogs'])} − УСН 7%."
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

    # 3. Остатки
    p = sheet(3, "Остатки и хранение", 2, max(1, len(ending)) + max(1, len(excess)))
    cols = [(0.05, "Товар", "left"), (0.45, "на Ozon", "right"), (0.60, "в день", "right"), (0.74, "хватит", "right"),
            (0.95, "свой склад", "right")]
    rows = []
    for x in ending:
        own = (m["ms"].get(x["offer_id"]) or {}).get("stock", 0)
        good = (m.get("profits", {}).get(str(x.get("sku"))) or {}).get("profit", 0) > 0
        rows.append(([(x["offer_id"], INK, True), (num(x["current_stock"]), INK, False), (f"{x['ads']:.1f}", INK, False),
                      (f"{x['idc']:.0f} дн", BAD, True),
                      (f"{num(own)} шт", INK, True) if own > 0 else ("0", BAD, True)],
                     short(x["name"], 48) if own > 0 or not good else f"{short(x['name'], 34)} · {ORDER_HINT}"))
    p.table(f"Скоро закончится: меньше 20 дней ({len(m['ending'])} SKU)", cols,
            rows or [([("● всё в норме", GOOD, True), None, None, None, None], "")])
    cols = [(0.05, "Товар", "left"), (0.58, "остаток", "right"), (0.76, "в день", "right"), (0.95, "оборач.", "right")]
    rows = [([(x["offer_id"], INK, True), (num(x["current_stock"]), INK, False), (f"{x['ads']:.1f}", INK, False),
              (f"{x['turnover']:.0f} дн", EXCESS, True)], short(x["name"], 48)) for x in excess]
    p.table(f"Платное хранение: красных {len(m['excess'])}, скоро {len(m['soon'])} SKU", cols,
            rows or [([("● перебора нет", GOOD, True), None, None, None], "")])
    done(p, "Полный список по складам — кнопка «Поставки» в меню. Свой склад — МойСклад.\n"
         f"«{ORDER_HINT}» — своего нет, а товар в чистой прибыли с 1-го числа прошлого месяца.")

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
