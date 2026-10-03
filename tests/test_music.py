import asyncio
import shlex
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from discord.ext import commands

from src.music import (
    EnqueueResult,
    MusicManager,
    MusicSession,
    Track,
    YoutubeSelection,
    build_ffmpeg_before_options,
    format_duration,
    format_track_label,
    is_youtube_playlist_url,
    is_youtube_url,
    remaining_queue_seconds,
    track_from_youtube_entry,
)
from src.music.commands import play_confirmation, register_music_commands
from src.music.radio import RADIO_STATIONS
from src.music.youtube import search_youtube_tracks


class YouTubeUrlTests(unittest.TestCase):
    def test_accepts_supported_https_youtube_urls(self):
        self.assertTrue(
            is_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        )
        self.assertTrue(is_youtube_url("https://youtu.be/dQw4w9WgXcQ"))
        self.assertTrue(is_youtube_url("https://music.youtube.com/watch?v=abc"))
        self.assertTrue(
            is_youtube_url("https://www.youtube.com/playlist?list=PLtest")
        )

    def test_rejects_non_youtube_and_unsafe_urls(self):
        self.assertFalse(is_youtube_url("http://youtube.com/watch?v=abc"))
        self.assertFalse(
            is_youtube_url("https://youtube.com.example.com/watch?v=abc")
        )
        self.assertFalse(is_youtube_url("not a url"))


class YouTubePlaylistUrlTests(unittest.TestCase):
    def test_detects_playlist_links(self):
        self.assertTrue(
            is_youtube_playlist_url(
                "https://www.youtube.com/playlist?list=PLrAXtmRdnEQy6nuLMO"
            )
        )
        self.assertTrue(
            is_youtube_playlist_url(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
                "&list=PLrAXtmRdnEQy6nuLMO"
            )
        )
        self.assertTrue(
            is_youtube_playlist_url(
                "https://youtu.be/dQw4w9WgXcQ?list=PLrAXtmRdnEQy6nuLMO"
            )
        )

    def test_rejects_single_videos_and_mixes(self):
        self.assertFalse(
            is_youtube_playlist_url(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
            )
        )
        self.assertFalse(
            is_youtube_playlist_url(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
                "&list=RDdQw4w9WgXcQ"
            )
        )


class TrackParsingTests(unittest.TestCase):
    def test_builds_watch_url_from_video_id(self):
        track = track_from_youtube_entry(
            {
                "id": "dQw4w9WgXcQ",
                "title": "Never Gonna Give You Up",
                "duration": 213,
                "url": "dQw4w9WgXcQ",
            }
        )

        assert track is not None
        self.assertEqual(
            track.url,
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )
        self.assertEqual(track.title, "Never Gonna Give You Up")
        self.assertEqual(track.duration, 213)

    def test_skips_nested_playlists(self):
        self.assertIsNone(
            track_from_youtube_entry({"_type": "playlist", "id": "PLtest"})
        )


class YouTubeSearchTests(unittest.TestCase):
    def test_returns_playable_tracks_from_search_results(self):
        captured = {}

        class FakeDownloader:
            def __init__(self, options):
                captured["options"] = options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=False):
                captured["url"] = url
                captured["download"] = download
                return {
                    "entries": [
                        {
                            "id": "dQw4w9WgXcQ",
                            "title": "Never Gonna Give You Up",
                            "duration": 213,
                        }
                    ]
                }

        with patch(
            "src.music.youtube.yt_dlp.YoutubeDL",
            FakeDownloader,
        ):
            tracks = search_youtube_tracks("Rick Astley", max_results=3)

        self.assertEqual("ytsearch3:Rick Astley", captured["url"])
        self.assertFalse(captured["download"])
        self.assertTrue(captured["options"]["extract_flat"])
        self.assertEqual(1, len(tracks))
        self.assertEqual("Never Gonna Give You Up", tracks[0].title)
        self.assertEqual(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            tracks[0].url,
        )


class DurationFormatTests(unittest.TestCase):
    def test_formats_minutes_and_hours(self):
        self.assertEqual(format_duration(0), "0:00")
        self.assertEqual(format_duration(65), "1:05")
        self.assertEqual(format_duration(3723), "1:02:03")
        self.assertIsNone(format_duration(None))

    def test_labels_current_and_queued_tracks(self):
        track = Track(
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            title="Never Gonna Give You Up",
            duration=213,
        )
        self.assertEqual(
            format_track_label(track, elapsed=65),
            "Never Gonna Give You Up (1:05/3:33)",
        )
        self.assertEqual(
            format_track_label(track),
            "Never Gonna Give You Up (3:33)",
        )


class RemainingQueueTests(unittest.TestCase):
    def test_includes_remaining_current_and_queued_durations(self):
        current = Track(url="https://youtu.be/current", duration=200)
        queued = [
            Track(url="https://youtu.be/one", duration=90),
            Track(url="https://youtu.be/two", duration=120),
        ]

        remaining, complete = remaining_queue_seconds(current, queued, elapsed=65)

        self.assertTrue(complete)
        self.assertEqual(345, remaining)

    def test_marks_incomplete_when_a_duration_is_missing(self):
        remaining, complete = remaining_queue_seconds(
            Track(url="https://youtu.be/current", duration=100),
            [Track(url="https://youtu.be/unknown")],
            elapsed=20,
        )

        self.assertFalse(complete)
        self.assertEqual(80, remaining)


class FfmpegOptionsTests(unittest.TestCase):
    def test_forwards_youtube_headers_to_ffmpeg(self):
        options = build_ffmpeg_before_options(
            {
                "User-Agent": "Mozilla/5.0 Test",
                "Referer": "https://www.youtube.com/",
                "Cookie": "example=value",
            }
        )
        arguments = shlex.split(options)

        self.assertEqual(
            arguments[arguments.index("-user_agent") + 1],
            "Mozilla/5.0 Test",
        )
        headers = arguments[arguments.index("-headers") + 1]
        self.assertIn("Referer: https://www.youtube.com/\r\n", headers)
        self.assertIn("Cookie: example=value\r\n", headers)

    def test_rejects_unsafe_header_values(self):
        options = build_ffmpeg_before_options(
            {
                "Host": "malicious.example",
                "Bad Header": "value",
                "Referer": "safe\r\nInjected: value",
            }
        )

        self.assertNotIn("malicious.example", options)
        self.assertNotIn("Injected", options)


class QueueManagementTests(unittest.TestCase):
    def setUp(self):
        self.session = MusicSession(
            SimpleNamespace(id=1, voice_client=None),
            lambda: None,
        )
        self.session.current = Track(
            url="https://www.youtube.com/watch?v=current1111",
            title="Current",
            duration=200,
        )
        self.session.queue.extend(
            [
                Track(
                    url="https://www.youtube.com/watch?v=queued11111",
                    title="First",
                    duration=90,
                ),
                Track(
                    url="https://www.youtube.com/watch?v=queued22222",
                    title="Second",
                    duration=120,
                ),
                Track(
                    url="https://www.youtube.com/watch?v=queued33333",
                    title="Third",
                    duration=45,
                ),
            ]
        )

    def test_describe_queue_includes_elapsed_and_durations(self):
        self.session.playback_started_at = time.monotonic() - 65
        description = self.session.describe_queue()
        elapsed = format_duration(self.session.elapsed_seconds())

        self.assertIn(
            "Now playing: "
            f"[Current ({elapsed}/3:20)](<https://www.youtube.com/watch?v=current1111>)",
            description,
        )
        self.assertIn(
            "1. [First (1:30)](<https://www.youtube.com/watch?v=queued11111>)",
            description,
        )
        self.assertIn(
            "2. [Second (2:00)](<https://www.youtube.com/watch?v=queued22222>)",
            description,
        )
        self.assertIn(
            "3. [Third (0:45)](<https://www.youtube.com/watch?v=queued33333>)",
            description,
        )
        remaining, complete = remaining_queue_seconds(
            self.session.current,
            self.session.queue,
            elapsed=self.session.elapsed_seconds(),
        )
        self.assertTrue(complete)
        self.assertIn(f"Queue length: {format_duration(remaining)}", description)

    def test_remove_uses_one_based_queue_positions(self):
        removed = self.session.remove_from_queue(2)

        assert removed is not None
        self.assertEqual(removed.title, "Second")
        self.assertEqual(
            [track.title for track in self.session.queue],
            ["First", "Third"],
        )
        self.assertIsNone(self.session.remove_from_queue(9))

    def test_swap_exchanges_one_based_queue_positions(self):
        self.assertTrue(self.session.swap_queued(1, 3))
        self.assertEqual(
            [track.title for track in self.session.queue],
            ["Third", "Second", "First"],
        )
        self.assertFalse(self.session.swap_queued(1, 9))

    def test_move_reinserts_track_at_one_based_position(self):
        self.assertTrue(self.session.move_queued(1, 3))
        self.assertEqual(
            [track.title for track in self.session.queue],
            ["Second", "Third", "First"],
        )
        self.assertFalse(self.session.move_queued(1, 9))

    def test_snapshot_returns_structured_queue_state(self):
        self.session.playback_started_at = time.monotonic() - 10

        snapshot = self.session.snapshot()

        self.assertEqual("playing", snapshot["status"])
        self.assertEqual("Current", snapshot["current"]["title"])
        self.assertGreaterEqual(snapshot["current"]["elapsed_seconds"], 10)
        self.assertEqual(1, snapshot["queue"][0]["position"])
        self.assertEqual("First", snapshot["queue"][0]["title"])
        self.assertGreater(snapshot["remaining_seconds"], 0)
        self.assertTrue(snapshot["remaining_complete"])


class PlayConfirmationTests(unittest.TestCase):
    def test_mentions_playlist_when_several_tracks_are_added(self):
        selection = YoutubeSelection(
            tracks=[Track(url="https://youtu.be/dQw4w9WgXcQ")] * 3,
            playlist_title="Road Trip",
        )
        queued = play_confirmation(
            EnqueueResult(started=False, count=3, position=1),
            selection,
        )
        started = play_confirmation(
            EnqueueResult(started=True, count=3, position=0),
            selection,
        )

        self.assertEqual(
            "Added 3 tracks to the queue from **Road Trip**.",
            queued,
        )
        self.assertEqual("Playing 3 tracks from **Road Trip**.", started)


class FakeChannel:
    def __init__(self, channel_id):
        self.id = channel_id
        self.connect_calls = 0

    async def connect(self, **kwargs):
        self.connect_calls += 1
        return FakeVoiceClient(self)


class FakeVoiceClient:
    def __init__(self, channel):
        self.channel = channel
        self.connected = True
        self.moved_to = None

    def is_connected(self):
        return self.connected

    async def move_to(self, channel, timeout=None):
        self.moved_to = channel
        self.channel = channel

    def is_playing(self):
        return False

    def is_paused(self):
        return False

    def stop(self):
        pass

    async def disconnect(self, force=False):
        self.connected = False


class ConnectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = SimpleNamespace(id=1, voice_client=None)
        self.session = MusicSession(self.guild, lambda: None)

    async def test_connects_when_not_already_in_voice(self):
        channel = FakeChannel(10)

        status = await self.session.connect(channel)

        self.assertEqual("joined", status)
        self.assertEqual(1, channel.connect_calls)
        self.assertIs(self.session.voice_client.channel, channel)

    async def test_reuses_connection_in_the_same_channel(self):
        channel = FakeChannel(10)
        existing = FakeVoiceClient(channel)
        self.guild.voice_client = existing

        status = await self.session.connect(channel)

        self.assertEqual("already_here", status)
        self.assertEqual(0, channel.connect_calls)
        self.assertIs(self.session.voice_client, existing)

    async def test_refuses_to_switch_channels_without_move(self):
        current = FakeChannel(10)
        other = FakeChannel(20)
        self.guild.voice_client = FakeVoiceClient(current)

        with self.assertRaisesRegex(
            RuntimeError,
            "already playing in another voice channel",
        ):
            await self.session.connect(other)

        self.assertIsNone(self.guild.voice_client.moved_to)

    async def test_moves_to_another_channel_when_requested(self):
        current = FakeChannel(10)
        other = FakeChannel(20)
        existing = FakeVoiceClient(current)
        self.guild.voice_client = existing

        status = await self.session.connect(other, move=True)

        self.assertEqual("moved", status)
        self.assertIs(existing.moved_to, other)
        self.assertIs(self.session.voice_client, existing)
        self.assertEqual(0, other.connect_calls)

    async def test_manager_indexes_and_expires_session_token(self):
        manager = MusicManager()
        guild = SimpleNamespace(id=5, voice_client=None)
        session = manager.get_or_create(guild)

        await session.connect(FakeChannel(10))

        self.assertIsNotNone(session.token)
        self.assertIs(manager.get_by_token(session.token), session)
        token = session.token
        await session.close()
        self.assertIsNone(manager.get_by_token(token))


class IdleDisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnects_after_thirty_minutes_idle(self):
        on_close = Mock()
        session = MusicSession(
            SimpleNamespace(id=1, voice_client=None),
            on_close,
            idle_timeout_seconds=0,
        )
        session.text_channel = SimpleNamespace(send=AsyncMock())

        await session.connect(FakeChannel(10))
        idle_task = session.idle_disconnect_task
        self.assertIsNotNone(idle_task)
        await idle_task

        self.assertTrue(session.closed)
        self.assertFalse(session.voice_client.is_connected())
        on_close.assert_called_once_with()
        session.text_channel.send.assert_awaited_once()
        self.assertEqual(
            "Left the voice channel after 30 minutes of inactivity.",
            session.text_channel.send.await_args.args[0],
        )

    async def test_enqueuing_track_cancels_idle_disconnect(self):
        session = MusicSession(
            SimpleNamespace(id=1, voice_client=None),
            lambda: None,
        )
        await session.connect(FakeChannel(10))
        idle_task = session.idle_disconnect_task
        self.assertIsNotNone(idle_task)
        track = Track(url="https://youtu.be/queued", title="Queued")

        with patch.object(session, "_start_track", new_callable=AsyncMock):
            await session.enqueue_tracks([track], None)
            await asyncio.sleep(0)

        self.assertIsNone(session.idle_disconnect_task)
        self.assertTrue(idle_task.cancelled())
        self.assertFalse(session.closed)
        await session.close()

    async def test_finishing_last_track_starts_idle_disconnect(self):
        session = MusicSession(
            SimpleNamespace(id=1, voice_client=None),
            lambda: None,
        )
        await session.connect(FakeChannel(10))
        track = Track(url="https://youtu.be/current", title="Current")

        with patch.object(session, "_start_track", new_callable=AsyncMock):
            await session.enqueue_tracks([track], None)
            await session._finish_track(track, None)

        self.assertIsNotNone(session.idle_disconnect_task)
        await session.close()


class LocalTrackCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_remove_and_close_delete_local_files(self):
        with tempfile.TemporaryDirectory() as directory:
            first_path = Path(directory) / "first.mp3"
            second_path = Path(directory) / "second.mp3"
            first_path.write_bytes(b"first")
            second_path.write_bytes(b"second")
            session = MusicSession(
                SimpleNamespace(id=1, voice_client=None),
                lambda: None,
            )
            session.queue.extend(
                [
                    Track(
                        url="local:first",
                        title="First",
                        source="local",
                        path=str(first_path),
                    ),
                    Track(
                        url="local:second",
                        title="Second",
                        source="local",
                        path=str(second_path),
                    ),
                ]
            )

            await session.remove_queued(1)
            self.assertFalse(first_path.exists())
            self.assertTrue(second_path.exists())

            await session.close()
            self.assertFalse(second_path.exists())

    async def test_skip_deletes_current_local_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "current.mp3"
            path.write_bytes(b"audio")
            session = MusicSession(
                SimpleNamespace(id=1, voice_client=None),
                lambda: None,
            )
            session.current = Track(
                url="local:current",
                title="Current",
                source="local",
                path=str(path),
            )

            self.assertTrue(await session.skip())
            self.assertFalse(path.exists())
            self.assertIsNone(session.current)

    async def test_failed_track_deletes_current_local_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "failed.mp3"
            path.write_bytes(b"audio")
            session = MusicSession(
                SimpleNamespace(id=1, voice_client=None),
                lambda: None,
            )
            track = Track(
                url="local:failed",
                title="Failed",
                source="local",
                path=str(path),
            )
            session.current = track

            await session._finish_track(track, RuntimeError("failed"))

            self.assertFalse(path.exists())
            self.assertIsNone(session.current)


class FakeRadioVoiceClient:
    def __init__(self):
        self.stopped = 0

    def is_connected(self):
        return True

    def is_playing(self):
        return True

    def is_paused(self):
        return False

    def stop(self):
        self.stopped += 1


class RadioPlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_radio_preserves_and_resumes_interrupted_track_position(self):
        session = MusicSession(
            SimpleNamespace(id=1, voice_client=None),
            lambda: None,
        )
        session.voice_client = FakeRadioVoiceClient()
        track = Track(
            url="https://youtu.be/current",
            title="Interrupted",
            duration=180,
        )
        session.current = track
        session.playback_started_at = time.monotonic() - 42
        station = RADIO_STATIONS[0]

        with (
            patch.object(session, "_start_radio", new_callable=AsyncMock),
            patch.object(session, "_start_track", new_callable=AsyncMock) as start_track,
        ):
            self.assertEqual("playing", await session.start_radio(station))
            await asyncio.sleep(0)
            snapshot = session.snapshot()
            self.assertEqual("radio", snapshot["status"])
            self.assertEqual("KDFC", snapshot["radio"]["id"])
            self.assertNotIn("stream_url", snapshot["radio"])
            self.assertIsNone(session.current)
            self.assertIs(track, session.interrupted_track)

            self.assertTrue(await session.stop_radio())
            await asyncio.sleep(0)
            await session._finish_track(track, None, generation=0)

        self.assertIs(track, session.current)
        self.assertIsNone(session.radio_station)
        self.assertGreaterEqual(
            start_track.await_args.kwargs["start_at"],
            42,
        )

    async def test_tracks_added_during_radio_wait_in_queue(self):
        session = MusicSession(
            SimpleNamespace(id=1, voice_client=None),
            lambda: None,
        )
        session.radio_station = RADIO_STATIONS[0]
        track = Track(url="https://youtu.be/queued", title="Queued")

        result = await session.enqueue_tracks([track], None)

        self.assertFalse(result.started)
        self.assertIsNone(session.current)
        self.assertEqual([track], list(session.queue))


class CommandRegistrationTests(unittest.TestCase):
    def test_registers_all_music_commands(self):
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())

        group = register_music_commands(bot)

        self.assertEqual(
            {"join", "leave", "pause", "play", "queue", "resume", "skip"},
            {command.name for command in group.commands},
        )
        queue_group = group.get_command("queue")
        assert isinstance(queue_group, discord.app_commands.Group)
        self.assertEqual(
            {"remove", "show", "swap"},
            {command.name for command in queue_group.commands},
        )


if __name__ == "__main__":
    unittest.main()
