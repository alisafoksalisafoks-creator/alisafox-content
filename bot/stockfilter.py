"""Артикулы без остатка на FBO (меньше 1 шт) в списках и сигналах бота не показываем (владелец 2026-10-29).

Общие суммы (заказы, прибыль, реклама за день) не трогаем — эти продажи были. Остатки Ozon перечитываются раз в 30 минут;
если Ozon не ответил, ничего не прячем.
"""
import logging
import threading
import time

log = logging.getLogger("bot.stockfilter")
TTL = 1800
_cache = {"at": 0.0, "skus": None, "offers": None}
_lock = threading.Lock()


def load(oz):
    with _lock:
        if _cache["skus"] is None or time.time() - _cache["at"] > TTL:
            try:
                st = oz.stocks()
                _cache["skus"] = {k for k, v in st.items() if v["fbo"] >= 1}
                _cache["offers"] = {(v["offer_id"] or "").upper() for v in st.values() if v["fbo"] >= 1}
                _cache["at"] = time.time()
            except Exception as e:  # noqa: BLE001 — без остатков список лучше показать целиком
                log.warning("остатки FBO не прочитались: %r", e)
        return _cache["skus"], _cache["offers"]


def no_fbo(oz, sku=None, offer_id=None):
    """True — на FBO меньше 1 шт, артикул в списках не показываем."""
    skus, offers = load(oz)
    if not skus:
        return False
    if sku is not None:
        return str(sku) not in skus
    return bool(offer_id) and offer_id.upper() not in offers
