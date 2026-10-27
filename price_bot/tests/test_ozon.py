import asyncio
import unittest

from price_bot.ozon import Ozon


class Fake(Ozon):
    """Ozon без сети: отвечает заготовками по пути запроса и запоминает тела запросов."""

    def __init__(self, answers):
        super().__init__("1", "k")
        self.answers, self.sent = answers, []

    async def call(self, path, body=None, method="POST"):
        self.sent.append((path, body))
        a = self.answers[path]
        return a(body) if callable(a) else a


def run(coro):
    return asyncio.run(coro)


class TestOzon(unittest.TestCase):
    def test_catalog_types_and_sku(self):
        o = Fake({
            "/v3/product/list": {"result": {"items": [{"product_id": 10}, {"product_id": 11}], "last_id": ""}},
            "/v1/description-category/tree": {"result": [{"description_category_id": 5, "category_name": "Авто",
                                                           "children": [{"type_id": 7, "type_name": "FM-трансмиттер",
                                                                         "children": []}]}]},
            "/v3/product/info/list": {"items": [
                {"id": 10, "offer_id": "OZ214", "name": "FM", "type_id": 7, "description_category_id": 5,
                 "sources": [{"sku": 424936924}],
                 "stocks": {"stocks": [{"source": "fbo", "present": 30}, {"source": "fbs", "present": 5}]}},
                {"id": 12, "offer_id": "FBS-ONLY", "name": "только FBS", "type_id": 7, "sources": [{"sku": 99}],
                 "stocks": {"stocks": [{"source": "fbs", "present": 5}]}},
                {"id": 11, "offer_id": "NEW", "name": "без SKU", "type_id": 7, "sources": []}]},
        })
        goods = run(o.catalog())
        self.assertEqual(goods, [{"product_id": 10, "offer_id": "OZ214", "sku": 424936924, "name": "FM",
                                  "fbo": 30, "cat": "FM-трансмиттер"}])
        self.assertEqual(o.sent[0][1]["filter"]["visibility"], "VISIBLE_WITH_FBO_STOCK")

    def test_stairways_roundtrip(self):
        o = Fake({"/v1/product/stairway-discount/by-quantity/get": {"stairways": [
                      {"sku": "5", "enabled": True, "status": "SUCCESS",
                       "stairway": {"steps": [{"step": 2, "quantity": 3, "discount": 10},
                                              {"step": 1, "quantity": 2, "discount": 5}]}}]},
                  "/v1/product/stairway-discount/by-quantity/set": {
                      "accepted": True, "errors": [{"sku": 6, "data": [{"message": "слишком большая скидка"}]}]}})
        self.assertEqual(run(o.stairways([5])), {5: {"enabled": True, "steps": [(2, 5), (3, 10)], "status": "SUCCESS"}})
        r = run(o.set_stairways([(5, True, [(2, 5), (3, 10)]), (6, False, [(2, 5)])]))
        body = o.sent[-1][1]
        self.assertEqual(body["stairways"][0]["stairway"]["steps"],
                         [{"step": 1, "quantity": 2, "discount": 5}, {"step": 2, "quantity": 3, "discount": 10}])
        self.assertFalse(body["stairways"][1]["enabled"])
        self.assertEqual(r["errors"], {6: ["слишком большая скидка"]})

    def test_elastic_prefers_participation(self):
        prod = {"id": 10, "price_min_elastic": {"amount": "351"}, "price_max_elastic": {"amount": "221"},
                "current_boost": 12}
        o = Fake({"/v1/actions": {"result": [{"id": 1, "title": "Эластичный бустинг"}, {"id": 2, "title": "Распродажа"}]},
                  "/v2/actions/products": {"products": [prod], "last_id": ""},
                  "/v2/actions/candidates": {"products": [dict(prod, current_boost=0)], "last_id": ""}})
        el = run(o.elastic())
        self.assertEqual((el[10]["min_el"], el[10]["max_el"], el[10]["in_action"], el[10]["boost"]),
                         (351.0, 221.0, True, 12))
        self.assertNotIn(2, [b["action_id"] for _, b in o.sent if b and "action_id" in b])

    def test_set_price_errors(self):
        o = Fake({"/v1/product/import/prices": {"result": [{"updated": False, "errors": [{"message": "old_price"}]}]}})
        self.assertEqual(run(o.set_price("OZ214", 1290)), ["old_price"])
        self.assertEqual(o.sent[0][1], {"prices": [{"offer_id": "OZ214", "price": "1290"}]})


if __name__ == "__main__":
    unittest.main()
