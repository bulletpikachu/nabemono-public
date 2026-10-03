from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import discord

from src.music.models import (
    EnqueueResult,
    Track,
    format_duration,
    format_track_label,
    remaining_queue_seconds,
)
from src.music.radio import RadioStation
from src.music.telemetry import music_telemetry
from src.music.youtube import build_ffmpeg_before_options, resolve_youtube_audio, youtube_artwork

logger = logging.getLogger(__name__)

MUSIC_IDLE_TIMEOUT_SECONDS = 30 * 60


def _track_payload(track: Track) -> dict[str, Any]:
    return {
        "id": track.id,
        "source": track.source,
        "title": track.title,
        "url": track.url if track.source == "youtube" else None,
        "duration_seconds": track.duration,
        "requested_by": track.requested_by_name,
        "artwork_url": track.artwork_url or (youtube_artwork({}, track.url) if track.source == "youtube" else None),
        "artist": track.artist,
    }


def _format_track_link(track: Track, elapsed: float | None = None) -> str:
    label = discord.utils.escape_markdown(format_track_label(track, elapsed=elapsed))
    if track.source == "local":
        return label
    return f"[{label}](<{track.url}>)"


class MusicSession:
    def __init__(
        self,
        guild: discord.Guild,
        on_close: Callable[[], None],
        on_token: Callable[[MusicSession], None] | None = None,
        idle_timeout_seconds: float = MUSIC_IDLE_TIMEOUT_SECONDS,
    ) -> None:
        self.guild = guild
        self.on_close = on_close
        self.on_token = on_token
        self.queue: deque[Track] = deque()
        self.history: list[dict[str, Any]] = []
        self.current: Track | None = None
        self.radio_station: RadioStation | None = None
        self.radio_started_at: float | None = None
        self.interrupted_track: Track | None = None
        self.interrupted_elapsed = 0.0
        self.interrupted_paused = False
        self.voice_client: discord.VoiceClient | None = None
        self.text_channel: discord.abc.Messageable | None = None
        self.connect_lock = asyncio.Lock()
        self.mutation_lock = asyncio.Lock()
        self.closed = False
        self.token: str | None = None
        self.revision = 0
        self.playback_generation = 0
        self._subscribers: set[asyncio.Queue[None]] = set()
        self.playback_started_at: float | None = None
        self.pause_started_at: float | None = None
        self.paused_duration = 0.0
        self.idle_timeout_seconds = idle_timeout_seconds
        self.idle_disconnect_task: asyncio.Task[None] | None = None

    def _reset_playback_clock(self) -> None:
        self.playback_started_at = None
        self.pause_started_at = None
        self.paused_duration = 0.0

    def elapsed_seconds(self) -> float | None:
        if self.playback_started_at is None:
            return None

        now = time.monotonic()
        paused = self.paused_duration
        if self.pause_started_at is not None:
            paused += now - self.pause_started_at
        return max(0.0, now - self.playback_started_at - paused)

    def _is_idle(self) -> bool:
        return (
            self.current is None
            and self.radio_station is None
            and self.interrupted_track is None
            and not self.queue
        )

    def _cancel_idle_disconnect(self) -> None:
        task = self.idle_disconnect_task
        self.idle_disconnect_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _schedule_idle_disconnect(self) -> None:
        self._cancel_idle_disconnect()
        voice_client = self.voice_client
        if (
            self.closed
            or not self._is_idle()
            or voice_client is None
            or not voice_client.is_connected()
        ):
            return
        self.idle_disconnect_task = asyncio.create_task(
            self._disconnect_after_idle_timeout()
        )

    async def _disconnect_after_idle_timeout(self) -> None:
        task = asyncio.current_task()
        try:
            await asyncio.sleep(self.idle_timeout_seconds)
            async with self.mutation_lock:
                if (
                    self.closed
                    or self.idle_disconnect_task is not task
                    or not self._is_idle()
                ):
                    return
                self.idle_disconnect_task = None
                self._close_locked()

            await self._send_notice(
                "Left the voice channel after 30 minutes of inactivity."
            )
            await self._disconnect_voice_client()
        except asyncio.CancelledError:
            return

    async def connect(
        self,
        channel: discord.VoiceChannel | discord.StageChannel,
        *,
        move: bool = False,
    ) -> str:
        if self.closed:
            raise RuntimeError("This music session has already ended.")

        async with self.connect_lock:
            existing = self.guild.voice_client
            if existing is not None and existing.is_connected():
                if existing.channel.id == channel.id:
                    self.voice_client = existing
                    self._ensure_token()
                    self._schedule_idle_disconnect()
                    return "already_here"
                if not move:
                    raise RuntimeError(
                        "The bot is already playing in another voice channel."
                    )
                try:
                    await existing.move_to(channel, timeout=20)
                except (asyncio.TimeoutError, discord.DiscordException) as error:
                    raise RuntimeError(
                        "Could not connect to that voice channel."
                    ) from error
                self.voice_client = existing
                self._ensure_token()
                self._schedule_idle_disconnect()
                return "moved"

            try:
                self.voice_client = await channel.connect(
                    timeout=20,
                    reconnect=True,
                    self_deaf=True,
                )
            except (asyncio.TimeoutError, discord.DiscordException) as error:
                raise RuntimeError(
                    "Could not connect to that voice channel."
                ) from error
            self._ensure_token()
            self._schedule_idle_disconnect()
            return "joined"

    def _ensure_token(self) -> str:
        if self.token is None:
            self.token = secrets.token_urlsafe(32)
            if self.on_token is not None:
                self.on_token(self)
        return self.token

    def subscribe(self) -> asyncio.Queue[None]:
        queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[None]) -> None:
        self._subscribers.discard(queue)

    def _notify(self) -> None:
        self.revision += 1
        for queue in tuple(self._subscribers):
            if queue.empty():
                queue.put_nowait(None)

    def enqueue(
        self,
        tracks: Sequence[Track],
        text_channel: discord.abc.Messageable | None,
        requester: object | None = None,
    ) -> EnqueueResult:
        if self.closed:
            raise RuntimeError("This music session has already ended.")
        if not tracks:
            raise RuntimeError("No tracks to enqueue.")

        self._cancel_idle_disconnect()
        self.text_channel = text_channel
        track_list = list(tracks)
        user_id = getattr(requester, "id", None)
        username = getattr(requester, "display_name", None) or getattr(
            requester, "name", None
        )
        for track in track_list:
            if user_id is not None:
                track.requested_by_id = int(user_id)
            if username is not None:
                track.requested_by_name = str(username)
            music_telemetry.record(
                "requested",
                guild_id=self.guild.id,
                track=track,
            )

        if self.radio_station is not None:
            self.queue.extend(track_list)
            self._notify()
            return EnqueueResult(
                started=False,
                count=len(track_list),
                position=len(self.queue) - len(track_list) + 1,
            )

        if self.current is None:
            self.current = track_list[0]
            self.queue.extend(track_list[1:])
            asyncio.create_task(self._start_track(track_list[0]))
            self._notify()
            return EnqueueResult(started=True, count=len(track_list), position=0)

        self.queue.extend(track_list)
        self._notify()
        return EnqueueResult(
            started=False,
            count=len(track_list),
            position=len(self.queue) - len(track_list) + 1,
        )

    async def enqueue_tracks(
        self,
        tracks: Sequence[Track],
        text_channel: discord.abc.Messageable | None,
        requester: object | None = None,
    ) -> EnqueueResult:
        async with self.mutation_lock:
            return self.enqueue(tracks, text_channel, requester=requester)

    async def _start_track(
        self,
        track: Track,
        *,
        start_at: float = 0.0,
        start_paused: bool = False,
    ) -> None:
        source: discord.AudioSource | None = None
        source_started = False
        try:
            if track.source == "local":
                if track.path is None:
                    raise RuntimeError("Local track has no file.")
                title = track.title or Path(track.path).name
                duration = track.duration
                source = discord.FFmpegOpusAudio(
                    track.path,
                    before_options=(
                        f"-ss {start_at:.3f}" if start_at > 0 else None
                    ),
                    options="-vn",
                    bitrate=192,
                )
            else:
                stream_url, title, http_headers, audio_codec, duration = (
                    await asyncio.to_thread(resolve_youtube_audio, track.url)
                )
                ffmpeg_options = (
                    {"codec": "copy"}
                    if audio_codec == "opus"
                    else {"bitrate": 192}
                )
                before_options = build_ffmpeg_before_options(http_headers)
                if start_at > 0:
                    before_options += f" -ss {start_at:.3f}"
                source = discord.FFmpegOpusAudio(
                    stream_url,
                    before_options=before_options,
                    options="-vn",
                    **ffmpeg_options,
                )
            if self.closed or self.current is not track:
                source.cleanup()
                if self.interrupted_track is not track:
                    self._cleanup_track_file(track)
                return

            track.title = title
            if duration is not None:
                track.duration = duration

            if self.voice_client is None or not self.voice_client.is_connected():
                raise RuntimeError("The bot is no longer connected to voice.")

            loop = asyncio.get_running_loop()
            self.playback_generation += 1
            generation = self.playback_generation

            def after_playback(error: Exception | None) -> None:
                loop.call_soon_threadsafe(
                    asyncio.create_task,
                    self._finish_track(track, error, generation),
                )

            self._reset_playback_clock()
            self.playback_started_at = time.monotonic() - max(0.0, start_at)
            self.voice_client.play(source, after=after_playback)
            source_started = True
            if start_paused:
                self.voice_client.pause()
                self.pause_started_at = time.monotonic()
            self._notify()
            music_telemetry.record(
                "played",
                guild_id=self.guild.id,
                track=track,
            )
            await self._send_notice(
                f"Now playing: **{discord.utils.escape_markdown(title)[:250]}**"
            )
        except Exception as error:
            if source is not None and not source_started:
                source.cleanup()
            logger.exception("Could not play %s", track.url or track.path)
            await self._send_notice(
                "Could not play that track. Skipping it."
            )
            await self._finish_track(track, error)

    async def start_radio(self, station: RadioStation) -> str:
        async with self.mutation_lock:
            if self.closed:
                raise RuntimeError("This music session has already ended.")
            voice_client = self.voice_client
            if voice_client is None or not voice_client.is_connected():
                raise RuntimeError("The bot is no longer connected to voice.")
            if self.radio_station == station:
                return "already_playing"

            self._cancel_idle_disconnect()
            if self.radio_station is None:
                self.interrupted_track = self.current
                self.interrupted_elapsed = self.elapsed_seconds() or 0.0
                self.interrupted_paused = self.pause_started_at is not None
                self.current = None
                self._reset_playback_clock()
            else:
                self.radio_station = None

            self.playback_generation += 1
            if voice_client.is_playing() or voice_client.is_paused():
                voice_client.stop()

            self.radio_station = station
            self.radio_started_at = time.monotonic()
            asyncio.create_task(self._start_radio(station))
            self._notify()
            return "playing"

    async def _start_radio(self, station: RadioStation) -> None:
        source: discord.AudioSource | None = None
        source_started = False
        generation = self.playback_generation
        try:
            source = discord.FFmpegOpusAudio(
                station.stream_url,
                before_options=(
                    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
                ),
                options="-vn",
                bitrate=192,
            )
            if self.closed or self.radio_station is not station:
                source.cleanup()
                return
            voice_client = self.voice_client
            if voice_client is None or not voice_client.is_connected():
                raise RuntimeError("The bot is no longer connected to voice.")
            loop = asyncio.get_running_loop()
            self.playback_generation += 1
            generation = self.playback_generation

            def after_radio(error: Exception | None) -> None:
                loop.call_soon_threadsafe(
                    asyncio.create_task,
                    self._finish_radio(station, error, generation),
                )

            voice_client.play(source, after=after_radio)
            source_started = True
            self.radio_started_at = time.monotonic()
            self._notify()
            await self._send_notice(
                f"Now playing radio: **{discord.utils.escape_markdown(station.name)}**"
            )
        except Exception as error:
            if source is not None and not source_started:
                source.cleanup()
            logger.exception("Could not play radio station %s", station.id)
            await self._send_notice(
                f"Could not play radio station **{station.name}**."
            )
            await self._finish_radio(station, error, generation)

    async def _finish_radio(
        self,
        station: RadioStation,
        error: Exception | None,
        generation: int,
    ) -> None:
        async with self.mutation_lock:
            if (
                self.closed
                or self.radio_station is not station
                or generation != self.playback_generation
            ):
                return
            if error is not None:
                logger.error(
                    "Radio playback failed in guild %s: %s",
                    self.guild.id,
                    error,
                )
            self.radio_station = None
            self.radio_started_at = None
            self._resume_interrupted_locked()

    def _resume_interrupted_locked(self) -> None:
        track = self.interrupted_track
        start_at = self.interrupted_elapsed
        start_paused = self.interrupted_paused
        self.interrupted_track = None
        self.interrupted_elapsed = 0.0
        self.interrupted_paused = False
        if track is None and self.queue:
            track = self.queue.popleft()
            start_at = 0.0
            start_paused = False
        if track is not None:
            self._cancel_idle_disconnect()
            self.current = track
            asyncio.create_task(
                self._start_track(
                    track,
                    start_at=start_at,
                    start_paused=start_paused,
                )
            )
        else:
            self._schedule_idle_disconnect()
        self._notify()

    async def stop_radio(self) -> bool:
        async with self.mutation_lock:
            if self.radio_station is None:
                return False
            self.radio_station = None
            self.radio_started_at = None
            self.playback_generation += 1
            voice_client = self.voice_client
            if voice_client is not None and (
                voice_client.is_playing() or voice_client.is_paused()
            ):
                voice_client.stop()
            self._resume_interrupted_locked()
            return True

    async def _finish_track(
        self,
        track: Track,
        error: Exception | None,
        generation: int | None = None,
    ) -> None:
        async with self.mutation_lock:
            if (
                self.closed
                or self.current is not track
                or (
                    generation is not None
                    and generation != self.playback_generation
                )
            ):
                return
            self._finish_track_locked(track, error)

    def _finish_track_locked(
        self,
        track: Track,
        error: Exception | None,
        *,
        skipped: bool = False,
    ) -> None:
        if error is not None:
            logger.error("Playback failed in guild %s: %s", self.guild.id, error)
            music_telemetry.record(
                "failed",
                guild_id=self.guild.id,
                track=track,
            )

        self.history.append({
            **_track_payload(track),
            "ended_at": time.time(),
            "elapsed_seconds": self.elapsed_seconds(),
            "outcome": "failed" if error else "skipped" if skipped else "played",
        })
        self._cleanup_track_file(track)
        self.current = None
        self._reset_playback_clock()
        if self.queue:
            next_track = self.queue.popleft()
            self.current = next_track
            asyncio.create_task(self._start_track(next_track))
        self._schedule_idle_disconnect()
        self._notify()

    async def skip(self) -> bool:
        async with self.mutation_lock:
            if self.radio_station is not None:
                self.radio_station = None
                self.radio_started_at = None
                self.playback_generation += 1
                voice_client = self.voice_client
                if voice_client is not None and (
                    voice_client.is_playing() or voice_client.is_paused()
                ):
                    voice_client.stop()
                self._resume_interrupted_locked()
                return True
            if self.current is None:
                return False

            track = self.current
            music_telemetry.record(
                "skipped",
                guild_id=self.guild.id,
                track=track,
            )
            if self.voice_client is not None and (
                self.voice_client.is_playing() or self.voice_client.is_paused()
            ):
                self.voice_client.stop()

            self._finish_track_locked(track, None, skipped=True)
            return True

    def pause(self) -> str:
        voice_client = self.voice_client
        if voice_client is None or not voice_client.is_connected():
            return "not_playing"
        if voice_client.is_paused():
            return "already_paused"
        if not voice_client.is_playing():
            return "not_playing"

        voice_client.pause()
        self.pause_started_at = time.monotonic()
        self._notify()
        return "paused"

    async def pause_async(self) -> str:
        async with self.mutation_lock:
            return self.pause()

    def resume(self) -> str:
        voice_client = self.voice_client
        if voice_client is None or not voice_client.is_paused():
            return "not_paused"

        voice_client.resume()
        if self.pause_started_at is not None:
            self.paused_duration += time.monotonic() - self.pause_started_at
            self.pause_started_at = None
        self._notify()
        return "resumed"

    async def resume_async(self) -> str:
        async with self.mutation_lock:
            return self.resume()

    @staticmethod
    def _cleanup_track_file(track: Track) -> None:
        if track.source != "local" or not track.path:
            return
        try:
            path = Path(track.path)
            path.unlink(missing_ok=True)
            try:
                path.parent.rmdir()
            except OSError:
                pass
        except OSError:
            logger.warning("Could not delete local track %s", track.path)
        track.path = None

    def remove_from_queue(self, number: int) -> Track | None:
        if number < 1 or number > len(self.queue):
            return None

        items = list(self.queue)
        removed = items.pop(number - 1)
        self.queue = deque(items)
        self._cleanup_track_file(removed)
        self._schedule_idle_disconnect()
        self._notify()
        return removed

    async def remove_queued(self, number: int) -> Track | None:
        async with self.mutation_lock:
            return self.remove_from_queue(number)

    def swap_queued(self, first: int, second: int) -> bool:
        count = len(self.queue)
        if not (1 <= first <= count and 1 <= second <= count):
            return False
        if first == second:
            return True

        items = list(self.queue)
        items[first - 1], items[second - 1] = items[second - 1], items[first - 1]
        self.queue = deque(items)
        self._notify()
        return True

    async def swap_queued_async(self, first: int, second: int) -> bool:
        async with self.mutation_lock:
            return self.swap_queued(first, second)

    def move_queued(self, source: int, destination: int) -> bool:
        count = len(self.queue)
        if not (1 <= source <= count and 1 <= destination <= count):
            return False
        if source == destination:
            return True
        items = list(self.queue)
        track = items.pop(source - 1)
        items.insert(destination - 1, track)
        self.queue = deque(items)
        self._notify()
        return True

    async def move_queued_async(self, source: int, destination: int) -> bool:
        async with self.mutation_lock:
            return self.move_queued(source, destination)

    def snapshot(self) -> dict[str, Any]:
        current = None
        if self.current is not None:
            current = {
                **_track_payload(self.current),
                "elapsed_seconds": self.elapsed_seconds(),
                "paused": self.pause_started_at is not None,
            }

        remaining_current = (
            self.interrupted_track
            if self.radio_station is not None
            else self.current
        )
        remaining_elapsed = (
            self.interrupted_elapsed
            if self.radio_station is not None
            else self.elapsed_seconds()
        )
        remaining_seconds, remaining_complete = remaining_queue_seconds(
            remaining_current,
            self.queue,
            elapsed=remaining_elapsed,
        )
        if self.radio_station is not None:
            status = "radio"
        elif current is None and not self.queue:
            status = "empty"
        elif current is not None and current["paused"]:
            status = "paused"
        else:
            status = "playing"

        return {
            "revision": self.revision,
            "history": list(reversed(self.history)),
            "status": status,
            "radio": (
                self.radio_station.public_payload()
                if self.radio_station is not None
                else None
            ),
            "interrupted": (
                {
                    **_track_payload(self.interrupted_track),
                    "elapsed_seconds": self.interrupted_elapsed,
                    "paused": self.interrupted_paused,
                }
                if self.interrupted_track is not None
                else None
            ),
            "current": current,
            "queue": [
                {"position": position, **_track_payload(track)}
                for position, track in enumerate(self.queue, start=1)
            ],
            "remaining_seconds": remaining_seconds,
            "remaining_complete": remaining_complete,
        }

    def describe_queue(self) -> str:
        if (
            self.radio_station is None
            and self.current is None
            and not self.queue
        ):
            return "The queue is empty."

        lines: list[str] = []
        if self.radio_station is not None:
            lines.append(f"Live radio: **{self.radio_station.name}**")
            if self.interrupted_track is not None:
                lines.append(
                    "Resumes afterward: "
                    f"{_format_track_link(self.interrupted_track)}"
                )
        elif self.current is not None:
            elapsed = self.elapsed_seconds()
            display_elapsed = 0.0 if elapsed is None else elapsed
            line = (
                "Now playing: "
                f"{_format_track_link(self.current, elapsed=display_elapsed)}"
            )
            if self.pause_started_at is not None:
                line += " (paused)"
            lines.append(line)

        if self.queue:
            lines.extend(
                [
                    "",
                    "Up next:",
                    *[
                        f"{index}. {_format_track_link(track)}"
                        for index, track in enumerate(self.queue, start=1)
                    ],
                ]
            )

        remaining_seconds, remaining_complete = remaining_queue_seconds(
            (
                self.interrupted_track
                if self.radio_station is not None
                else self.current
            ),
            self.queue,
            elapsed=(
                self.interrupted_elapsed
                if self.radio_station is not None
                else self.elapsed_seconds()
            ),
        )
        remaining_label = format_duration(remaining_seconds) or "0:00"
        if lines:
            lines.append("")
        if remaining_complete:
            lines.append(f"Queue length: {remaining_label}")
        else:
            lines.append(f"Queue length: {remaining_label}+ (some durations unknown)")

        output = "\n".join(lines)
        return output if len(output) <= 1_900 else f"{output[:1_897]}..."

    async def _send_notice(self, message: str) -> None:
        if self.text_channel is None:
            return
        try:
            await self.text_channel.send(
                message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.DiscordException:
            pass

    def _close_locked(self) -> None:
        self.closed = True
        tracks = (
            ([self.current] if self.current is not None else [])
            + (
                [self.interrupted_track]
                if self.interrupted_track is not None
                else []
            )
            + list(self.queue)
        )
        for track in tracks:
            self._cleanup_track_file(track)
        self.queue.clear()
        self.current = None
        self.radio_station = None
        self.radio_started_at = None
        self.interrupted_track = None
        self.interrupted_elapsed = 0.0
        self.interrupted_paused = False
        self._reset_playback_clock()
        self._notify()
        self.on_close()

    async def _disconnect_voice_client(self) -> None:
        if self.voice_client is not None:
            if self.voice_client.is_playing() or self.voice_client.is_paused():
                self.voice_client.stop()
            if self.voice_client.is_connected():
                await self.voice_client.disconnect(force=True)

    async def close(self) -> None:
        async with self.mutation_lock:
            if self.closed:
                return

            self._cancel_idle_disconnect()
            self._close_locked()

        await self._disconnect_voice_client()


class MusicManager:
    def __init__(self) -> None:
        self.sessions: dict[int, MusicSession] = {}
        self.sessions_by_token: dict[str, MusicSession] = {}

    def get(self, guild_id: int) -> MusicSession | None:
        return self.sessions.get(guild_id)

    def get_by_token(self, token: str) -> MusicSession | None:
        session = self.sessions_by_token.get(token)
        if session is None or session.closed:
            return None
        return session

    def get_or_create(self, guild: discord.Guild) -> MusicSession:
        session = self.sessions.get(guild.id)
        if session is None:
            guild_id = guild.id

            def remove_session() -> None:
                removed = self.sessions.pop(guild_id, None)
                if removed is not None and removed.token is not None:
                    self.sessions_by_token.pop(removed.token, None)

            def index_token(connected: MusicSession) -> None:
                if connected.token is not None:
                    self.sessions_by_token[connected.token] = connected

            session = MusicSession(
                guild,
                remove_session,
                index_token,
            )
            self.sessions[guild_id] = session
        return session

    async def close_all(self) -> None:
        await asyncio.gather(
            *(session.close() for session in tuple(self.sessions.values())),
            return_exceptions=True,
        )


music_manager = MusicManager()
