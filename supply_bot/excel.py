"""Excel с распределением. Цифры те же, что бот показал на экране."""
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from . import config
from .calc import PARTIAL

BOLD = Font(bold=True)
HEAD = PatternFill("solid", fgColor="DDE7F0")
TOTAL = PatternFill("solid", fgColor="F2F2F2")
PARTIAL_FILL = PatternFill("solid", fgColor="FFF2A8")
STOP_FILL = PatternFill("solid", fgColor="EDEDED")
LATER_FILL = PatternFill("solid", fgColor="DDEBF7")   # голубой: запас от 30 дн, нужно меньше партии — довезти в след. отгрузку


def _later(plan, wh):
    return [z["line"] for z in plan.zero if z.get("line") is not None and z["line"].later and z["warehouse"] == wh]
STOP_FONT = Font(color="808080")
DEMAND_FILL = {"опт": PatternFill("solid", fgColor="FFD8A8"), "всплеск": PatternFill("solid", fgColor="FFD8A8"),
               "рост": PatternFill("solid", fgColor="C6EFCE")}   # опт и всплеск — оранжевый, рост спроса — зелёный
WARN_FILL = {"платное": PatternFill("solid", fgColor="FFC7CE"),   # платное хранение — красный
             "выкуп": PatternFill("solid", fgColor="E4DFEC"),     # выкуп ниже 85% — сиреневый
             "короб": PatternFill("solid", fgColor="FCE4D6"),     # после поставки запас больше 60 дн — оранжевый
             "ждать": PatternFill("solid", fgColor="FFF2CC")}     # Москва: хит с запасом 30–37 дн — жёлтый, решает владелец


def _fill(ln, partial=False):
    """Цвет строки: платное > выкуп > большой запас (оранжевый) > неполный короб > опт/всплеск/рост."""
    if ln.warn:
        return WARN_FILL[ln.warn]
    if partial:
        return PARTIAL_FILL
    return DEMAND_FILL.get(ln.demand)


def _paint(ws, fill):
    for c in ws[ws.max_row]:
        c.fill = fill


def _sheet(wb, title, header, rows, widths=None):
    ws = wb.create_sheet(title)
    ws.append(header)
    for c in ws[1]:
        c.font, c.fill = BOLD, HEAD
        c.alignment = Alignment(wrap_text=True, vertical="top")
    for r in rows:
        ws.append(r)
    ws.freeze_panes = "A2"
    for i, w in enumerate(widths or [], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    return ws


def _boxes(ln):
    # без размеров коробов (решение владельца 05.11.2026): только сколько коробов по сколько штук
    return ", ".join(f"неполный {s} шт" if name.startswith(PARTIAL) else f"{k}×{s} шт" for name, s, k in ln.boxes) or "—"


def _distribution(wb, plan, groups):
    """Лист распределения: {направление: [строки]} — по коробам, итог по каждому направлению."""
    ws = _sheet(wb, "Лист распределения",
                ["Направление", "Кластер", "Склад приёмки", "Артикул", "Кол-во, шт", "Шт в коробе", "Коробов",
                 "Дата отгрузки", "Приоритет", "Пометки"],
                [], [30, 26, 28, 28, 11, 11, 9, 13, 11, 50])
    for wh, lines in groups.items():
        for ln in sorted(lines, key=lambda x: (-["обычно", "наверх", "срочно"].index(x.priority), -x.qty)):
            if ln.boxes:
                for name, s, k in ln.boxes:
                    ws.append([wh, ln.cluster, ln.ship_to, ln.offer, s * k, f"неполный {s}" if name.startswith(PARTIAL) else s,
                               k, plan.ship_date, ln.priority, "; ".join(ln.flags)])
                    fill = _fill(ln, name.startswith(PARTIAL))  # неполный короб — вся строка жёлтая
                    if fill:
                        _paint(ws, fill)
            else:
                ws.append([wh, ln.cluster, ln.ship_to, ln.offer, ln.qty, "нет кванта", None, plan.ship_date, ln.priority,
                           "; ".join(ln.flags)])
                if _fill(ln):
                    _paint(ws, _fill(ln))
        row = [f"Итого {wh}", "", "", f"{len(lines)} арт.", sum(x.qty for x in lines), "",
               sum(x.box_count for x in lines), "", "", ""]
        ws.append(row)
        for c in ws[ws.max_row]:
            c.font, c.fill = BOLD, TOTAL
        # «Решить / стоп» — серым под итогом, в итог не входят; в скобках потребность, если бы везли
        for x in plan.stopped.get(wh, []):
            ws.append([wh, lines[0].cluster if lines else wh, "", x["offer"], f"({x['need']})", "—", None,
                       None, "не везём", "не везём: " + x["reason"]])
            for c in ws[ws.max_row]:
                c.fill, c.font = STOP_FILL, STOP_FONT
        for ln in _later(plan, wh):   # голубым под итогом: сейчас не везём, довезти в следующую отгрузку
            ws.append([wh, ln.cluster, "", ln.offer, f"({ln.later})", "—", None, None, "след. отгрузка",
                       next((f for f in ln.flags if f.startswith("не везём сейчас")), "")])
            _paint(ws, LATER_FILL)
    for c in ws["H"][1:]:
        c.number_format = "DD.MM.YYYY"


GREY = PatternFill("solid", fgColor="EFEFEF")
YELLOW_HEAD = PatternFill("solid", fgColor="FFF2CC")


def _order_sheet(wb, plan, lines):
    """Лист «Заявка» — как в ручных расчётах владельца (05.11.2026): артикул, тип, рекомендация,
    пустой столбец «Ручное кол-во», транзит, продано за 14 дн, остатки Ozon и нашего склада. Без размеров коробов."""
    header = ["Направление", "Артикул", "Тип - категория", "Кол-во рекоменд", "Ручное кол-во",
              "Транзит", "Продано за 2 недели", "Остатки Ozon", "Остатки наш склад", "Корневой артикул", "Статус",
              "Коробов", "Дней запаса", "Пометки"]
    ws = wb.create_sheet("Заявка", 0)
    ws.append(header)
    for i, c in enumerate(ws[1], 1):
        c.font = BOLD
        c.fill = YELLOW_HEAD if header[i - 1] == "Остатки наш склад" else GREY
        c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
    ws.row_dimensions[1].height = 56
    for ln in sorted(lines, key=lambda x: (x.warehouse, x.offer)):
        inf = plan.info.get(ln.offer, {})
        ws.append([ln.warehouse, ln.offer, inf.get("category"), ln.qty, None, ln.transit, ln.sales14,
                   ln.available, inf.get("free"), inf.get("root"), (inf.get("status") or "").capitalize(),
                   _boxes(ln), round(ln.days) if ln.days is not None else None, "; ".join(ln.flags)])
        if _fill(ln):
            _paint(ws, _fill(ln))
    for wh in sorted({x.warehouse for x in lines}):
        for x in plan.stopped.get(wh, []):
            ws.append([wh, x["offer"], None, 0, None, None, None, None, None, None, None, None, None,
                       "не везём: " + x["reason"]])
            for c in ws[ws.max_row]:
                c.fill, c.font = STOP_FILL, STOP_FONT
        for ln in _later(plan, wh):
            inf = plan.info.get(ln.offer, {})
            ws.append([wh, ln.offer, inf.get("category"), 0, None, ln.transit, ln.sales14, ln.available,
                       inf.get("free"), inf.get("root"), (inf.get("status") or "").capitalize(), None,
                       round(ln.days) if ln.days is not None else None,
                       next((f for f in ln.flags if f.startswith("не везём сейчас")), "")])
            _paint(ws, LATER_FILL)
    ws.append(["Итого", f"{len(lines)} арт.", None, sum(x.qty for x in lines)])
    for c in ws[ws.max_row]:
        c.font = BOLD
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(header))}{ws.max_row - 1}"
    for i, w in enumerate([22, 26, 22, 11, 11, 9, 11, 10, 11, 18, 14, 14, 9, 50], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2, max_col=3):
        for c in row:
            c.alignment = Alignment(wrap_text=False, vertical="top")


def _zero_sheet(wb, plan, directions=None):
    """Лист «Ноль — проверить»: всё, что бот не везёт, с причиной (решение владельца 05.11.2026)."""
    header = ["Направление", "Артикул", "Тип - категория", "Кол-во рекоменд", "Ручное кол-во", "Транзит",
              "Продано за 2 недели", "Остатки Ozon", "Остатки наш склад", "Корневой артикул", "Статус",
              "Дней запаса", "Почему 0"]
    ws = wb.create_sheet("Ноль — проверить", 1)
    ws.append(header)
    for i, c in enumerate(ws[1], 1):
        c.font = BOLD
        c.fill = YELLOW_HEAD if header[i - 1] == "Остатки наш склад" else GREY
        c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
    ws.row_dimensions[1].height = 56
    for z in plan.zero:
        if directions is not None and z["warehouse"] not in directions:
            continue
        ln = z.get("line")
        if ln is not None:
            inf = plan.info.get(ln.offer, {})
            ws.append([z["warehouse"], ln.offer, inf.get("category"), 0, None, ln.transit, ln.sales14, ln.available,
                       inf.get("free"), inf.get("root"), (z["status"] or "").capitalize(),
                       z["days"], z["why"]])
            if ln.later:
                _paint(ws, LATER_FILL)
        else:
            sp = z["sales14"] / config.SALES_DAYS
            ws.append([z["warehouse"], z["offer"], z["category"], 0, None, z["transit"], z["sales14"], z["available"],
                       None, z["root"], (z["status"] or "").capitalize(),
                       round((z["available"] + z["transit"]) / sp) if sp else None, z["why"]])
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(header))}{max(ws.max_row, 2)}"
    for i, w in enumerate([22, 26, 22, 11, 11, 9, 11, 10, 11, 18, 14, 9, 50], 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _later_sheet(wb, plan, directions=None):
    """Лист «Довезти в след. отгрузку»: запас от 30 дн, а до цели не хватает меньше партии — сейчас не везём (09.11.2026)."""
    rows = []
    for z in plan.zero:
        ln = z.get("line")
        if ln is None or not ln.later or (directions is not None and z["warehouse"] not in directions):
            continue
        rows.append([z["warehouse"], ln.offer, z["days"], ln.later, ln.sales14, ln.available, ln.transit,
                     (plan.info.get(ln.offer) or {}).get("free"), z["why"]])
    if rows:
        _sheet(wb, "Довезти в след. отгрузку",
               ["Направление", "Артикул", "Дней запаса сейчас", "Не хватает до цели, шт", "Продано за 2 недели",
                "Остатки Ozon", "Транзит", "Остатки наш склад", "Почему не сейчас"],
               rows, [22, 26, 10, 11, 11, 10, 9, 11, 70])
        ws = wb["Довезти в след. отгрузку"]
        for r in ws.iter_rows(min_row=2):
            for c in r:
                c.fill = LATER_FILL


def _need(wb, lines, title="Потребность по направлениям"):
    """Весь расчёт по строкам: из чего получилась потребность и сколько везём."""
    ws = _sheet(wb, title,
           ["Направление", "Кластер", "Артикул", "Отгрузки 14 дн", "Отгрузки 30 дн", "Коэф. сезона", "Скорость, шт/дн",
            "Доступно", "В пути", "Дней запаса", "Срок L, дн", "Потребность", "К отправке", "Коробов",
            "Короба", "Приоритет", "Пометки"],
           [[ln.warehouse, ln.cluster, ln.offer, ln.sales14, ln.sales30, ln.season, round(ln.speed, 2),
             ln.available, ln.transit, round(ln.days, 1) if ln.days is not None else None, ln.lead, ln.need,
             ln.qty, ln.box_count, _boxes(ln), ln.priority, "; ".join(ln.flags)] for ln in lines],
           [28, 24, 26, 9, 9, 8, 9, 9, 8, 9, 8, 10, 10, 8, 34, 10, 44])
    for i, ln in enumerate(lines, 2):
        if _fill(ln):
            for c in ws[i]:
                c.fill = _fill(ln)


def build_direction(plan, name, path, snap_time=""):
    """Файл одного направления: лист распределения и как посчитано — после «Всё верно» на экране направления."""
    wb = Workbook()
    wb.remove(wb.active)
    lines = [x for x in plan.lines if x.warehouse == name and x.qty > 0]
    _distribution(wb, plan, {name: lines})
    _need(wb, lines, "Как посчитано")
    _order_sheet(wb, plan, lines)
    _zero_sheet(wb, plan, {name})
    _later_sheet(wb, plan, {name})
    _sheet(wb, "Параметры", ["Параметр", "Значение"], [
        ["Направление", name],
        ["Снимок данных", snap_time],
        ["Дата отгрузки", plan.ship_date.strftime("%d.%m.%Y")],
        ["Спрос", f"отгрузки FBO из кластера за {config.SALES_DAYS} дн без отмен ÷ {config.SALES_DAYS} × коэф. сезона"],
        ["Потребность", "запас (остаток + в пути) от 30 дн — 0; иначе до 40 дн (хит, от 40 шт за 14 дн), 45 дн (20–39 шт), 50 дн (меньше 20) − в пути − доступно; короб рядом — ровно короб, иначе до 10 (больше 100 шт — до 50), минимум 30 шт"],
        ["К отправке", "потребность в пределах свободного остатка нашего склада, целыми моно-коробами"],
    ], [24, 90])
    wb.save(path)
    return path


def build(plan, path, snap_time=""):
    wb = Workbook()
    wb.remove(wb.active)

    _distribution(wb, plan, plan.by_warehouse())
    _need(wb, plan.lines)
    _order_sheet(wb, plan, [x for x in plan.lines if x.qty > 0])
    _zero_sheet(wb, plan)
    _later_sheet(wb, plan)

    _sheet(wb, "Пустые кластеры",
           ["Кластер", "Артикул", "Продажи 14 дн (весь Ozon)", "Рекомендуемый склад", "Свободно у нас"],
           [[x["cluster"], "ни одного нашего товара в кластере", None, x["warehouse"], None] for x in plan.empty_whole]
           + [[x["cluster"], x["offer"], x["sales14"], x["warehouse"], x["ff_free"]] for x in plan.empty_clusters],
           [26, 28, 14, 30, 12])

    ws = _sheet(wb, "Опт и всплески", ["Артикул", "Что", "Как посчитано"],
                [[x["offer"], x["kind"], x["text"]] for x in plan.demand_notes], [28, 10, 110])
    for i, x in enumerate(plan.demand_notes, 2):
        for c in ws[i]:
            c.fill = DEMAND_FILL[x["kind"]]

    _sheet(wb, "Не попало в план", ["Артикул", "Направление", "Потребность", "Скорость, шт/дн", "Мин. короб", "Почему"],
           [[x["offer"], x["warehouse"], x["need"], round(x["speed"], 2), x["box"], x["why"]]
            for x in sorted(plan.small, key=lambda x: -x["need"])], [28, 28, 12, 12, 10, 26])

    _sheet(wb, "Решить - стоп", ["Артикул", "Статус", "Рейтинг", "Выкуп", "Продажи 14 дн", "Причина"],
           [[x["offer"], x.get("status"), x.get("rating"), x.get("buyout"), x.get("sales14"), x["reason"]]
            for x in plan.decide], [28, 16, 9, 9, 12, 70])

    _sheet(wb, "Заказать",
           ["Артикул", "Корень", "Заказать, шт", "На Ozon (доступно + в пути)", "Хватит на Ozon, дн", "Продажи, шт/дн",
            "Потребность кластеров", "Свободно у нас", "Отгружаем сейчас", "Прибыль, ₽/шт", "В заказ"],
           [[x["offer"], x["root"], x["order"], x["ozon"], round(x["days"], 1), round(x["speed"], 2), x["need"], x["free"],
             x["ship"], x["profit"], "да"] for x in plan.order]
           + [[x["offer"], x["root"], x["order"], x["ozon"], round(x["days"], 1), round(x["speed"], 2), x["need"], x["free"],
               x["ship"], x["profit"], "нет: " + x["why"]] for x in plan.order_skipped],
           [26, 20, 12, 14, 12, 12, 14, 12, 12, 12, 30])

    _sheet(wb, "Скоро платно", ["Артикул", "Шт", "Платное с", "Дней до платного"],
           [[x["offer"], x["qty"], x["date"], x["days"]] for x in plan.paid], [28, 8, 12, 14])

    _sheet(wb, "FBO+FBS", ["Артикул", "FBO, шт", "FBS, шт", "Сигнал"],
           [[x["offer"], x["fbo"], x["fbs"], "приоритет FBO — снять FBS"] for x in plan.fbo_fbs], [28, 10, 10, 28])

    _sheet(wb, "Баланс нашего склада",
           ["Артикул «Моего склада»", "Остаток − резерв", "В заявках, не уехали", "Свободно", "Отправляем", "Остаётся"],
           [[r["article"], r["stock"], r["unshipped"], r["free"], r["used"], r["free"] - r["used"]]
            for r in sorted(plan.ff.values(), key=lambda r: -r["used"]) if r["used"] or r["unshipped"]],
           [26, 14, 14, 12, 12, 12])

    _sheet(wb, "Параметры", ["Параметр", "Значение"], [
        ["Снимок данных", snap_time],
        ["Дата отгрузки", plan.ship_date.strftime("%d.%m.%Y")],
        ["Спрос", f"отгрузки FBO из кластера за {config.SALES_DAYS} дн без отмен ÷ {config.SALES_DAYS} × коэф. сезона"],
        ["Потребность", "запас (остаток + в пути) от 30 дн — 0; иначе до 40 дн (хит, от 40 шт за 14 дн), 45 дн (20–39 шт), 50 дн (меньше 20) − в пути − доступно; короб рядом — ровно короб, иначе до 10 (больше 100 шт — до 50), минимум 30 шт"],
        ["Наш склад", f"«Мой склад», {config.MS_STORE}: остаток − резерв − заявки, которые ещё не уехали"],
        ["Короба", "моно-короб под одно направление; большими коробами, остаток — коробом поменьше"],
        ["Направления", "кластер целиком; склад приёмки — подсказка (наш основной склад кластера)"],
        ["Прибыль (Заказать)", "бот считает сам: с 1-го числа прошлого месяца по вчера. Продажи и комиссия — отчёт о "
                               "реализации Ozon, услуги Ozon — на штуку и по доле выручки, себестоимость — «Мой склад», "
                               "УСН 7%. В заказ — только прибыль на 1 шт больше 0"],
        ["Статусы", "таблица AlisaFox, лист «Юнит экономика Алиса», столбец «Статус ЗАЯВОК» (только чтение)"],
        ["Предупреждения", f"платное ≤ {config.PAID_SOON_DAYS} дн, выкуп < {config.BUYOUT_MIN:.0%}, "
                           f"рейтинг < {config.RATING_MIN}, сезон < {config.SEASON_LOW}"],
        ["Опт и всплески", f"заказ от {config.WHOLESALE_MIN} шт одного артикула — опт, в спрос не идёт (оранжевый). "
                           f"День больше {config.SPIKE_RATIO} × обычный (медиана {config.SALES_DAYS} дн) — всплеск, "
                           "считаем как обычный день (оранжевый). 2 всплеска и больше за последние 7 дней — спрос растёт: "
                           "считаем по факту, подсказка (зелёный)"],
        ["Сезонность", "коэффициенты по категории комиссии (тип товара), из «Программа формирования заявок на поставки», "
                       "лист «Коэффициент сезонности тех», сохранены в боте (supply_bot/season.tsv)"],
    ] + [["Сезон", f"{x['offer']} ({x['category']}): {x['k']} → {x['k_next']}"] for x in plan.season_notes]
      + [["Нет сезонности", f"{x['offer']} ({x['category']}): коэф. 1"] for x in plan.no_season]
      + [["Дубли", f"{x['root']}: {', '.join(x['offers'])}"] for x in plan.duplicates], [24, 90])

    wb.save(path)
    return path


def build_supply(take, rest, path):
    """После записи поставки: что везём (формат загрузки Ozon) и остаток на следующую отгрузку."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Везём"
    ws.append(["артикул", "имя (необязательно)", "количество"])
    for o, q in take.items():
        ws.append([o, None, q])
    ws2 = wb.create_sheet("Остаток на следующую")
    ws2.append(["артикул", "не принял склад, шт"])
    for o, q in rest.items():
        ws2.append([o, q])
    for w in (ws, ws2):
        for c in w[1]:
            c.font = BOLD
        w.column_dimensions["A"].width = 28
    wb.save(path)
