"""«Мой склад», только чтение: остатки нашего склада по артикулам."""
import os

import httpx

BASE = "https://api.moysklad.ru/api/remap/1.2/"


class MoySkladClient:
    def __init__(self, token=None):
        self.http = httpx.Client(base_url=BASE, timeout=60, headers={
            "Authorization": "Bearer " + (token or os.environ["MOYSKLAD_TOKEN"]).strip(),
            "Accept-Encoding": "gzip",
        })

    def get(self, path, **params):
        r = self.http.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def store_href(self, name):
        for s in self.get("entity/store")["rows"]:
            if s["name"].strip() == name:
                return s["meta"]["href"]
        raise LookupError(f"В «Моём складе» нет склада «{name}»")

    def stock(self, store_name):
        """[{article, name, stock, reserve, in_transit}] по одному складу.

        Свободно = stock − reserve. Поле quantity («Доступно») не берём:
        в нём есть ожидаемое поступление, которого на складе ещё нет.
        """
        href, rows, offset = self.store_href(store_name), [], 0
        while True:
            page = self.get("report/stock/all", limit=1000, offset=offset, filter=f"store={href}")["rows"]
            for r in page:
                rows.append({
                    "article": (r.get("article") or r.get("code") or "").strip(),
                    "name": r.get("name", ""),
                    "stock": r.get("stock") or 0,
                    "reserve": r.get("reserve") or 0,
                    "in_transit": r.get("inTransit") or 0,
                })
            if len(page) < 1000:
                return rows
            offset += 1000

    def _all(self, path):
        offset = 0
        while True:
            page = self.get(path, limit=1000, offset=offset)["rows"]
            yield from page
            if len(page) < 1000:
                return
            offset += 1000

    def costs(self):
        """{корневой артикул: себестоимость 1 шт, ₽}. Средняя себестоимость точнее закупочной цены;
        нет средней (товара нет на складе) — закупочная цена из карточки."""
        from ..profit import root
        out = {}
        for r in self._all("entity/assortment"):
            key = root(r.get("article") or r.get("code"))
            buy = (r.get("buyPrice") or {}).get("value", 0) / 100
            if key and buy > 0 and not out.get(key):
                out[key] = buy
        for r in self._all("report/stock/all"):
            key = root(r.get("article") or r.get("code"))
            avg = (r.get("price") or 0) / 100
            if key and avg > 0:
                out[key] = avg
        return out
