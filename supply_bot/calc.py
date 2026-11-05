"""Расчёт поставки FBO по кластерам Ozon.

Направление поставки — только кластер целиком, без отдельных складов (решение владельца 25.10.2026).
Склад приёмки — подсказка: наш основной склад кластера по отгрузкам за 30 дней.
Потребность кластера = отгрузки FBO из кластера за 14 дн ÷ 14 — формула «Заявки» владельца: (отгрузки 14 дн × 2,5 − в пути − доступно) × коэф. сезона.
Дальше: статусы и стопы → лимит «Моего склада» с выравниванием дней запаса →
полные моно-короба под каждое направление → пустые кластеры и предупреждения.
"""
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import config
from .sources.sheets import norm, root_of


@dataclass
class Snapshot:
    today: date
    postings: list            # [(day 'YYYY-MM-DD', offer_id, sku, warehouse, qty)] — FBO без отмен, 30 дней
    wh_stocks: list           # строки /v1/analytics/stocks
    wh_cluster: dict          # {склад: кластер}
    product_stocks: dict      # {sku: {offer_id, fbo, fbs}}
    ms: list                  # [{article, name, stock, reserve}] — «Мой склад», основной склад
    unshipped: list           # [{order, warehouse, state, slot, items {offer: qty}}]
    unit: dict                # {артикул: {status, root, rating, buyout, category, volume}}
    paid: dict                # {артикул: [{qty, date}]}
    boxes: dict               # {артикул: [(короб, шт)]}
    season: dict = field(default_factory=dict)   # {категория: {месяц 1..12: коэф}}
    profit: dict = field(default_factory=dict)   # {offer: {per_unit, profit, units, revenue}} — profit.py
    taken_at: str = ""
    not_accepted: list = field(default_factory=list)  # [{order, warehouse, cluster, state, slot, items}] — в пути / на приёмке


@dataclass
class Line:
    offer: str
    root: str
    warehouse: str            # направление: название кластера
    cluster: str
    sales14: int
    sales30: int
    speed: float              # шт/день с учётом сезона
    available: int
    transit: int
    lead: int
    need: int                 # потребность до лимита ФФ
    season: float = 1.0
    qty: int = 0              # к отправке после ФФ и коробов
    boxes: list = field(default_factory=list)   # [(короб, шт в коробе, коробов)]
    priority: str = "обычно"
    flags: list = field(default_factory=list)
    manual: bool = False
    ship_to: str = ""         # склад приёмки — подсказка: наш основной склад кластера
    demand: str = ""          # «опт», «всплеск» или «рост» — строку подсвечиваем
    main: bool = False        # приоритетный кластер артикула при нехватке нашего склада
    partial: bool = False     # статус «Частично грузим» — цель 35 дн
    bf: float = 1.0           # доля выкупа, если выкуп < 85%: скорость считаем по выкупам
    warn: str = ""            # «платное» (красный) или «выкуп» (сиреневый) — грузим, но подсвечиваем
    absent: bool = False      # товара в кластере нет совсем — скорость оценена по другим кластерам

    @property
    def eff14(self):
        return self.sales14 * self.bf

    @property
    def have(self):
        return self.available + self.transit

    @property
    def days(self):
        return self.have / self.speed if self.speed else None

    @property
    def box_count(self):
        return sum(b[2] for b in self.boxes)


@dataclass
class Plan:
    today: date
    ship_date: date
    lines: list = field(default_factory=list)
    decide: list = field(default_factory=list)      # [{offer, warehouse, reason, ...}] — «Решить / стоп»
    no_stock: list = field(default_factory=list)    # [{offer, root, sales14}] — на нашем складе 0
    order: list = field(default_factory=list)       # «Заказать»: на Ozon кончается, у нас нет или мало, прибыльные
    order_skipped: list = field(default_factory=list)  # не попали в «Заказать»: минус или нет данных о прибыли
    purchase: list = field(default_factory=list)    # [{root, short, need, free}]
    empty_clusters: list = field(default_factory=list)
    empty_whole: list = field(default_factory=list)
    small: list = field(default_factory=list)      # потребность меньше короба или не хватило остатка
    paid: list = field(default_factory=list)
    stuck: list = field(default_factory=list)       # [{order, cluster, slot, days, qty}] — висят на приёмке, в «в пути» не берём
    fbo_fbs: list = field(default_factory=list)
    season_notes: list = field(default_factory=list)
    demand_notes: list = field(default_factory=list)  # [{offer, kind, text}] — опт, всплески, рост спроса
    no_season: list = field(default_factory=list)   # категория без коэффициента на этот месяц — считаем с 1
    duplicates: list = field(default_factory=list)
    ff: dict = field(default_factory=dict)          # {ключ МС: {article, stock, unshipped, free, used}}
    notes: list = field(default_factory=list)
    stopped: dict = field(default_factory=dict)     # {кластер: [{offer, need, reason}]} — «Решить / стоп» под итогом кластера

    def by_warehouse(self):
        out = defaultdict(list)
        for ln in self.lines:
            if ln.qty > 0:
                out[ln.warehouse].append(ln)
        return dict(sorted(out.items(), key=lambda kv: -sum(x.qty for x in kv[1])))


def next_ship_date(today):
    d = today + timedelta(days=1)
    while d.weekday() not in config.SHIP_WEEKDAYS:
        d += timedelta(days=1)
    return d


def season_k(snap, category, month):
    return (snap.season.get(category) or {}).get(month, 1.0)


def demand_spikes(rows, today):
    """Опт и всплески по артикулу (решение владельца 26.10.2026).

    rows: [(день, артикул, склад, шт)] — одна строка = один заказ.
    Опт — заказ от WHOLESALE_MIN шт: в спрос не идёт, артикул подсвечиваем.
    Всплеск — день за 14 дн больше SPIKE_RATIO × обычный день (медиана 14 дней): с 05.11.2026 считаем по факту, только подсвечиваем.
    Всплесков 2 и больше за последние 7 дней — пошёл спрос: всплески оставляем, подсказка «спрос растёт».
    Возвращает ({(артикул, день): доля, которую оставляем}, {артикул: {kind, text}}).
    """
    days14 = [str(today - timedelta(days=i)) for i in range(config.SALES_DAYS, 0, -1)]
    d7 = str(today - timedelta(days=7))
    daily, opt = defaultdict(lambda: defaultdict(int)), defaultdict(list)
    for day, offer, _wh, qty in rows:
        if qty >= config.WHOLESALE_MIN:
            if day >= days14[0]:
                opt[offer].append((day, qty))
        elif day >= days14[0]:
            daily[offer][day] += qty
    cut, out = {}, {}
    for offer, by_day in daily.items():
        usual = statistics.median(by_day.get(d, 0) for d in days14)
        if usual < config.SPIKE_MIN_DAY:
            continue
        hot = [d for d in days14 if by_day.get(d, 0) > config.SPIKE_RATIO * usual]
        if not hot:
            continue
        what = ", ".join(f"{by_day[d]} шт {d[8:10]}.{d[5:7]}" for d in hot)
        if sum(d >= d7 for d in hot) >= 2:
            out[offer] = {"kind": "рост", "text": f"спрос растёт: всплески {what} при обычном дне {usual:g} шт — "
                                                  "считаю по факту, проверьте, не пора ли поднять закупку"}
            continue
        # с 05.11.2026 всплеск считаем по факту (опт от 10 шт в заказе уже убран) — решение владельца
        out[offer] = {"kind": "всплеск", "text": f"всплеск {what} при обычном дне {usual:g} шт — посчитан по факту"}
    for offer, items in opt.items():
        what = ", ".join(f"{q} шт {d[8:10]}.{d[5:7]}" for d, q in sorted(items))
        text = f"опт: {len(items)} заказ(а) {what} — в спрос не взят"
        if offer in out:
            out[offer]["text"] = text + "; " + out[offer]["text"]
        else:
            out[offer] = {"kind": "опт", "text": text}
    return cut, out


def pick_boxes(qty, sizes, cap):
    """Полные моно-короба под одно направление.

    Большими коробами, остаток — самым маленьким коробом, в который он помещается.
    Вверх — только если итог не больше cap (потолок горизонта + запас на округление), иначе вниз.
    """
    sizes = sorted({s for _, s in sizes if s > 0}, reverse=True)
    if not sizes or qty <= 0:
        return []
    out, rem = [], qty
    for s in sizes:
        k = rem // s
        if k:
            out.append([s, k])
            rem -= k * s
    if rem:
        up = min((s for s in sizes if s >= rem), default=sizes[-1])
        if sum(s * k for s, k in out) + up <= cap:
            for item in out:
                if item[0] == up:
                    item[1] += 1
                    break
            else:
                out.append([up, 1])
    return [(s, k) for s, k in sorted(out, key=lambda x: -x[0])]


PARTIAL = "неполный короб"


def pack(qty, sizes):
    """Правило коробов (26.10.2026): полными коробами от большого к малому.

    Сверх короба (что не влезло в полные короба) не меньше половины самого маленького короба — ещё один
    маленький короб, меньше — не везём. Потребность меньше половины самого маленького короба — неполный
    короб, округлённый вверх до 10 шт. Возвращает ([(шт, коробов)], сверх короба не везём шт, шт в неполном).
    """
    sizes = sorted({s for _, s in sizes if s > 0}, reverse=True)
    if not sizes or qty <= 0:
        return [], 0, 0
    small = sizes[-1]
    out, rem = {}, qty
    for s in sizes:
        if rem // s:
            out[s] = rem // s
            rem -= out[s] * s
    if not out and rem * 2 < small:
        if rem < config.PARTIAL_MIN:  # меньше 5 шт неполным коробом не везём
            return [], rem, 0
        part = min(-(-rem // 10) * 10, small)
        return ([(small, 1)], 0, 0) if part == small else ([], 0, part)
    dropped = 0
    if rem and rem * 2 >= small:
        out[small] = out.get(small, 0) + 1
    else:
        dropped = rem
    return sorted(out.items(), key=lambda x: -x[0]), dropped, 0


def label_boxes(boxes, sizes):
    """[(шт, коробов)] → [(короб, шт, коробов)] с названием короба из файла квантов."""
    name_of = {}
    for name, s in sizes:
        name_of.setdefault(s, name)
    return [(name_of.get(s, f"{s} шт"), s, k) for s, k in boxes]


def build(snap, overrides=None):
    """Полный расчёт. overrides: {(артикул, склад): шт} — ручные поправки владельца."""
    overrides = overrides or {}
    today = snap.today
    plan = Plan(today=today, ship_date=next_ship_date(today))
    # сезон — месяц, когда поставка доедет и начнёт продаваться (в конце октября это уже ноябрь)
    month = (plan.ship_date + timedelta(days=config.LEAD_DEFAULT)).month

    foreign_wh = {w for w, cl in snap.wh_cluster.items() if config.foreign(cl)} | \
        {r["warehouse"] for r in snap.wh_stocks if config.foreign(r.get("cluster"))}

    # --- спрос по складам ---
    d14, d30 = str(today - timedelta(days=config.SALES_DAYS)), str(today - timedelta(days=config.REF_DAYS))
    rows = [(day, norm(offer), wh, qty) for day, offer, _sku, wh, qty in snap.postings
            if day < str(today) and wh and wh not in foreign_wh]
    cut, demand = demand_spikes(rows, today)
    s14, s30 = defaultdict(float), defaultdict(int)
    wh_sales30 = defaultdict(int)
    for day, offer, wh, qty in rows:
        if qty >= config.WHOLESALE_MIN:
            continue
        key = (offer, wh)
        if day >= d30:
            s30[key] += qty
            wh_sales30[wh] += qty
        if day >= d14:
            s14[key] += qty * cut.get((offer, day), 1)

    stock = {}
    cluster_of = {w: cl for w, cl in snap.wh_cluster.items() if not config.foreign(cl)}
    for r in snap.wh_stocks:
        if config.foreign(r.get("cluster")):
            continue
        key = (norm(r["offer_id"]), r["warehouse"])
        rec = stock.setdefault(key, {"available": 0, "transit": 0})
        rec["available"] += r["available"]
        rec["transit"] += r["transit"]
        if r.get("cluster"):
            cluster_of[r["warehouse"]] = r["cluster"]

    offer_name = {norm(r["offer_id"]): r["offer_id"] for r in snap.wh_stocks}
    for _d, offer, _s, _w, _q in snap.postings:
        offer_name.setdefault(norm(offer), offer)
    for art, u in snap.unit.items():
        offer_name.setdefault(art, u["article"])

    # --- «Мой склад»: свободно = остаток − резерв − заявки, которые ещё не уехали ---
    ms_by = {}
    for m in snap.ms:
        k = norm(m["article"])
        rec = ms_by.setdefault(k, {"article": m["article"], "stock": 0, "raw": 0})
        rec["raw"] += int(m["stock"])
        rec["stock"] += max(0, int(m["stock"] - m["reserve"]))
    unshipped = defaultdict(int)
    for s in snap.unshipped:
        for offer, q in s["items"].items():
            unshipped[_ms_key(offer, snap.unit, ms_by)] += q
    for k, rec in ms_by.items():
        rec["unshipped"] = unshipped.get(k, 0)
        rec["free"] = max(0, rec["stock"] - rec["unshipped"])
        rec["used"] = 0
    plan.ff = ms_by

    # --- отгрузки по кластерам: для подсказки склада приёмки и доли кластера ---
    by_cluster = defaultdict(list)
    for wh, q in wh_sales30.items():
        by_cluster[cluster_of.get(wh, "")].append((q, wh))

    # --- проверки по артикулу ---
    offers = {k[0] for k in list(s30) + list(stock)}
    eligible, no_stock_k = {}, {}
    for offer in sorted(offers):
        name = offer_name.get(offer, offer)
        u = snap.unit.get(offer)
        tot14 = round(sum(v for (o, _), v in s14.items() if o == offer))
        info = {"offer": name, "sales14": tot14}
        if not u:
            if tot14:
                plan.decide.append({**info, "reason": "нет статуса в таблице AlisaFox"})
            continue
        st = u["status"]
        info.update(status=st, rating=u.get("rating"), buyout=u.get("buyout"))
        if u.get("conflict"):
            plan.decide.append({**info, "reason": "в таблице два разных статуса: " + " / ".join(u["conflict"])})
            continue
        if st == config.NO_LOAD:
            continue
        if st == config.NEW:
            plan.decide.append({**info, "reason": "новинка — нужна стратегия первой поставки"})
            continue
        if st not in config.LOAD:
            plan.decide.append({**info, "reason": f"неизвестный статус «{st}»"})
            continue
        if u.get("rating") and 0 < u["rating"] < config.RATING_MIN:
            plan.decide.append({**info, "reason": f"рейтинг {u['rating']:.1f} < {config.RATING_MIN}"})
            continue
        # выкуп ниже 85% и платное хранение — грузим, но подсвечиваем (решение владельца 02.11.2026)
        warn = None
        if u.get("buyout") is not None and u["buyout"] < config.BUYOUT_MIN and tot14 > 0:
            warn = ("выкуп", f"выкуп {u['buyout']:.0%} < 85% — проверьте товар")
        paid = [p for p in snap.paid.get(offer, []) if (p["date"] - today).days <= config.PAID_SOON_DAYS]
        if paid:
            p = min(paid, key=lambda x: x["date"])
            left = (p["date"] - today).days
            when = "уже платное хранение" if left <= 0 else f"платное хранение с {p['date']:%d.%m} (через {left} дн)"
            plan.paid.append({"offer": name, "qty": p["qty"], "date": p["date"], "days": left})
            warn = ("платное", f"{when}, {p['qty']} шт" + (f"; {warn[1]}" if warn else ""))
        key = _ms_key(offer, snap.unit, ms_by)
        if not key or ms_by[key]["free"] <= 0:
            if tot14:
                plan.no_stock.append({"offer": name, "root": u["root"], "sales14": tot14})
                no_stock_k[offer] = {"u": u, "k": season_k(snap, u.get("category"), month), "name": name}
            continue
        k = season_k(snap, u.get("category"), month)
        if snap.season and month not in (snap.season.get(u.get("category")) or {}):
            plan.no_season.append({"offer": name, "category": u.get("category") or "категория не указана"})
        k_next = season_k(snap, u.get("category"), month % 12 + 1)
        if k < config.SEASON_LOW or (k and k_next < k * (1 - config.SEASON_DROP)):
            plan.season_notes.append({"offer": name, "category": u.get("category"), "k": k, "k_next": k_next})
        bf = 1.0   # выкуп < 85%: с 05.11.2026 скорость снова по заказам, только подсвечиваем (решение владельца)
        eligible[offer] = {"u": u, "k": k, "key": key, "name": name, "warn": warn, "bf": bf}

    # --- направления: кластер целиком, в Москве — основные склады ---
    s14c, s30c, stock_c = defaultdict(float), defaultdict(int), {}
    for (offer, wh), v in s14.items():
        if wh in cluster_of:
            s14c[(offer, cluster_of[wh])] += v
    for (offer, wh), v in s30.items():
        if wh in cluster_of:
            s30c[(offer, cluster_of[wh])] += v
    for (offer, wh), st in stock.items():
        rec = stock_c.setdefault((offer, cluster_of.get(wh, "")), {"available": 0, "transit": 0})
        rec["available"] += st["available"]
        rec["transit"] += st["transit"]
    # --- поставки, которые Ozon не принял больше STUCK_DAYS после слота: считаем потерянными (решение владельца 05.11.2026) ---
    stuck_cut = defaultdict(list)
    for sp in getattr(snap, "not_accepted", None) or []:
        if not sp.get("slot"):
            continue
        age = (today - date.fromisoformat(sp["slot"][:10])).days
        cl = cluster_of.get(sp.get("warehouse")) or sp.get("cluster")
        if age <= config.STUCK_DAYS or not cl:
            continue
        plan.stuck.append({"order": sp["order"], "cluster": cl, "slot": sp["slot"][:10], "days": age,
                           "qty": sum(sp["items"].values())})
        for o, q in sp["items"].items():
            rec = stock_c.get((norm(o), cl))
            if rec:
                cut = min(q, rec["transit"])
                rec["transit"] -= cut
                if cut:
                    stuck_cut[(norm(o), cl)].append(f"из «в пути» убрала {cut} шт: поставка {sp['order']} не принята "
                                                    f"Ozon с {sp['slot'][8:10]}.{sp['slot'][5:7]} ({age} дн) — напишите в поддержку")
    top_wh = {cl: max(items)[1] for cl, items in by_cluster.items() if items}
    zero = {"available": 0, "transit": 0}

    lines = {}
    for (offer, cl) in sorted(set(s30c) | set(stock_c)):
        if offer not in eligible or not cl:
            continue
        e = eligible[offer]
        st = stock_c.get((offer, cl), zero)
        speed = s14c.get((offer, cl), 0) / config.SALES_DAYS * e["k"]
        lead = config.lead_days(cl)
        need = config.need(s14c.get((offer, cl), 0) * e["bf"], st["available"] + st["transit"], e["k"], cl,
                           partial=e["u"].get("status") == "частично грузим")
        lines[(offer, cl)] = Line(
            offer=e["name"], root=e["u"]["root"], warehouse=cl, cluster=cl, ship_to=top_wh.get(cl, ""),
            sales14=round(s14c.get((offer, cl), 0)), sales30=s30c.get((offer, cl), 0), speed=speed,
            available=st["available"], transit=st["transit"], lead=lead, need=need, season=e["k"], bf=e["bf"],
            partial=e["u"].get("status") == "частично грузим")
        lines[(offer, cl)].flags += stuck_cut.get((offer, cl), [])
        if e.get("warn"):
            lines[(offer, cl)].warn = e["warn"][0]
            lines[(offer, cl)].flags.append(e["warn"][1])

    # --- ходовой товар, которого в кластере нет совсем (ни остатка, ни продаж за 14 дн): довозим по оценке
    #     из продаж в других кластерах (решение владельца 05.11.2026). Оценка = продажи артикула за 14 дн ×
    #     доля кластера / доля кластеров, где артикул продавался. ---
    cl_s14 = defaultdict(float)
    for (_o, cl), v in s14c.items():
        if cl:
            cl_s14[cl] += v
    shop14 = sum(cl_s14.values()) or 1
    all_cl = sorted(set(cluster_of.values()) - {""})
    root_in = defaultdict(float)   # модель (корневой артикул) уже есть в кластере другой карточкой — не считаем «нет»
    for (o, cl), st in stock_c.items():
        if o in eligible:
            root_in[(eligible[o]["u"]["root"], cl)] += st["available"] + st["transit"]
    for (o, cl), v in s14c.items():
        if o in eligible:
            root_in[(eligible[o]["u"]["root"], cl)] += v
    for offer, e in eligible.items():
        tot = sum(v for (o, cl), v in s14c.items() if o == offer and cl)
        if tot / config.SALES_DAYS < config.HIT_MIN_PER_DAY:
            continue
        sold_share = sum(cl_s14[cl] for (o, cl), v in s14c.items() if o == offer and cl and v > 0) / shop14
        for cl in all_cl:
            share = cl_s14.get(cl, 0) / shop14
            st = stock_c.get((offer, cl), zero)
            if share < config.EMPTY_CLUSTER_MIN_SHARE or s14c.get((offer, cl), 0) > 0 \
                    or st["available"] + st["transit"] > 0 or not sold_share or root_in.get((e["u"]["root"], cl)):
                continue
            est = tot * share / sold_share
            partial = e["u"].get("status") == "частично грузим"
            need = config.need(est, 0, e["k"], cl, partial=partial)
            ln = lines[(offer, cl)] = Line(
                offer=e["name"], root=e["u"]["root"], warehouse=cl, cluster=cl, ship_to=top_wh.get(cl, ""),
                sales14=0, sales30=s30c.get((offer, cl), 0), speed=est / config.SALES_DAYS * e["k"],
                available=0, transit=0, lead=config.lead_days(cl), need=need, season=e["k"], bf=e["bf"],
                partial=partial)
            ln.flags.append(f"в кластере нет: по продажам в других кластерах ждём ~{est:.0f} шт за 14 дн (оценка)")
            ln.absent = True
            ln.priority = "наверх"
            if e.get("warn"):
                ln.warn = e["warn"][0]
                ln.flags.append(e["warn"][1])

    for (offer, _cl), ln in lines.items():
        if offer in demand:
            ln.demand = demand[offer]["kind"]
            ln.flags.append(demand[offer]["text"])
    for offer, d in sorted(demand.items()):
        if offer in eligible:
            plan.demand_notes.append({"offer": eligible[offer]["name"], **d})

    for ln in lines.values():
        if ln.days is not None and ln.days < ln.lead:
            ln.priority = "срочно"
        elif ln.speed >= config.HIT_MIN_PER_DAY and ln.days is not None and ln.days < config.URGENT_HIT_DAYS:
            ln.priority = max(ln.priority, "наверх", key=_prio_rank)

    # --- лимит «Моего склада»: при нехватке выравниваем дни запаса ---
    groups = defaultdict(list)
    for (offer, _wh), ln in lines.items():
        if ln.need > 0:
            groups[eligible[offer]["key"]].append(ln)
    cl_sales = defaultdict(float)   # продажи всего магазина по кластеру — очередь кластеров после Казани
    for (_o, cl), v in s14c.items():
        cl_sales[cl] += v
    for key, group in groups.items():
        ff = ms_by[key]
        total = sum(ln.need for ln in group)
        if total <= ff["free"]:
            for ln in group:
                ln.qty = ln.need
        else:
            _level(group, ff["free"], cl_sales)
            plan.purchase.append({"root": ff["article"], "need": total, "free": ff["free"], "short": total - ff["free"]})

    # --- ручные поправки на склад, которого нет в расчёте ---
    directions = directions_of(plan, cluster_of)
    for (offer_in, wh), q in overrides.items():
        offer = norm(offer_in)
        if (offer, wh) in lines or offer not in eligible or wh not in directions:
            continue
        e, cl = eligible[offer], directions[wh]
        st = stock_c.get((offer, wh), zero)
        ln = lines[(offer, wh)] = Line(
            offer=e["name"], root=e["u"]["root"], warehouse=wh, cluster=cl, ship_to=top_wh.get(cl, ""),
            sales14=0, sales30=0, speed=0, available=st["available"], transit=st["transit"],
            lead=config.lead_days(cl), need=0, season=e["k"])
        groups[e["key"]].append(ln)

    # --- моно-короба под каждое направление ---
    for (offer, wh), ln in lines.items():
        manual = next((q for (o, w), q in overrides.items() if norm(o) == offer and w == wh), None)
        if manual is not None:
            ln.qty, ln.manual = int(manual), True
            ln.flags.append("вручную")
        if ln.qty <= 0:
            continue
        sizes = _sizes(offer, ln.root, snap.boxes)
        if ln.manual:
            if not sizes:
                ln.flags.append("нет кванта коробов — количество без округления")
                continue
            # ручное количество — до ближайших полных коробов (поровну — вверх)
            down, up = pick_boxes(ln.qty, sizes, ln.qty), pick_boxes(ln.qty, sizes, 10 ** 9)
            boxes = min((up, down), key=lambda bx: abs(sum(s * k for s, k in bx) - ln.qty))
            ln.boxes = label_boxes(boxes, sizes)
            ln.qty = sum(s * k for _, s, k in ln.boxes)
            continue
        # логика владельца (02.11.2026): короб рядом — ровно короб; иначе круглое число, не меньше 30 шт
        raw = ln.qty
        q, box = config.round_qty(raw, [sz for _, sz in sizes])
        bigger = sorted(sz for _, sz in sizes if sz >= raw)
        if ln.have <= 0 and bigger:   # на Ozon 0 — ближайший полный короб (решение владельца 02.11.2026)
            q, box = bigger[0], bigger[0]
        if ln.absent and ln.speed:
            if q / ln.speed > config.SKIP_AFTER_DAYS:   # полный короб слишком велик — пробуем круглое число
                q, box = config.round_qty(raw, [sz for _, sz in sizes])
            if q / ln.speed > config.SKIP_AFTER_DAYS:
                ln.flags.append(f"не везём: {q} шт хватит на {q / ln.speed:.0f} дн по оценке "
                                f"(больше {config.SKIP_AFTER_DAYS})")
                ln.qty, ln.boxes = 0, []
                continue
        d0 = ln.eff14 / config.SALES_DAYS if ln.sales14 else 0
        if d0 and ln.have > 0 and ln.have / d0 >= config.ORANGE_WAIT_DAYS and (ln.have + q) / d0 > config.SLOW_MAX_DAYS:
            # оранжевая строка, а запаса ещё от 25 дн — ждём неделю, довезём больше (решение владельца 02.11.2026)
            ln.flags.append(f"не везём: запас {ln.have / d0:.0f} дн, а {q} шт дали бы {(ln.have + q) / d0:.0f} дн "
                            f"(больше {config.SLOW_MAX_DAYS}) — повезём со следующей поставкой")
            ln.qty, ln.boxes = 0, []
            continue
        if d0 and ln.have > 0 and (ln.have + q) / d0 > config.SKIP_AFTER_DAYS:
            ln.flags.append(f"не везём: нужно {raw}, но {q} шт дали бы запас {(ln.have + q) / d0:.0f} дн "
                            f"(больше {config.SKIP_AFTER_DAYS})")
            ln.qty, ln.boxes = 0, []
            continue
        ln.qty = q
        small = min((sz for _, sz in sizes), default=0)
        fit = max((sz for _, sz in sizes if sz and q % sz == 0), default=0)
        if box:
            ln.boxes = label_boxes([(box, q // box)], sizes)
        elif fit:   # круглое число ровно в одинаковых коробах — берём самый большой такой короб
            ln.boxes = label_boxes([(fit, q // fit)], sizes)
        else:
            ln.boxes = []
        d = ln.eff14 / config.SALES_DAYS if ln.sales14 else 0
        ln.flags.append((f"скорость по выкупу {ln.bf:.0%}: {ln.eff14:.0f} из {ln.sales14} шт за 14 дн; " if ln.bf < 1 else "")
                        + f"запас {ln.have / d:.0f} дн → везём до {config.TARGET_PARTIAL if ln.partial else config.target_days(ln.eff14)} дн"
                        + (" («частично грузим»)" if ln.partial else "") + f": нужно {raw}, везём {q}"
                        + (f" (ровно {'короб' if q == box else f'{q // box} короба по'} {box})" if box else "") if d else f"нужно {raw}, везём {q}")
        if not sizes:
            ln.flags.append("нет кванта коробов")

    # --- итоговая проверка: не больше свободного остатка ---
    for key, group in groups.items():
        ff = ms_by[key]
        before = {id(ln): ln.qty for ln in group}
        _fit(group, ff["free"], cl_sales)
        for ln in group:   # урезали под наш склад до меньше минимума — не везём (ровно короб 20 шт по расчёту — везём)
            cut = ln.qty < before[id(ln)] or sum(x.need for x in group) > ff["free"]
            if not ln.manual and cut and 0 < ln.qty < config.MIN_SHIP:
                ln.flags.insert(0, f"не везём: на нашем складе осталось {ln.qty} шт для этого кластера — "
                                   f"меньше минимума {config.MIN_SHIP} шт")
                ln.qty, ln.boxes = 0, []
        ff["used"] = sum(ln.qty for ln in group)
        total = sum(ln.need for ln in group)
        if total > ff["free"]:
            for ln in group:
                ln.flags.insert(0, f"не хватает ФФ: потребность всех кластеров {total} шт больше свободного на ФФ "
                                   f"{ff['free']} шт — сюда {ln.qty} из {ln.need}; очередь: СПб и Москва, Ростов, Казань, "
                                   "дальше по продажам кластера")
        for ln in group:
            d = ln.eff14 / config.SALES_DAYS if ln.sales14 else 0
            after = (ln.have + ln.qty) / d if d else None
            before = ln.have / d if d else None
            if ln.qty and before is not None and before >= config.TRIGGER_DAYS and not ln.warn:
                # Москва: хит с запасом 30–37 дн — можно и подождать до следующей отгрузки (пн/чт), решает владелец
                ln.warn = "ждать"
                ln.flags.append(f"запас {before:.0f} дн (30–{config.MOSCOW_TRIGGER_HIT}): хит в Москве — "
                                "можно везти сейчас или в следующую отгрузку (пн/чт), решите")
            if ln.qty and after and after > config.SLOW_MAX_DAYS:
                small = min((sz for _, sz in _sizes(norm(ln.offer), ln.root, snap.boxes)), default=None)
                if not ln.warn:
                    ln.warn = "короб"
                ln.flags.append(f"после поставки запас {after:.0f} дн (больше {config.SLOW_MAX_DAYS})"
                                + (f"; самый маленький короб {small} шт" if small else "")
                                + " — решите: неполный короб, не везти или закроет соседний кластер")

    # --- «Заказать»: потребность кластеров больше, чем можем отгрузить с нашего склада ---
    shipped = defaultdict(int)
    for (offer, _d), ln in lines.items():
        shipped[offer] += ln.qty
    profits = {norm(k): v for k, v in (getattr(snap, "profit", None) or {}).items()}
    cl_of_offer = defaultdict(set)
    for (o, cl) in sorted(set(s14c) | set(stock_c), key=str):
        if cl:
            cl_of_offer[o].add(cl)
    for offer, e in list(eligible.items()) + list(no_stock_k.items()):
        need = have = 0
        speed = 0.0
        for cl in cl_of_offer[offer]:
            o = offer
            sp = s14c.get((o, cl), 0) / config.SALES_DAYS * e["k"]
            st = stock_c.get((o, cl), zero)
            need += config.need(s14c.get((o, cl), 0) * e.get("bf", 1.0), st["available"] + st["transit"], e["k"], cl,
                                    partial=e["u"].get("status") == "частично грузим")
            have += st["available"] + st["transit"]
            speed += sp
        key = _ms_key(offer, snap.unit, ms_by)
        free = ms_by[key]["free"] if key else 0
        # заказываем только то, чего нет у нас вообще: остаток, который не влез в целые короба, — не заказ
        short = need - max(shipped.get(offer, 0), free)
        if short <= 0 or not speed:
            continue
        pr = profits.get(offer) or {}
        item = {"offer": e["name"], "root": e["u"]["root"], "profit": pr.get("per_unit"),
                "speed": speed, "ozon": have, "days": have / speed, "need": need,
                "free": free, "ship": shipped.get(offer, 0), "order": short}
        if item["profit"] is None or item["profit"] <= 0:
            item["why"] = "прибыль не посчитать: нет продаж или себестоимости" if item["profit"] is None \
                else "минусовой артикул"
            plan.order_skipped.append(item)
        else:
            plan.order.append(item)
    plan.order.sort(key=lambda x: (x["days"], -x["speed"]))

    plan.small = [{"offer": ln.offer, "warehouse": ln.warehouse, "need": ln.need, "speed": ln.speed,
                   "box": min((sz for _, sz in _sizes(norm(ln.offer), ln.root, snap.boxes)), default=None),
                   "priority": ln.priority,
                   "why": next((f for f in ln.flags if f.startswith("не везём:")), None)
                   or "не хватило нашего склада" if any(x["root"] == ms_by[eligible[norm(ln.offer)]["key"]]["article"]
                                                        for x in plan.purchase) and not any(f.startswith("не везём:") for f in ln.flags)
                   else next((f for f in ln.flags if f.startswith("не везём:")), None) if any(f.startswith("не везём:") for f in ln.flags)
                   else "не хватило нашего склада" if any(x["root"] == ms_by[eligible[norm(ln.offer)]["key"]]["article"]
                                                             for x in plan.purchase) else "меньше полного короба"}
                  for ln in lines.values() if ln.need > 0 and ln.qty == 0 and not ln.manual]
    plan.lines = sorted((ln for ln in lines.values() if ln.qty > 0 or ln.manual),
                        key=lambda x: (_prio_rank(x.priority) * -1, x.warehouse, -x.qty))

    # --- кластеры, где нашего товара нет ни на одном складе ---
    # 1) кластер целиком пуст: ни одного нашего товара ни на одном складе кластера;
    # 2) хит (от 1 шт/день по Ozon) отсутствует во всём кластере, где у магазина заметная доля отгрузок,
    #    и в план туда не попал.
    have_cl = defaultdict(int)
    cl_total = defaultdict(int)
    for (offer, wh), st in stock.items():
        cl = cluster_of.get(wh, "")
        have_cl[(offer, cl)] += st["available"] + st["transit"]
        cl_total[cl] += st["available"] + st["transit"]
    clusters = sorted(set(cluster_of.values()) - {""})
    plan.empty_whole = [{"cluster": cl, "warehouse": top_wh.get(cl, "")} for cl in clusters if not cl_total.get(cl)]
    planned_cl = {(o, ln.cluster) for (o, _w), ln in lines.items() if ln.qty > 0}
    shop30 = sum(wh_sales30.values()) or 1
    cl_share = {cl: sum(q for q, _ in items) / shop30 for cl, items in by_cluster.items()}
    for offer, e in eligible.items():
        tot14 = round(sum(v for (o, _), v in s14.items() if o == offer))
        if tot14 / config.SALES_DAYS < config.HIT_MIN_PER_DAY or ms_by[e["key"]]["free"] <= 0:
            continue
        for cl in clusters:
            if cl_share.get(cl, 0) < config.EMPTY_CLUSTER_MIN_SHARE or not cl_total.get(cl):
                continue
            if have_cl.get((offer, cl), 0) == 0 and (offer, cl) not in planned_cl:
                plan.empty_clusters.append({"cluster": cl, "offer": e["name"], "sales14": tot14,
                                            "warehouse": top_wh[cl], "ff_free": ms_by[e["key"]]["free"]})

    # --- FBO + FBS: сигнал «снять FBS» ---
    for sku, rec in snap.product_stocks.items():
        if rec["fbo"] > 0 and rec["fbs"] > 0:
            plan.fbo_fbs.append({"offer": rec["offer_id"], "fbo": rec["fbo"], "fbs": rec["fbs"]})

    # --- дубли одной модели, обе карточки грузим ---
    roots = defaultdict(set)
    for offer, e in eligible.items():
        roots[e["u"]["root"]].add(e["name"])
    plan.duplicates = [{"root": r, "offers": sorted(o)} for r, o in roots.items() if len(o) > 1]
    # --- «Решить / стоп» по кластерам: сколько было бы нужно, если везти (серые строки под итогом) ---
    for x in plan.decide:
        o = norm(x["offer"])
        for (oo, cl), v in s14c.items():
            if oo != o or not cl:
                continue
            st = stock_c.get((o, cl), zero)
            need = config.need(v, st["available"] + st["transit"], 1, cl)
            if need > 0:
                plan.stopped.setdefault(cl, []).append({"offer": x["offer"], "need": need, "reason": x["reason"]})
    for items in plan.stopped.values():
        items.sort(key=lambda x: -x["need"])
    return plan


def directions_of(plan, cluster_of):
    """{направление: кластер}: направление — кластер целиком."""
    return {cl: cl for cl in set(cluster_of.values()) if cl}


_PRIO = {"обычно": 0, "наверх": 1, "срочно": 2}


def _prio_rank(p):
    return _PRIO.get(p, 0)


def _ms_key(offer, unit, ms_by):
    """Ключ «Моего склада» для артикула Ozon: сам артикул, корень из таблицы или корень без NEW."""
    o = norm(offer)
    for k in (o, (unit.get(o) or {}).get("root"), root_of(o)):
        if k and k in ms_by:
            return k
    return None


def _sizes(offer, root, boxes):
    """Кванты по артикулу, а если его строки нет — по корню: OZ378VBLUENEW берёт строку OZ378VBLUE или OZ378VBLUENEW2."""
    if boxes.get(norm(offer)):
        return boxes[norm(offer)]
    for r in dict.fromkeys((root_of(offer), root_of(root))):  # сначала корень самого артикула
        if boxes.get(r):
            return boxes[r]
        for k, v in boxes.items():
            if v and root_of(k) == r:
                return v
    return []


def _order(ln, cl_sales):
    """Ключ очереди кластера: сначала СПб и Москва, затем Ростов, Казань, затем остальные по продажам магазина."""
    return (config.cluster_tier(ln.cluster), -cl_sales.get(ln.cluster, 0))


def _level(group, free, cl_sales):
    """Нашего товара не хватает на все кластеры (решение владельца 02.11.2026).

    Раздаём полную потребность по очереди кластеров: СПб и Москва, затем Ростов, затем Казань, затем остальные
    по убыванию продаж кластера. Если на СПб и Москву вместе не хватает — делим между ними пропорционально потребности.
    """
    for ln in group:
        ln.qty = 0
        ln.main = config.cluster_tier(ln.cluster) == 0
    rest = free
    tiers = defaultdict(list)
    for ln in group:
        tiers[config.cluster_tier(ln.cluster) if ln.main else (config.cluster_tier(ln.cluster), ln.cluster)].append(ln)
    for key in sorted(tiers, key=lambda k: _order(tiers[k][0], cl_sales)):
        lns = tiers[key]
        need = sum(ln.need for ln in lns)
        if need <= rest:
            for ln in lns:
                ln.qty = ln.need
            rest -= need
            continue
        for ln in lns:
            ln.qty = int(rest * ln.need / need)
        left = rest - sum(ln.qty for ln in lns)
        for ln in sorted(lns, key=lambda x: -x.need)[:left]:
            ln.qty += 1
        break


def _fit(group, free, cl_sales):
    """Если после округления до коробов вышли за свободный остаток — снимаем короба с самых обеспеченных складов."""
    while sum(ln.qty for ln in group) > free:
        cand = [ln for ln in group if ln.boxes]
        if not cand:
            over = sum(ln.qty for ln in group) - free
            # снимаем с кластера, который в очереди последний (СПб и Москва — последними)
            ln = max((x for x in group if x.qty), key=lambda x: (_order(x, cl_sales), x.qty))
            ln.qty = max(0, ln.qty - over)
            continue
        # снимаем с кластера, который в очереди последний (СПб и Москва — последними), среди равных — с самого обеспеченного
        ln = max(cand, key=lambda x: (_order(x, cl_sales), (x.have + x.qty) / x.speed if x.speed else 1e9))
        name, s, k = min(ln.boxes, key=lambda b: b[1])
        ln.boxes = [(n, sz, c - 1 if (n, sz) == (name, s) else c) for n, sz, c in ln.boxes]
        ln.boxes = [b for b in ln.boxes if b[2] > 0]
        ln.qty = sum(sz * c for _, sz, c in ln.boxes)
