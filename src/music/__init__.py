from src.music.models import (
    EnqueueResult,
    Track,
    YoutubeSelection,
    format_duration,
    format_track_label,
    remaining_queue_seconds,
)
from src.music.player import MusicManager, MusicSession, music_manager
from src.music.youtube import (
    MAX_PLAYLIST_TRACKS,
    build_ffmpeg_before_options,
    discover_youtube_tracks,
    is_youtube_playlist_url,
    is_youtube_url,
    track_from_youtube_entry,
)

__all__ = [
    "EnqueueResult",
    "MAX_PLAYLIST_TRACKS",
    "MusicManager",
    "MusicSession",
    "Track",
    "YoutubeSelection",
    "build_ffmpeg_before_options",
    "discover_youtube_tracks",
    "format_duration",
    "format_track_label",
    "is_youtube_playlist_url",
    "is_youtube_url",
    "music_manager",
    "remaining_queue_seconds",
    "track_from_youtube_entry",
]
