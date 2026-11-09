"""Правила расчёта поставок FBO. Цифры — решения владельца из ТЗ (24.10.2026)."""
import math
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("SUPPLY_BOT_DATA", Path(__file__).parent / "data"))

SALES_DAYS = 14          # спрос = отгрузки FBO за 14 полных дней ÷ 14
REF_DAYS = 30            # продажи за 30 дней — справочно и для выбора обязательных складов
# Логика владельца (02.11.2026, выведена из её ручных расчётов по 7 кластерам, заменяет формулу «×2,5»):
# запас на Ozon от 30 дней — не везём; меньше — довозим до 40 дн (хит, от 40 шт за 14 дн), 45 дн (20–39 шт), 50 дн (меньше 20).
TRIGGER_DAYS = 30        # запас на Ozon (остаток + в пути) от 30 дней по продажам 14 дн — не везём
# Москва (05.11.2026): отгрузка 2 раза в неделю (пн и чт). Хиты везём до запаса 37 дн, остальное — при запасе меньше 25 дн
MOSCOW_CLUSTER = "Москва, МО и Дальние регионы"
MOSCOW_TRIGGER_HIT, MOSCOW_TRIGGER_OTHER = 37, 25
# Москва и СПб (09.11.2026): обе ездят пн/чт. Не хиты при запасе от 27 дн не везём; довозим до 35 дн (хиты в Москве — до 40);
# короб больше потребности не берём — круглое число; мало нужно при запасе от 25 дн — «довезти в чт».
SPB_CLUSTER = "Санкт-Петербург и СЗО"
FAST_CLUSTERS = {MOSCOW_CLUSTER, SPB_CLUSTER}
FAST_TRIGGER_OTHER = 27
TARGET_FAST = 35
FAST_LATER_DAYS = 25
HIT_SALES14 = 40         # хит — от 40 шт за 14 дней в кластере
TARGET_HIT, TARGET_MID, TARGET_SLOW = 40, 45, 50
MID_SALES14 = 20
MIN_SHIP = 30            # меньше 30 шт в кластер не возим
# запас от 30 дн и не хватает меньше партии — не везём, довезём в следующую отгрузку (09.11.2026)
MOSCOW_NEXT_GAP, NEXT_SHIP_GAP = 4, 7   # дней до следующей отгрузки: Москва и СПб пн/чт, остальные раз в неделю
BOX_NEAR = 0.25          # короб из квантов в пределах 25% от нужного — везём ровно короб
SLOW_MAX_DAYS = 60       # после поставки запаса больше 60 дней — оранжевая подсветка, решает владелец
# склады Ozon, которые не работают: остаток там не считаем, поставки туда — потерянные, туда не везём
ABSENT_MIN14 = 10        # «товара нет в кластере»: везём, только если ждём от 10 шт за 14 дн (решение владельца 05.11.2026)
SUPPLY_DROP_OFF_ID = 1020001649204000   # СПБ_КОЛПИНО_РФЦ_КРОССДОКИНГ — наш пункт отгрузки (по истории заявок)
CLOSED_WAREHOUSES = {"АДЫГЕЙСК_РФЦ": "05.11.2026"}   # разбомбили, в кластер Краснодар — только Новороссийск
STUCK_DAYS = 21          # поставка не принята Ozon больше 21 дня после слота — считаем потерянной, в «в пути» не берём (05.11.2026)
# дальние кластеры едут дольше — ждём дольше, прежде чем считать поставку потерянной (решение владельца 05.11.2026; дни — допущение)
STUCK_DAYS_BY = {"Екатеринбург": 30, "Красноярск": 30, "Дальний Восток": 45}


def stuck_days(cluster):
    for key, days in STUCK_DAYS_BY.items():
        if key.lower() in (cluster or "").lower():
            return days
    return STUCK_DAYS
TARGET_PARTIAL = 35      # статус «Частично грузим» — довозим до 35 дн, остальное в следующую отгрузку (05.11.2026)
SKIP_AFTER_DAYS = 80     # после поставки запаса было бы больше 80 дней — не везём (решение владельца 02.11.2026)
ORANGE_WAIT_DAYS = 25    # оранжевая строка при запасе от 25 дн — не везём, ждём неделю (02.11.2026)
TARGET_DAYS = TARGET_MID  # для подписей и проверки ручных правок
URGENT_HIT_DAYS = 14     # ходовой товар с запасом меньше 14 дней — наверх
HIT_MIN_PER_DAY = 1.0    # «ходовой» — от 1 шт в день на складе
ROUND_UP_EXTRA_DAYS = 3  # короб вверх, если запас не больше горизонта + 3 дней
WHOLESALE_MIN = 10       # заказ от 10 шт одного артикула — опт: в спрос не идёт, подсвечиваем
SPIKE_RATIO = 2          # день больше 2 × обычный (медиана 14 дней) — всплеск: считаем по факту, подсвечиваем (05.11.2026)
SPIKE_MIN_DAY = 3        # всплески ищем, только если в обычный день от 3 шт — иначе это шум
# Не хватает нашего товара на все кластеры (решение владельца 02.11.2026, заменяет 14/7 дней от 26.10):
# сначала полная потребность СПб и Москвы, затем Ростов, затем Казань, затем остальные — по продажам кластера.
CLUSTER_ORDER = (("Санкт-Петербург", "Москва"), ("Ростов",), ("Казань",))


def cluster_tier(cluster):
    """Очередь кластера при нехватке нашего склада: 0 — СПб и Москва, 1 — Ростов, 2 — Казань, 3 — остальные."""
    for i, keys in enumerate(CLUSTER_ORDER):
        if any(k.lower() in (cluster or "").lower() for k in keys):
            return i
    return len(CLUSTER_ORDER)
PARTIAL_MIN = 5          # неполный короб — только если потребность от 5 шт
URGENT_MAX_DAYS = 60     # срочно и потребность меньше короба: 1 короб, если запас не больше L + 60 дней

PAID_SOON_DAYS = 28      # платное хранение в ближайшие 28 дней — не отгружать
BUYOUT_MIN = 0.85        # выкуп ниже 85% — «с товаром что-то не так»
RATING_MIN = 4.6         # рейтинг ниже 4,6 — не считаем, список «Решить»
EMPTY_CLUSTER_MIN_SHARE = 0.02  # «хита нет в кластере» — только где ≥ 2% наших отгрузок FBO
SEASON_LOW = 0.8         # коэффициент сезона ниже 0,8 — «не сезон»
SEASON_DROP = 0.2        # следующий месяц ниже текущего больше чем на 20% — «спад сезона»

# Везём только в кластер целиком, отдельных складов нет — Москва и Питер тоже (решение владельца 25.10.2026).

# Склады за пределами РФ не считаем и не предлагаем (решение владельца 25.10.2026).
FOREIGN_CLUSTERS = {"алматы", "астана", "казахстан", "беларусь", "армения", "узбекистан", "кыргызстан",
                    "киргизия", "грузия", "азербайджан", "таджикистан"}

# Срок L от отгрузки с нашего склада до продажи, дней. Медиана 300 заявок, 13.10.2026.
LEAD_DAYS = {
    "Санкт-Петербург": 3, "Москва": 7, "Уфа": 7,
    "Ростов": 8, "Екатеринбург": 8, "Краснодар": 8, "Пермь": 8, "Тюмень": 8, "Омск": 8, "Тверь": 8,
    "Казань": 9, "Новосибирск": 9, "Воронеж": 9, "Ярославль": 9, "Невинномысск": 9,
    "Красноярск": 11, "Саратов": 12, "Самара": 13, "Дальний Восток": 30,
}
LEAD_DEFAULT = 12

LOAD = {"грузим", "частично грузим"}
NO_LOAD = "не грузим"
NEW = "новинки"

SHIP_WEEKDAYS = (0, 3)  # пн и чт (решение владельца 05.11.2026)

# Таблицы владельца — только чтение.
UNIT_SHEET_ID = os.environ.get("UNIT_SHEET_ID", "1PjwfdbTI47_gQNDelUBkQ8yhsd4i05Te1JjEDoYxA0U")
UNIT_SHEET = "Юнит экономика Алиса"
PAID_SHEET = "СКОРО ПЛАТНО"
BOX_SHEET_ID = os.environ.get("BOX_SHEET_ID", "1AJPOlUacb6ZtHNQTwjML6Ty0qOlv9GJMwVCo54qOQ0Q")
BOX_SHEET = "РАЗМЕЩЕНИЕ ПО КОРОБАМ"
BOX_NAMES = ("61×40×33", "43×29×35", "40×30×30", "31×23×19,5", "штатный")

MS_STORE = os.environ.get("MS_STORE", "1. Основной склад")


def target_days(sales14, cluster=None):
    """Сколько дней запаса держать после поставки: хиты 40, средние 45, медленные 50 (решение владельца 02.11.2026).
    Москва и СПб — 35 дн; хиты в Москве — 40 (09.11.2026: так ближе всего к её ручной поставке в Москву)."""
    if cluster == MOSCOW_CLUSTER and sales14 >= HIT_SALES14:
        return TARGET_HIT
    if cluster in FAST_CLUSTERS:
        return TARGET_FAST
    if sales14 >= HIT_SALES14:
        return TARGET_HIT
    return TARGET_MID if sales14 >= MID_SALES14 else TARGET_SLOW


def trigger_days(sales14, cluster=None):
    if cluster == MOSCOW_CLUSTER:
        return MOSCOW_TRIGGER_HIT if sales14 >= HIT_SALES14 else FAST_TRIGGER_OTHER
    if cluster == SPB_CLUSTER:
        return TRIGGER_DAYS if sales14 >= HIT_SALES14 else FAST_TRIGGER_OTHER
    return TRIGGER_DAYS


def next_gap(cluster):
    return MOSCOW_NEXT_GAP if cluster in FAST_CLUSTERS else NEXT_SHIP_GAP


def need(sales14, have, k=None, cluster=None, partial=False):
    """Потребность кластера по логике владельца: запас от 30 дн — 0; иначе цель (40/45/50 дн) × скорость − (остаток + в пути).

    Скорость = продажи 14 дн / 14. Коэффициент сезона в расчёт не входит (k оставлен для совместимости), как в её ручных расчётах."""
    if sales14 <= 0:
        return 0
    speed = sales14 / SALES_DAYS
    if have / speed >= trigger_days(sales14, cluster):
        return 0
    target = TARGET_PARTIAL if partial else target_days(sales14, cluster)
    return max(0, math.ceil(target * speed - have))


BOX_DOWN = 0.35   # короб меньше расчёта — до 35% (05.11.2026)


def round_qty(qty, sizes, no_up=False):
    """no_up (Москва, СПб — 09.11.2026): короб больше потребности не берём, круглое число не выше потребности + 5 шт.

    Округление по логике владельца: короб из квантов в пределах 25% — ровно короб; иначе до 100 шт — до 10,
    больше — до 50; не меньше 30 шт. Возвращает (шт, размер короба или None)."""
    if qty <= 0:
        return 0, None
    # короб чуть меньше расчёта можно смелее (до 35%): отгрузка 2 раза в неделю, довезём (решение владельца 05.11.2026)
    if no_up:
        sizes = [s for s in sizes if s <= qty + 5 or s < MIN_SHIP]
    near = [s for s in sizes if s > 0 and (abs(s - qty) <= max(5, BOX_NEAR * qty)
                                           or (s < qty and qty - s <= BOX_DOWN * qty and s >= 20))]
    if near:
        best = min(near, key=lambda s: (abs(s - qty), -s))
        return best, best
    # несколько одинаковых коробов в пределах 25% — берём самый большой короб (05.11.2026: MD60 81 → 2×50, а не 8×10)
    multi = [(k * s, s) for s in sizes if s > 0 for k in range(2, 11) if abs(k * s - qty) < BOX_NEAR * qty
             and (not no_up or k * s <= qty + 5)]
    if multi:
        q, s = max(multi, key=lambda t: (t[1], -abs(t[0] - qty)))
        return max(q, MIN_SHIP) if q >= MIN_SHIP else q, s
    # мало нужно, а есть короб меньше 30 шт — везём один короб, а не 30 (05.11.2026: OZ522-HB1A — 1 короб 20)
    small = [s for s in sizes if s > 0 and qty <= s < MIN_SHIP]
    if small:
        return min(small), min(small)
    if qty <= 100:
        q = int(round(qty / 10.0) * 10)
    else:
        q = int(round(qty / 50.0) * 50)
        if no_up and q > qty + 5:
            q -= 50
    if no_up and qty <= 100 and q > qty + 5:
        q -= 10
    return max(MIN_SHIP, q), None


def lead_days(cluster):
    for key, days in LEAD_DAYS.items():
        if key.lower() in (cluster or "").lower():
            return days
    return LEAD_DEFAULT


def foreign(cluster):
    return (cluster or "").strip().lower() in FOREIGN_CLUSTERS
