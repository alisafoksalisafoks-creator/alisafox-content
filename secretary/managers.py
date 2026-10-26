"""Кто какие артикулы ведёт: колонка «Менеджер» в юнитке Озон (лист «Юнит экономика Алиса»), только чтение.

Менеджер закреплён за артикулом, а не за категорией: одна категория бывает у нескольких менеджеров.
Таблица читается сервисным аккаунтом Google (тот же ключ, что у себестоимости в боте «Аналитик»),
результат кладётся в managers.json рядом — его и читает бот. Имена и артикулы в git не попадают.
"""
import json
import os
from urllib.parse import quote

import httpx

from bot.sources.gsheet_costs import KEY_FILES, SCOPE, SHEET_ID

RANGE = "'Юнит экономика Алиса'!C5:N"  # C артикул, D корень, L FBO SKU, M менеджер, N категория
PATH = os.path.join(os.path.dirname(__file__), "managers.json")
NO_MANAGER = "Без менеджера"


def parse(values):
    """Строки C:N → [{offer_id, root, sku, manager, category}]."""
    out = []
    for row in values:
        row = row + [""] * (12 - len(row))
        offer = row[0].strip().upper()
        if not offer:
            continue
        out.append({"offer_id": offer, "root": row[1].strip().upper(), "sku": row[9].strip(),
                    "manager": row[10].strip() or NO_MANAGER, "category": row[11].strip()})
    return out


def refresh(path=PATH):
    """Перечитывает таблицу и перезаписывает managers.json. Без ключа Google — None, старый файл остаётся."""
    key = next((k for k in KEY_FILES if k and os.path.exists(k)), None)
    if not key:
        return None
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(key, scopes=[SCOPE])
    creds.refresh(Request())
    r = httpx.get(f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{quote(RANGE)}",
                  headers={"Authorization": f"Bearer {creds.token}"}, timeout=60)
    r.raise_for_status()
    rows = parse(r.json().get("values", []))
    if not rows:
        return 0
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    os.replace(tmp, path)
    return len(rows)


def load(path=PATH):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def assign(db, rows):
    """{sku Ozon: менеджер}. SKU из колонки L, а если его нет — по артикулу продавца из базы бота."""
    by_offer = {r["offer_id"]: r["manager"] for r in rows}
    out = {r["sku"]: r["manager"] for r in rows if r["sku"]}
    for sku, offer in db.execute("select sku, offer_id from sku where offer_id is not null"):
        if sku not in out and offer and offer.upper() in by_offer:
            out[sku] = by_offer[offer.upper()]
    return out


def names(rows):
    """Менеджеры по числу артикулов, «Без менеджера» — в конце."""
    count = {}
    for r in rows:
        count[r["manager"]] = count.get(r["manager"], 0) + 1
    return sorted((m for m in count if m != NO_MANAGER), key=lambda m: -count[m]) + \
        ([NO_MANAGER] if NO_MANAGER in count else [])


if __name__ == "__main__":  # update.sh: python -m secretary.managers
    from bot import env  # noqa: F401
    try:
        n = refresh()
        print("Менеджеры из юнитки: " + ("нет ключа Google — пропущено" if n is None else f"{n} артикулов"))
    except Exception as e:
        print(f"Юнитка не прочиталась: {e}")
