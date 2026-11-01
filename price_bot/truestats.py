"""TrueStats (ТС): чистая прибыль на 1 шт и ДРР по SKU Ozon за последние 10 дней.

Берём отчёт «Оцифровка → Товары» (reporting_products) через MCP-сервер TrueStats по HTTP.
Адрес и ключ — в .env: TRUESTATS_MCP_URL, TRUESTATS_API_KEY.
РК ДРР пока = ДРРз из ТС (реклама / заказы): кампании Ozon в ТС не отслеживаются.
"""
import datetime as dt
import itertools
import json
import logging
import os

import aiohttp

log = logging.getLogger("price_bot.truestats")
DAYS = 10
OZON = 1  # accountTypes в ТС: 0=WB, 1=Ozon, 2=ЯМ


class TrueStatsError(Exception):
    pass


class TrueStats:
    def __init__(self, url=None, key=None):
        self.url = url or os.environ.get("TRUESTATS_MCP_URL", "")
        self.key = key or os.environ.get("TRUESTATS_API_KEY", "")
        self._ids = itertools.count(1)
        self._sid = None

    @property
    def ready(self):
        return bool(self.url and self.key)

    async def _rpc(self, s, method, params, notify=False):
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notify:
            body["id"] = next(self._ids)
        headers = {"Authorization": f"Bearer {self.key}", "Accept": "application/json, text/event-stream"}
        if self._sid:
            headers["Mcp-Session-Id"] = self._sid
        async with s.post(self.url, json=body, headers=headers) as r:
            if r.status >= 400:
                raise TrueStatsError(f"ТС ответил {r.status}")
            self._sid = r.headers.get("Mcp-Session-Id", self._sid)
            if notify:
                return None
            text = await r.text()
        if "text/event-stream" in r.headers.get("Content-Type", ""):
            # ответ потоком: берём последнее событие data:
            text = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")][-1]
        msg = json.loads(text)
        if "error" in msg:
            raise TrueStatsError(msg["error"].get("message", "ошибка ТС"))
        return msg["result"]

    async def tool(self, name, args):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90), trust_env=True) as s:
            if not self._sid:
                await self._rpc(s, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                  "clientInfo": {"name": "alisafox-price-bot", "version": "1"}})
                await self._rpc(s, "notifications/initialized", {}, notify=True)
            try:
                res = await self._rpc(s, "tools/call", {"name": name, "arguments": args})
            except TrueStatsError:
                self._sid = None  # сессия могла истечь — в следующий раз начнём заново
                raise
        if res.get("isError"):
            raise TrueStatsError(" ".join(c.get("text", "") for c in res.get("content", []))[:200])
        if res.get("structuredContent"):
            return res["structuredContent"]
        return json.loads("".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"))

    async def unit_metrics(self, skus, today=None):
        """{sku: {profit, drr, drr_rk, period}} за 10 полных дней до сегодня."""
        if not self.ready:
            raise TrueStatsError("ТС не подключён: нет TRUESTATS_MCP_URL / TRUESTATS_API_KEY в .env")
        today = today or dt.date.today()
        d_to, d_from = today - dt.timedelta(days=1), today - dt.timedelta(days=DAYS)
        period = f"{d_from:%d.%m}–{d_to:%d.%m}"
        out, page = {}, 1
        while True:
            r = await self.tool("reporting_products", {
                "dateFrom": d_from.isoformat(), "dateTo": d_to.isoformat(), "groupBy": "nm_id",
                "filters": {"accountTypes": [OZON], "article": [str(s) for s in skus]},
                "columns": ["article", "averageProfitPerPiece", "drr", "drrz"], "limit": 500, "page": page})
            rows = r.get("byProduct", [])
            for row in rows:
                out[int(row["article"])] = {"profit": row.get("averageProfitPerPiece"), "drr": row.get("drr"),
                                            "drr_rk": row.get("drrz"), "period": period}
            total = (r.get("pagination") or {}).get("total", 0)
            if not rows or page * 500 >= total:
                return out
            page += 1
