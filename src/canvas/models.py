from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class CanvasEvent:
    uid: str
    title: str
    due_at: datetime | None
    due_date: date | None
    all_day: bool
    url: str | None = None
    course: str | None = None
    kind: str = "event"

    def local_date(self, zone: ZoneInfo) -> date:
        if self.due_date is not None:
            return self.due_date
        if self.due_at is None:
            raise ValueError("event has no due date")
        value = self.due_at
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(zone).date()

    def local_time(self, zone: ZoneInfo) -> str | None:
        if self.all_day or self.due_at is None:
            return None
        value = self.due_at
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(zone).strftime("%I:%M %p").lstrip("0")


@dataclass(frozen=True)
class CanvasConnection:
    user_id: int
    feed_url: str
    timezone: str
    lookahead_days: int
    next_summary_at: datetime
    last_summary_date: date | None = None
    last_refresh_at: datetime | None = None
    etag: str | None = None
    last_modified: str | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class FetchResult:
    events: tuple[CanvasEvent, ...]
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False

