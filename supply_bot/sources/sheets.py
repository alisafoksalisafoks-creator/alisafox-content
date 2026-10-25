"""Google-таблицы владельца — только чтение (правило владельца: в таблицах ничего не меняем).

Доступ — сервисный аккаунт с ролью «Читатель»: путь к ключу в GOOGLE_SA_JSON.
Для проверки без Google можно положить выгрузки листов в SHEETS_SNAPSHOT (unit.tsv, paid.tsv, boxes.tsv).
"""
import os
import re
from datetime import date
from pathlib import Path
from urllib.parse import quote

import httpx

from .. import config

SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"


def _token():
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    creds = service_account.Credentials.from_service_account_file(os.environ["GOOGLE_SA_JSON"], scopes=[SCOPE])
    creds.refresh(Request())
    return creds.token


def read_range(sheet_id, rng):
    r = httpx.get(f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{quote(rng)}",
                  headers={"Authorization": f"Bearer {_token()}"}, timeout=60)
    r.raise_for_status()
    return r.json().get("values", [])


def _snapshot(name):
    path = Path(os.environ["SHEETS_SNAPSHOT"]) / name
    return [line.rstrip("\n").split("\t") for line in path.open(encoding="utf-8")]


def norm(article):
    """Артикул для сравнения: без пробелов, длинное тире → дефис, верхний регистр."""
    return re.sub(r"\s+", "", article or "").replace("–", "-").upper()


def root_of(article):
    """Корень артикула без хвоста NEW / NEW2 …: OZ299NEW2 → OZ299."""
    return re.sub(r"NEW\d*$", "", norm(article))


def _num(s):
    s = (s or "").replace("\xa0", "").replace(" ", "").replace("%", "").replace(",", ".")
    m = re.match(r"-?\d+(\.\d+)?", s)
    return float(m.group()) if m else None


def _find(headers, *names):
    for i, h in enumerate(headers):
        h = re.sub(r"\s+", " ", h or "").strip().lower()
        if any(n.lower() == h or h.startswith(n.lower()) for n in names):
            return i
    raise LookupError(f"В таблице нет столбца «{names[0]}» — шапку переименовали?")


def _find_exact(headers, name):
    """Столбец с точно таким названием; нет — None (у «Чистая прибыль» есть похожие соседи)."""
    for i, h in enumerate(headers):
        if re.sub(r"\s+", " ", h or "").strip().lower() == name.lower():
            return i
    return None


def parse_unit(rows):
    """Лист «Юнит экономика Алиса» → {артикул: {...}}. Шапка в строках 1–2, данные с 5-й."""
    h1 = rows[0] + [""] * 200
    h2 = rows[1] + [""] * 200
    merged = [b or a for a, b in zip(h1, h2)]
    col = {
        "article": _find(h2, "Артикул"),
        "root": _find(h2, "Корневой артикул"),
        "status": _find(h2, "Статус ЗАЯВОК"),
        "rating": _find(h2, "рейтинг"),
        "category": _find(h2, "Категория комиссии"),
        "volume": _find(h2, "Объем товара, л"),
        "buyout": _find(merged, "Выкупаемость по заказам факт"),
    }
    profit_col = _find_exact(h2, config.PROFIT_COLUMN)
    out, dupes = {}, {}
    for r in rows[4:]:
        r = r + [""] * (max(col.values()) + 1 - len(r))
        art = norm(r[col["article"]])
        if not art.startswith("OZ"):
            continue
        rec = {
            "article": r[col["article"]].strip(),
            "root": norm(r[col["root"]]) or root_of(art),
            "status": re.sub(r"\s+", " ", r[col["status"]]).strip().lower(),
            "rating": _num(r[col["rating"]]),
            "category": r[col["category"]].strip(),
            "volume": _num(r[col["volume"]]),
            "buyout": (_num(r[col["buyout"]]) / 100) if _num(r[col["buyout"]]) is not None else None,
            "profit": _num(r[profit_col]) if profit_col is not None and profit_col < len(r) else None,
        }
        if art in out and out[art]["status"] != rec["status"]:
            dupes[art] = {out[art]["status"], rec["status"]}
        out.setdefault(art, rec)
    for art, statuses in dupes.items():
        out[art]["conflict"] = sorted(statuses)
    return out


def parse_paid(rows, today=None):
    """Лист «СКОРО ПЛАТНО» → {артикул: [{qty, date}]}.

    Лист ведут блоками: строка с одной датой (например «20.10») открывает новый срез.
    Берём только последний срез. Дата без года — ближайшая к сегодня, «уже» — платное сейчас.
    """
    today = today or date.today()
    start = 0
    for i, r in enumerate(rows):
        cells = [c.strip() for c in r if c.strip()]
        if len(cells) == 1 and re.fullmatch(r"\d{1,2}\.\d{1,2}(\.\d{2,4})?", cells[0]):
            start = i + 1
    out = {}
    for r in rows[start:]:
        if len(r) < 3 or not norm(r[0]).startswith("OZ"):
            continue
        cell = r[2].strip().lower()
        m = re.match(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?", cell)
        if cell.startswith("уже"):
            when = today
        elif m:
            d, mo = int(m.group(1)), int(m.group(2))
            year = int(m.group(3)) if m.group(3) else today.year
            year = year + 2000 if year < 100 else year
            when = date(year, mo, d)
            if not m.group(3) and (when - today).days > 180:
                when = date(year - 1, mo, d)
            elif not m.group(3) and (today - when).days > 180:
                when = date(year + 1, mo, d)
        else:
            continue
        out.setdefault(norm(r[0]), []).append({"qty": int(_num(r[1]) or 0), "date": when})
    return out


def parse_boxes(rows):
    """Лист «РАЗМЕЩЕНИЕ ПО КОРОБАМ» → {артикул: [(короб, шт), …]} без пустых и нулевых."""
    out = {}
    for r in rows:
        if not r or norm(r[0]) in ("", "АРТИКУЛ"):
            continue
        sizes = []
        for name, v in zip(config.BOX_NAMES, r[1:6]):
            n = _num(v)
            if n and n > 0:
                sizes.append((name, int(n)))
        out[norm(r[0])] = sizes
    return out


def load_unit():
    rows = _snapshot("unit.tsv") if os.environ.get("SHEETS_SNAPSHOT") else \
        read_range(config.UNIT_SHEET_ID, f"'{config.UNIT_SHEET}'!A1:DH3000")
    return parse_unit(rows)


def load_paid(today=None):
    rows = _snapshot("paid.tsv") if os.environ.get("SHEETS_SNAPSHOT") else \
        read_range(config.UNIT_SHEET_ID, f"'{config.PAID_SHEET}'!A1:D3000")
    return parse_paid(rows, today)


def load_boxes():
    rows = _snapshot("boxes.tsv") if os.environ.get("SHEETS_SNAPSHOT") else \
        read_range(config.BOX_SHEET_ID, f"'{config.BOX_SHEET}'!A1:F3000")
    return parse_boxes(rows)
