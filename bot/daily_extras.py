"""Утром после сводки: погода в городе пользователя и гороскоп по его знаку на сегодня.

Погода — Open-Meteo (бесплатно, без ключа), гороскоп — ежедневный XML ignio.com (по-русски, без ключа).
Город и знак каждый пользователь задаёт сам кнопкой «🌤 Погода и гороскоп».
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

    def t(x):
        return f"{round(x):+d}°".replace("+0°", "0°")
    rain = d.get("precipitation_probability_max")
    return (f"🌤 Погода в г. {p['city']} сегодня: {WMO.get(d.get('weather_code'), 'без описания')}, "
            f"{t(d['temperature_2m_min'])}…{t(d['temperature_2m_max'])}"
            + (f", осадки {rain}%" if rain is not None else "")
            + f", ветер до {round(d['wind_speed_10m_max'] / 3.6)} м/с.")


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
    return f"{SIGN_NAME[sign]} — гороскоп на сегодня:\n{text}" if text else None


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
