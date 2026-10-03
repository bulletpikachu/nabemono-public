from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from src.canvas.models import CanvasConnection, CanvasEvent
from src.config import (
    CANVAS_DB_PATH,
    CANVAS_ENCRYPTION_KEY,
    CANVAS_MAX_LOOKAHEAD_DAYS,
)


class CanvasError(ValueError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None) -> float | None:
    return value.timestamp() if value is not None else None


def _from_timestamp(value: float | None) -> datetime | None:
    return datetime.fromtimestamp(value, timezone.utc) if value is not None else None


class FeedCipher:
    def __init__(self, key: str | bytes | None = None) -> None:
        self._key = key.encode("ascii") if isinstance(key, str) and key else key

    @property
    def configured(self) -> bool:
        return bool(self._key)

    def _fernet(self) -> Fernet:
        if not self._key:
            raise CanvasError(
                "Canvas is not configured: set CANVAS_ENCRYPTION_KEY and restart the bot"
            )
        try:
            return Fernet(self._key)
        except (ValueError, TypeError):
            raise CanvasError("CANVAS_ENCRYPTION_KEY is not a valid Fernet key") from None

    def encrypt(self, value: str) -> str:
        return self._fernet().encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet().decrypt(value.encode("ascii")).decode("utf-8")
        except InvalidToken:
            raise CanvasError(
                "the saved Canvas feed cannot be decrypted with the configured key"
            ) from None


class CanvasStore:
    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        cipher: FeedCipher | None = None,
        max_lookahead_days: int = CANVAS_MAX_LOOKAHEAD_DAYS,
    ) -> None:
        self.db_path = Path(db_path or CANVAS_DB_PATH)
        self.cipher = cipher or FeedCipher(CANVAS_ENCRYPTION_KEY)
        self.max_lookahead_days = max_lookahead_days
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
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS canvas_connections (
                        user_id INTEGER PRIMARY KEY,
                        encrypted_feed_url TEXT NOT NULL,
                        timezone TEXT NOT NULL,
                        lookahead_days INTEGER NOT NULL,
                        next_summary_at REAL NOT NULL,
                        last_summary_date TEXT,
                        last_refresh_at REAL,
                        etag TEXT,
                        last_modified TEXT,
                        last_error TEXT,
                        created_at TEXT NOT NULL DEFAULT (datetime('now')),
                        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS canvas_events (
                        user_id INTEGER NOT NULL,
                        uid TEXT NOT NULL,
                        title TEXT NOT NULL,
                        due_at REAL,
                        due_date TEXT,
                        all_day INTEGER NOT NULL,
                        url TEXT,
                        course TEXT,
                        kind TEXT NOT NULL,
                        PRIMARY KEY (user_id, uid)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_canvas_due
                    ON canvas_connections(next_summary_at)
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_canvas_events_user
                    ON canvas_events(user_id, due_at, due_date)
                    """
                )
            self._initialized = True

    def _validate_days(self, value: int) -> int:
        try:
            days = int(value)
        except (TypeError, ValueError):
            raise CanvasError("look-ahead days must be an integer") from None
        if not 1 <= days <= self.max_lookahead_days:
            raise CanvasError(
                f"look-ahead days must be between 1 and {self.max_lookahead_days}"
            )
        return days

    def _connection_from_row(self, row: sqlite3.Row) -> CanvasConnection:
        last_summary = row["last_summary_date"]
        return CanvasConnection(
            user_id=row["user_id"],
            feed_url=self.cipher.decrypt(row["encrypted_feed_url"]),
            timezone=row["timezone"],
            lookahead_days=row["lookahead_days"],
            next_summary_at=_from_timestamp(row["next_summary_at"]),
            last_summary_date=date.fromisoformat(last_summary) if last_summary else None,
            last_refresh_at=_from_timestamp(row["last_refresh_at"]),
            etag=row["etag"],
            last_modified=row["last_modified"],
            last_error=row["last_error"],
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> CanvasEvent:
        return CanvasEvent(
            uid=row["uid"],
            title=row["title"],
            due_at=_from_timestamp(row["due_at"]),
            due_date=date.fromisoformat(row["due_date"]) if row["due_date"] else None,
            all_day=bool(row["all_day"]),
            url=row["url"],
            course=row["course"],
            kind=row["kind"],
        )

    def upsert_connection(
        self,
        *,
        user_id: int,
        feed_url: str,
        timezone_name: str,
        lookahead_days: int,
        next_summary_at: datetime,
    ) -> CanvasConnection:
        self._ensure_db()
        days = self._validate_days(lookahead_days)
        encrypted = self.cipher.encrypt(feed_url)
        with self._write_lock, closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM canvas_events WHERE user_id = ?", (user_id,))
            connection.execute(
                """
                INSERT INTO canvas_connections (
                    user_id, encrypted_feed_url, timezone, lookahead_days,
                    next_summary_at, last_summary_date, last_refresh_at,
                    etag, last_modified, last_error
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL)
                ON CONFLICT(user_id) DO UPDATE SET
                    encrypted_feed_url = excluded.encrypted_feed_url,
                    timezone = excluded.timezone,
                    lookahead_days = excluded.lookahead_days,
                    next_summary_at = excluded.next_summary_at,
                    last_summary_date = NULL,
                    last_refresh_at = NULL,
                    etag = NULL,
                    last_modified = NULL,
                    last_error = NULL,
                    updated_at = datetime('now')
                """,
                (
                    user_id,
                    encrypted,
                    timezone_name,
                    days,
                    next_summary_at.timestamp(),
                ),
            )
        connection_value = self.get_connection(user_id)
        assert connection_value is not None
        return connection_value

    def configure(
        self,
        user_id: int,
        *,
        timezone_name: str,
        lookahead_days: int,
        next_summary_at: datetime,
    ) -> CanvasConnection:
        self._ensure_db()
        days = self._validate_days(lookahead_days)
        with self._write_lock, closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE canvas_connections
                SET timezone = ?, lookahead_days = ?, next_summary_at = ?,
                    updated_at = datetime('now')
                WHERE user_id = ?
                """,
                (timezone_name, days, next_summary_at.timestamp(), user_id),
            )
        if cursor.rowcount == 0:
            raise CanvasError("connect a Canvas calendar first")
        result = self.get_connection(user_id)
        assert result is not None
        return result

    def get_connection(self, user_id: int) -> CanvasConnection | None:
        self._ensure_db()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM canvas_connections WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return self._connection_from_row(row) if row else None

    def due(self, now: datetime | None = None) -> list[CanvasConnection]:
        self._ensure_db()
        now = now or _utcnow()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM canvas_connections
                WHERE next_summary_at <= ?
                ORDER BY next_summary_at
                """,
                (now.timestamp(),),
            ).fetchall()
        return [self._connection_from_row(row) for row in rows]

    def next_due_time(self) -> datetime | None:
        self._ensure_db()
        with closing(self._connect()) as connection:
            value = connection.execute(
                "SELECT MIN(next_summary_at) FROM canvas_connections"
            ).fetchone()[0]
        return _from_timestamp(value)

    def replace_events(
        self,
        user_id: int,
        events: list[CanvasEvent] | tuple[CanvasEvent, ...],
        *,
        fetched_at: datetime | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        self._ensure_db()
        fetched_at = fetched_at or _utcnow()
        with self._write_lock, closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM canvas_events WHERE user_id = ?", (user_id,))
            connection.executemany(
                """
                INSERT INTO canvas_events (
                    user_id, uid, title, due_at, due_date, all_day,
                    url, course, kind
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        user_id,
                        event.uid,
                        event.title,
                        _timestamp(event.due_at),
                        event.due_date.isoformat() if event.due_date else None,
                        int(event.all_day),
                        event.url,
                        event.course,
                        event.kind,
                    )
                    for event in events
                ],
            )
            connection.execute(
                """
                UPDATE canvas_connections
                SET last_refresh_at = ?, etag = ?, last_modified = ?,
                    last_error = NULL, updated_at = datetime('now')
                WHERE user_id = ?
                """,
                (fetched_at.timestamp(), etag, last_modified, user_id),
            )

    def mark_not_modified(
        self,
        user_id: int,
        *,
        fetched_at: datetime | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> None:
        self._ensure_db()
        fetched_at = fetched_at or _utcnow()
        with self._write_lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE canvas_connections
                SET last_refresh_at = ?, etag = COALESCE(?, etag),
                    last_modified = COALESCE(?, last_modified), last_error = NULL,
                    updated_at = datetime('now')
                WHERE user_id = ?
                """,
                (fetched_at.timestamp(), etag, last_modified, user_id),
            )

    def record_refresh_error(self, user_id: int, error: str) -> None:
        self._ensure_db()
        with self._write_lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE canvas_connections
                SET last_error = ?, updated_at = datetime('now')
                WHERE user_id = ?
                """,
                (error[:500], user_id),
            )

    def list_events(self, user_id: int) -> list[CanvasEvent]:
        self._ensure_db()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM canvas_events
                WHERE user_id = ?
                ORDER BY COALESCE(due_at, 0), due_date, title
                """,
                (user_id,),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def reschedule(
        self,
        user_id: int,
        next_summary_at: datetime,
        *,
        last_summary_date: date | None = None,
    ) -> None:
        self._ensure_db()
        with self._write_lock, closing(self._connect()) as connection, connection:
            connection.execute(
                """
                UPDATE canvas_connections
                SET next_summary_at = ?, last_summary_date = COALESCE(?, last_summary_date),
                    updated_at = datetime('now')
                WHERE user_id = ?
                """,
                (
                    next_summary_at.timestamp(),
                    last_summary_date.isoformat() if last_summary_date else None,
                    user_id,
                ),
            )

    def disconnect(self, user_id: int) -> bool:
        self._ensure_db()
        with self._write_lock, closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM canvas_events WHERE user_id = ?", (user_id,))
            cursor = connection.execute(
                "DELETE FROM canvas_connections WHERE user_id = ?",
                (user_id,),
            )
        return cursor.rowcount > 0


canvas_store = CanvasStore()

