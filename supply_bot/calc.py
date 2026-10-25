"""Расчёт поставки FBO по кластерам Ozon.

Направление поставки — только кластер целиком, без отдельных складов (решение владельца 25.10.2026).
Склад приёмки — подсказка: наш основной склад кластера по отгрузкам за 30 дней.
Потребность кластера = отгрузки FBO из кластера за 14 дн ÷ 14 × коэф. сезона × (L + 30) − (доступно + в пути).
Дальше: статусы и стопы → лимит «Моего склада» с выравниванием дней запаса →
полные моно-короба под каждое направление → пустые кластеры и предупреждения.
"""
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
    taken_at: str = ""


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
    fbo_fbs: list = field(default_factory=list)
    season_notes: list = field(default_factory=list)
    duplicates: list = field(default_factory=list)
    ff: dict = field(default_factory=dict)          # {ключ МС: {article, stock, unshipped, free, used}}
    notes: list = field(default_factory=list)

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


def pick_boxes(qty, sizes, cap):
    """Полные моно-короба под одно направление.

    Большими коробами, остаток — самым маленьким коробом, в который он помещается.
    Вверх — только если итог не больше cap (потолок 30 дней + запас на округление), иначе вниз.
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
    month = today.month

    foreign_wh = {w for w, cl in snap.wh_cluster.items() if config.foreign(cl)} | \
        {r["warehouse"] for r in snap.wh_stocks if config.foreign(r.get("cluster"))}

    # --- спрос по складам ---
    d14, d30 = str(today - timedelta(days=config.SALES_DAYS)), str(today - timedelta(days=config.REF_DAYS))
    s14, s30 = defaultdict(int), defaultdict(int)
    wh_sales30 = defaultdict(int)
    for day, offer, _sku, wh, qty in snap.postings:
        if day >= str(today) or not wh:
            continue
        if wh in foreign_wh:
            continue
        key = (norm(offer), wh)
        if day >= d30:
            s30[key] += qty
            wh_sales30[wh] += qty
        if day >= d14:
            s14[key] += qty

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
        tot14 = sum(v for (o, _), v in s14.items() if o == offer)
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
        if u.get("buyout") is not None and u["buyout"] < config.BUYOUT_MIN and tot14 > 0:
            plan.decide.append({**info, "reason": f"выкуп {u['buyout']:.0%} < 85% — с товаром что-то не так, возможно, пока не везти"})
            continue
        paid = [p for p in snap.paid.get(offer, []) if (p["date"] - today).days <= config.PAID_SOON_DAYS]
        if paid:
            p = min(paid, key=lambda x: x["date"])
            left = (p["date"] - today).days
            when = "уже платное" if left <= 0 else f"платное с {p['date']:%d.%m} (через {left} дн)"
            plan.paid.append({"offer": name, "qty": p["qty"], "date": p["date"], "days": left})
            plan.decide.append({**info, "reason": f"не отгружать: {when}, {p['qty']} шт"})
            continue
        key = _ms_key(offer, snap.unit, ms_by)
        if not key or ms_by[key]["free"] <= 0:
            if tot14:
                plan.no_stock.append({"offer": name, "root": u["root"], "sales14": tot14})
                no_stock_k[offer] = {"u": u, "k": season_k(snap, u.get("category"), month), "name": name}
            continue
        k = season_k(snap, u.get("category"), month)
        k_next = season_k(snap, u.get("category"), month % 12 + 1)
        if k < config.SEASON_LOW or (k and k_next < k * (1 - config.SEASON_DROP)):
            plan.season_notes.append({"offer": name, "category": u.get("category"), "k": k, "k_next": k_next})
        eligible[offer] = {"u": u, "k": k, "key": key, "name": name}

    # --- направления: кластер целиком, в Москве — основные склады ---
    s14c, s30c, stock_c = defaultdict(int), defaultdict(int), {}
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
    top_wh = {cl: max(items)[1] for cl, items in by_cluster.items() if items}
    zero = {"available": 0, "transit": 0}

    lines = {}
    for (offer, cl) in set(s30c) | set(stock_c):
        if offer not in eligible or not cl:
            continue
        e = eligible[offer]
        st = stock_c.get((offer, cl), zero)
        speed = s14c.get((offer, cl), 0) / config.SALES_DAYS * e["k"]
        lead = config.lead_days(cl)
        need = max(0, round(speed * (lead + config.TARGET_DAYS) - st["available"] - st["transit"]))
        lines[(offer, cl)] = Line(
            offer=e["name"], root=e["u"]["root"], warehouse=cl, cluster=cl, ship_to=top_wh.get(cl, ""),
            sales14=s14c.get((offer, cl), 0), sales30=s30c.get((offer, cl), 0), speed=speed,
            available=st["available"], transit=st["transit"], lead=lead, need=need, season=e["k"])

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
    for key, group in groups.items():
        ff = ms_by[key]
        total = sum(ln.need for ln in group)
        if total <= ff["free"]:
            for ln in group:
                ln.qty = ln.need
        else:
            _level(group, ff["free"])
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
        if not sizes:
            ln.flags.append("нет кванта коробов — количество без округления")
            continue
        if ln.manual:
            # ручное количество — до ближайших полных коробов (поровну — вверх)
            down, up = pick_boxes(ln.qty, sizes, ln.qty), pick_boxes(ln.qty, sizes, 10 ** 9)
            boxes = min((up, down), key=lambda bx: abs(sum(s * k for s, k in bx) - ln.qty))
        else:
            # вверх до короба — только если запас после поставки не больше L + 30 + 3 дней
            cap = round(ln.need + ln.speed * config.ROUND_UP_EXTRA_DAYS)
            if ln.priority == "срочно" and ln.qty < min(sz for _, sz in sizes):
                # срочно, а потребность меньше короба: один короб, если запас не уйдёт дальше L + 60 дней
                cap = max(cap, int(ln.speed * (ln.lead + config.URGENT_MAX_DAYS) - ln.have))
            boxes = pick_boxes(ln.qty, sizes, max(cap, ln.qty))
            got = sum(sz * k for sz, k in boxes)
            if got > ln.need + ln.speed * config.ROUND_UP_EXTRA_DAYS and ln.speed:
                ln.flags.append(f"короб вверх: запас после поставки {(ln.have + got) / ln.speed - ln.lead:.0f} дн")
        ln.boxes = label_boxes(boxes, sizes)
        ln.qty = sum(s * k for _, s, k in ln.boxes)

    # --- итоговая проверка: не больше свободного остатка ---
    for key, group in groups.items():
        ff = ms_by[key]
        _fit(group, ff["free"])
        ff["used"] = sum(ln.qty for ln in group)
        for ln in group:
            if ln.manual and ln.qty > ln.need + ln.speed * config.ROUND_UP_EXTRA_DAYS and ln.speed:
                ln.flags.append(f"запас после поставки {(ln.have + ln.qty) / ln.speed - ln.lead:.0f} дн — больше 30")

    # --- «Заказать»: потребность кластеров больше, чем можем отгрузить с нашего склада ---
    shipped = defaultdict(int)
    for (offer, _d), ln in lines.items():
        shipped[offer] += ln.qty
    cl_of_offer = defaultdict(set)
    for (o, cl) in set(s14c) | set(stock_c):
        if cl:
            cl_of_offer[o].add(cl)
    for offer, e in list(eligible.items()) + list(no_stock_k.items()):
        need = have = 0
        speed = 0.0
        for cl in cl_of_offer[offer]:
            o = offer
            sp = s14c.get((o, cl), 0) / config.SALES_DAYS * e["k"]
            st = stock_c.get((o, cl), zero)
            need += max(0, round(sp * (config.lead_days(cl) + config.TARGET_DAYS) - st["available"] - st["transit"]))
            have += st["available"] + st["transit"]
            speed += sp
        short = need - shipped.get(offer, 0)
        if short <= 0 or not speed:
            continue
        key = _ms_key(offer, snap.unit, ms_by)
        item = {"offer": e["name"], "root": e["u"]["root"], "profit": e["u"].get("profit"),
                "speed": speed, "ozon": have, "days": have / speed, "need": need,
                "free": ms_by[key]["free"] if key else 0, "ship": shipped.get(offer, 0), "order": short}
        if item["profit"] is None or item["profit"] <= 0:
            item["why"] = "нет данных о прибыли" if item["profit"] is None else "минусовой артикул"
            plan.order_skipped.append(item)
        else:
            plan.order.append(item)
    plan.order.sort(key=lambda x: (x["days"], -x["speed"]))

    plan.small = [{"offer": ln.offer, "warehouse": ln.warehouse, "need": ln.need, "speed": ln.speed,
                   "box": min((sz for _, sz in _sizes(norm(ln.offer), ln.root, snap.boxes)), default=None),
                   "priority": ln.priority,
                   "why": "не хватило нашего склада" if any(x["root"] == ms_by[eligible[norm(ln.offer)]["key"]]["article"]
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
        tot14 = sum(v for (o, _), v in s14.items() if o == offer)
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
    for k in (norm(offer), norm(root)):
        if boxes.get(k):
            return boxes[k]
    for k, v in boxes.items():
        if v and root_of(k) == norm(root):
            return v
    return []


def _level(group, free):
    """Делим свободный остаток так, чтобы дни запаса по складам выровнялись."""
    def total(t):
        return sum(min(ln.need, max(0, ln.speed * t - ln.have)) for ln in group)
    lo, hi = 0.0, max(ln.lead + config.TARGET_DAYS for ln in group)
    for _ in range(60):
        mid = (lo + hi) / 2
        if total(mid) > free:
            hi = mid
        else:
            lo = mid
    for ln in group:
        ln.qty = int(min(ln.need, max(0, ln.speed * lo - ln.have)))
        if ln.speed == 0:
            ln.qty = 0
    rest = free - sum(ln.qty for ln in group)
    for ln in sorted(group, key=lambda x: (-_prio_rank(x.priority), x.days or 0)):
        add = min(rest, ln.need - ln.qty)
        ln.qty += add
        rest -= add


def _fit(group, free):
    """Если после округления до коробов вышли за свободный остаток — снимаем короба с самых обеспеченных складов."""
    while sum(ln.qty for ln in group) > free:
        cand = [ln for ln in group if ln.boxes]
        if not cand:
            over = sum(ln.qty for ln in group) - free
            ln = max(group, key=lambda x: x.qty)
            ln.qty = max(0, ln.qty - over)
            continue
        ln = max(cand, key=lambda x: ((x.have + x.qty) / x.speed if x.speed else 1e9, -_prio_rank(x.priority)))
        name, s, k = min(ln.boxes, key=lambda b: b[1])
        ln.boxes = [(n, sz, c - 1 if (n, sz) == (name, s) else c) for n, sz, c in ln.boxes]
        ln.boxes = [b for b in ln.boxes if b[2] > 0]
        ln.qty = sum(sz * c for _, sz, c in ln.boxes)
