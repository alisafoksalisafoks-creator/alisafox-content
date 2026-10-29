"""TrueStats REST API (только чтение): KPI «Общие показатели» за период, как на экране оцифровки.

Ключ — TRUESTATS_BOT_TOKEN или TRUESTATS_TOKEN в .env. Лимит TrueStats — 60 запросов в минуту.
"""
import os
import time

import httpx

BASE = "https://api.truestats.ru"


def token():
    return os.environ.get("TRUESTATS_BOT_TOKEN") or os.environ.get("TRUESTATS_TOKEN")


def stats(start, end, retries=4):
    """KPI за период (даты включительно): {profit, marginality, sales, totalSales, orders, ...}."""
    for attempt in range(retries):
        try:
            r = httpx.post(f"{BASE}/reporting/main/stats", json={"dateFrom": str(start), "dateTo": str(end)},
                           headers={"X-Api-Token": token()}, timeout=90)
            if r.status_code == 429 or r.status_code >= 500 or not r.content:
                raise httpx.TransportError(f"TrueStats {r.status_code}")
            r.raise_for_status()
            return r.json()["stats"]
        except httpx.TransportError:  # TrueStats иногда рвёт соединение или отвечает пусто — повторяем
            if attempt == retries - 1:
                raise
            time.sleep(3 * (attempt + 1))
