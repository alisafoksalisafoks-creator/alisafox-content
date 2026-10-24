"""МойСклад: себестоимость и остатки на собственном складе. Только чтение."""
import os
import re

import httpx

BASE = "https://api.moysklad.ru/api/remap/1.2"
# учёт в МоёмСкладе ведётся по корневому артикулу: OZ204NEW, OZ204NEW2 на Ozon = OZ204 на складе
RELAUNCH = re.compile(r"NEW\d*$")


def _rows(http, path):
    offset = 0
    while True:
        r = http.get(path, params={"limit": 1000, "offset": offset})
        r.raise_for_status()
        rows = r.json()["rows"]
        yield from rows
        if len(rows) < 1000:
            return
        offset += 1000


def _key(row):
    return (row.get("article") or row.get("code") or "").strip().upper()


class Catalog:
    """Себестоимость за штуку (₽) и свой остаток по артикулу Ozon."""

    def __init__(self):
        http = httpx.Client(base_url=BASE, timeout=60, headers={
            "Authorization": f"Bearer {os.environ['MOYSKLAD_TOKEN']}", "Accept-Encoding": "gzip"})
        self.items = {}
        for row in _rows(http, "/entity/assortment"):
            if _key(row):
                self.items[_key(row)] = {"stock": 0, "in_transit": 0,
                                         "cost": (row.get("buyPrice") or {}).get("value", 0) / 100}
        for row in _rows(http, "/report/stock/all"):
            item = self.items.setdefault(_key(row), {"stock": 0, "in_transit": 0, "cost": 0})
            item["stock"] = row.get("stock", 0)
            item["in_transit"] = row.get("inTransit", 0)
            item["avg_cost"] = row.get("price", 0) / 100
            if item["avg_cost"] > 0:  # средняя себестоимость точнее закупочной цены
                item["cost"] = item["avg_cost"]
            elif item["avg_cost"] < 0:
                item["bad_cost"] = True

        # один остаток на корневой артикул: дубли с NEW не суммируем, берём остаток корня;
        # если корня в МоёмСкладе нет — остаток самого дубля
        self.roots = {}
        for key in sorted(self.items, key=len):
            item, root = self.items[key], RELAUNCH.sub("", key)
            if root not in self.roots:
                self.roots[root] = dict(item, variants=[])
            r = self.roots[root]
            if key != root:
                r["variants"].append(key)
                if r["cost"] <= 0 < item["cost"]:
                    r["cost"] = item["cost"]

    def get(self, offer_id):
        return self.roots.get(RELAUNCH.sub("", (offer_id or "").strip().upper()))

    def anomalies(self):
        """Позиции с отрицательным остатком или себестоимостью — прибыль по ним неточна."""
        return {k: v for k, v in self.items.items() if v["stock"] < 0 or v.get("bad_cost")}
