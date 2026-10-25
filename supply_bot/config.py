"""Правила расчёта поставок FBO. Цифры — решения владельца из ТЗ (24.10.2026)."""
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("SUPPLY_BOT_DATA", Path(__file__).parent / "data"))

SALES_DAYS = 14          # спрос = отгрузки FBO за 14 полных дней ÷ 14
REF_DAYS = 30            # продажи за 30 дней — справочно и для выбора обязательных складов
TARGET_DAYS = 30         # после поставки запас на складе не больше 30 дней
URGENT_HIT_DAYS = 14     # ходовой товар с запасом меньше 14 дней — наверх
HIT_MIN_PER_DAY = 1.0    # «ходовой» — от 1 шт в день на складе
ROUND_UP_EXTRA_DAYS = 3  # короб вверх, если запас после поставки не больше 30 + 3 дней
URGENT_MAX_DAYS = 60     # срочно и потребность меньше короба: 1 короб, если запас не больше L + 60 дней

PAID_SOON_DAYS = 28      # платное хранение в ближайшие 28 дней — не отгружать
BUYOUT_MIN = 0.85        # выкуп ниже 85% — «с товаром что-то не так»
RATING_MIN = 4.6         # рейтинг ниже 4,6 — не считаем, список «Решить»
EMPTY_CLUSTER_MIN_SHARE = 0.02  # «хита нет в кластере» — только где ≥ 2% наших отгрузок FBO
SEASON_LOW = 0.8         # коэффициент сезона ниже 0,8 — «не сезон»
SEASON_DROP = 0.2        # следующий месяц ниже текущего больше чем на 20% — «спад сезона»

# Везём в кластер целиком. Исключение — Москва: рекомендация на топ-5 складов по нашим отгрузкам FBO за 30 дней
# (решение владельца 25.10.2026).
MANDATORY = {"Москва": 5}

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

SHIP_WEEKDAYS = (0, 2, 4)  # пн, ср, пт

# Таблицы владельца — только чтение.
UNIT_SHEET_ID = os.environ.get("UNIT_SHEET_ID", "1PjwfdbTI47_gQNDelUBkQ8yhsd4i05Te1JjEDoYxA0U")
UNIT_SHEET = "Юнит экономика Алиса"
PAID_SHEET = "СКОРО ПЛАТНО"
BOX_SHEET_ID = os.environ.get("BOX_SHEET_ID", "1AJPOlUacb6ZtHNQTwjML6Ty0qOlv9GJMwVCo54qOQ0Q")
BOX_SHEET = "РАЗМЕЩЕНИЕ ПО КОРОБАМ"
BOX_NAMES = ("61×40×33", "43×29×35", "40×30×30", "31×23×19,5", "штатный")

MS_STORE = os.environ.get("MS_STORE", "1. Основной склад")


def lead_days(cluster):
    for key, days in LEAD_DAYS.items():
        if key.lower() in (cluster or "").lower():
            return days
    return LEAD_DEFAULT


def mandatory_count(cluster):
    for key, n in MANDATORY.items():
        if key.lower() in (cluster or "").lower():
            return n
    return 0


def foreign(cluster):
    return (cluster or "").strip().lower() in FOREIGN_CLUSTERS
