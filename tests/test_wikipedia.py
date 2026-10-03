import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.wikipedia import (
    find_local_zim,
    get_page,
    parse_zim_filenames,
    pick_latest_zim,
    search_pages,
    _search_archive,
)
from src.tools import execute_tool_call


LISTING_HTML = """
<a href="wikipedia_en_all_mini_2026-03.zim">wikipedia_en_all_mini_2026-03.zim</a>
<a href="wikipedia%5Fen%5Fall%5Fmini%5F2026-06.zim">latest</a>
<a href="wikipedia_en_all_nopic_2026-06.zim">nopic</a>
<a href="wikipedia_en_all_mini_2026-06.zim.sha256">checksum</a>
"""


class FakeItem:
    def __init__(self, html):
        self.content = html.encode("utf-8")


class FakeEntry:
    def __init__(self, title, path, html="", redirect=None):
        self.title = title
        self.path = path
        self.is_redirect = redirect is not None
        self._html = html
        self._redirect = redirect

    def get_redirect_entry(self):
        return self._redirect

    def get_item(self):
        return FakeItem(self._html)


class FakeArchive:
    def __init__(self, entries):
        self.entries = {entry.path: entry for entry in entries}
        self.titles = {entry.title: entry for entry in entries}

    def has_entry_by_path(self, path):
        return path in self.entries

    def get_entry_by_path(self, path):
        return self.entries[path]

    def has_entry_by_title(self, title):
        return title in self.titles

    def get_entry_by_title(self, title):
        return self.titles[title]


class ParseZimListingTests(unittest.TestCase):
    def test_parses_and_unquotes_zim_names(self):
        names = parse_zim_filenames(LISTING_HTML)

        self.assertEqual(
            [
                "wikipedia_en_all_mini_2026-03.zim",
                "wikipedia_en_all_mini_2026-06.zim",
                "wikipedia_en_all_nopic_2026-06.zim",
            ],
            names,
        )

    def test_picks_latest_matching_prefix(self):
        latest = pick_latest_zim(parse_zim_filenames(LISTING_HTML), "wikipedia_en_all_mini")

        self.assertEqual("wikipedia_en_all_mini_2026-06.zim", latest)

    def test_finds_newest_local_zim(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            older = root / "wikipedia_en_all_mini_2026-03.zim"
            newer = root / "wikipedia_en_all_mini_2026-06.zim"
            older.write_bytes(b"old")
            newer.write_bytes(b"new")
            (root / "wikipedia_en_all_nopic_2026-06.zim").write_bytes(b"other")
            (root / "empty.zim").write_bytes(b"")

            found = find_local_zim(root, "wikipedia_en_all_mini")

            self.assertEqual(newer, found)

    def test_returns_none_when_directory_is_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertIsNone(find_local_zim(tmpdir, "wikipedia_en_all_mini"))


class WikipediaToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.einstein = FakeEntry(
            "Albert Einstein",
            "Albert_Einstein",
            "<html><head><title>Albert Einstein</title></head>"
            "<body><p>A physicist.</p></body></html>",
        )
        self.archive = FakeArchive([self.einstein])

    def test_search_archive_uses_suggestions_then_full_text(self):
        class FakeSuggestion:
            def getResults(self, start, count):
                return ["Albert_Einstein"][:count]

        class FakeSearch:
            def getResults(self, start, count):
                return []

        results = _search_archive(
            self.archive,
            "einstein",
            max_results=3,
            backends=(
                lambda archive: SimpleNamespace(suggest=lambda query: FakeSuggestion()),
                lambda archive: SimpleNamespace(search=lambda query: FakeSearch()),
                lambda: SimpleNamespace(set_query=lambda query: query),
            ),
        )

        self.assertEqual("Albert Einstein", results[0]["title"])
        self.assertEqual("Albert_Einstein", results[0]["path"])

    def test_search_pages_returns_archive_results(self):
        with (
            patch("src.wikipedia._archive", self.archive),
            patch(
                "src.wikipedia._state",
                {"ready": True, "downloading": False, "error": None},
            ),
            patch(
                "src.wikipedia._search_archive",
                return_value=[{
                    "title": "Albert Einstein",
                    "path": "Albert_Einstein",
                }],
            ),
        ):
            summary = search_pages("einstein", max_results=3)

        self.assertEqual("einstein", summary["query"])
        self.assertEqual("Albert Einstein", summary["results"][0]["title"])

    def test_get_page_extracts_readable_text(self):
        with (
            patch("src.wikipedia._archive", self.archive),
            patch("src.wikipedia._state", {"ready": True, "downloading": False, "error": None}),
        ):
            page = get_page("Albert Einstein")

        self.assertEqual("Albert_Einstein", page["url"])
        self.assertEqual("Albert Einstein", page["title"])
        self.assertIn("A physicist.", page["content"])

    def test_reports_when_wikipedia_is_not_ready(self):
        with patch("src.wikipedia._archive", None), patch(
            "src.wikipedia._state",
            {"ready": False, "downloading": True, "downloaded_bytes": 10 * 1024 * 1024, "error": None},
        ):
            summary = search_pages("einstein")
            page = get_page("Albert Einstein")

        self.assertIn("still downloading", summary["note"])
        self.assertIn("still downloading", page["note"])

    async def test_execute_tool_call_search_and_page(self):
        with (
            patch("src.wikipedia.search_pages", return_value={
                "query": "einstein",
                "results": [{"title": "Albert Einstein", "path": "Albert_Einstein"}],
            }),
            patch("src.wikipedia.get_page", return_value={
                "url": "Albert_Einstein",
                "title": "Albert Einstein",
                "content": "A physicist.",
            }),
            patch("builtins.print"),
        ):
            search_result = await execute_tool_call(
                SimpleNamespace(),
                SimpleNamespace(name="wikipedia_search", arguments={"query": "einstein"}),
            )
            page_result = await execute_tool_call(
                SimpleNamespace(),
                SimpleNamespace(name="wikipedia_page", arguments={"title": "Albert Einstein"}),
            )

        self.assertEqual("Albert Einstein", json.loads(search_result)["results"][0]["title"])
        self.assertEqual("A physicist.", json.loads(page_result)["content"])


class EnsureWikipediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from src import wikipedia as wiki

        self.wiki = wiki
        self.original_state = dict(wiki._state)

    def tearDown(self):
        self.wiki._state.clear()
        self.wiki._state.update(self.original_state)

    async def test_opens_existing_local_zim_without_download(self):
        from src.wikipedia import ensure_wikipedia

        with tempfile.TemporaryDirectory() as tmpdir:
            local = Path(tmpdir) / "wikipedia_en_all_mini_2026-06.zim"
            local.write_bytes(b"zim")

            with (
                patch("src.wikipedia._archive", None),
                patch("src.wikipedia._archive_path", None),
                patch("src.wikipedia._open_archive") as open_archive,
                patch("src.wikipedia._download_file", new=AsyncMock()) as download_file,
            ):
                path = await ensure_wikipedia(tmpdir, "wikipedia_en_all_mini")

            self.assertEqual(local, path)
            open_archive.assert_called_once_with(local)
            download_file.assert_not_awaited()

    async def test_downloads_latest_zim_when_missing(self):
        from src.wikipedia import ensure_wikipedia

        with tempfile.TemporaryDirectory() as tmpdir:
            destination = Path(tmpdir) / "wikipedia_en_all_mini_2026-06.zim"

            async def fake_download(url, dest):
                Path(dest).write_bytes(b"zim")
                return Path(dest)

            with (
                patch("src.wikipedia._archive", None),
                patch("src.wikipedia._archive_path", None),
                patch(
                    "src.wikipedia._latest_remote_zim",
                    new=AsyncMock(return_value=(
                        "wikipedia_en_all_mini_2026-06.zim",
                        "https://lb.download.kiwix.org/zim/wikipedia/wikipedia_en_all_mini_2026-06.zim",
                    )),
                ),
                patch("src.wikipedia._download_file", new=AsyncMock(side_effect=fake_download)) as download_file,
                patch("src.wikipedia._open_archive") as open_archive,
            ):
                path = await ensure_wikipedia(tmpdir, "wikipedia_en_all_mini")

            self.assertEqual(destination, path)
            download_file.assert_awaited_once()
            open_archive.assert_called_once_with(destination)

    async def test_download_sends_descriptive_user_agent(self):
        from src.wikipedia import DOWNLOAD_HEADERS, _download_file

        captured = {}

        async def chunks(size):
            yield b"zim"

        class FakeResponse:
            status = 200
            content = SimpleNamespace(iter_chunked=chunks)

            def raise_for_status(self):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class FakeSession:
            def __init__(self, *args, **kwargs):
                pass

            def get(self, url, headers=None):
                captured["url"] = url
                captured["headers"] = headers
                return FakeResponse()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        with tempfile.TemporaryDirectory() as tmpdir:
            destination = Path(tmpdir) / "wikipedia_en_all_mini_2026-06.zim"
            with patch("src.wikipedia.aiohttp.ClientSession", FakeSession):
                await _download_file(
                    "https://dumps.wikimedia.org/kiwix/zim/wikipedia/wikipedia_en_all_mini_2026-06.zim",
                    destination,
                )
            self.assertTrue(destination.exists())

        self.assertEqual(
            DOWNLOAD_HEADERS["User-Agent"],
            captured["headers"]["User-Agent"],
        )


if __name__ == "__main__":
    unittest.main()
