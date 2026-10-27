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


def soinvest(seller_price, customer_price):
    """Соинвест Ozon, % = (наша цена с учётом наших акций − цена покупателя) / наша цена × 100."""
    if not seller_price or not customer_price:
        return None
    return round((seller_price - customer_price) / seller_price * 100, 1)


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
    lines = []
    if el.get("in_action"):
        b = f", бустинг {pct(el['boost'])}" if el.get("boost") is not None else ""
        lines.append(f"✅ В акции «{html.escape(el['action'])}»{b}")
    for title, limit in (("🎯 Вход в акцию (мин. бустинг)", el.get("min_el")),
                         ("🚀 Макс. бустинг", el.get("max_el"))):
        if not limit:
            continue
        if price and price > limit:
            lines.append(f"{title}: ≤ {rub(limit)} → снизьте на {rub(price - limit)}")
        else:
            lines.append(f"{title}: ≤ {rub(limit)} ✅ уже проходите")
    return lines


def fmt_steps(steps):
    if not steps:
        return "нет"
    return ", ".join(f"от {q} шт −{d}%" for q, d in steps)


def parse_steps(text):
    """«2-5, 3-10» / «от 2 шт 5%; от 3 шт 10%» / «2 5\\n3 10» → [(2, 5), (3, 10)]. Ошибка — ValueError с текстом."""
    pairs = re.findall(r"(\d+)\s*(?:шт\.?)?\s*[-–—:/ ]\s*[−-]?\s*(\d+)\s*%?", text.replace("от", " "))
    if not pairs:
        raise ValueError("Не поняла формат. Пример: 2-5, 3-10 (от 2 шт скидка 5%, от 3 шт — 10%).")
    steps = [(int(q), int(d)) for q, d in pairs]
    if len(steps) > MAX_STEPS:
        raise ValueError(f"Ozon разрешает не больше {MAX_STEPS} уровней.")
    for q, d in steps:
        if q < 2:
            raise ValueError("Количество в уровне — от 2 шт.")
        if not 1 <= d <= 90:
            raise ValueError("Скидка — от 1 до 90%.")
    qs, ds = [q for q, _ in steps], [d for _, d in steps]
    if qs != sorted(set(qs)):
        raise ValueError("Количество в уровнях должно расти: 2, 3, 5…")
    if ds != sorted(ds) or len(set(ds)) != len(ds):
        raise ValueError("Скидка должна расти от уровня к уровню: 5%, 10%…")
    return steps


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
             f"💰 Цена: <b>{rub(price)}</b>" + (f" (зачёркнутая {rub(d['old_price'])})" if d.get("old_price") else ""),
             f"   с вашими акциями: {rub(d.get('seller_price'))}, у покупателя: {rub(d.get('customer_price'))}"]
    if d.get("min_price"):
        lines.append(f"   мин. цена для автоакций: {rub(d['min_price'])}")
    lines += boost_hint(price, d.get("elastic"))
    lines.append("")
    ts = d.get("ts")
    if ts:
        lines.append(f"📈 ЧП на 1 шт (ТС, {ts['period']}): <b>{rub(ts.get('profit'))}</b>")
        lines.append(f"📣 ДРР общий {pct(ts.get('drr'))} | РК ДРР {pct(ts.get('drr_rk'))}")
    else:
        lines.append("📈 ЧП и ДРР: нет данных из ТС" + (f" ({d['ts_error']})" if d.get("ts_error") else ""))
    si = d.get("soinvest")
    lines.append(f"🤝 Соинвест сегодня: {pct(si)} · {trend(si, d.get('soinvest_y'), unit='%')}" if si is not None
                 else "🤝 Соинвест: нет цены покупателя (нужен Premium Pro)")
    if si is not None and d.get("ozon_percent") is not None and abs(d["ozon_percent"] - si) >= 1:
        lines.append(f"   по данным Ozon: {pct(d['ozon_percent'])}")
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
