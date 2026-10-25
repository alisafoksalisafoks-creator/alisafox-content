"""Тексты для Telegram (HTML). Каждое сообщение — не длиннее лимита Telegram."""
from html import escape

from .sources.sheets import norm, root_of

LIMIT = 3800
ICON = {"срочно": "🔴", "наверх": "🟡", "обычно": "⚪"}


def fmt(n):
    return f"{int(round(n)):,}".replace(",", " ")


def chunks(lines, head=""):
    out, cur = [], head
    for line in lines:
        if len(cur) + len(line) + 1 > LIMIT:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur)
    return out


def _boxes(ln):
    if not ln.boxes:
        return "нет кванта"
    return " + ".join(f"{k}×{s}" for _, s, k in ln.boxes)


def summary(plan, taken_at=""):
    wh = plan.by_warehouse()
    qty = sum(x.qty for x in plan.lines)
    boxes = sum(x.box_count for x in plan.lines)
    urgent = sum(x.qty for x in plan.lines if x.priority == "срочно")
    lines = [
        f"<b>Распределение FBO на {plan.ship_date:%d.%m}</b>",
        f"Данные на {escape(taken_at)}" if taken_at else "",
        "",
        f"Везём <b>{fmt(qty)} шт</b> в <b>{fmt(boxes)} коробах</b> по {len(wh)} кластерам, срочно {fmt(urgent)} шт.",
    ]
    if plan.empty_whole:
        lines.append("❗ Рекомендуется отгрузка в кластер: " + ", ".join(escape(x["cluster"]) for x in plan.empty_whole)
                     + " — там нет ни одного нашего товара.")
    if plan.empty_clusters:
        cl = sorted({x["cluster"] for x in plan.empty_clusters})
        lines.append(f"Хиты, которых нет во всём кластере: {len(plan.empty_clusters)} — в {len(cl)} кластерах, кнопка «Пустые кластеры».")
    if plan.order:
        lines.append(f"🛒 Заказать: {len(plan.order)} арт., {fmt(sum(x['order'] for x in plan.order))} шт — "
                     "на Ozon заканчиваются, у нас нет или мало. Кнопка «Заказать».")
    if plan.decide:
        lines.append(f"Решить / стоп: {len(plan.decide)} арт.")
    if plan.paid:
        lines.append(f"Платное хранение ≤ 28 дн: {len(plan.paid)} арт. — не отгружаем.")
    if plan.season_notes:
        lines.append(f"Не сезон или спад сезона: {len(plan.season_notes)} арт.")
    no_box = sum(1 for x in plan.lines if x.qty and not x.boxes)
    if no_box:
        lines.append(f"⚠️ Нет кванта коробов: {no_box} строк, количество без округления.")
    lines += ["", "<b>По кластерам</b>"]
    for name, items in wh.items():
        lines.append(f"{escape(name)}: {fmt(sum(x.qty for x in items))} шт, {sum(x.box_count for x in items)} кор., {len(items)} арт.")
    return chunks([x for x in lines if x is not None])


def warehouse(plan, name):
    items = [x for x in plan.lines if x.warehouse == name and x.qty > 0]
    if not items:
        return [f"В {escape(name)} сейчас везти нечего."]
    where = items[0].cluster if items[0].cluster != name else "кластер"
    ship = f"склад приёмки: {escape(items[0].ship_to)}" if items[0].ship_to else ""
    head = [f"<b>{escape(name)}</b> · {escape(where)}" + (f"\n{ship} (подсказка)" if ship else ""),
            f"{fmt(sum(x.qty for x in items))} шт, {sum(x.box_count for x in items)} коробов, отгрузка {plan.ship_date:%d.%m}", ""]
    rows = []
    for ln in sorted(items, key=lambda x: (-["обычно", "наверх", "срочно"].index(x.priority), -x.qty)):
        days = f"{ln.days:.0f} дн" if ln.days is not None else "—"
        flag = (" · " + escape("; ".join(ln.flags))) if ln.flags else ""
        rows.append(f"{ICON[ln.priority]} <code>{escape(ln.offer)}</code> — <b>{fmt(ln.qty)}</b> шт ({_boxes(ln)}) · "
                    f"{ln.speed:.1f}/дн, запас {days}{flag}")
    return chunks(head + rows)


def all_warehouses(plan):
    out = []
    for name in plan.by_warehouse():
        out += warehouse(plan, name)
    return out


def offer(plan, query, snap_unit=None):
    q = norm(query)
    items = [x for x in plan.lines if norm(x.offer) == q]
    if not items:
        why = [x for x in plan.decide if norm(x["offer"]) == q]
        if why:
            return [f"<code>{escape(query)}</code>: не везём — {escape(why[0]['reason'])}."]
        if any(norm(x["offer"]) == q for x in plan.no_stock):
            tail = " Он в списке «Заказать»." if any(norm(x["offer"]) == q for x in plan.order) else ""
            return [f"<code>{escape(query)}</code>: у вас нет товара, везти нечего.{tail}"]
        small = [x for x in plan.small if norm(x["offer"]) == q]
        if small:
            ff = next((r for r in plan.ff.values() if norm(r["article"]) in (q, root_of(q))), None)
            free = f"свободно у нас {fmt(ff['free'] - ff['used'])} шт" if ff else ""
            rows = [f"<code>{escape(query)}</code>: потребность есть, но в план не попала. {free}"]
            rows += [f"  {escape(x['warehouse'])} — нужно {fmt(x['need'])}, короб {x['box'] or 'нет кванта'} · {x['why']}"
                     for x in sorted(small, key=lambda x: -x["need"])[:25]]
            if len(small) > 25:
                rows.append(f"  … ещё {len(small) - 25} направлений в файле")
            rows.append("Поставить вручную: «Исправить кол-во».")
            return chunks(rows)
        st = (snap_unit or {}).get(q, {}).get("status")
        if st == "не грузим":
            return [f"<code>{escape(query)}</code>: статус «Не грузим»."]
        return [f"<code>{escape(query)}</code>: потребности нет — запаса на складах хватает или нет продаж."]
    ff = next((r for r in plan.ff.values() if r["used"] and norm(r["article"]) in (q, norm(items[0].root))), None)
    head = [f"<b>{escape(items[0].offer)}</b> · отгрузка {plan.ship_date:%d.%m}"]
    if ff:
        head.append(f"Наш склад ({escape(ff['article'])}): свободно {fmt(ff['free'])}, везём {fmt(ff['used'])}")
    head.append("")
    rows = [f"{ICON[x.priority]} {escape(x.warehouse)} — <b>{fmt(x.qty)}</b> шт ({_boxes(x)}) · "
            f"{x.speed:.1f}/дн, доступно {fmt(x.available)}, в пути {fmt(x.transit)}"
            + (f" · {escape('; '.join(x.flags))}" if x.flags else "")
            for x in sorted(items, key=lambda x: -x.qty)]
    gaps = [x for x in plan.empty_clusters if norm(x["offer"]) == q]
    if gaps:
        rows += ["", "Нет ни на одном складе кластера: " + ", ".join(escape(x["cluster"]) for x in gaps)]
    return chunks(head + rows)


def order(plan):
    """Кнопка «Заказать»: на Ozon кончается или уже 0, на нашем складе нет или мало. Только прибыльные."""
    if not plan.order:
        return ["Заказывать нечего: всё, что кончается на Ozon, есть на нашем складе."]
    lines = ["<b>🛒 Заказать</b>",
             "На Ozon скоро закончится или уже 0, а на нашем складе нет или не хватает. Только прибыльные артикулы.",
             "Прибыль на 1 шт бот считает сам по отчётам Ozon с 1-го числа прошлого месяца, минус себестоимость и УСН 7%.",
             "Заказать = потребность кластеров на срок L + 30 дн − всё, что есть на нашем складе.", ""]
    for x in plan.order:
        when = "на Ozon уже 0" if x["ozon"] <= 0 else f"на Ozon хватит на {x['days']:.0f} дн"
        lines.append(f"<code>{escape(x['offer'])}</code> — заказать <b>{fmt(x['order'])}</b> шт · {when} · "
                     f"{x['speed']:.1f} шт/дн · у нас {fmt(x['free'])} · прибыль {fmt(x['profit'])} ₽/шт")
    if plan.order_skipped:
        minus = sum(1 for x in plan.order_skipped if x["why"] == "минусовой артикул")
        lines += ["", f"Не включены: минусовые — {minus}, прибыль не посчитать (нет продаж или себестоимости) — "
                      f"{len(plan.order_skipped) - minus}. "
                      "Список в файле, лист «Заказать»."]
    return chunks(lines)


def empty_clusters(plan):
    if not plan.empty_clusters and not plan.empty_whole:
        return ["Пустых кластеров нет: в каждом кластере товар есть хотя бы на одном складе."]
    lines = []
    if plan.empty_whole:
        lines += ["<b>Кластеры, где нет ни одного нашего товара</b>"]
        for x in plan.empty_whole:
            lines.append(f"❗ {escape(x['cluster'])} — рекомендуется отгрузка в кластер"
                         + (f", склад {escape(x['warehouse'])}" if x["warehouse"] else ", склад выбрать в кабинете"))
        lines.append("")
    by = {}
    for x in plan.empty_clusters:
        by.setdefault(x["cluster"], []).append(x)
    if by:
        lines += ["<b>Хиты, которых нет ни на одном складе кластера</b>",
                  "Продаются от 1 шт/день, в кластере 0 и в расчёт туда не попали.", ""]
    for cl, items in sorted(by.items(), key=lambda kv: -sum(x["sales14"] for x in kv[1])):
        wh = items[0]["warehouse"] or "склад выбрать в кабинете"
        lines.append(f"<b>{escape(cl)}</b> → {escape(wh)}")
        for x in sorted(items, key=lambda x: -x["sales14"])[:30]:
            lines.append(f"  <code>{escape(x['offer'])}</code> — продажи 14 дн по Ozon {fmt(x['sales14'])}, у нас свободно {fmt(x['ff_free'])}")
        if len(items) > 30:
            lines.append(f"  … ещё {len(items) - 30} арт. в файле")
    return chunks(lines)


def decide(plan):
    if not plan.decide:
        return ["Список «Решить / стоп» пуст."]
    lines = ["<b>Решить / стоп</b>", ""]
    for x in sorted(plan.decide, key=lambda x: -(x.get("sales14") or 0)):
        lines.append(f"<code>{escape(x['offer'])}</code> — {escape(x['reason'])} · продажи 14 дн {fmt(x.get('sales14') or 0)}")
    return chunks(lines)


def paid(plan):
    if not plan.paid:
        return ["В ближайшие 28 дней платного хранения нет."]
    lines = ["<b>Платное хранение: не отгружаем</b>", ""]
    for x in sorted(plan.paid, key=lambda x: x["days"]):
        when = "уже платное" if x["days"] <= 0 else f"с {x['date']:%d.%m}, через {x['days']} дн"
        lines.append(f"<code>{escape(x['offer'])}</code> — {fmt(x['qty'])} шт, {when}")
    return chunks(lines)


def reconcile(plan):
    rows = [r for r in plan.ff.values() if r["raw"] < 0 or r["unshipped"] > r["stock"]]
    lines = ["<b>Сверка «Мой склад» ↔ заявки Ozon</b>", ""]
    if not rows:
        lines.append("Расхождений нет.")
    for r in rows:
        if r["raw"] < 0:
            lines.append(f"<code>{escape(r['article'])}</code>: в «Моём складе» минус {fmt(-r['raw'])} шт — проверьте оприходование.")
        else:
            lines.append(f"<code>{escape(r['article'])}</code>: в заявках {fmt(r['unshipped'])} шт, а на складе {fmt(r['stock'])}.")
    return chunks(lines)
