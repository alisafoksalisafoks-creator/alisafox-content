"""Реклама Ozon по артикулу для кнопки «Ключи»: только активные кампании.

- фразы «Вывод в топ» (PLACEMENT_TOP_PROMOTION): показы, клики, CTR по поисковому запросу.
  Расходов и заказов по фразе Ozon не отдаёт;
- заказы, выручка и расход рекламы по SKU — отчёт statistics/json по всем активным товарным кампаниям.
Отчёты Performance асинхронные, в очереди кабинета максимум один, поэтому всё кэшируется в SQLite
по (start, end, кампания): прошлые периоды не меняются.
"""
import csv
import io
import time
from datetime import date

import httpx

from bot.sources.performance import PerformanceClient

TOP = "PLACEMENT_TOP_PROMOTION"
MAX_CAMPS = 10  # кампаний в одном отчёте
MIN_VIEWS = 100  # фразы с меньшим числом показов не показываем (владелец 2026-10-26)


def ensure_tables(db):
    db.executescript("""
        create table if not exists ad_camp_sku (
            day text, campaign text, placement text, title text, sku text,
            primary key (day, campaign, sku));
        create table if not exists ad_phrases (
            start text, end text, campaign text, sku text, query text, views real, clicks real,
            primary key (start, end, campaign, sku, query));
        create table if not exists ad_sku (
            start text, end text, campaign text, sku text,
            views real, clicks real, to_cart real, spent real, orders real, orders_money real,
            primary key (start, end, campaign, sku));
        create table if not exists ad_done (
            start text, end text, kind text, campaign text, primary key (start, end, kind, campaign));
    """)


def _num(s):
    s = str(s or 0).replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def campaigns(db, perf: PerformanceClient, today: date):
    """Активные товарные кампании и их SKU на сегодня: [(campaign, placement, title, sku)]."""
    ensure_tables(db)
    rows = db.execute("select campaign, placement, title, sku from ad_camp_sku where day = ?", (str(today),)).fetchall()
    if rows:
        return [tuple(r) for r in rows]
    out = []
    for c in perf.get("/api/client/campaign")["list"]:
        if c.get("advObjectType") != "SKU" or c.get("state") != "CAMPAIGN_STATE_RUNNING":
            continue
        placement = (c.get("placement") or [""])[0]
        for p in perf.get(f"/api/client/campaign/{c['id']}/v2/products").get("products", []):
            out.append((str(c["id"]), placement, c.get("title") or "", str(p["sku"])))
    db.execute("delete from ad_camp_sku where day < ?", (str(today),))
    db.executemany("insert or ignore into ad_camp_sku values (?, ?, ?, ?, ?)", [(str(today), *r) for r in out])
    db.commit()
    return out


def _report(perf: PerformanceClient, path, body, timeout=900):
    """Заказать отчёт, дождаться очереди и вернуть ответ (httpx.Response)."""
    perf._auth()
    deadline = time.time() + timeout
    while True:
        r = perf.http.post(path, json=body)
        if r.status_code != 429 or time.time() > deadline:
            break
        time.sleep(15)  # в очереди кабинета чужой отчёт
    r.raise_for_status()
    uuid = r.json()["UUID"]
    while True:
        state = perf.get(f"/api/client/statistics/{uuid}").get("state")
        if state == "OK":
            break
        if state == "ERROR" or time.time() > deadline:
            raise TimeoutError(f"отчёт Performance {path} не собрался: {state}")
        time.sleep(5)
    perf._auth()
    r = perf.http.get("/api/client/statistics/report", params={"UUID": uuid})
    r.raise_for_status()
    return r


def _todo(db, start, end, kind, cids):
    have = {r[0] for r in db.execute("select campaign from ad_done where start = ? and end = ? and kind = ?",
                                     (str(start), str(end), kind))}
    return [c for c in cids if c not in have]


def _done(db, start, end, kind, cids):
    db.executemany("insert or ignore into ad_done values (?, ?, ?, ?)", [(str(start), str(end), kind, c) for c in cids])
    db.commit()


def load_phrases(db, perf, cids, start: date, end: date):
    """Фразы кампаний «Вывод в топ» за период — по одному отчёту на кампанию."""
    for cid in _todo(db, start, end, "phrases", cids):
        try:
            text = _report(perf, "/api/client/statistics/phrases", {
                "campaigns": [cid], "dateFrom": str(start), "dateTo": str(end), "groupBy": "NO_GROUP_BY"}).text
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 400:  # тип кампании не поддерживает фразы
                _done(db, start, end, "phrases", [cid])
                continue
            raise
        rows = list(csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=";"))[2:]
        db.executemany("insert or replace into ad_phrases values (?, ?, ?, ?, ?, ?, ?)",
                       [(str(start), str(end), cid, r[0], r[2].strip(), _num(r[3]), _num(r[4]))
                        for r in rows if len(r) >= 6 and r[0].isdigit()])
        _done(db, start, end, "phrases", [cid])


def load_sku(db, perf, cids, start: date, end: date):
    """Заказы, выручка и расход рекламы по SKU — пачками по 10 кампаний."""
    todo = _todo(db, start, end, "sku", cids)
    for i in range(0, len(todo), MAX_CAMPS):
        batch = todo[i:i + MAX_CAMPS]
        data = _report(perf, "/api/client/statistics/json", {
            "campaigns": batch, "dateFrom": str(start), "dateTo": str(end), "groupBy": "NO_GROUP_BY"}).json()
        rows = []
        for cid, rep in data.items():
            for r in (rep.get("report") or {}).get("rows", []):
                rows.append((str(start), str(end), str(cid), str(r.get("sku")), _num(r.get("views")),
                             _num(r.get("clicks")), _num(r.get("toCart")), _num(r.get("moneySpent")),
                             _num(r.get("orders")), _num(r.get("ordersMoney"))))
        db.executemany("insert or replace into ad_sku values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        _done(db, start, end, "sku", batch)


def for_sku(db, perf, sku, start: date, end: date, today: date):
    """Реклама артикула за период: фразы, итоги по SKU и список активных кампаний."""
    sku = str(sku)
    camps = [c for c in campaigns(db, perf, today) if c[3] == sku]
    cids = sorted({c[0] for c in camps})
    top = sorted({c[0] for c in camps if c[1] == TOP})
    load_sku(db, perf, cids, start, end)
    load_phrases(db, perf, top, start, end)
    key = (str(start), str(end))
    marks = ",".join("?" * len(cids)) or "''"
    tot = db.execute(
        f"select coalesce(sum(views), 0) views, coalesce(sum(clicks), 0) clicks, coalesce(sum(to_cart), 0) to_cart, "
        f"coalesce(sum(spent), 0) spent, coalesce(sum(orders), 0) orders, coalesce(sum(orders_money), 0) orders_money "
        f"from ad_sku where start = ? and end = ? and sku = ? and campaign in ({marks})", (*key, sku, *cids)).fetchone()
    tmarks = ",".join("?" * len(top)) or "''"
    phrases = db.execute(
        f"select query, sum(views) views, sum(clicks) clicks from ad_phrases "
        f"where start = ? and end = ? and sku = ? and campaign in ({tmarks}) group by lower(query) "
        f"having sum(views) >= {MIN_VIEWS} order by clicks desc, views desc", (*key, sku, *top)).fetchall()
    return {"campaigns": camps, "totals": dict(tot), "phrases": [dict(p) for p in phrases]}


def prefetch(db, perf, start: date, end: date, today: date):
    """Ночная загрузка по всем активным кампаниям: чтобы днём кнопка не ждала очередь отчётов."""
    camps = campaigns(db, perf, today)
    load_sku(db, perf, sorted({c[0] for c in camps}), start, end)
    load_phrases(db, perf, sorted({c[0] for c in camps if c[1] == TOP}), start, end)
