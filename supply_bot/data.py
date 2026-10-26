"""Сбор снимка данных для расчёта. Отправления FBO кешируются по дням: Ozon отдаёт по 100 штук за запрос."""
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

from . import config, profit
from .calc import Snapshot
from .sources import sheets
from .sources.moysklad import MoySkladClient
from .sources.ozon import OzonClient

MSK = timezone(timedelta(hours=3))
REFRESH_LAST_DAYS = 2   # последние дни перекачиваем: статусы отмен ещё меняются


def today_msk():
    return datetime.now(MSK).date()


def postings(oz, today, days=config.REF_DAYS, log=print):
    cache = config.DATA_DIR / "postings"
    cache.mkdir(parents=True, exist_ok=True)
    rows = []
    for i in range(days, 0, -1):
        day = today - timedelta(days=i)
        path = cache / f"{day}.json"
        if path.exists() and i > REFRESH_LAST_DAYS:
            rows += [tuple(r) for r in json.loads(path.read_text())]
            continue
        log(f"Ozon: заказы FBO за {day:%d.%m}")
        part = oz.fbo_postings(day, day + timedelta(days=1))
        path.write_text(json.dumps(part, ensure_ascii=False))
        rows += part
    for old in cache.glob("*.json"):
        if old.stem < str(today - timedelta(days=days + 7)):
            old.unlink()
    return rows


def load_season():
    """season.tsv: категория \\t коэф. января … декабря. Сначала файл на сервере (DATA_DIR), затем — из кода.
    Нет файла — коэффициент 1."""
    path = config.DATA_DIR / "season.tsv"
    if not path.exists():
        path = Path(__file__).with_name("season.tsv")
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 13:
            continue
        try:
            out[parts[0].strip()] = {m: float(v.replace(",", ".")) for m, v in enumerate(parts[1:13], 1)}
        except ValueError:
            continue
    return out


def collect(log=print, today=None):
    today = today or today_msk()
    oz, ms = OzonClient(), MoySkladClient()
    log("Ozon: остатки по товарам")
    prod = oz.product_stocks()
    log("Ozon: остатки и в пути по складам")
    wh_stocks = oz.warehouse_stocks(prod.keys())
    log("Ozon: кластеры и склады")
    wh_cluster = oz.clusters()
    post = postings(oz, today, log=log)
    log("Ozon: заявки, которые ещё не уехали")
    try:
        unshipped = oz.unshipped_supplies()
    except Exception as e:  # noqa: BLE001 — без заявок считаем, но предупреждаем
        log(f"Не удалось получить заявки: {e}")
        unshipped = []
    log("«Мой склад»: остатки")
    ms_rows = ms.stock(config.MS_STORE)
    try:
        profits = profit.collect(oz, ms, today - timedelta(days=1), log)
    except Exception as e:  # noqa: BLE001 — без прибыли распределение считаем, «Заказать» предупредит
        log(f"Не удалось посчитать прибыль: {e}")
        profits = {}
    log("Таблица AlisaFox: статусы, рейтинг, выкуп, платное хранение")
    unit = sheets.load_unit()
    paid = sheets.load_paid(today)
    log("Таблица «Актуальный СКЛАД»: кванты коробов")
    boxes = sheets.load_boxes()
    return Snapshot(today=today, postings=post, wh_stocks=wh_stocks, wh_cluster=wh_cluster,
                    product_stocks=prod, ms=ms_rows, unshipped=unshipped, unit=unit, paid=paid,
                    boxes=boxes, season=load_season(), profit=profits, taken_at=datetime.now(MSK).strftime("%d.%m.%Y %H:%M"))
