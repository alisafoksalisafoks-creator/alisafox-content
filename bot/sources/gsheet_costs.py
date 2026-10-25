"""Запасная себестоимость из Google-таблицы «Юнит экономика Алиса» (только чтение).

Бот читает таблицу сервисным аккаунтом Google: ключ — JSON-файл на сервере (GOOGLE_SA_KEY), таблица открыта
этому аккаунту на просмотр. Результат кладётся в costs_fallback.csv, который читает moysklad.load_fallback.
Себестоимость в git не попадает: репозиторий публичный.
"""
import os
from urllib.parse import quote

import httpx

from bot.sources.moysklad import FALLBACK

SHEET_ID = os.environ.get("COSTS_SHEET_ID", "1PjwfdbTI47_gQNDelUBkQ8yhsd4i05Te1JjEDoYxA0U")
RANGE = "'Юнит экономика Алиса'!C:R"  # C — артикул, D — корневой артикул, R — «Себес. с упак»
# ключ сервисного аккаунта: свой (GOOGLE_SA_KEY) или тот же, что у бота поставок на этом сервере
KEY_FILES = [os.environ.get("GOOGLE_SA_KEY", ""), os.environ.get("GOOGLE_SA_JSON", ""), "/opt/supply-bot/sa.json"]
SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"


def _number(v):
    try:
        return float(str(v).replace(" ", "").replace(" ", "").replace("₽", "").replace(",", "."))
    except ValueError:
        return 0.0


def parse(values):
    """Строки C:R → {артикул: себестоимость}; корневой артикул получает цену, если своей строки у него нет."""
    out, roots = {}, {}
    for row in values:
        if len(row) < 16:
            continue
        cost = _number(row[15])
        if cost <= 0:
            continue
        art, root = row[0].strip().upper(), row[1].strip().upper()
        if art:
            out[art] = cost
        if root:
            roots.setdefault(root, cost)
    for root, cost in roots.items():
        out.setdefault(root, cost)
    return out


def refresh(path=FALLBACK):
    """Перечитывает таблицу и перезаписывает CSV. Без ключа ничего не делает и возвращает None."""
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
    costs = parse(r.json().get("values", []))
    if not costs:
        return 0  # пустой ответ — старый файл не трогаем
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(f"{k};{v}\n" for k, v in costs.items())
    os.replace(tmp, path)
    return len(costs)


if __name__ == "__main__":  # update.sh: python -m bot.sources.gsheet_costs
    from bot import env  # noqa: F401
    try:
        n = refresh()
        print("Себестоимость из таблицы: " + ("нет ключа Google — пропущено" if n is None else f"{n} артикулов"))
    except Exception as e:  # обновление бота не должно падать из-за таблицы
        print(f"Себестоимость из таблицы не прочиталась: {e}")
