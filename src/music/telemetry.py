from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any

from src.config import MUSIC_TELEMETRY_PATH
from src.music.models import Track

logger = logging.getLogger(__name__)


def _user_identity(user: object | None) -> tuple[int | None, str | None]:
    if user is None:
        return None, None
    user_id = getattr(user, "id", None)
    if user_id is not None:
        try:
            user_id = int(user_id)
        except (TypeError, ValueError):
            user_id = None
    username = getattr(user, "display_name", None) or getattr(user, "name", None)
    if username is not None:
        username = str(username)
    return user_id, username


class MusicTelemetry:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path or MUSIC_TELEMETRY_PATH)
        self._write_lock = threading.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_db(self) -> None:
        if self._initialized:
            return
        with self._write_lock:
            if self._initialized:
                return
            self._init_db()
            self._initialized = True

    def _init_db(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS music_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    guild_id INTEGER,
                    user_id INTEGER,
                    username TEXT,
                    url TEXT,
                    title TEXT,
                    duration_seconds REAL,
                    query TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_music_events_type
                ON music_events(event_type)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_music_events_user
                ON music_events(user_id)
                """
            )

    def record(
        self,
        event_type: str,
        *,
        guild_id: int | None = None,
        user: object | None = None,
        user_id: int | None = None,
        username: str | None = None,
        url: str | None = None,
        title: str | None = None,
        duration_seconds: float | None = None,
        query: str | None = None,
        track: Track | None = None,
    ) -> None:
        if user is not None:
            derived_id, derived_name = _user_identity(user)
            if user_id is None:
                user_id = derived_id
            if username is None:
                username = derived_name
        if track is not None:
            url = url if url is not None else track.url
            title = title if title is not None else track.title
            duration_seconds = (
                duration_seconds
                if duration_seconds is not None
                else track.duration
            )
            if user_id is None:
                user_id = track.requested_by_id
            if username is None:
                username = track.requested_by_name

        try:
            self._ensure_db()
            with self._write_lock, closing(self._connect()) as connection, connection:
                connection.execute(
                    """
                    INSERT INTO music_events (
                        event_type,
                        guild_id,
                        user_id,
                        username,
                        url,
                        title,
                        duration_seconds,
                        query
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_type,
                        guild_id,
                        user_id,
                        username,
                        url,
                        title,
                        duration_seconds,
                        query,
                    ),
                )
        except Exception:
            logger.exception("Could not record music telemetry event %s", event_type)

    def summary(self) -> dict[str, Any]:
        self._ensure_db()
        with closing(self._connect()) as connection:
            counts = {
                row["event_type"]: row["count"]
                for row in connection.execute(
                    """
                    SELECT event_type, COUNT(*) AS count
                    FROM music_events
                    GROUP BY event_type
                    """
                )
            }
            unique_users = connection.execute(
                """
                SELECT COUNT(DISTINCT user_id) AS count
                FROM music_events
                WHERE user_id IS NOT NULL
                """
            ).fetchone()["count"]
            unique_tracks = connection.execute(
                """
                SELECT COUNT(DISTINCT url) AS count
                FROM music_events
                WHERE event_type = 'requested' AND url IS NOT NULL
                """
            ).fetchone()["count"]

        return {
            "tracks_requested": counts.get("requested", 0),
            "tracks_played": counts.get("played", 0),
            "tracks_skipped": counts.get("skipped", 0),
            "tracks_failed": counts.get("failed", 0),
            "searches": counts.get("searched", 0),
            "unique_users": unique_users,
            "unique_tracks_requested": unique_tracks,
        }


music_telemetry = MusicTelemetry()
