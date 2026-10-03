from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

from aiohttp import web

from src.music.models import Track
from src.music.player import MusicManager, MusicSession
from src.music.radio import RADIO_STATIONS, get_radio_station
from src.music.youtube import discover_youtube_tracks, is_youtube_url
from src.persona.store import PersonaStore
from src.persona.web import PersonaDashboard

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
REQUEST_OVERHEAD_BYTES = 1024 * 1024
ALLOWED_AUDIO_EXTENSIONS = {
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
}
STATIC_DIR = Path(__file__).with_name("static")


def _public_origin(base_url: str) -> str | None:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


async def probe_audio(path: Path) -> float | None:
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_type,duration:format=duration",
        "-of",
        "json",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
    except TimeoutError:
        process.kill()
        await process.communicate()
        raise ValueError("Audio validation timed out.")

    if process.returncode != 0:
        raise ValueError("The uploaded file is not valid audio.")
    try:
        payload = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("The uploaded file is not valid audio.") from error
    if not payload.get("streams"):
        raise ValueError("The uploaded file does not contain an audio stream.")

    values = [
        payload["streams"][0].get("duration"),
        payload.get("format", {}).get("duration"),
    ]
    for value in values:
        try:
            duration = float(value)
        except (TypeError, ValueError):
            continue
        if duration > 0:
            return duration
    return None


@web.middleware
async def security_headers(
    request: web.Request,
    handler: web.RequestHandler,
) -> web.StreamResponse:
    try:
        response = await handler(request)
    except web.HTTPException as error:
        _apply_security_headers(error)
        raise
    if not response.prepared:
        _apply_security_headers(response)
    return response


def _apply_security_headers(response: web.StreamResponse) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self' data: https://i.ytimg.com https://i9.ytimg.com; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"


class MusicWebApplication:
    def __init__(
        self,
        manager: MusicManager,
        upload_dir: str | Path,
        base_url: str,
        persona_store: PersonaStore | None = None,
        dashboard_password: str = "",
    ) -> None:
        self.manager = manager
        self.upload_dir = Path(upload_dir)
        self.public_origin = _public_origin(base_url)
        self.persona_store = persona_store
        self.dashboard_password = dashboard_password
        self.base_url = base_url

    def build(self) -> web.Application:
        app = web.Application(
            client_max_size=MAX_UPLOAD_BYTES + REQUEST_OVERHEAD_BYTES,
            middlewares=[security_headers],
        )
        app.router.add_get("/s/{token}", self.index)
        app.router.add_get("/s/{token}/api/queue", self.queue)
        app.router.add_get("/s/{token}/api/events", self.events)
        app.router.add_post("/s/{token}/api/play", self.play)
        app.router.add_post("/s/{token}/api/upload", self.upload)
        app.router.add_post("/s/{token}/api/queue/move", self.move)
        app.router.add_post("/s/{token}/api/queue/remove", self.remove)
        app.router.add_post("/s/{token}/api/skip", self.skip)
        app.router.add_post("/s/{token}/api/pause", self.pause)
        app.router.add_post("/s/{token}/api/resume", self.resume)
        app.router.add_get("/s/{token}/api/radio", self.radio_stations)
        app.router.add_post("/s/{token}/api/radio/play", self.play_radio)
        app.router.add_post("/s/{token}/api/radio/stop", self.stop_radio)
        app.router.add_get("/assets/music.css", self.stylesheet)
        app.router.add_get("/assets/music.js", self.javascript)
        app.router.add_get("/assets/noto-sans-mono.woff2", self.font)
        app.router.add_get("/assets/favicon.png", self.favicon)
        app.router.add_get("/favicon.ico", self.favicon)
        if self.persona_store is not None:
            PersonaDashboard(
                self.persona_store,
                self.dashboard_password,
                self.base_url,
            ).register(app)
        return app

    def _session(self, request: web.Request) -> MusicSession:
        session = self.manager.get_by_token(request.match_info["token"])
        if session is None:
            raise web.HTTPNotFound(text="Music session not found.")
        return session

    def _validate_origin(self, request: web.Request) -> None:
        origin = request.headers.get("Origin")
        expected = self.public_origin
        if expected is None:
            expected_origins = {
                f"http://{request.host}",
                f"https://{request.host}",
            }
        else:
            expected_origins = {expected}
        if origin not in expected_origins:
            raise web.HTTPForbidden(text="Cross-origin requests are not allowed.")

    async def _json(self, request: web.Request) -> dict[str, object]:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, web.HTTPBadRequest) as error:
            raise web.HTTPBadRequest(text="Expected a JSON request body.") from error
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="Expected a JSON object.")
        return payload

    async def index(self, request: web.Request) -> web.FileResponse:
        self._session(request)
        return web.FileResponse(STATIC_DIR / "index.html")

    async def stylesheet(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "music.css")

    async def javascript(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "music.js")

    async def font(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "noto-sans-mono.woff2")

    async def favicon(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "favicon.png")

    async def queue(self, request: web.Request) -> web.Response:
        return web.json_response(self._session(request).snapshot())

    async def events(self, request: web.Request) -> web.StreamResponse:
        session = self._session(request)
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
        )
        _apply_security_headers(response)
        await response.prepare(request)
        subscriber = session.subscribe()
        try:
            while True:
                event = "ended" if session.closed else "queue"
                payload = json.dumps(session.snapshot(), separators=(",", ":"))
                await response.write(
                    f"event: {event}\ndata: {payload}\n\n".encode()
                )
                if session.closed:
                    break
                try:
                    await asyncio.wait_for(subscriber.get(), timeout=1)
                except TimeoutError:
                    pass
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            session.unsubscribe(subscriber)
        return response

    async def play(self, request: web.Request) -> web.Response:
        self._validate_origin(request)
        session = self._session(request)
        payload = await self._json(request)
        url = str(payload.get("url", "")).strip()
        if not is_youtube_url(url):
            raise web.HTTPBadRequest(text="Provide a valid HTTPS YouTube URL.")

        selection = await asyncio.to_thread(discover_youtube_tracks, url)
        if self.manager.get_by_token(request.match_info["token"]) is not session:
            raise web.HTTPNotFound(text="Music session not found.")
        result = await session.enqueue_tracks(
            selection.tracks,
            session.text_channel,
            requester=SimpleNamespace(display_name="Web app"),
        )
        return web.json_response(
            {
                "added": result.count,
                "started": result.started,
                "position": result.position,
            }
        )

    async def upload(self, request: web.Request) -> web.Response:
        self._validate_origin(request)
        session = self._session(request)
        try:
            reader = await request.multipart()
            field = await reader.next()
        except (AssertionError, ValueError) as error:
            raise web.HTTPBadRequest(text="Expected a multipart upload.") from error
        if field is None or field.name != "file" or not field.filename:
            raise web.HTTPBadRequest(text="Expected an audio file field.")

        extension = Path(field.filename).suffix.lower()
        if extension not in ALLOWED_AUDIO_EXTENSIONS:
            raise web.HTTPBadRequest(text="That audio file type is not supported.")

        track_id = uuid4().hex
        session_dir = self.upload_dir / request.match_info["token"]
        path = session_dir / f"{track_id}{extension}"
        session_dir.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            with path.open("xb") as output:
                while chunk := await field.read_chunk(size=64 * 1024):
                    written += len(chunk)
                    if written > MAX_UPLOAD_BYTES:
                        raise web.HTTPRequestEntityTooLarge(
                            max_size=MAX_UPLOAD_BYTES,
                            actual_size=written,
                        )
                    output.write(chunk)
            try:
                duration = await probe_audio(path)
            except ValueError as error:
                raise web.HTTPBadRequest(text=str(error)) from error
            if self.manager.get_by_token(request.match_info["token"]) is not session:
                raise web.HTTPNotFound(text="Music session not found.")
            title = Path(field.filename).stem.strip()[:200] or "Uploaded track"
            track = Track(
                id=track_id,
                url=f"local:{track_id}",
                title=title,
                duration=duration,
                source="local",
                path=str(path),
            )
            result = await session.enqueue_tracks(
                [track],
                session.text_channel,
                requester=SimpleNamespace(display_name="Web upload"),
            )
        except Exception:
            path.unlink(missing_ok=True)
            try:
                session_dir.rmdir()
            except OSError:
                pass
            raise

        return web.json_response(
            {
                "id": track.id,
                "title": track.title,
                "started": result.started,
                "position": result.position,
            }
        )

    async def move(self, request: web.Request) -> web.Response:
        self._validate_origin(request)
        session = self._session(request)
        payload = await self._json(request)
        try:
            source = int(payload["source"])
            destination = int(payload["destination"])
        except (KeyError, TypeError, ValueError) as error:
            raise web.HTTPBadRequest(text="Source and destination are required.") from error
        if not await session.move_queued_async(source, destination):
            raise web.HTTPBadRequest(text="Queue positions do not exist.")
        return web.json_response(session.snapshot())

    async def remove(self, request: web.Request) -> web.Response:
        self._validate_origin(request)
        session = self._session(request)
        payload = await self._json(request)
        try:
            position = int(payload["position"])
        except (KeyError, TypeError, ValueError) as error:
            raise web.HTTPBadRequest(text="A queue position is required.") from error
        track = await session.remove_queued(position)
        if track is None:
            raise web.HTTPBadRequest(text="Queue position does not exist.")
        return web.json_response({"removed": track.id})

    async def skip(self, request: web.Request) -> web.Response:
        self._validate_origin(request)
        skipped = await self._session(request).skip()
        return web.json_response({"skipped": skipped})

    async def pause(self, request: web.Request) -> web.Response:
        self._validate_origin(request)
        status = await self._session(request).pause_async()
        return web.json_response({"status": status})

    async def resume(self, request: web.Request) -> web.Response:
        self._validate_origin(request)
        status = await self._session(request).resume_async()
        return web.json_response({"status": status})

    async def radio_stations(self, request: web.Request) -> web.Response:
        self._session(request)
        return web.json_response(
            {
                "stations": [
                    station.public_payload()
                    for station in RADIO_STATIONS
                ]
            }
        )

    async def play_radio(self, request: web.Request) -> web.Response:
        self._validate_origin(request)
        session = self._session(request)
        payload = await self._json(request)
        station_id = str(payload.get("station_id", "")).strip()
        station = get_radio_station(station_id)
        if station is None:
            raise web.HTTPBadRequest(text="That radio station is not available.")
        try:
            status = await session.start_radio(station)
        except RuntimeError as error:
            raise web.HTTPConflict(text=str(error)) from error
        return web.json_response(
            {
                "status": status,
                "station": station.public_payload(),
            }
        )

    async def stop_radio(self, request: web.Request) -> web.Response:
        self._validate_origin(request)
        stopped = await self._session(request).stop_radio()
        return web.json_response(
            {"status": "stopped" if stopped else "not_playing"}
        )


def create_music_web_app(
    manager: MusicManager,
    upload_dir: str | Path,
    base_url: str = "",
    persona_store: PersonaStore | None = None,
    dashboard_password: str = "",
) -> web.Application:
    return MusicWebApplication(
        manager,
        upload_dir,
        base_url,
        persona_store,
        dashboard_password,
    ).build()


class MusicWebServer:
    def __init__(
        self,
        manager: MusicManager,
        host: str,
        port: int,
        upload_dir: str | Path,
        base_url: str,
        persona_store: PersonaStore | None = None,
        dashboard_password: str = "",
    ) -> None:
        self.app = create_music_web_app(
            manager,
            upload_dir,
            base_url,
            persona_store,
            dashboard_password,
        )
        self.host = host
        self.port = port
        self.runner: web.AppRunner | None = None

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host, self.port)
        await site.start()
        logger.info("Nabemono web app listening on %s:%s", self.host, self.port)

    async def close(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
