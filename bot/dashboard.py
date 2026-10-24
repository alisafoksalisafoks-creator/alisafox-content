"""PNG-дашборды для Telegram."""
from datetime import timedelta

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

from bot.category_card import FLAT, LOW_DAYS, OK_DAYS, fmt_value, short  # noqa: E402

INK, INK2, MUTED, GRID = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e1"
SURFACE, TILE = "#fcfcfb", "#f3f2ef"
BLUE, PREV = "#2a78d6", "#b5b4ae"
GOOD, WARN, BAD = "#0ca30c", "#eda100", "#d03b3b"

for f in ("Inter", "DejaVu Sans"):
    if any(f in x.name for x in font_manager.fontManager.ttflist):
        plt.rcParams["font.family"] = f
        break


def delta_style(delta, lower_is_better=False):
    if delta is None:
        return "новинка", MUTED
    if abs(delta) < FLAT:
        return f"≈ {delta:+.0f}%", MUTED
    up = delta > 0
    good = up != lower_is_better
    return f"{'▲' if up else '▼'} {delta:+.0f}%", GOOD if good else BAD


def stock_style(days):
    if days is None:
        return "нет продаж", MUTED
    if days < LOW_DAYS:
        return f"{days:.0f} дн", BAD
    if days < OK_DAYS:
        return f"{days:.0f} дн", WARN
    return f"{days:.0f} дн", GOOD


def category_png(category, day, data, path, top=8):
    fig = plt.figure(figsize=(8, 10.4), dpi=160, facecolor=SURFACE)
    start = day - timedelta(days=6)
    fig.text(0.05, 0.965, category, fontsize=17, fontweight="bold", color=INK, va="top")
    fig.text(0.05, 0.935, f"Ozon · неделя {start:%d.%m}–{day:%d.%m.%Y} к прошлой неделе",
             fontsize=10, color=INK2, va="top")

    # KPI-плитки 3×2
    w, h, gx, gy, x0, y0 = 0.29, 0.085, 0.02, 0.015, 0.05, 0.815
    for i, t in enumerate(data["tiles"]):
        x = x0 + (i % 3) * (w + gx)
        y = y0 - (i // 3) * (h + gy)
        ax = fig.add_axes([x, y, w, h])
        ax.set_facecolor(TILE)
        ax.set_xticks([]), ax.set_yticks([])
        for s in ax.spines.values():
            s.set_visible(False)
        label, color = delta_style(t["delta"], t["lower_is_better"])
        ax.text(0.06, 0.78, t["name"], fontsize=8.5, color=INK2, transform=ax.transAxes, va="center")
        ax.text(0.06, 0.36, fmt_value(t["name"], t["value"]), fontsize=15, fontweight="bold",
                color=INK, transform=ax.transAxes, va="center")
        ax.text(0.94, 0.36, label, fontsize=10, fontweight="bold", color=color,
                transform=ax.transAxes, va="center", ha="right")

    # Заказы по дням: 28 дней против предыдущих 28
    ax = fig.add_axes([0.08, 0.455, 0.87, 0.2])
    xs = list(range(28))
    cur = [v for _, v in data["series"]]
    ax.plot(xs, data["series_prev"], color=PREV, lw=1.6, ls=(0, (4, 3)), label="предыдущие 28 дней")
    ax.plot(xs, cur, color=BLUE, lw=2, label="последние 28 дней")
    ax.scatter([27], [cur[-1]], s=28, color=BLUE, zorder=3, edgecolor=SURFACE, linewidth=1.5)
    ax.annotate(f"{cur[-1]:.0f}", (27, cur[-1]), xytext=(6, 0), textcoords="offset points",
                fontsize=9, color=INK, va="center")
    ticks = [0, 7, 14, 21, 27]
    ax.set_xticks(ticks, [f"{data['series'][i][0]:%d.%m}" for i in ticks], fontsize=8.5, color=INK2)
    ax.tick_params(axis="y", labelsize=8.5, colors=INK2, length=0)
    ax.tick_params(axis="x", length=0)
    ax.set_ylim(bottom=0)
    ax.grid(axis="y", color=GRID, lw=0.8)
    ax.set_facecolor(SURFACE)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.set_title("Заказы по дням, шт", loc="left", fontsize=11, fontweight="bold", color=INK, pad=22)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=2, frameon=False, fontsize=8.5,
              labelcolor=INK2, handlelength=2.2, borderaxespad=0.2)

    # Топ товаров
    fig.text(0.05, 0.385, f"Топ-{top} товаров за неделю", fontsize=11, fontweight="bold", color=INK)
    cols = [(0.05, "Товар", "left"), (0.66, "шт", "right"), (0.80, "к пр. неделе", "right"),
            (0.95, "остаток", "right")]
    yh = 0.36
    for x, name, ha in cols:
        fig.text(x, yh, name, fontsize=8.5, color=MUTED, ha=ha)
    fig.add_artist(plt.Line2D([0.05, 0.95], [yh - 0.008, yh - 0.008], color=GRID, lw=0.8))
    row_h = 0.038
    for i, s in enumerate(data["skus"][:top]):
        y = yh - 0.033 - i * row_h
        fig.text(0.05, y + 0.006, s["offer_id"] or s["sku"], fontsize=9, fontweight="bold", color=INK)
        fig.text(0.05, y - 0.011, short(s["name"], 58), fontsize=7.5, color=INK2)
        fig.text(0.66, y, f"{s['units']}", fontsize=9.5, color=INK, ha="right")
        lab, col = delta_style(s["delta"])
        fig.text(0.80, y, lab, fontsize=9.5, fontweight="bold", color=col, ha="right")
        lab, col = stock_style(s["stock_days"])
        fig.text(0.95, y, lab, fontsize=9.5, color=INK, ha="right")
        fig.text(0.95 - 0.012 * (len(lab) * 0.62 + 1.2), y, "●", fontsize=9, color=col, ha="right")
        fig.add_artist(plt.Line2D([0.05, 0.95], [y - 0.02, y - 0.02], color=GRID, lw=0.5))
    x = 0.05
    for lab, col in ((f"меньше {LOW_DAYS} дн", BAD), (f"{LOW_DAYS}–{OK_DAYS} дн", WARN), (f"больше {OK_DAYS} дн", GOOD)):
        fig.text(x, 0.02, "●", fontsize=8, color=col)
        fig.text(x + 0.018, 0.02, lab, fontsize=7.5, color=MUTED)
        x += 0.025 + 0.0095 * len(lab) + 0.03
    fig.text(x, 0.02, "· дни = свободный FBO+FBS / заказы в день · Ozon Seller API",
             fontsize=7.5, color=MUTED)
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
