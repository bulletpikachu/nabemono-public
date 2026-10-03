import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from src.music.models import Track, YoutubeSelection
from src.music.player import MusicManager
from src.music.web.server import create_music_web_app


class MusicWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = MusicManager()
        guild = SimpleNamespace(id=42, voice_client=None)
        self.session = self.manager.get_or_create(guild)
        self.session.token = "active-token"
        self.manager.sessions_by_token[self.session.token] = self.session
        self.session.current = Track(
            url="https://youtu.be/current123",
            title="Current",
            duration=120,
        )
        self.session.queue.extend(
            [
                Track(url="https://youtu.be/first123", title="First"),
                Track(url="https://youtu.be/second123", title="Second"),
            ]
        )
        app = create_music_web_app(
            self.manager,
            self.temp_dir.name,
            "https://music.example.com",
        )
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.origin = {"Origin": "https://music.example.com"}

    async def asyncTearDown(self):
        await self.client.close()
        await self.session.close()
        self.temp_dir.cleanup()

    async def test_controller_and_snapshot_require_active_token(self):
        response = await self.client.get("/s/active-token")
        self.assertEqual(200, response.status)
        html = await response.text()
        self.assertIn('href="#radio"', html)
        self.assertIn('href="/assets/favicon.png"', html)
        self.assertEqual("no-referrer", response.headers["Referrer-Policy"])
        self.assertEqual("no-store", response.headers["Cache-Control"])

        response = await self.client.get("/s/missing/api/queue")
        self.assertEqual(404, response.status)
        self.assertEqual("DENY", response.headers["X-Frame-Options"])

    async def test_favicon_is_served(self):
        for path in ("/assets/favicon.png", "/favicon.ico"):
            response = await self.client.get(path)
            self.assertEqual(200, response.status, path)
            self.assertEqual("image/png", response.content_type)
            body = await response.read()
            self.assertTrue(body.startswith(b"\x89PNG"), path)

    async def test_event_stream_sends_live_snapshot(self):
        response = await self.client.get("/s/active-token/api/events")
        try:
            event_line = await response.content.readline()
            data_line = await response.content.readline()
        finally:
            response.close()

        self.assertEqual(b"event: queue\n", event_line)
        self.assertIn(b'"title":"Current"', data_line)
        self.assertEqual("no-referrer", response.headers["Referrer-Policy"])

    async def test_history_is_shared_and_not_duplicated_by_playback_callback(self):
        track = self.session.current
        self.session.queue.clear()
        await self.session.skip()
        await self.session._finish_track(track, None)
        response = await self.client.get("/s/active-token/api/queue")
        history = (await response.json())["history"]
        self.assertEqual(1, len(history))
        self.assertEqual("Current", history[0]["title"])
        self.assertEqual("skipped", history[0]["outcome"])
        self.assertNotIn("path", history[0])

    async def test_finished_and_failed_tracks_are_newest_first(self):
        self.session.queue.clear()
        await self.session._finish_track(self.session.current, None)
        self.session.current = Track(url="https://youtu.be/dQw4w9WgXcQ", title="Failed")
        await self.session._finish_track(self.session.current, RuntimeError("test failure"))
        history = self.session.snapshot()["history"]
        self.assertEqual(["failed", "played"], [item["outcome"] for item in history])
        self.assertEqual("https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg", history[0]["artwork_url"])

    async def test_artwork_and_artist_are_exposed_without_local_paths(self):
        self.session.current.artwork_url = "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"
        self.session.current.artist = "Artist"
        response = await self.client.get("/s/active-token/api/queue")
        current = (await response.json())["current"]
        self.assertEqual("Artist", current["artist"])
        self.assertEqual(self.session.current.artwork_url, current["artwork_url"])
        self.assertNotIn("path", current)
        self.assertIn("https://i.ytimg.com", response.headers["Content-Security-Policy"])

    async def test_expired_token_returns_404(self):
        await self.session.close()

        response = await self.client.get("/s/active-token")

        self.assertEqual(404, response.status)

    async def test_move_and_remove_queue_tracks(self):
        response = await self.client.post(
            "/s/active-token/api/queue/move",
            json={"source": 1, "destination": 2},
            headers=self.origin,
        )
        self.assertEqual(200, response.status)
        self.assertEqual(
            ["Second", "First"],
            [track.title for track in self.session.queue],
        )

        response = await self.client.post(
            "/s/active-token/api/queue/remove",
            json={"position": 1},
            headers=self.origin,
        )
        self.assertEqual(200, response.status)
        self.assertEqual(["First"], [track.title for track in self.session.queue])

    async def test_rejects_cross_origin_mutations(self):
        response = await self.client.post(
            "/s/active-token/api/skip",
            headers={"Origin": "https://attacker.example"},
        )
        self.assertEqual(403, response.status)

    async def test_adds_youtube_selection(self):
        selection = YoutubeSelection(
            tracks=[
                Track(url="https://youtu.be/queued123", title="Queued online")
            ]
        )
        with patch(
            "src.music.web.server.discover_youtube_tracks",
            return_value=selection,
        ):
            response = await self.client.post(
                "/s/active-token/api/play",
                json={"url": "https://youtu.be/queued123"},
                headers=self.origin,
            )

        self.assertEqual(200, response.status)
        self.assertEqual(1, (await response.json())["added"])
        self.assertEqual("Queued online", self.session.queue[-1].title)

    async def test_radio_registry_does_not_expose_stream_urls(self):
        response = await self.client.get("/s/active-token/api/radio")
        payload = await response.json()

        self.assertEqual(200, response.status)
        self.assertEqual("KDFC", payload["stations"][0]["id"])
        self.assertNotIn("stream_url", payload["stations"][0])
        self.assertNotIn("streamtheworld", await response.text())

    async def test_starts_only_backend_configured_radio_station(self):
        with patch.object(
            self.session,
            "start_radio",
            new_callable=AsyncMock,
            return_value="playing",
        ) as start_radio:
            response = await self.client.post(
                "/s/active-token/api/radio/play",
                json={"station_id": "KDFC"},
                headers=self.origin,
            )

        payload = await response.json()
        self.assertEqual(200, response.status)
        self.assertEqual("KDFC", payload["station"]["id"])
        self.assertNotIn("stream_url", payload["station"])
        self.assertEqual("KDFC", start_radio.await_args.args[0].id)

        response = await self.client.post(
            "/s/active-token/api/radio/play",
            json={"station_id": "user-supplied-url"},
            headers=self.origin,
        )
        self.assertEqual(400, response.status)

    async def test_uploads_validated_audio_and_removes_file(self):
        form = FormData()
        form.add_field(
            "file",
            b"audio bytes",
            filename="local song.mp3",
            content_type="audio/mpeg",
        )
        with patch(
            "src.music.web.server.probe_audio",
            return_value=15.5,
        ):
            response = await self.client.post(
                "/s/active-token/api/upload",
                data=form,
                headers=self.origin,
            )

        self.assertEqual(200, response.status)
        uploaded = self.session.queue[-1]
        self.assertEqual("local", uploaded.source)
        self.assertEqual(15.5, uploaded.duration)
        assert uploaded.path is not None
        upload_path = Path(uploaded.path)
        self.assertTrue(upload_path.exists())

        position = len(self.session.queue)
        response = await self.client.post(
            "/s/active-token/api/queue/remove",
            json={"position": position},
            headers=self.origin,
        )
        self.assertEqual(200, response.status)
        self.assertFalse(upload_path.exists())

    async def test_rejects_invalid_audio_and_deletes_partial_file(self):
        form = FormData()
        form.add_field("file", b"not audio", filename="bad.mp3")
        with patch(
            "src.music.web.server.probe_audio",
            side_effect=ValueError("The uploaded file is not valid audio."),
        ):
            response = await self.client.post(
                "/s/active-token/api/upload",
                data=form,
                headers=self.origin,
            )

        self.assertEqual(400, response.status)
        upload_root = Path(self.temp_dir.name)
        self.assertEqual([], list(upload_root.rglob("*.mp3")))

    async def test_enforces_streamed_upload_limit(self):
        form = FormData()
        form.add_field("file", b"too large", filename="large.mp3")
        with patch("src.music.web.server.MAX_UPLOAD_BYTES", 4):
            response = await self.client.post(
                "/s/active-token/api/upload",
                data=form,
                headers=self.origin,
            )

        self.assertEqual(413, response.status)
        self.assertEqual([], list(Path(self.temp_dir.name).rglob("*.mp3")))


if __name__ == "__main__":
    unittest.main()
