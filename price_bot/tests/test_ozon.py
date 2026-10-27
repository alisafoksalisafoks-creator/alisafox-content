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

    def test_actions_elastic_and_site_price(self):
        prod = {"id": 10, "price_min_elastic": {"amount": "351"}, "price_max_elastic": {"amount": "221"},
                "current_boost": 12, "website_prices": {"price": {"amount": "837"}}}
        o = Fake({"/v1/actions": {"result": [{"id": 1, "title": "Эластичный бустинг"}, {"id": 2, "title": "Распродажа"}]},
                  "/v2/actions/products": {"products": [prod], "last_id": ""},
                  "/v2/actions/candidates": {"products": [dict(prod, current_boost=0)], "last_id": ""}})
        r = run(o.actions())
        el = r["elastic"]
        self.assertEqual(r["site"], {10: 837.0})
        self.assertEqual((el[10]["min_el"], el[10]["max_el"], el[10]["in_action"], el[10]["boost"]),
                         (351.0, 221.0, True, 12))

    def test_elastic_action_beats_other_boost(self):
        # товар участвует в другом «…бустинге» с ценой 760, а в «Эластичном» — кандидат с вилкой 637 / 539
        other = {"id": 10, "price_min_elastic": 760, "price_max_elastic": 760}
        el = {"id": 10, "price_min_elastic": 637, "price_max_elastic": 539}
        o = Fake({"/v1/actions": {"result": [{"id": 1, "title": "Максимальный бустинг"},
                                             {"id": 2, "title": "Эластичный бустинг"}]},
                  "/v2/actions/products": lambda b: {"products": [other] if b["action_id"] == 1 else []},
                  "/v2/actions/candidates": lambda b: {"products": [el] if b["action_id"] == 2 else []}})
        e = run(o.actions())["elastic"][10]
        self.assertEqual((e["action"], e["min_el"], e["max_el"], e["in_action"]),
                         ("Эластичный бустинг", 637.0, 539.0, False))

    def test_one_failed_action_keeps_others(self):
        # акция без «бустинга» в названии, но с вилкой; другая акция падает в v2 и в v1 — остальное не теряем
        from price_bot.ozon import OzonError

        def v2(b):
            if b["action_id"] == 1:
                raise OzonError("/v2/actions/candidates: 400 bad action")
            return {"products": [{"id": 10, "price_min_elastic": 637, "price_max_elastic": 539,
                                  "website_prices": {"price": {"amount": "700"}}}]}

        def v1(b):
            raise OzonError("/v1/actions/candidates: 400")
        o = Fake({"/v1/actions": {"result": [{"id": 1, "title": "Купон"}, {"id": 2, "title": "Бонус за цену"}]},
                  "/v2/actions/products": {"products": []}, "/v2/actions/candidates": v2,
                  "/v1/actions/candidates": v1})
        r = run(o.actions())
        self.assertEqual((r["elastic"][10]["min_el"], r["elastic"][10]["action"]), (637.0, "Бонус за цену"))
        self.assertEqual(r["site"], {10: 700.0})
        self.assertEqual(len(r["errors"]), 1)
        self.assertEqual(len(r["seen"][10]), 1)

    def test_timeout_in_one_action(self):
        def v2(b):
            if b["action_id"] == 1:
                raise asyncio.TimeoutError()
            return {"products": [{"id": 10, "price_min_elastic": 993, "price_max_elastic": 838, "current_boost": 55}]}
        o = Fake({"/v1/actions": {"result": [{"id": 1, "title": "Распродажа"},
                                             {"id": 2, "title": "Эластичный бустинг. Без ограничения срока действия"}]},
                  "/v2/actions/products": v2, "/v2/actions/candidates": {"products": []},
                  "/v1/actions/products": lambda b: (_ for _ in ()).throw(asyncio.TimeoutError())})
        r = run(o.actions())
        e = r["elastic"][10]
        self.assertEqual((e["min_el"], e["max_el"], e["in_action"], e["boost"]), (993.0, 838.0, True, 55))
        self.assertEqual(len(r["errors"]), 1)

    def test_v1_fallback_unwraps_result(self):
        from price_bot.ozon import OzonError

        def v2(b):
            raise OzonError("/v2: 404")
        o = Fake({"/v1/actions": {"result": [{"id": 3, "title": "Эластичный бустинг"}]},
                  "/v2/actions/products": v2, "/v2/actions/candidates": v2,
                  "/v1/actions/products": {"result": {"products": [], "total": 0}},
                  "/v1/actions/candidates": {"result": {"products": [{"id": 10, "price_min_elastic": 637}],
                                                        "last_id": 0}}})
        e = run(o.actions())["elastic"][10]
        self.assertEqual((e["min_el"], e["max_el"], e["in_action"]), (637.0, None, False))

    def test_site_price_is_card_price(self):
        from price_bot.ozon import _card_price
        wp = {"price": {"amount": "1767"},
              "prices_by_schema": {"FBS": {"black_price": {"amount": "1742"}, "green_price": {"amount": "1610"}},
                                   "FBO": {"black_price": {"amount": "1767"}, "green_price": {"amount": "1590"}}}}
        self.assertEqual(_card_price(wp), 1590.0)
        self.assertEqual(_card_price({"prices_by_schema": {"FBO": {"black_price": {"amount": "1767"}}}}), 1767.0)
        self.assertEqual(_card_price({"price": {"amount": "837"}}), 837.0)
        self.assertIsNone(_card_price(None))

    def test_action_pages_by_100(self):
        pages = {"": {"products": [{"id": i, "price_min_elastic": 900} for i in range(100)], "last_id": "p2"},
                 "p2": {"products": [{"id": 100, "price_min_elastic": 900}], "last_id": ""}}
        o = Fake({"/v1/actions": {"result": [{"id": 1, "title": "Эластичный бустинг"}]},
                  "/v2/actions/products": lambda b: pages[b.get("last_id", "")],
                  "/v2/actions/candidates": {"products": []}})
        r = run(o.actions())
        self.assertEqual(len(r["elastic"]), 101)
        self.assertTrue(all(b["limit"] <= 100 for path, b in o.sent if path.startswith("/v2/actions")))

    def test_elastic_candidate_without_elastic_prices(self):
        # OZ470-MD60: кандидат в «Эластичный бустинг», Ozon не отдал price_min_elastic — порог из max_action_price
        prod = {"id": 10, "max_action_price": {"amount": "1597"},
                "website_prices": {"prices_by_schema": {"FBO": {"green_price": {"amount": "909"}}}}}
        o = Fake({"/v1/actions": {"result": [{"id": 1, "title": "Эластичный бустинг. Без ограничения срока действия"}]},
                  "/v2/actions/products": {"products": []}, "/v2/actions/candidates": {"products": [prod]}})
        r = run(o.actions())
        e = r["elastic"][10]
        self.assertEqual((e["min_el"], e["in_action"]), (1597.0, False))
        self.assertEqual(r["site"][10], 909.0)

    def test_set_price_errors(self):
        o = Fake({"/v1/product/import/prices": {"result": [{"updated": False, "errors": [{"message": "old_price"}]}]}})
        self.assertEqual(run(o.set_price("OZ214", 1290)), ["old_price"])
        self.assertEqual(o.sent[0][1], {"prices": [{"offer_id": "OZ214", "price": "1290"}]})


if __name__ == "__main__":
    unittest.main()
