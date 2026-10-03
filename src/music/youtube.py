from __future__ import annotations

import re
import shlex
from urllib.parse import parse_qs, urlparse

import yt_dlp

from src.music.models import Track, YoutubeSelection

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}
HTTP_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
MAX_PLAYLIST_TRACKS = 100
MAX_YOUTUBE_SEARCH_RESULTS = 10
YOUTUBE_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def is_youtube_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return parsed.scheme == "https" and parsed.hostname in YOUTUBE_HOSTS
    except ValueError:
        return False


def is_youtube_playlist_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False

    if parsed.scheme != "https" or parsed.hostname not in YOUTUBE_HOSTS:
        return False

    path = parsed.path.rstrip("/").lower()
    playlist_ids = parse_qs(parsed.query).get("list", [])
    if not playlist_ids:
        return path.endswith("/playlist")

    playlist_id = playlist_ids[0]
    # YouTube Mix/radio playlists are generated and can be unbounded.
    if playlist_id.startswith("RD"):
        return False
    return True


def youtube_watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def track_from_youtube_entry(
    entry: object,
    fallback_url: str | None = None,
) -> Track | None:
    if not isinstance(entry, dict):
        return None
    if entry.get("_type") == "playlist":
        return None

    video_id = entry.get("id")
    if isinstance(video_id, str) and YOUTUBE_VIDEO_ID.fullmatch(video_id):
        url = youtube_watch_url(video_id)
    else:
        raw_url = entry.get("webpage_url") or entry.get("url")
        if isinstance(raw_url, str) and is_youtube_url(raw_url):
            url = raw_url
        elif fallback_url is not None:
            url = fallback_url
        else:
            return None

    if is_youtube_playlist_url(url):
        return None

    title = entry.get("title")
    duration = entry.get("duration")
    return Track(
        url=url,
        title=title if isinstance(title, str) and title not in {"", "NA"} else None,
        duration=float(duration) if isinstance(duration, (int, float)) else None,
        artwork_url=youtube_artwork(entry, url),
        artist=entry.get("artist") or entry.get("uploader") or entry.get("channel"),
    )


def youtube_artwork(entry: dict, url: str) -> str | None:
    candidates = [entry.get("thumbnail")]
    candidates.extend(
        item.get("url") for item in reversed(entry.get("thumbnails") or [])
        if isinstance(item, dict)
    )
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        try:
            parsed = urlparse(candidate)
            if parsed.scheme == "https" and parsed.hostname in {"i.ytimg.com", "i9.ytimg.com"}:
                return candidate
        except ValueError:
            continue
    parsed = urlparse(url)
    video_id = (parsed.path.strip("/") if parsed.hostname == "youtu.be"
                else parse_qs(parsed.query).get("v", [""])[0])
    if not video_id and parsed.path.startswith(("/shorts/", "/embed/", "/live/")):
        video_id = parsed.path.split("/")[2]
    if YOUTUBE_VIDEO_ID.fullmatch(video_id):
        return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    return None


def search_youtube_tracks(query: str, max_results: int = 5) -> list[Track]:
    query = query.strip()
    if not query:
        raise ValueError("YouTube search query must not be empty.")
    if not 1 <= max_results <= MAX_YOUTUBE_SEARCH_RESULTS:
        raise ValueError(
            f"max_results must be between 1 and {MAX_YOUTUBE_SEARCH_RESULTS}."
        )

    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "noplaylist": True,
        "ignoreerrors": True,
    }
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(
            f"ytsearch{max_results}:{query}",
            download=False,
        )

    if not isinstance(info, dict):
        return []
    entries = info.get("entries")
    if not isinstance(entries, list):
        return []

    return [
        track
        for entry in entries
        if (track := track_from_youtube_entry(entry)) is not None
    ]


def discover_youtube_tracks(url: str) -> YoutubeSelection:
    playlist = is_youtube_playlist_url(url)
    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "noplaylist": not playlist,
        "ignoreerrors": True,
        "playlistend": MAX_PLAYLIST_TRACKS if playlist else 1,
    }
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=False)

    if not isinstance(info, dict):
        raise RuntimeError("yt-dlp did not return any data.")

    playlist_title = info.get("title") if playlist else None
    tracks: list[Track] = []
    truncated = False

    if playlist:
        entries = info.get("entries")
        if entries is None:
            raise RuntimeError("No playable YouTube videos were found.")
        for entry in entries:
            track = track_from_youtube_entry(entry)
            if track is None:
                continue
            tracks.append(track)
            if len(tracks) >= MAX_PLAYLIST_TRACKS:
                break
        playlist_count = info.get("playlist_count")
        truncated = (
            isinstance(playlist_count, int)
            and playlist_count > MAX_PLAYLIST_TRACKS
        )
    else:
        track = track_from_youtube_entry(info, fallback_url=url)
        if track is not None:
            tracks.append(track)

    if not tracks:
        raise RuntimeError("No playable YouTube videos were found.")

    return YoutubeSelection(
        tracks=tracks,
        playlist_title=playlist_title if isinstance(playlist_title, str) else None,
        truncated=truncated,
    )


def resolve_youtube_audio(
    url: str,
) -> tuple[str, str, dict[str, str], str | None, float | None]:
    options = {
        "format": "bestaudio[acodec=opus]/bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=False)

    if not isinstance(info, dict):
        raise RuntimeError("yt-dlp did not return any data.")

    stream_url = info.get("url")
    if not isinstance(stream_url, str):
        raise RuntimeError("yt-dlp did not return a playable audio stream.")

    title = info.get("title")
    raw_headers = info.get("http_headers")
    headers = (
        {
            str(name): str(value)
            for name, value in raw_headers.items()
            if isinstance(name, str) and isinstance(value, str)
        }
        if isinstance(raw_headers, dict)
        else {}
    )
    audio_codec = info.get("acodec")
    duration = info.get("duration")
    return (
        stream_url,
        title if isinstance(title, str) else url,
        headers,
        audio_codec if isinstance(audio_codec, str) else None,
        float(duration) if isinstance(duration, (int, float)) else None,
    )


def build_ffmpeg_before_options(headers: dict[str, str]) -> str:
    arguments = [
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "5",
    ]
    forwarded_headers: list[str] = []

    for name, value in headers.items():
        if (
            not HTTP_HEADER_NAME.fullmatch(name)
            or "\r" in value
            or "\n" in value
        ):
            continue

        lower_name = name.lower()
        if lower_name == "user-agent":
            arguments.extend(["-user_agent", value])
        elif lower_name not in {"host", "content-length"}:
            forwarded_headers.append(f"{name}: {value}")

    if forwarded_headers:
        arguments.extend(["-headers", "\r\n".join(forwarded_headers) + "\r\n"])

    return shlex.join(arguments)
