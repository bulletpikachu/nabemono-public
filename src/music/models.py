from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4


@dataclass
class Track:
    url: str
    title: str | None = None
    duration: float | None = None
    requested_by_id: int | None = None
    requested_by_name: str | None = None
    id: str = ""
    source: Literal["youtube", "local"] = "youtube"
    path: str | None = None
    artwork_url: str | None = None
    artist: str | None = None

    def __post_init__(self) -> None:
        if not self.id:
            self.id = uuid4().hex
        if self.source == "local" and not self.path:
            raise ValueError("Local tracks require a filesystem path.")


@dataclass
class EnqueueResult:
    started: bool
    count: int
    position: int


@dataclass
class YoutubeSelection:
    tracks: list[Track]
    playlist_title: str | None = None
    truncated: bool = False


def format_duration(seconds: float | int | None) -> str | None:
    if seconds is None:
        return None

    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_track_label(track: Track, elapsed: float | None = None) -> str:
    name = track.title or track.url
    total = format_duration(track.duration)
    if elapsed is not None:
        current = format_duration(elapsed) or "0:00"
        timing = f"{current}/{total}" if total else current
        return f"{name} ({timing})"
    if total:
        return f"{name} ({total})"
    return name


def remaining_queue_seconds(
    current: Track | None,
    queued: Iterable[Track],
    elapsed: float | None = None,
) -> tuple[float, bool]:
    total = 0.0
    complete = True

    if current is not None:
        if current.duration is None:
            complete = False
        else:
            played = 0.0 if elapsed is None else max(0.0, elapsed)
            total += max(0.0, current.duration - played)

    for track in queued:
        if track.duration is None:
            complete = False
        else:
            total += max(0.0, track.duration)

    return total, complete
