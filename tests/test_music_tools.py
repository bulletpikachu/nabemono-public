import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.music.models import EnqueueResult, Track, YoutubeSelection
from src.music.tools import music_play, music_queue, youtube_search
from src.tools import available_functions, execute_tool_call


class MusicQueueToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_empty_queue_for_new_guild(self):
        manager = SimpleNamespace(get=Mock(return_value=None))
        message = SimpleNamespace(guild=SimpleNamespace(id=42))

        result = await music_queue(message, manager=manager)

        payload = json.loads(result)
        self.assertEqual(42, payload["guild_id"])
        self.assertEqual("empty", payload["status"])
        self.assertIsNone(payload["current"])
        self.assertEqual([], payload["queue"])

    async def test_returns_shared_session_snapshot(self):
        snapshot = {
            "status": "playing",
            "current": {"title": "Current"},
            "queue": [{"position": 1, "title": "Next"}],
        }
        session = SimpleNamespace(snapshot=Mock(return_value=snapshot))
        manager = SimpleNamespace(get=Mock(return_value=session))
        message = SimpleNamespace(guild=SimpleNamespace(id=42))

        result = await music_queue(message, manager=manager)

        payload = json.loads(result)
        self.assertEqual("Current", payload["current"]["title"])
        self.assertEqual("Next", payload["queue"][0]["title"])

    async def test_rejects_direct_messages(self):
        message = SimpleNamespace(guild=None)

        with self.assertRaisesRegex(ValueError, "only be used in a server"):
            await music_queue(message)


class MusicPlayToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guild = SimpleNamespace(id=42)
        self.channel = SimpleNamespace(id=100, send=AsyncMock())
        self.voice_channel = SimpleNamespace(id=200, guild=self.guild)
        self.message = SimpleNamespace(
            guild=self.guild,
            author=SimpleNamespace(),
            channel=self.channel,
        )
        self.session = SimpleNamespace(
            connect=AsyncMock(),
            enqueue=Mock(
                return_value=EnqueueResult(
                    started=False,
                    count=1,
                    position=2,
                )
            ),
            current=Track(url="https://youtu.be/dQw4w9WgXcQ"),
            queue=[],
            token="controller-token",
            snapshot=Mock(
                return_value={
                    "status": "playing",
                    "current": {"title": "Current"},
                    "queue": [{"position": 1, "title": "Next"}],
                }
            ),
        )
        self.manager = SimpleNamespace(
            get_or_create=Mock(return_value=self.session),
            get=Mock(return_value=self.session),
        )

    async def test_queues_youtube_url_and_returns_structured_result(self):
        selection = YoutubeSelection(
            tracks=[
                Track(
                    url="https://youtu.be/dQw4w9WgXcQ",
                    title="A Song",
                    duration=213,
                )
            ]
        )
        with (
            patch(
                "src.music.tools.voice_channel_from_user",
                return_value=self.voice_channel,
            ),
            patch(
                "src.music.service.discover_youtube_tracks",
                return_value=selection,
            ),
        ):
            result = await music_play(
                self.message,
                "https://youtu.be/dQw4w9WgXcQ",
                manager=self.manager,
            )

        payload = json.loads(result)
        self.session.connect.assert_awaited_once_with(self.voice_channel)
        self.session.enqueue.assert_called_once_with(
            selection.tracks,
            self.channel,
            requester=self.message.author,
        )
        self.assertEqual("queued", payload["status"])
        self.assertEqual(1, payload["tracks_added"])
        self.assertEqual(2, payload["queue_position"])

    async def test_sends_controller_link_when_play_creates_session(self):
        selection = YoutubeSelection(
            tracks=[Track(url="https://youtu.be/dQw4w9WgXcQ", title="A Song")]
        )
        self.manager.get.side_effect = [None, self.session]
        with (
            patch(
                "src.music.tools.voice_channel_from_user",
                return_value=self.voice_channel,
            ),
            patch(
                "src.music.service.discover_youtube_tracks",
                return_value=selection,
            ),
            patch(
                "src.music.service.MUSIC_WEB_BASE_URL",
                "https://music.example.com",
            ),
        ):
            result = await music_play(
                self.message,
                "https://youtu.be/dQw4w9WgXcQ",
                manager=self.manager,
            )

        self.channel.send.assert_awaited_once_with(
            "Control this session "
            "[here](https://music.example.com/s/controller-token)."
        )
        payload = json.loads(result)
        self.assertNotIn("controller_url", payload)

    async def test_does_not_resend_controller_link_for_existing_session(self):
        selection = YoutubeSelection(
            tracks=[Track(url="https://youtu.be/dQw4w9WgXcQ", title="A Song")]
        )
        with (
            patch(
                "src.music.tools.voice_channel_from_user",
                return_value=self.voice_channel,
            ),
            patch(
                "src.music.service.discover_youtube_tracks",
                return_value=selection,
            ),
            patch(
                "src.music.service.MUSIC_WEB_BASE_URL",
                "https://music.example.com",
            ),
        ):
            result = await music_play(
                self.message,
                "https://youtu.be/dQw4w9WgXcQ",
                manager=self.manager,
            )

        self.channel.send.assert_not_awaited()
        payload = json.loads(result)
        self.assertNotIn("controller_url", payload)

    async def test_rejects_non_youtube_url(self):
        with patch(
            "src.music.tools.voice_channel_from_user",
            return_value=self.voice_channel,
        ):
            with self.assertRaisesRegex(ValueError, "valid HTTPS YouTube URL"):
                await music_play(
                    self.message,
                    "https://example.com/song",
                    manager=self.manager,
                )

    async def test_requires_requesting_user_in_voice(self):
        with self.assertRaisesRegex(ValueError, "server member"):
            await music_play(
                self.message,
                "https://youtu.be/dQw4w9WgXcQ",
                manager=self.manager,
            )


class YouTubeSearchToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_youtube_search_results_with_urls(self):
        tracks = [
            Track(
                url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                title="Never Gonna Give You Up",
                duration=213,
            )
        ]
        with (
            patch(
                "src.music.tools.search_youtube_tracks",
                return_value=tracks,
            ) as search,
            patch("src.music.tools.music_telemetry.record"),
        ):
            result = await youtube_search(
                SimpleNamespace(),
                "Rick Astley",
                max_results=3,
            )

        search.assert_called_once_with("Rick Astley", 3)
        payload = json.loads(result)
        self.assertEqual("Rick Astley", payload["query"])
        self.assertEqual(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            payload["results"][0]["url"],
        )
        self.assertEqual("Never Gonna Give You Up", payload["results"][0]["title"])


class MusicToolRegistryTests(unittest.IsolatedAsyncioTestCase):
    def test_registers_only_play_and_queue_music_tools(self):
        self.assertIn("music_queue", available_functions)
        self.assertIn("music_play", available_functions)
        self.assertIn("youtube_search", available_functions)
        self.assertNotIn("music_skip", available_functions)
        self.assertNotIn("music_leave", available_functions)

    async def test_execute_tool_call_dispatches_music_queue(self):
        expected = json.dumps(
            {
                "guild_id": 42,
                "status": "empty",
                "current": None,
                "queue": [],
            }
        )
        tool_call = SimpleNamespace(name="music_queue", arguments={})

        with (
            patch(
                "src.music.tools.music_queue",
                AsyncMock(return_value=expected),
            ) as handler,
            patch("builtins.print"),
        ):
            result = await execute_tool_call(
                SimpleNamespace(guild=SimpleNamespace(id=42)),
                tool_call,
            )

        handler.assert_awaited_once()
        self.assertEqual(expected, result)


if __name__ == "__main__":
    unittest.main()
