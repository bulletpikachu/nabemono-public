import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet

from src.canvas.models import CanvasEvent
from src.canvas.store import CanvasError, CanvasStore, FeedCipher


NOW = datetime(2026, 9, 26, 14, 0, tzinfo=timezone.utc)
NEXT = datetime(2026, 9, 27, 14, 0, tzinfo=timezone.utc)


class CanvasStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db_path = Path(directory.name) / "canvas.sqlite3"
        self.store = CanvasStore(
            self.db_path,
            cipher=FeedCipher(Fernet.generate_key()),
            max_lookahead_days=14,
        )

    def connect(self, user_id=1, url="https://canvas.example.edu/private.ics"):
        return self.store.upsert_connection(
            user_id=user_id,
            feed_url=url,
            timezone_name="America/Los_Angeles",
            lookahead_days=7,
            next_summary_at=NEXT,
        )

    def test_encrypts_feed_url_at_rest_and_decrypts_for_use(self):
        connection = self.connect()

        with closing(sqlite3.connect(self.db_path)) as database:
            saved = database.execute(
                "SELECT encrypted_feed_url FROM canvas_connections WHERE user_id = 1"
            ).fetchone()[0]

        self.assertNotIn("canvas.example.edu", saved)
        self.assertEqual("https://canvas.example.edu/private.ics", connection.feed_url)

    def test_requires_an_encryption_key_before_saving(self):
        store = CanvasStore(
            self.db_path,
            cipher=FeedCipher(None),
        )
        with self.assertRaisesRegex(CanvasError, "CANVAS_ENCRYPTION_KEY"):
            store.upsert_connection(
                user_id=1,
                feed_url="https://canvas.example.edu/private.ics",
                timezone_name="UTC",
                lookahead_days=1,
                next_summary_at=NEXT,
            )

    def test_replaces_events_per_user_without_touching_other_users(self):
        self.connect(1)
        self.connect(2, "https://canvas.example.edu/other.ics")
        first = CanvasEvent(
            uid="assignment_1",
            title="Essay",
            due_at=NEXT,
            due_date=None,
            all_day=False,
            kind="assignment",
        )
        second = CanvasEvent(
            uid="assignment_2",
            title="Lab",
            due_at=None,
            due_date=date(2026, 9, 28),
            all_day=True,
            kind="assignment",
        )

        self.store.replace_events(1, [first], fetched_at=NOW)
        self.store.replace_events(2, [second], fetched_at=NOW)
        self.store.replace_events(1, [], fetched_at=NEXT)

        self.assertEqual([], self.store.list_events(1))
        self.assertEqual([second], self.store.list_events(2))

    def test_disconnect_removes_connection_and_cached_events(self):
        self.connect()
        self.store.replace_events(
            1,
            [
                CanvasEvent(
                    uid="assignment_1",
                    title="Essay",
                    due_at=NEXT,
                    due_date=None,
                    all_day=False,
                )
            ],
            fetched_at=NOW,
        )

        self.assertTrue(self.store.disconnect(1))

        self.assertIsNone(self.store.get_connection(1))
        self.assertEqual([], self.store.list_events(1))
        self.assertFalse(self.store.disconnect(1))

    def test_enforces_lookahead_limit(self):
        with self.assertRaisesRegex(CanvasError, "between 1 and 14"):
            self.store.upsert_connection(
                user_id=1,
                feed_url="https://canvas.example.edu/private.ics",
                timezone_name="UTC",
                lookahead_days=15,
                next_summary_at=NEXT,
            )


if __name__ == "__main__":
    unittest.main()

