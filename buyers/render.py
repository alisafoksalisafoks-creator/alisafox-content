"""Таблица-картинка для Telegram: заголовок, таблица, итог, примечание. Длинные таблицы — несколько листов."""
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

plt.rcParams["font.family"] = "DejaVu Sans"
INK, MUTED, LINE, ZEBRA, ACCENT = "#1f2328", "#6e7781", "#d0d7de", "#f6f8fa", "#0969da"
W = 6.2          # дюймов: в телефоне шрифт выходит крупнее
ROW = 0.34
PER_SHEET = 24


def short(s, n):
    s = str(s or "")
    return s if len(s) <= n else s[:n - 1] + "…"


def money(x):
    return "—" if x is None else f"{x:,.0f}".replace(",", " ") + " ₽"


def table_png(title, subtitle, cols, rows, prefix, total=None, note=None, empty="Покупок нет"):
    """cols: [(x 0..1, заголовок, 'left'|'right')]; rows: списки строк; total — строка итога."""
    pages = [rows[i:i + PER_SHEET] for i in range(0, len(rows), PER_SHEET)] or [[]]
    paths = []
    for n, page in enumerate(pages, 1):
        last = n == len(pages)
        lines = len(page) + (1 if total and last else 0) + (0 if page else 1)
        h = 1.25 + ROW * (lines + 1) + (0.45 if note and last else 0)
        fig = plt.figure(figsize=(W, h), dpi=200)
        fig.patch.set_facecolor("white")
        y = h - 0.42

        def at(v):
            return v / h

        head = title + (f"  ·  лист {n}/{len(pages)}" if len(pages) > 1 else "")
        fig.text(0.04, at(y), head, fontsize=13, weight="bold", color=INK, va="center")
        y -= 0.34
        fig.text(0.04, at(y), subtitle, fontsize=9, color=MUTED, va="center")
        y -= 0.45
        for x, name, al in cols:
            fig.text(x if al == "left" else x, at(y), name, fontsize=8.5, color=MUTED, weight="bold",
                     ha=al, va="center")
        fig.add_artist(plt.Line2D([0.03, 0.97], [at(y - ROW / 2)] * 2, color=LINE, lw=0.8))
        y -= ROW
        if not page:
            fig.text(0.5, at(y), empty, fontsize=10, color=MUTED, ha="center", va="center")
        for i, row in enumerate(page):
            if i % 2:
                fig.add_artist(plt.Rectangle((0.03, at(y - ROW / 2)), 0.94, at(ROW), color=ZEBRA, zorder=0))
            for (x, _, al), v in zip(cols, row):
                fig.text(x, at(y), str(v), fontsize=9, color=INK, ha=al, va="center")
            y -= ROW
        if total and last:
            fig.add_artist(plt.Line2D([0.03, 0.97], [at(y + ROW / 2)] * 2, color=INK, lw=0.8))
            for (x, _, al), v in zip(cols, total):
                fig.text(x, at(y), str(v), fontsize=9.5, color=ACCENT, weight="bold", ha=al, va="center")
            y -= ROW
        if note and last:
            fig.text(0.04, at(y - 0.1), note, fontsize=7.5, color=MUTED, va="top", linespacing=1.4)
        p = f"{prefix}_{n}.png"
        fig.savefig(p, facecolor="white")
        plt.close(fig)
        paths.append(p)
    return paths
