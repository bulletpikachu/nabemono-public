import tempfile
import unittest
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet

from src.canvas.client import CanvasFetchError
from src.canvas.models import CanvasEvent, FetchResult
from src.canvas.scheduler import CanvasScheduler, RETRY_SECONDS
from src.canvas.service import (
    CanvasService,
    format_summary,
    next_summary_at,
    upcoming_payload,
)
from src.canvas.store import CanvasStore, FeedCipher


NOW = datetime(2026, 9, 26, 7, 0, tzinfo=timezone.utc)
DUE = datetime(2026, 9, 26, 16, 0, tzinfo=timezone.utc)


class CanvasSchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = CanvasStore(
            Path(directory.name) / "canvas.sqlite3",
            cipher=FeedCipher(Fernet.generate_key()),
        )
        self.http = SimpleNamespace(fetch=AsyncMock())
        self.service = CanvasService(
            self.store,
            self.http,
            cache_seconds=300,
            summary_time="07:00",
        )
        self.scheduler = CanvasScheduler(self.store, self.service)
        self.user = SimpleNamespace(send=AsyncMock())
        self.scheduler.client = SimpleNamespace(
            get_user=lambda user_id: self.user,
            fetch_user=AsyncMock(),
        )
        self.event = CanvasEvent(
            uid="assignment_1",
            title="Essay",
            due_at=DUE,
            due_date=None,
            all_day=False,
            url="https://canvas.example.edu/courses/1/assignments/1",
            course="English",
            kind="assignment",
        )

    def connect(self, *, with_cache=True):
        connection = self.store.upsert_connection(
            user_id=1,
            feed_url="https://canvas.example.edu/private.ics",
            timezone_name="UTC",
            lookahead_days=1,
            next_summary_at=NOW,
        )
        if with_cache:
            self.store.replace_events(
                1,
                [self.event],
                fetched_at=NOW - timedelta(hours=1),
            )
            connection = self.store.get_connection(1)
        return connection

    async def test_refreshes_before_dm_and_does_not_send_twice(self):
        self.connect()
        self.http.fetch.return_value = FetchResult(events=(self.event,))

        await self.scheduler.run_due(NOW)
        await self.scheduler.run_due(NOW)

        self.http.fetch.assert_awaited_once()
        self.user.send.assert_awaited_once()
        message = self.user.send.await_args.args[0]
        self.assertIn("Assignments due today", message)
        self.assertIn("Essay", message)
        connection = self.store.get_connection(1)
        self.assertEqual(NOW.date(), connection.last_summary_date)
        self.assertGreater(connection.next_summary_at, NOW)

    async def test_uses_cached_events_with_warning_when_refresh_fails(self):
        connection = self.connect()
        self.http.fetch.side_effect = CanvasFetchError("Canvas is unavailable")

        await self.scheduler.deliver(connection, now=NOW)

        message = self.user.send.await_args.args[0]
        self.assertIn("Essay", message)
        self.assertIn("last saved feed", message)
        self.assertEqual(NOW.date(), self.store.get_connection(1).last_summary_date)

    async def test_retries_when_refresh_fails_without_a_cache(self):
        connection = self.connect(with_cache=False)
        self.http.fetch.side_effect = CanvasFetchError("Canvas is unavailable")

        await self.scheduler.deliver(connection, now=NOW)

        self.user.send.assert_not_awaited()
        self.assertEqual(
            NOW + timedelta(seconds=RETRY_SECONDS),
            self.store.get_connection(1).next_summary_at,
        )

    async def test_tool_payload_redacts_feed_url_and_multi_day_summary_has_dates(self):
        self.connect()
        self.http.fetch.return_value = FetchResult(events=(self.event,))

        result = await self.service.upcoming(
            1,
            days=2,
            force_refresh=True,
            now=NOW,
        )

        payload = json.dumps(upcoming_payload(result))
        self.assertNotIn("private.ics", payload)
        self.assertNotIn("feed_url", payload)
        self.assertIn("__Today__", format_summary(result, now=NOW))

    def test_next_summary_keeps_seven_am_across_dst(self):
        before_dst = datetime(2026, 3, 7, 16, 0, tzinfo=timezone.utc)

        result = next_summary_at(
            before_dst,
            "America/Los_Angeles",
            summary_time="07:00",
        )

        self.assertEqual(
            datetime(2026, 3, 8, 14, 0, tzinfo=timezone.utc),
            result,
        )


if __name__ == "__main__":
    unittest.main()

