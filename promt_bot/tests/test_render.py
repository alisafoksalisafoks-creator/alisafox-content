import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("ANTHROPIC_API_KEY", "test")  # клиент создаётся при импорте, в сеть тесты не ходят

from promt_bot import claude, render  # noqa: E402


class Block(SimpleNamespace):
    def to_dict(self):
        return dict(self.__dict__)


class TestChunks(unittest.TestCase):
    def test_short_text_one_piece(self):
        self.assertEqual(render.chunks("привет"), ["привет"])

    def test_long_text_under_limit(self):
        text = "\n\n".join("абзац " * 200 for _ in range(10))
        parts = render.chunks(text)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= render.LIMIT for p in parts))
        self.assertEqual("".join(parts).replace("\n", "").replace(" ", ""),
                         text.replace("\n", "").replace(" ", ""))

    def test_one_huge_line(self):
        parts = render.chunks("я" * 9000)
        self.assertEqual([len(p) for p in parts], [4000, 4000, 1000])


class TestFiles(unittest.TestCase):
    def test_skill_and_prompt(self):
        skill = "---\nname: ozon-answer-question\ndescription: ответ\n---\n" + "шаг\n" * 100
        prompt = "Ты — копирайтер.\n" + "правило\n" * 60
        text = f"Вот скил:\n```markdown\n{skill}```\nИ промпт:\n```\n{prompt}```\nкоротко: ```\nok\n```"
        got = render.files(text)
        self.assertEqual([g[0] for g in got], ["SKILL.md", "promt_1.md"])
        self.assertIn("ozon-answer-question", got[0][2])
        self.assertTrue(got[1][1].startswith("Ты — копирайтер."))

    def test_chat_text_hides_only_long_blocks(self):
        short, long = "к" * 400, "д" * 5000
        got = render.chat_text(f"а\n```\n{short}\n```\nб\n```\n{long}\n```")
        self.assertIn(short, got)
        self.assertNotIn(long, got)
        self.assertIn("во вложенном файле", got)


class TestEcho(unittest.TestCase):
    def test_no_fallback_keeps_all(self):
        blocks = [Block(type="thinking", thinking="", signature="s"), Block(type="text", text="ок")]
        self.assertEqual(claude.echo(blocks), [b.to_dict() for b in blocks])

    def test_fallback_drops_thinking_and_tools_before_boundary(self):
        blocks = [Block(type="thinking", thinking="", signature="a"), Block(type="text", text="начало"),
                  Block(type="tool_use", id="t1", name="x", input={}), Block(type="fallback"),
                  Block(type="thinking", thinking="", signature="b"), Block(type="text", text="конец")]
        self.assertEqual([b["type"] for b in claude.echo(blocks)], ["text", "thinking", "text"])

    def test_questions_of(self):
        content = [{"type": "text", "text": "Пара вопросов"},
                   {"type": "tool_use", "id": "tu1", "name": claude.ASK,
                    "input": {"intro": "Чтобы сделать точно", "questions": [{"question": "Где?"}]}}]
        self.assertEqual(claude.questions_of(content), ("tu1", [{"question": "Где?"}], "Чтобы сделать точно"))
        self.assertIsNone(claude.questions_of(content[:1]))


if __name__ == "__main__":
    unittest.main()
