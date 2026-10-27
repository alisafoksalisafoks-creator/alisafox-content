"""Таблица-картинка для Telegram: заголовок, таблица, итог, примечание. Длинные таблицы — несколько листов."""
from datetime import timedelta

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


UP, DOWN = "#dafbe1", "#ffebe9"   # фон ячейки: поднялся / опустился к прошлому дню
FLAT = 5                          # изменение меньше 5% — обычный разброс средней позиции, без цвета
DAYS_PER_SHEET = 7


def _move(cur, old):
    if cur is None or old is None or abs(old - cur) < max(1, old * FLAT / 100):
        return None
    return UP if cur < old else DOWN


def matrix_png(title, subtitle, items, days, prefix, note=None):
    """Артикулы строками, даты столбцами, в ячейках место. items: [(подпись, {день iso: место})].
    Больше 7 дней — по неделе на лист; больше 24 артикулов — следующие листы."""
    weeks = [days[i:i + DAYS_PER_SHEET] for i in range(0, len(days), DAYS_PER_SHEET)] or [[]]
    chunks = [items[i:i + PER_SHEET] for i in range(0, len(items), PER_SHEET)] or [[]]
    sheets = [(w, c) for c in chunks for w in weeks]
    paths = []
    for n, (week, chunk) in enumerate(sheets, 1):
        last = n == len(sheets)
        lines = max(len(chunk), 1)
        h = 1.25 + ROW * (lines + 1) + (0.6 if note and last else 0)
        fig = plt.figure(figsize=(W, h), dpi=200)
        fig.patch.set_facecolor("white")

        def at(v):
            return v / h

        y = h - 0.42
        head = title + (f"  ·  лист {n}/{len(sheets)}" if len(sheets) > 1 else "")
        fig.text(0.04, at(y), head, fontsize=13, weight="bold", color=INK, va="center")
        y -= 0.34
        fig.text(0.04, at(y), subtitle, fontsize=9, color=MUTED, va="center")
        y -= 0.45
        x0, cw = 0.33, (0.97 - 0.33) / DAYS_PER_SHEET
        fig.text(0.04, at(y), "Артикул", fontsize=8.5, color=MUTED, weight="bold", va="center")
        for j, d in enumerate(week):
            fig.text(x0 + cw * (j + 0.5), at(y), f"{d:%d.%m}", fontsize=8, color=MUTED, weight="bold",
                     ha="center", va="center")
        fig.add_artist(plt.Line2D([0.03, 0.97], [at(y - ROW / 2)] * 2, color=LINE, lw=0.8))
        y -= ROW
        if not chunk:
            fig.text(0.5, at(y), "Покупок нет — артикулов для мест нет", fontsize=10, color=MUTED,
                     ha="center", va="center")
        for label, pos in chunk:
            fig.text(0.04, at(y), short(label, 16), fontsize=9, color=INK, va="center")
            for j, d in enumerate(week):
                cur = pos.get(d.isoformat())
                old = pos.get((d - timedelta(days=1)).isoformat())
                color = _move(cur, old)
                if color:
                    fig.add_artist(plt.Rectangle((x0 + cw * j + 0.003, at(y - ROW / 2 + 0.03)), cw - 0.006,
                                                 at(ROW - 0.06), color=color, zorder=0))
                fig.text(x0 + cw * (j + 0.5), at(y), "—" if cur is None else f"{cur:.0f}", fontsize=9,
                         color=INK if cur is not None else MUTED, ha="center", va="center")
            fig.add_artist(plt.Line2D([0.03, 0.97], [at(y - ROW / 2)] * 2, color=ZEBRA, lw=0.6))
            y -= ROW
        if note and last:
            fig.text(0.04, at(y - 0.1), note, fontsize=7.5, color=MUTED, va="top", linespacing=1.4)
        p = f"{prefix}_{n}.png"
        fig.savefig(p, facecolor="white")
        plt.close(fig)
        paths.append(p)
    return paths
