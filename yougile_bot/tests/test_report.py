import os
import tempfile
import unittest
from datetime import date
from zoneinfo import ZoneInfo

os.environ.setdefault("YG_BOT_DATA", tempfile.mkdtemp())

from yougile_bot import report  # noqa: E402

TZ = ZoneInfo("Asia/Yekaterinburg")
report.set_tz(TZ)
D = date(2026, 11, 6)
START, END = report.day_window(D, TZ)
H = 3600_000
U, OTHER = "u1", "u2"


def task(i, **kw):
    return {"id": i, "title": f"Задача {i}", "timestamp": START - 5 * 86400_000, "assigned": [U], **kw}


class TestCollect(unittest.TestCase):
    def test_events_and_tasks(self):
        tasks = [task("t1", completed=True, completedTimestamp=START + 2 * H),
                 task("t2", createdBy=U, timestamp=START + H),
                 task("t3", completed=True, completedTimestamp=START - H)]  # вчера — не считается
        events = [
            {"ts": START + 3 * H, "user_id": U, "kind": "move", "task_id": "t2", "chat_id": None, "text": "",
             "extra": {"to": "c1"}},
            {"ts": START + 4 * H, "user_id": U, "kind": "message", "task_id": "t1", "chat_id": "t1", "text": "ок",
             "extra": {}},
            {"ts": START + 5 * H, "user_id": U, "kind": "message", "task_id": None, "chat_id": "g1", "text": "привет",
             "extra": {}},
            {"ts": START + 5 * H, "user_id": OTHER, "kind": "done", "task_id": "t2", "chat_id": None, "text": "",
             "extra": {}},
        ]
        g = report.collect(U, events, tasks, START, END)
        self.assertEqual(report.count(g), 5)  # done t1, add t2, move t2, сообщение t1, сообщение g1
        self.assertIn("g1", g)
        self.assertNotIn("t3", g)

    def test_log_done_not_doubled(self):
        tasks = [task("t1", completed=True, completedTimestamp=START + 2 * H)]
        events = [{"ts": START + 2 * H, "user_id": U, "kind": "done", "task_id": "t1", "chat_id": None, "text": "",
                   "extra": {}}]
        self.assertEqual(report.count(report.collect(U, events, tasks, START, END)), 1)


class TestPlan(unittest.TestCase):
    def test_plan_statuses(self):
        tasks = [task("a", deadline={"deadline": START + 10 * H}),  # срок сегодня, не сделана
                 task("b", deadline={"deadline": START + 10 * H}, completed=True, completedTimestamp=START + H),
                 task("c", deadline={"deadline": START - 86400_000}),  # хвост
                 task("d", deadline={"deadline": START - 86400_000}, completed=True,
                      completedTimestamp=START - H),  # закрыта вчера — не в плане
                 task("e", deadline={"deadline": END + H})]  # завтра
        got = {t["id"]: s for t, s in report.plan(U, tasks, START, END)}
        self.assertEqual(got, {"a": "open", "b": "done", "c": "late"})


class TestRender(unittest.TestCase):
    def test_sections(self):
        tasks = [task("t1", columnId="c1")]
        events = [{"ts": START + H, "user_id": U, "kind": "move", "task_id": "t1", "chat_id": None, "text": "",
                   "extra": {"to": "c1"}},
                  {"ts": START + 2 * H, "user_id": U, "kind": "message", "task_id": "t1", "chat_id": "t1",
                   "text": "<p>Готово &amp; проверено</p>", "extra": {}},
                  {"ts": START + 3 * H, "user_id": U, "kind": "message", "task_id": None, "chat_id": "g1",
                   "text": "всем привет", "extra": {}}]
        g = report.collect(U, events, tasks, START, END)
        text = report.render("Вероника", D, g, {"t1": tasks[0]}, lambda t: "Доска › В работе",
                             lambda c: "В работе", lambda c: "Чат менеджеров",
                             plan_items=report.plan(U, tasks, START, END))
        for part in ("План|Факт", "Доски задач", "Чаты задач", "Групповые чаты", "«В работе»",
                     "Готово &amp; проверено", "Чат менеджеров", "06.11.2026 (пт)"):
            self.assertIn(part, text)

    def test_company(self):
        g = report.collect(U, [], [task("t1", completed=True, completedTimestamp=START + H)], START, END)
        line = report.company_line("Анна", g, {"t1": task("t1")})
        self.assertIn("✅1", line)
        self.assertIn("Задача t1", line)


class TestChunks(unittest.TestCase):
    def test_long_block_split(self):
        text = "шапка\n\n" + "\n".join("строка " * 30 for _ in range(100))
        parts = report.chunks(text)
        self.assertTrue(all(len(p) <= 3900 for p in parts))
        self.assertGreater(len(parts), 1)

    def test_names(self):
        self.assertEqual(report.short_name("Настасья 👩🏽‍💻"), "Настасья")
        self.assertEqual(report.first_word("Вера Карсаева"), "вера")
        self.assertEqual(report.first_word("Вероника"), "вероника")


class TestPlanFactDay(unittest.TestCase):
    def test_find_and_status(self):
        tz = ZoneInfo("Asia/Yekaterinburg")
        start, end = report.day_window(date(2026, 11, 9), tz)
        cols = {"c1": {"title": "В работе"}, "c2": {"title": "ПЛАН/ФАКТ ДНЯ"}}
        tasks = [{"id": "a", "title": "ПЛАН/ФАКТ ДНЯ Вероника", "assigned": ["u"], "columnId": "c1", "timestamp": 1},
                 {"id": "b", "title": "План - факт дня 2026.11.02", "assigned": ["u"], "columnId": "c1",
                  "timestamp": 2, "completed": True, "completedTimestamp": start - 1},
                 {"id": "c", "title": "Закупка", "assigned": ["u"], "columnId": "c2", "timestamp": 3},
                 {"id": "d", "title": "Заполнить план факт финансисту", "assigned": ["u"], "columnId": "c1",
                  "timestamp": 4}]
        self.assertEqual({t["id"] for t in report.pf_tasks("u", tasks, cols, start, end)}, {"a", "c"})
        morning = start + 9 * 3600_000
        self.assertEqual(report.pf_status([(morning, "План: цены", "")], tz), (True, False))
        self.assertEqual(report.pf_status([(morning, "План", ""), (morning + 1, "сделала", "")], tz), (True, True))
        self.assertEqual(report.pf_status([], tz), (False, False))
        self.assertEqual(report.clean_lines("1) а<br>2) б"), "1) а\n2) б")


if __name__ == "__main__":
    unittest.main()
