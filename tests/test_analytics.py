import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.analytics import ResponseAnalytics, ResponseAnalyticsStore


class ResponseAnalyticsTests(unittest.TestCase):
    def test_store_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "response-analytics.jsonl"
            store = ResponseAnalyticsStore(path)
            record = ResponseAnalytics(
                message_id=101,
                request_message_id=100,
                channel_id=42,
                model="gemini-2.5-pro",
                timestamp=datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc),
                latency_ms=250,
                input_tokens=100,
                output_tokens=50,
                thought_tokens=25,
                tool_tokens=10,
                total_tokens=185,
                tool_steps=2,
                thought_signatures=["I should use the search tool."],
                tool_results=[{
                    "name": "web_search",
                    "arguments": {"query": "test"},
                    "result": '{"results": []}',
                }],
            )

            store.append(record)
            loaded = store.load()

            self.assertEqual(1, len(loaded))
            self.assertEqual(record.message_id, loaded[0].message_id)
            self.assertEqual(record.request_message_id, loaded[0].request_message_id)
            self.assertEqual(record.model, loaded[0].model)
            self.assertEqual(record.total_tokens, loaded[0].total_tokens)
            self.assertEqual(record.timestamp, loaded[0].timestamp)
            self.assertEqual(
                record.thought_signatures,
                loaded[0].thought_signatures,
            )
            self.assertEqual(record.tool_results, loaded[0].tool_results)
            self.assertFalse(loaded[0].is_interim)

    def test_load_returns_sorted_by_message_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "response-analytics.jsonl"
            store = ResponseAnalyticsStore(path)

            first = ResponseAnalytics(
                message_id=1,
                request_message_id=1,
                channel_id=1,
                model="gemini",
                timestamp=datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc),
                latency_ms=100,
                input_tokens=10,
                output_tokens=10,
                thought_tokens=0,
                tool_tokens=0,
                total_tokens=20,
                tool_steps=0,
            )
            second = ResponseAnalytics(
                message_id=2,
                request_message_id=2,
                channel_id=1,
                model="gemini",
                timestamp=datetime(2026, 7, 21, 12, 1, 0, tzinfo=timezone.utc),
                latency_ms=200,
                input_tokens=20,
                output_tokens=20,
                thought_tokens=5,
                tool_tokens=2,
                total_tokens=47,
                tool_steps=1,
            )

            store.append(first)
            store.append(second)

            loaded = store.load(limit=1)

            self.assertEqual(1, len(loaded))
            self.assertEqual(first.message_id, loaded[0].message_id)

    def test_find_by_message_id_uses_binary_search_on_sorted_store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "response-analytics.jsonl"
            store = ResponseAnalyticsStore(path)

            first = ResponseAnalytics(
                message_id=10,
                request_message_id=100,
                channel_id=1,
                model="gemini",
                timestamp=datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc),
                latency_ms=100,
                input_tokens=10,
                output_tokens=10,
                thought_tokens=0,
                tool_tokens=0,
                total_tokens=20,
                tool_steps=0,
            )
            second = ResponseAnalytics(
                message_id=20,
                request_message_id=200,
                channel_id=1,
                model="gemini",
                timestamp=datetime(2026, 7, 21, 12, 1, 0, tzinfo=timezone.utc),
                latency_ms=200,
                input_tokens=20,
                output_tokens=20,
                thought_tokens=5,
                tool_tokens=2,
                total_tokens=47,
                tool_steps=1,
            )

            store.append(first)
            store.append(second)

            found = store.find_by_message_id(20)

            self.assertIsNotNone(found)
            self.assertEqual(second.message_id, found.message_id)
            self.assertEqual(second.request_message_id, found.request_message_id)

            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(2, len(lines))
            self.assertIn('"message_id": 10', lines[0])
            self.assertIn('"message_id": 20', lines[1])

    def test_is_interim_round_trips_and_defaults_false(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "response-analytics.jsonl"
            store = ResponseAnalyticsStore(path)
            record = ResponseAnalytics(
                message_id=30,
                request_message_id=200,
                channel_id=1,
                model="gemini",
                timestamp=datetime(2026, 7, 21, 12, 2, 0, tzinfo=timezone.utc),
                latency_ms=50,
                input_tokens=20,
                output_tokens=5,
                thought_tokens=2,
                tool_tokens=0,
                total_tokens=27,
                tool_steps=1,
                is_interim=True,
            )

            store.append(record)
            loaded = store.find_by_message_id(30)

            self.assertIsNotNone(loaded)
            self.assertTrue(loaded.is_interim)
            self.assertIn('"is_interim": true', path.read_text(encoding="utf-8"))

            path.write_text(
                '{"message_id": 31, "request_message_id": 200, "channel_id": 1, '
                '"model": "gemini", "timestamp": "2026-07-21T12:03:00+00:00", '
                '"latency_ms": 10, "input_tokens": 1, "output_tokens": 1, '
                '"thought_tokens": 0, "tool_tokens": 0, "total_tokens": 2, '
                '"tool_steps": 0}\n',
                encoding="utf-8",
            )
            legacy = store.find_by_message_id(31)
            self.assertIsNotNone(legacy)
            self.assertFalse(legacy.is_interim)


if __name__ == "__main__":
    unittest.main()
