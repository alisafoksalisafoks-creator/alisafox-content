"""Метрики, которые уже собрал «Аналитик», — копией в базу секретаря (база «Аналитика» только читается).

Копируются продажи и воронка по дням, справочник SKU, отчёты о реализации, контент-рейтинг и реклама
по артикулам (ad_sku). Чего у «Аналитика» нет, секретарь докачивает из API сам.
"""
import logging
import os
import sqlite3

log = logging.getLogger("secretary")
PATH = os.environ.get("ANALYTICS_DB", "/opt/alisafox-content/bot/data.sqlite")
TABLES = ("sku", "daily", "realization", "realization_days", "rating", "ad_sku", "ad_done")


def sync(db, path=PATH):
    """Переливает таблицы «Аналитика» в db; возвращает {таблица: строк} или None, если базы нет."""
    if not os.path.exists(path):
        return None
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    out = {}
    try:
        names = {r[0] for r in src.execute("select name from sqlite_master where type = 'table'")}
        for t in TABLES:
            if t not in names:
                continue
            cols = [r[1] for r in src.execute(f"pragma table_info({t})")]
            if not db.execute("select 1 from sqlite_master where name = ?", (t,)).fetchone():
                ddl = src.execute("select sql from sqlite_master where name = ?", (t,)).fetchone()[0]
                db.execute(ddl)
            mine = {r[1] for r in db.execute(f"pragma table_info({t})")}
            cols = [c for c in cols if c in mine]  # порядок колонок в базах может отличаться — только по именам
            rows = src.execute(f"select {', '.join(cols)} from {t}").fetchall()
            db.executemany(f"insert or replace into {t} ({', '.join(cols)}) values ({', '.join('?' * len(cols))})", rows)
            out[t] = len(rows)
        db.commit()
    finally:
        src.close()
    log.info("из базы «Аналитика»: %s", out)
    return out
