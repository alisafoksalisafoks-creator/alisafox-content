import asyncio
import os
import tempfile
import unittest

os.environ.setdefault("PRICE_BOT_DATA", tempfile.mkdtemp())

from price_bot import app, calc  # noqa: E402


class FakeOzon:
    async def prices(self, ids):
        return {10: {"price": 2200, "seller_price": 2200}}

    async def actions(self):
        return {"elastic": {10: {"action": "Эластичный бустинг. Без ограничения срока действия", "min_el": 2969,
                                 "max_el": 2666, "in_action": True, "boost": 55}},
                "site": {10: 1058.0}, "seen": {}, "errors": []}

    async def stairways(self, skus):
        return {}

    async def stocks(self, ids):
        return {10: {"fbo": 157, "fbs": 192}}


class TestCollect(unittest.TestCase):
    """Экран артикула должен брать бустинг и цену с картой из тех же данных, что и /audit."""

    def test_dashboard_shows_elastic(self):
        app.ozon, app.cache = FakeOzon(), {}

        async def no_ts(*a, **k):
            return {}
        app.ts.unit_metrics = no_ts
        p = {"product_id": 10, "offer_id": "OZ634-GM52NEW", "sku": 5987930314, "name": "Мышь", "cat": "Мышь"}
        d = asyncio.run(app.collect(p))
        text = calc.dashboard(p, d)
        self.assertIn("✅ В акции «Эластичный бустинг. Без ограничения срока действия», бустинг 55%", text)
        self.assertIn("на сайте с картой: 1 058 ₽", text)
        self.assertNotIn("нет в акциях «Эластичный", text)
        self.assertIn("Соинвест (сейчас): ≈51,9%", text)


if __name__ == "__main__":
    unittest.main()
