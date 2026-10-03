import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.music.models import Track
from src.music.telemetry import MusicTelemetry


class MusicTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.store = MusicTelemetry(
            Path(self.temporary_directory.name) / "music_telemetry.sqlite3"
        )

    def test_counts_requested_played_and_unique_users(self):
        first = Track(
            url="https://www.youtube.com/watch?v=trackone111",
            title="One",
            duration=90,
        )
        second = Track(
            url="https://www.youtube.com/watch?v=tracktwo222",
            title="Two",
            duration=120,
        )
        alice = SimpleNamespace(id=1, display_name="Alice")
        bob = SimpleNamespace(id=2, name="Bob")

        self.store.record("requested", guild_id=10, user=alice, track=first)
        self.store.record("played", guild_id=10, user=alice, track=first)
        self.store.record("requested", guild_id=10, user=bob, track=second)
        self.store.record("skipped", guild_id=10, user=bob, track=second)
        self.store.record("searched", guild_id=10, user=alice, query="lofi")

        summary = self.store.summary()
        self.assertEqual(2, summary["tracks_requested"])
        self.assertEqual(1, summary["tracks_played"])
        self.assertEqual(1, summary["tracks_skipped"])
        self.assertEqual(0, summary["tracks_failed"])
        self.assertEqual(1, summary["searches"])
        self.assertEqual(2, summary["unique_users"])
        self.assertEqual(2, summary["unique_tracks_requested"])

    def test_record_failures_do_not_raise(self):
        with (
            patch.object(self.store, "_connect", side_effect=OSError("disk full")),
            patch("src.music.telemetry.logger.exception"),
        ):
            self.store.record("requested", track=Track(url="https://youtu.be/x"))


if __name__ == "__main__":
    unittest.main()
