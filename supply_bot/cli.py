"""Расчёт без Telegram: python -m supply_bot.cli [--cached] [--out план.xlsx] [--offer АРТ] [--warehouse СКЛАД]."""
import argparse
import pickle
import re
from html import unescape

from . import calc, config, data, excel, render


def snapshot(cached=False, log=print):
    path = config.DATA_DIR / "snapshot.pkl"
    if cached and path.exists():
        return pickle.loads(path.read_bytes())
    snap = data.collect(log=log)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pickle.dumps(snap))
    return snap


def plain(html):
    return unescape(re.sub(r"<[^>]+>", "", html))


def main():
    ap = argparse.ArgumentParser(description="Распределение поставки FBO по складам Ozon")
    ap.add_argument("--cached", action="store_true", help="взять последний снимок данных, не ходить в API")
    ap.add_argument("--out", help="куда сохранить Excel")
    ap.add_argument("--offer", help="потребность по одному артикулу")
    ap.add_argument("--warehouse", help="список артикулов на один склад")
    a = ap.parse_args()
    snap = snapshot(a.cached)
    plan = calc.build(snap)
    if a.offer:
        texts = render.offer(plan, a.offer, snap.unit)
    elif a.warehouse:
        texts = render.warehouse(plan, a.warehouse)
    else:
        texts = render.summary(plan, snap.taken_at)
    for t in texts:
        print(plain(t))
    if a.out:
        excel.build(plan, a.out, snap.taken_at)
        print(f"Файл: {a.out}")


if __name__ == "__main__":
    main()
