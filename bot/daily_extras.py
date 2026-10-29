"""Утром после сводки: погода в городе пользователя и гороскоп по его знаку на сегодня.

Погода — Open-Meteo (бесплатно, без ключа), гороскоп — ежедневный XML ignio.com (по-русски, без ключа).
Город и дату рождения задаёт владелец: «👥 Пользователи» → 🎁 (сюрприз для пользователя).
"""
import logging
import re
import xml.etree.ElementTree as ET

import httpx

log = logging.getLogger("bot.extras")
SIGNS = [("aries", "♈ Овен"), ("taurus", "♉ Телец"), ("gemini", "♊ Близнецы"), ("cancer", "♋ Рак"),
         ("leo", "♌ Лев"), ("virgo", "♍ Дева"), ("libra", "♎ Весы"), ("scorpio", "♏ Скорпион"),
         ("sagittarius", "♐ Стрелец"), ("capricorn", "♑ Козерог"), ("aquarius", "♒ Водолей"), ("pisces", "♓ Рыбы")]
SIGN_NAME = dict(SIGNS)
WMO = {0: "☀️ ясно", 1: "🌤 малооблачно", 2: "⛅ переменная облачность", 3: "☁️ пасмурно", 45: "🌫 туман",
       48: "🌫 изморозь", 51: "🌦 лёгкая морось", 53: "🌦 морось", 55: "🌧 сильная морось", 56: "🌧 ледяная морось",
       57: "🌧 ледяная морось", 61: "🌦 небольшой дождь", 63: "🌧 дождь", 65: "🌧 сильный дождь", 66: "🌧 ледяной дождь",
       67: "🌧 ледяной дождь", 71: "🌨 небольшой снег", 73: "🌨 снег", 75: "❄️ сильный снег", 77: "🌨 снежная крупа",
       80: "🌦 ливень", 81: "🌧 ливни", 82: "⛈ сильные ливни", 85: "🌨 снегопад", 86: "❄️ сильный снегопад",
       95: "⛈ гроза", 96: "⛈ гроза с градом", 99: "⛈ сильная гроза с градом"}


def ensure(db):
    db.execute("create table if not exists profile (id integer primary key, city text, lat real, lon real, tz text, "
               "sign text, asked integer default 0)")
    db.commit()


def get(db, uid):
    ensure(db)
    return db.execute("select * from profile where id = ?", (uid,)).fetchone()


def save(db, uid, **kw):
    ensure(db)
    db.execute("insert or ignore into profile (id) values (?)", (uid,))
    for k, v in kw.items():
        db.execute(f"update profile set {k} = ? where id = ?", (v, uid))
    db.commit()


def find_city(name):
    """«Казань» → (Казань, широта, долгота, часовой пояс) или None."""
    r = httpx.get("https://geocoding-api.open-meteo.com/v1/search",
                  params={"name": name.strip(), "count": 1, "language": "ru"}, timeout=20)
    r.raise_for_status()
    res = (r.json().get("results") or [None])[0]
    if not res:
        return None
    return res["name"], res["latitude"], res["longitude"], res.get("timezone") or "Europe/Moscow"


def weather(p):
    r = httpx.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": p["lat"], "longitude": p["lon"], "timezone": p["tz"] or "Europe/Moscow", "forecast_days": 1,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max"},
        timeout=20)
    r.raise_for_status()
    d = {k: v[0] for k, v in r.json()["daily"].items()}

    return weather_text(p["city"], d)


def weather_text(city, d):
    """Живым языком: что за погода и как одеться. d — дневной прогноз Open-Meteo."""
    code, lo, hi = d.get("weather_code"), d["temperature_2m_min"], d["temperature_2m_max"]
    wind = round(d["wind_speed_10m_max"] / 3.6)  # Open-Meteo отдаёт км/ч
    rain = d.get("precipitation_probability_max") or 0
    sky = WMO.get(code, "").split(" ", 1)
    icon, what = (sky[0], sky[1]) if len(sky) == 2 else ("🌤", "")
    temp = f"от {round(lo)} до {round(hi)} градусов" if round(lo) != round(hi) else f"{round(hi)} градусов"
    lines = [f"{icon} {city}: сегодня {what + ', ' if what else ''}{temp}, ветер до {wind} м/с."]
    tips = []
    if hi <= -10:
        tips.append("На улице мороз — одевайтесь очень тепло, не забудьте шапку и перчатки")
    elif hi <= 0:
        tips.append("Холодно — одевайтесь тепло")
    elif hi <= 8:
        tips.append("Прохладно — одевайтесь теплее")
    elif hi <= 15:
        tips.append("Свежо — пригодится куртка")
    elif hi >= 28:
        tips.append("Жарко — пейте больше воды")
    if code in (71, 73, 75, 77, 85, 86):
        tips.append("идёт снег, на дорогах будьте осторожнее")
    elif code in (95, 96, 99):
        tips.append("возможна гроза, возьмите зонт")
    elif rain >= 50 or code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        tips.append("возьмите зонт")
    if wind >= 15:
        tips.append("ветер сильный — берегитесь сквозняков")
    elif wind >= 10:
        tips.append("ветрено")
    if tips:
        t = ", ".join(tips)
        lines.append(t[0].upper() + t[1:] + ".")
    return " ".join(lines)


def short_text(text, n=3):
    """Первые n предложений."""
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    return " ".join(parts[:n])


_horo = {"day": None, "data": {}}


def horoscope(sign, day):
    """Текст на сегодня по знаку; XML качаем раз в день на всех."""
    if _horo["day"] != day:
        r = httpx.get("https://ignio.com/r/export/utf/xml/daily/com.xml", timeout=20)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        _horo["data"] = {s: re.sub(r"\s+", " ", (root.findtext(f"{s}/today") or "")).strip() for s, _ in SIGNS}
        _horo["day"] = day
    text = _horo["data"].get(sign)
    return f"{SIGN_NAME[sign]}, гороскоп на сегодня: {short_text(text)}" if text else None


def message(p, day):
    """Сообщение пользователю: погода и гороскоп (что из них удалось получить). None — нечего слать."""
    parts = []
    if p and p["lat"] is not None:
        try:
            parts.append(weather(p))
        except Exception as e:  # noqa: BLE001
            log.warning("погода %s: %r", p["city"], e)
    if p and p["sign"]:
        try:
            h = horoscope(p["sign"], day)
            if h:
                parts.append(h)
        except Exception as e:  # noqa: BLE001
            log.warning("гороскоп: %r", e)
    return "\n\n".join(parts) or None


# начало знака: (месяц, день) — знак действует с этой даты до начала следующего
_STARTS = [((1, 20), "aquarius"), ((2, 19), "pisces"), ((3, 21), "aries"), ((4, 20), "taurus"), ((5, 21), "gemini"),
           ((6, 21), "cancer"), ((7, 23), "leo"), ((8, 23), "virgo"), ((9, 23), "libra"), ((10, 23), "scorpio"),
           ((11, 22), "sagittarius"), ((12, 22), "capricorn")]


def sign_of(day, month):
    """Знак зодиака по дню и месяцу рождения."""
    sign = "capricorn"  # 1–19 января
    for start, s in _STARTS:
        if (month, day) >= start:
            sign = s
    return sign


def parse_gift(text):
    """«Казань 12.05» / «Казань, 12.05.1990» → (город, день, месяц) или None."""
    m = re.match(r"\s*(.+?)[,\s]+(\d{1,2})\.(\d{1,2})(?:\.\d{2,4})?\s*$", text or "")
    if not m:
        return None
    d, mo = int(m.group(2)), int(m.group(3))
    return (m.group(1).strip(), d, mo) if 1 <= mo <= 12 and 1 <= d <= 31 else None


def describe(p):
    if not p or (p["city"] is None and p["sign"] is None):
        return "не задано"
    return f"{p['city'] or 'город не задан'}, {SIGN_NAME.get(p['sign'], 'знак не задан')}"
