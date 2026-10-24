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
