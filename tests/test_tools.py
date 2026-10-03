import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.tools import (
    execute_tool_call,
    extract_page_text,
    format_page_contents,
    format_search_results,
)


class FormatSearchResultsTests(unittest.TestCase):
    def test_keeps_top_results_and_trims_snippets(self):
        payload = {
            "query": {"original": "nabemono"},
            "web": {
                "results": [
                    {
                        "title": f"result {index}",
                        "url": f"https://example.com/{index}",
                        "description": "word " * 200,
                    }
                    for index in range(10)
                ],
            },
        }

        summary = format_search_results(payload, max_results=3)

        self.assertEqual("nabemono", summary["query"])
        self.assertEqual(3, len(summary["results"]))
        self.assertEqual("https://example.com/0", summary["results"][0]["url"])
        self.assertTrue(summary["results"][0]["content"].endswith("..."))
        self.assertLessEqual(len(summary["results"][0]["content"]), 303)
        self.assertNotIn("note", summary)

    def test_reports_when_no_web_results_are_returned(self):
        payload = {
            "query": {"original": "weather"},
            "web": {"results": []},
        }

        summary = format_search_results(payload)

        self.assertEqual("no results returned", summary["note"])


class WebSearchToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_tool_call_queries_brave(self):
        payload = {
            "query": {"original": "latest gemini model"},
            "web": {
                "results": [
                    {
                        "title": "Gemini",
                        "url": "https://example.com/gemini",
                        "description": "model notes",
                    }
                ],
            },
        }

        captured = {}

        class FakeResponse:
            def raise_for_status(self):
                pass

            async def json(self, content_type=None):
                return payload

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class FakeSession:
            def __init__(self, *args, **kwargs):
                pass

            def get(self, url, headers=None, params=None):
                captured["url"] = url
                captured["headers"] = headers
                captured["params"] = params
                return FakeResponse()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        tool_call = SimpleNamespace(
            name="web_search",
            arguments={"query": "latest gemini model", "time_range": "week"},
        )

        with (
            patch("src.tools.aiohttp.ClientSession", FakeSession),
            patch("src.tools.BRAVE_API_KEY", "test-api-key"),
            patch("builtins.print"),
        ):
            result = await execute_tool_call(SimpleNamespace(), tool_call)

        self.assertEqual(
            "https://api.search.brave.com/res/v1/web/search",
            captured["url"],
        )
        self.assertEqual(
            "test-api-key",
            captured["headers"]["X-Subscription-Token"],
        )
        self.assertEqual(
            {"q": "latest gemini model", "count": 5, "freshness": "pw"},
            captured["params"],
        )
        self.assertEqual(
            "https://example.com/gemini",
            json.loads(result)["results"][0]["url"],
        )


class ExtractPageTextTests(unittest.TestCase):
    def test_keeps_title_and_visible_text(self):
        html = """
        <html>
          <head><title>Example Page</title>
            <script>alert("no")</script>
            <style>body { color: red; }</style>
          </head>
          <body>
            <nav>skip me</nav>
            <h1>Hello</h1>
            <p>World  from   nabemono</p>
          </body>
        </html>
        """

        title, content = extract_page_text(html)

        self.assertEqual("Example Page", title)
        self.assertIn("Hello", content)
        self.assertIn("World from nabemono", content)
        self.assertNotIn("alert", content)
        self.assertNotIn("skip me", content)
        self.assertNotIn("color: red", content)

    def test_format_page_contents_truncates_and_notes_empty(self):
        summary = format_page_contents(
            "https://example.com",
            "Title",
            "word " * 200,
            max_chars=20,
        )

        self.assertTrue(summary["truncated"])
        self.assertTrue(summary["content"].endswith("..."))
        self.assertLessEqual(len(summary["content"]), 23)

        empty = format_page_contents("https://example.com", "", "")
        self.assertEqual("no readable text found", empty["note"])


class VisitPageToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_tool_call_reads_html_page(self):
        captured = {}

        async def read_body(n=-1):
            return (
                b"<html><head><title>News</title></head>"
                b"<body><p>Spain won.</p></body></html>"
            )

        class FakeResponse:
            status = 200
            reason = "OK"
            url = "https://example.com/article"
            headers = {"Content-Type": "text/html; charset=utf-8"}
            charset = "utf-8"
            content = SimpleNamespace(read=read_body)

            def raise_for_status(self):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class FakeSession:
            def __init__(self, *args, **kwargs):
                pass

            def get(self, url, headers=None, allow_redirects=None):
                captured["url"] = url
                captured["headers"] = headers
                captured["allow_redirects"] = allow_redirects
                return FakeResponse()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        tool_call = SimpleNamespace(
            name="visit_page",
            arguments={"url": "https://example.com/article"},
        )

        with (
            patch("src.tools.aiohttp.ClientSession", FakeSession),
            patch("builtins.print"),
        ):
            result = await execute_tool_call(SimpleNamespace(), tool_call)

        payload = json.loads(result)
        self.assertEqual("https://example.com/article", captured["url"])
        self.assertTrue(captured["allow_redirects"])
        self.assertEqual("News", payload["title"])
        self.assertEqual("Spain won.", payload["content"])

    async def test_http_error_returns_json_note_instead_of_raising(self):
        class FakeResponse:
            status = 403
            reason = "Forbidden"
            url = "https://usc.edu/academic-calendar/"
            headers = {"Content-Type": "text/html"}

            def raise_for_status(self):
                raise AssertionError("HTTP errors should be returned to the model")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class FakeSession:
            def __init__(self, *args, **kwargs):
                pass

            def get(self, url, headers=None, allow_redirects=None):
                return FakeResponse()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        tool_call = SimpleNamespace(
            name="visit_page",
            arguments={"url": "https://usc.edu/academic-calendar/"},
        )

        with (
            patch("src.tools.aiohttp.ClientSession", FakeSession),
            patch("builtins.print"),
        ):
            result = await execute_tool_call(SimpleNamespace(), tool_call)

        payload = json.loads(result)
        self.assertEqual("https://usc.edu/academic-calendar/", payload["url"])
        self.assertEqual("HTTP 403 Forbidden", payload["error"])
        self.assertIn("try a different URL", payload["note"])

    async def test_rejects_non_http_urls(self):
        tool_call = SimpleNamespace(
            name="visit_page",
            arguments={"url": "file:///etc/passwd"},
        )

        with self.assertRaises(ValueError):
            await execute_tool_call(SimpleNamespace(), tool_call)


class InterimMessageToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_tool_call_sends_discord_message(self):
        sent = SimpleNamespace(
            id=555,
            channel=SimpleNamespace(id=10),
            created_at=datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc),
        )
        original_message = SimpleNamespace(
            channel=SimpleNamespace(
                id=10,
                send=AsyncMock(return_value=sent),
            ),
        )
        tool_call = SimpleNamespace(
            name="interim_message",
            arguments={"message": "let me search that up.."},
        )

        with patch("builtins.print"):
            result = await execute_tool_call(original_message, tool_call)

        payload = json.loads(result)
        original_message.channel.send.assert_awaited_once_with(
            "let me search that up..",
        )
        self.assertEqual("sent", payload["status"])
        self.assertEqual(555, payload["message_id"])
        self.assertEqual(10, payload["channel_id"])
        self.assertEqual(
            "2026-09-01T12:00:00+00:00",
            payload["created_at"],
        )

    async def test_rejects_empty_message(self):
        tool_call = SimpleNamespace(
            name="interim_message",
            arguments={"message": "   "},
        )

        with self.assertRaises(ValueError):
            await execute_tool_call(SimpleNamespace(channel=SimpleNamespace()), tool_call)


class ChannelHistoryToolTests(unittest.IsolatedAsyncioTestCase):
    def make_message(self, message_id, author, content, created_at=None):
        timestamp = created_at or datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
        return SimpleNamespace(
            id=message_id,
            author=SimpleNamespace(
                id=author,
                name=author,
                display_name=author,
                global_name=author,
            ),
            content=content,
            created_at=timestamp,
            reference=None,
            attachments=[],
        )

    def make_channel(self, messages):
        channel = SimpleNamespace(id=10, history_kwargs=None)

        def history(**kwargs):
            channel.history_kwargs = kwargs

            async def iterator():
                for message in messages:
                    yield message

            return iterator()

        channel.history = history
        return channel

    async def test_reads_current_channel_oldest_first(self):
        newer = self.make_message(2, "bushima", "later")
        older = self.make_message(1, "nabemono", "earlier")
        channel = self.make_channel([newer, older])
        original_message = SimpleNamespace(channel=channel)
        tool_call = SimpleNamespace(
            name="read_channel_history",
            arguments={"limit": 10},
        )

        with patch("builtins.print"):
            result = await execute_tool_call(original_message, tool_call)

        payload = json.loads(result)
        self.assertEqual(10, payload["channel_id"])
        self.assertEqual(["earlier", "later"], [item["content"] for item in payload["messages"]])
        self.assertEqual(10, channel.history_kwargs["limit"])
        self.assertIsNone(channel.history_kwargs["before"])
        self.assertNotIn("channel_id", tool_call.arguments)

    async def test_searches_current_channel_only(self):
        messages = [
            self.make_message(3, "bushima", "I want pizza"),
            self.make_message(2, "nabemono", "ok"),
            self.make_message(1, "bushima", "hello"),
        ]
        channel = self.make_channel(messages)
        tool_call = SimpleNamespace(
            name="read_channel_history",
            arguments={"query": "PIZZA", "limit": 5},
        )

        with patch("builtins.print"):
            result = await execute_tool_call(SimpleNamespace(channel=channel), tool_call)

        payload = json.loads(result)
        self.assertEqual("PIZZA", payload["query"])
        self.assertEqual(["I want pizza"], [item["content"] for item in payload["messages"]])
        self.assertGreaterEqual(channel.history_kwargs["limit"], 5)

    async def test_filters_by_author(self):
        messages = [
            self.make_message(2, "bushima", "mine"),
            self.make_message(1, "nabemono", "not mine"),
        ]
        channel = self.make_channel(messages)
        tool_call = SimpleNamespace(
            name="read_channel_history",
            arguments={"author": "bushima"},
        )

        with patch("builtins.print"):
            result = await execute_tool_call(SimpleNamespace(channel=channel), tool_call)

        payload = json.loads(result)
        self.assertEqual(["mine"], [item["content"] for item in payload["messages"]])


class SendFileToolTests(unittest.IsolatedAsyncioTestCase):
    def make_message(self):
        sent = SimpleNamespace(
            id=777,
            created_at=datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc),
        )
        channel = SimpleNamespace(id=10, send=AsyncMock(return_value=sent))
        return SimpleNamespace(channel=channel)

    async def test_sends_txt_file_to_current_channel(self):
        original_message = self.make_message()
        tool_call = SimpleNamespace(
            name="send_file",
            arguments={
                "filename": "../../secret notes.txt",
                "content": "channel summary",
                "format": "txt",
                "caption": "here you go",
            },
        )

        with patch("builtins.print"):
            result = await execute_tool_call(original_message, tool_call)

        payload = json.loads(result)
        send = original_message.channel.send
        send.assert_awaited_once()
        kwargs = send.await_args.kwargs
        self.assertEqual("here you go", kwargs["content"])
        self.assertEqual("secret notes.txt", kwargs["file"].filename)
        kwargs["file"].fp.seek(0)
        self.assertEqual(b"channel summary", kwargs["file"].fp.read())
        self.assertEqual("sent", payload["status"])
        self.assertEqual("secret notes.txt", payload["filename"])
        self.assertEqual("txt", payload["format"])
        self.assertEqual(10, payload["channel_id"])

    async def test_sends_docx_file(self):
        original_message = self.make_message()
        tool_call = SimpleNamespace(
            name="send_file",
            arguments={
                "filename": "summary",
                "content": "hello\nworld",
                "format": "docx",
            },
        )

        with patch("builtins.print"):
            result = await execute_tool_call(original_message, tool_call)

        payload = json.loads(result)
        uploaded = original_message.channel.send.await_args.kwargs["file"]
        uploaded.fp.seek(0)
        self.assertEqual(b"PK", uploaded.fp.read(2))
        self.assertEqual("summary.docx", uploaded.filename)
        self.assertEqual("docx", payload["format"])

    async def test_rejects_empty_content_and_unknown_format(self):
        original_message = self.make_message()
        with self.assertRaises(ValueError):
            await execute_tool_call(
                original_message,
                SimpleNamespace(
                    name="send_file",
                    arguments={"filename": "a.txt", "content": "  ", "format": "txt"},
                ),
            )
        with self.assertRaises(ValueError):
            await execute_tool_call(
                original_message,
                SimpleNamespace(
                    name="send_file",
                    arguments={"filename": "a.pdf", "content": "hi", "format": "pdf"},
                ),
            )


if __name__ == "__main__":
    unittest.main()
