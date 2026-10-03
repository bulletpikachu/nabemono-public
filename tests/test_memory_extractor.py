import json
import unittest

from src.memory_extractor import _build_extraction_input, _parse_extraction


class FakeAuthor:
    def __init__(self, name):
        self.name = name


class FakeMessage:
    def __init__(self, author_name, content):
        self.author = FakeAuthor(author_name)
        self.content = content
        self.mentions = []


class MemoryExtractorTests(unittest.TestCase):
    def test_parse_valid_memory(self):
        result = _parse_extraction(
            json.dumps(
                {
                    "memories": [
                        {"content": "bushima prefers short answers", "importance": 2},
                    ]
                }
            )
        )

        self.assertEqual(
            [{"memory": "bushima prefers short answers", "importance": 2}],
            result,
        )

    def test_parse_multiple_memories(self):
        result = _parse_extraction(
            json.dumps(
                {
                    "memories": [
                        {"content": "bushima lives in tokyo", "importance": 1},
                        {"content": "bushima prefers short answers", "importance": 2},
                    ]
                }
            )
        )

        self.assertEqual(2, len(result))
        self.assertEqual("bushima lives in tokyo", result[0]["memory"])

    def test_parse_ignores_malformed_json(self):
        self.assertEqual([], _parse_extraction("not json"))

    def test_parse_ignores_empty_and_none(self):
        self.assertEqual([], _parse_extraction(""))
        self.assertEqual([], _parse_extraction(None))
        self.assertEqual([], _parse_extraction(json.dumps({"memories": []})))

    def test_parse_strips_markdown_fences(self):
        fenced = "```json\n" + json.dumps(
            {"memories": [{"content": "bushima likes music", "importance": 1}]}
        ) + "\n```"

        result = _parse_extraction(fenced)

        self.assertEqual([{"memory": "bushima likes music", "importance": 1}], result)

    def test_parse_clamps_importance(self):
        result = _parse_extraction(
            json.dumps(
                {
                    "memories": [
                        {"content": "a fact", "importance": 99},
                        {"content": "another fact", "importance": -5},
                        {"content": "bad importance", "importance": "high"},
                    ]
                }
            )
        )

        self.assertEqual([3, 1, 1], [memory["importance"] for memory in result])

    def test_parse_skips_blank_and_non_dict_items(self):
        result = _parse_extraction(
            json.dumps(
                {
                    "memories": [
                        {"content": "   ", "importance": 1},
                        "just a string",
                        {"content": "a real fact", "importance": 1},
                    ]
                }
            )
        )

        self.assertEqual([{"memory": "a real fact", "importance": 1}], result)

    def test_build_extraction_input_includes_context_and_newest(self):
        context = [FakeMessage("friend", "i love jazz")]
        message = FakeMessage("bushima", "remember that i hate mornings")

        prompt = _build_extraction_input(message, context_messages=context)

        self.assertIn("friend: i love jazz", prompt)
        self.assertIn("bushima: remember that i hate mornings", prompt)
        self.assertLess(prompt.index("friend:"), prompt.index("bushima:"))

    def test_build_extraction_input_without_context(self):
        message = FakeMessage("bushima", "hello")

        prompt = _build_extraction_input(message)

        self.assertIn("(no earlier messages)", prompt)
        self.assertIn("bushima: hello", prompt)


if __name__ == "__main__":
    unittest.main()
