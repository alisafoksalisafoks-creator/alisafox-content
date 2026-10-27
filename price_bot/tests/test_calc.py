import unittest

from price_bot import calc

P = {"product_id": 1, "offer_id": "OZ214", "sku": 424936924, "name": "FM трансмиттер <Bluetooth>", "cat": "FM-трансмиттер"}


class TestSteps(unittest.TestCase):
    def test_formats(self):
        for text in ("2-5, 3-10", "от 2 шт 5%; от 3 шт 10%", "2 5\n3 10", "2:5 3:10", "2 шт −5%, 3 шт −10%"):
            self.assertEqual(calc.parse_steps(text), [(2, 5), (3, 10)], text)

    def test_limits(self):
        for bad in ("abc", "1-5", "2-0", "2-95", "3-5, 2-10", "2-10, 3-5", "2-1,3-2,4-3,5-4,6-5"):
            with self.assertRaises(ValueError, msg=bad):
                calc.parse_steps(bad)

    def test_fmt(self):
        self.assertEqual(calc.fmt_steps([(2, 5), (3, 10)]), "от 2 шт −5%, от 3 шт −10%")
        self.assertEqual(calc.fmt_steps([]), "нет")


class TestEditor(unittest.TestCase):
    def test_validate_sorts_and_checks(self):
        self.assertEqual(calc.validate_steps([[3, 8], [2, 7]]), [(2, 7), (3, 8)])
        self.assertEqual(calc.validate_steps([]), [])
        for bad in ([[2, 7], [2, 8]], [[2, 9], [3, 8]], [[1, 5]], [[2, 5], [3, 6], [4, 7], [5, 8], [6, 9]]):
            with self.assertRaises(ValueError, msg=bad):
                calc.validate_steps(bad)

    def test_add_level_keeps_old(self):
        text = calc.editor_text("OZ357NEW5", [(2, 7)], [[2, 7], [3, 8]])
        self.assertIn("Сейчас в Ozon: от 2 шт −7%", text)
        self.assertIn("1) от 2 шт −7%\n2) от 3 шт −8%", text)
        self.assertNotIn("⚠️", text)

    def test_warns_and_no_changes(self):
        self.assertIn("Скидка должна расти", calc.editor_text("x", [], [[2, 9], [3, 8]]))
        self.assertIn("Изменений пока нет", calc.editor_text("x", [(2, 7)], [[2, 7]]))
        self.assertIn("без скидки", calc.editor_text("x", [(2, 7)], []))
        self.assertIn("у товаров разные уровни", calc.editor_text("x", [], [], note="⚠️ разные"))


class TestHistory(unittest.TestCase):
    def test_price_line(self):
        e = {"ts": "27.10 16:40", "who": "Алиса", "kind": "price", "offer": "OZ214", "old": 365, "new": 300}
        self.assertEqual(calc.fmt_history(e), "27.10 16:40 · Алиса · <b>OZ214</b>: цена 365 ₽ → 300 ₽ (−65 ₽, −17,8%)")

    def test_qty_line(self):
        e = {"ts": "27.10 16:41", "who": "Алиса", "kind": "qty", "offer": "OZ357NEW5", "old": [[2, 7]],
             "new": [[2, 7], [3, 8]]}
        self.assertIn("от 2 шт −7% → от 2 шт −7%, от 3 шт −8%", calc.fmt_history(e))


class TestExport(unittest.TestCase):
    def test_xlsx(self):
        import io
        from openpyxl import load_workbook
        from price_bot import export
        body = export.history_xlsx([
            {"ts": "27.10 16:40", "at": "2026-10-27T16:40+03:00", "who": "Алиса", "kind": "price", "offer": "OZ214",
             "old": 365, "new": 300},
            {"ts": "27.10 16:41", "who": "Алиса", "kind": "qty", "offer": "OZ357NEW5", "old": [[2, 7]],
             "new": [[2, 7], [3, 8]]}])
        rows = list(load_workbook(io.BytesIO(body)).active.values)
        self.assertEqual(rows[1], ("2026-10-27 16:40", "Алиса", "OZ214", "Цена", 365, 300, -65, -17.8))
        self.assertEqual(rows[2][3:6], ("Скидка от количества", "от 2 шт −7%", "от 2 шт −7%, от 3 шт −8%"))


class TestPrice(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(calc.parse_price("1 290 ₽"), 1290)
        self.assertEqual(calc.parse_price("999,6"), 1000)
        with self.assertRaises(ValueError):
            calc.parse_price("дорого")
        with self.assertRaises(ValueError):
            calc.parse_price("0")

    def test_soinvest(self):
        self.assertEqual(calc.soinvest(1000, 880), 12.0)
        self.assertIsNone(calc.soinvest(1000, None))

    def test_profit_forecast(self):
        # −100 ₽ цены при комиссии 40% и ДРР 10%: −100 × 0,5 × 0,93 = −46,5
        self.assertAlmostEqual(calc.profit_forecast(250, 1000, 900, 40, 10), 203.5)
        self.assertIsNone(calc.profit_forecast(None, 1000, 900, 40, 10))

    def test_drr_forecast(self):
        self.assertAlmostEqual(calc.drr_forecast(9, 1000, 900), 10)


class TestBoost(unittest.TestCase):
    EL = {"action": "Эластичный бустинг", "min_el": 351, "max_el": 221, "in_action": False}

    def test_need_lower(self):
        lines = calc.boost_hint(365, self.EL)
        self.assertIn("снизьте на 14 ₽", lines[0])
        self.assertIn("снизьте на 144 ₽", lines[1])

    def test_already_in(self):
        lines = calc.boost_hint(300, dict(self.EL, in_action=True, boost=12))
        self.assertIn("бустинг 12%", lines[0])
        self.assertIn("уже проходите", lines[1])

    def test_no_action(self):
        self.assertIn("нет в акциях", calc.boost_hint(300, None)[0])


class TestTrend(unittest.TestCase):
    def test_position_lower_is_better(self):
        self.assertTrue(calc.trend(5, 7, lower_is_better=True).startswith("🟢 ▼"))
        self.assertTrue(calc.trend(9, 7, lower_is_better=True).startswith("🔴 ▲"))

    def test_soinvest_drop(self):
        self.assertEqual(calc.trend(12, 15, unit="%"), "🔴 ▼ −3 п.п. к вчера")
        self.assertEqual(calc.trend(12, None), "нет данных за вчера")


class TestScreens(unittest.TestCase):
    def test_dashboard_full(self):
        d = {"price": 365, "seller_price": 365, "site_price": 320, "soinvest": 12.3, "soinvest_y": 15,
             "elastic": TestBoost.EL, "ts": {"profit": 252.55, "drr": 9.71, "drr_rk": 8.16, "period": "17.10–26.10"},
             "stairway": {"enabled": True, "steps": [(2, 5)]}, "position": 7, "position_y": 9,
             "stock": {"fbo": 30, "fbs": 5}}
        text = calc.dashboard(P, d)
        self.assertLess(text.index("FBO <b>30 шт</b> · FBS <b>5 шт</b>"), text.index("💰 Цена"))
        for part in ("&lt;Bluetooth&gt;", "365 ₽", "снизьте на 14 ₽", "253 ₽", "ДРР общий 9,7%", "РК ДРР 8,2%",
                     "🔴 ▼ −2,7 п.п.", "от 2 шт −5%", "🟢 ▼ −2"):
            self.assertIn(part, text)

    def test_dashboard_empty(self):
        text = calc.dashboard(P, {"ts_error": "ТС не подключён"})
        self.assertIn("нет данных из ТС", text)
        self.assertIn("Скидка от количества: нет", text)

    def test_confirm_warnings(self):
        d = {"price": 1000, "old_price": 1100, "commission": 40, "ts": {"profit": 50, "drr": 10}}
        text = calc.confirm_price(P, d, 700)
        self.assertIn("Было: 1 000 ₽ → станет: <b>700 ₽</b>", text)
        self.assertIn("больше 20%", text)
        self.assertIn("уйдёт в минус", text)
        self.assertIn("не ниже зачёркнутой", calc.confirm_price(P, d, 1150))


if __name__ == "__main__":
    unittest.main()
