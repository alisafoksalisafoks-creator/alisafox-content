"""Расчёты и тексты бота цен: соинвест, подсказка по бустингу, прогноз ЧП, ступени скидки, дашборд артикула.

Здесь нет сети и Telegram — только чистые функции, их проверяют тесты.
"""
import html
import re

USN = 0.07  # УСН «доходы минус расходы»: 7% с изменения прибыли
MAX_STEPS = 4  # ограничение Ozon на число уровней скидки от количества


def rub(v):
    if v is None:
        return "—"
    return f"{round(v):,}".replace(",", " ") + " ₽"


def pct(v, digits=1):
    if v is None:
        return "—"
    return f"{v:.{digits}f}".rstrip("0").rstrip(".").replace(".", ",") + "%"


def soinvest(seller_price, site_price):
    """Соинвест Ozon, % ≈ (наша цена с учётом наших акций − цена на сайте) / наша цена × 100."""
    if not seller_price or not site_price:
        return None
    return round((seller_price - site_price) / seller_price * 100, 1)


def trend(today, yesterday, lower_is_better=False, unit="", ref="вчера"):
    """«🟢 ▲ +3 к вчера» / «🔴 ▼ −2 п.п. к вчера» / «= как вчера»; для места в выдаче меньше — лучше."""
    if today is None or yesterday is None:
        return f"нет данных за {ref}"
    d = round(today - yesterday, 1)
    if d == 0:
        return f"= как {ref}"
    good = (d < 0) if lower_is_better else (d > 0)
    return f"{'🟢' if good else '🔴'} {'▲ +' if d > 0 else '▼ −'}{_num(abs(d))}{' п.п.' if unit == '%' else ''} к {ref}"


def _num(v):
    return f"{v:g}".replace(".", ",")


def profit_forecast(profit, old, new, commission_pct, drr_pct):
    """ЧП на 1 шт при новой цене ≈ ЧП + Δцены × (1 − комиссия − общий ДРР) × (1 − УСН). Прогноз, не факт."""
    if profit is None or not old or not new:
        return None
    share = 1 - (commission_pct or 0) / 100 - (drr_pct or 0) / 100
    return profit + (new - old) * share * (1 - USN)


def drr_forecast(drr_pct, old, new):
    """ДРР при новой цене, если расходы на рекламу на 1 шт останутся прежними."""
    if drr_pct is None or not old or not new:
        return None
    return drr_pct * old / new


def boost_hint(price, el):
    """Строки про «Эластичный бустинг»: сколько снизить до входа в акцию и до максимального бустинга."""
    if not el:
        return ["🎯 Бустинг: товара нет в акциях «Эластичный бустинг»"]
    if el.get("in_action"):
        b = f", бустинг {pct(el['boost'])}" if el.get("boost") is not None else ""
        lines = [f"✅ В акции «{html.escape(el['action'])}»{b}"]
    else:
        lines = [f"🎯 Акция «{html.escape(el['action'])}» (пока не участвуете):"]
    for title, limit in (("   вход (мин. бустинг)", el.get("min_el")),
                         ("   макс. бустинг", el.get("max_el"))):
        if not limit:
            continue
        if price and price > limit:
            lines.append(f"{title}: ≤ {rub(limit)} → снизьте на {rub(price - limit)}")
        else:
            lines.append(f"{title}: ≤ {rub(limit)} ✅ уже проходите")
    return lines


def check_text(p, acts):
    """Ответ /check: в каких акциях Ozon видит товар и какую вилку бустинга и цену на сайте отдаёт API."""
    rows = acts.get("seen", {}).get(p["product_id"], [])
    lines = [f"🔍 <b>{html.escape(p['offer_id'])}</b> в акциях Ozon (по API, сейчас)", ""]
    for r in rows:
        lines.append(f"• «{html.escape(r['action'])}» — {'участвует' if r['in_action'] else 'можно добавить'}: "
                     f"мин. бустинг {rub(r['min_el'])}, макс. {rub(r['max_el'])}, на сайте {rub(r['site'])}")
    if not rows:
        lines.append("Ozon не вернул этот товар ни в одной акции: ни среди участников, ни среди кандидатов. "
                     "Значит, по API вилки бустинга и цены на сайте для него сейчас нет.")
    el = acts.get("elastic", {}).get(p["product_id"])
    lines += ["", f"В «Ценах» беру вилку из «{html.escape(el['action'])}»: ≤ {rub(el['min_el'])} / ≤ {rub(el['max_el'])}"
              if el else "Вилки бустинга по API нет."]
    if acts.get("errors"):
        lines += ["", "⚠️ Не ответили акции:"] + [f"• {html.escape(e[:150])}" for e in acts["errors"][:10]]
    return "\n".join(lines)


def fmt_stock(s):
    if not s:
        return "🏬 Остаток: нет данных Ozon"
    return f"🏬 Остаток: FBO <b>{s['fbo']} шт</b> · FBS <b>{s['fbs']} шт</b>"


def fmt_steps(steps):
    if not steps:
        return "нет"
    return ", ".join(f"от {q} шт −{d}%" for q, d in steps)


def parse_steps(text):
    """«2-5, 3-10» / «от 2 шт 5%; от 3 шт 10%» / «2 5\\n3 10» → [(2, 5), (3, 10)]. Ошибка — ValueError с текстом."""
    pairs = re.findall(r"(\d+)\s*(?:шт\.?)?\s*[-–—:/ ]\s*[−-]?\s*(\d+)\s*%?", text.replace("от", " "))
    if not pairs:
        raise ValueError("Не поняла формат. Пример: 2-5, 3-10 (от 2 шт скидка 5%, от 3 шт — 10%).")
    return validate_steps([(int(q), int(d)) for q, d in pairs])


def validate_steps(steps):
    """Проверка уровней по правилам Ozon и здравому смыслу. Возвращает отсортированные уровни или ValueError."""
    steps = sorted((int(q), int(d)) for q, d in steps)
    if len(steps) > MAX_STEPS:
        raise ValueError(f"Ozon разрешает не больше {MAX_STEPS} уровней.")
    for q, d in steps:
        if q < 2:
            raise ValueError("Количество в уровне — от 2 шт.")
        if not 1 <= d <= 90:
            raise ValueError("Скидка — от 1 до 90%.")
    qs, ds = [q for q, _ in steps], [d for _, d in steps]
    if len(set(qs)) != len(qs):
        raise ValueError("Два уровня с одинаковым количеством — поправьте один.")
    if ds != sorted(ds) or len(set(ds)) != len(ds):
        raise ValueError("Скидка должна расти от уровня к уровню: у большего количества — больше %.")
    return steps


def editor_text(title, orig, draft, note=""):
    """Экран редактора уровней: что сейчас в Ozon и что будет после сохранения. title уже экранирован."""
    lines = [f"📦 Скидка от количества: <b>{title}</b>",
             f"Сейчас в Ozon: {'у товаров разные уровни' if note else fmt_steps(orig)}"]
    if note:
        lines.append(note)
    lines += ["", "<b>Будет после сохранения:</b>"]
    if draft:
        lines += [f"{i}) от {q} шт −{d}%" for i, (q, d) in enumerate(draft, 1)]
    else:
        lines.append("без скидки от количества")
    try:
        validate_steps(draft)
    except ValueError as e:
        lines += ["", f"⚠️ {e}"]
    if sorted(map(tuple, draft)) == sorted(map(tuple, orig)):
        lines += ["", "Изменений пока нет."]
    lines += ["", "±1% меняет скидку уровня, ✏️ — количество и скидку, 🗑 — убрать уровень."]
    return "\n".join(lines)


def fmt_history(e):
    """Строка истории: «27.10 16:40 · Алиса · OZ214: цена 365 ₽ → 300 ₽ (−65 ₽, −17,8%)»."""
    head = f"{e['ts']} · {html.escape(e['who'])} · <b>{html.escape(e['offer'])}</b>"
    if e["kind"] == "price":
        old, new = e.get("old"), e["new"]
        delta = f" ({'+' if new > old else '−'}{rub(abs(new - old))}, {'+' if new > old else '−'}" \
                f"{pct(abs(new - old) / old * 100)})" if old else ""
        return f"{head}: цена {rub(old)} → {rub(new)}{delta}"
    return f"{head}: скидка от кол-ва {fmt_steps(e.get('old'))} → {fmt_steps(e['new'])}"


def parse_price(text):
    t = (text or "").replace(" ", "").replace("₽", "").replace("р", "").replace(",", ".")
    try:
        v = float(t)
    except ValueError:
        raise ValueError("Пришлите цену числом, например 1290.")
    if v <= 0:
        raise ValueError("Цена должна быть больше нуля.")
    return round(v)


def dashboard(p, d):
    """Экран артикула. p — товар из каталога, d — собранные данные (может не хватать любых полей)."""
    price = d.get("price")
    lines = [f"📦 <b>{html.escape(p['name'][:90])}</b>",
             f"Арт. {html.escape(p['offer_id'])} · SKU {p['sku']}", "",
             fmt_stock(d.get("stock")),
             f"💰 Цена: <b>{rub(price)}</b>",
             f"   с вашими акциями: {rub(d.get('seller_price'))}, на сайте: {rub(d.get('site_price'))}"]
    lines += boost_hint(price, d.get("elastic"))
    if d.get("actions_errors"):
        lines.append(f"   ⚠️ Ozon не отдал данные {d['actions_errors']} акций — подробно: /check {html.escape(p['offer_id'])}")
    lines.append("")
    ts = d.get("ts")
    if ts:
        lines.append(f"📈 ЧП на 1 шт (ТС, {ts['period']}): <b>{rub(ts.get('profit'))}</b>")
        lines.append(f"📣 ДРР общий {pct(ts.get('drr'))} | РК ДРР {pct(ts.get('drr_rk'))}")
    else:
        lines.append("📈 ЧП и ДРР: нет данных из ТС" + (f" ({d['ts_error']})" if d.get("ts_error") else ""))
    si = d.get("soinvest")
    lines.append(f"🤝 Соинвест сегодня: ≈{pct(si)} · {trend(si, d.get('soinvest_y'), unit='%')}" if si is not None
                 else "🤝 Соинвест: нет цены на сайте (товара нет ни в одной акции Ozon)")
    st = d.get("stairway")
    lines.append("📦 Скидка от количества: " + (fmt_steps(st["steps"]) if st and st["enabled"] else "нет"))
    pos = d.get("position")
    lines.append(f"🔎 Место в выдаче{' (' + d['position_day'] + ')' if d.get('position_day') else ''}: "
                 f"{_num(round(pos, 1))} · {trend(pos, d.get('position_y'), lower_is_better=True,
                                                    ref='позавчера' if d.get('position_day') else 'вчера')}"
                 if pos else "🔎 Место в выдаче: нет данных за сегодня")
    return "\n".join(lines)


def confirm_price(p, d, new):
    """Текст «было → стало, точно?» с прогнозом ЧП и ДРР."""
    old = d.get("price")
    lines = [f"Меняем цену <b>{html.escape(p['offer_id'])}</b>?", "",
             f"Было: {rub(old)} → станет: <b>{rub(new)}</b>"
             + (f" ({'+' if new > old else '−'}{rub(abs(new - old))}, {pct(abs(new - old) / old * 100)})" if old else "")]
    ts = d.get("ts") or {}
    f = profit_forecast(ts.get("profit"), old, new, d.get("commission"), ts.get("drr"))
    if f is not None:
        lines.append(f"ЧП на 1 шт: {rub(ts['profit'])} → ≈ {rub(f)} (прогноз)")
    dr = drr_forecast(ts.get("drr"), old, new)
    if dr is not None:
        lines.append(f"ДРР общий: {pct(ts['drr'])} → ≈ {pct(dr)}, если реклама на 1 шт не изменится")
    lines += [""] + boost_hint(new, d.get("elastic"))
    warn = []
    if old and abs(new - old) / old > 0.2:
        warn.append("⚠️ Изменение больше 20%. Проверьте, нет ли лишнего нуля.")
    if d.get("old_price") and new >= d["old_price"]:
        warn.append("⚠️ Новая цена не ниже зачёркнутой — Ozon может не принять.")
    if d.get("min_price") and new < d["min_price"]:
        warn.append(f"⚠️ Ниже вашей мин. цены для автоакций ({rub(d['min_price'])}).")
    if f is not None and f < 0:
        warn.append("🔴 По прогнозу товар уйдёт в минус.")
    return "\n".join(lines + ([""] + warn if warn else []))
