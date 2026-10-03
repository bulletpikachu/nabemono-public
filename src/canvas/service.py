from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from src.canvas.client import CanvasClient, CanvasFetchError, canvas_client, load_timezone
from src.canvas.models import CanvasConnection, CanvasEvent
from src.canvas.store import CanvasError, CanvasStore, canvas_store
from src.config import (
    CANVAS_CACHE_SECONDS,
    CANVAS_DEFAULT_LOOKAHEAD_DAYS,
    CANVAS_DEFAULT_TIMEZONE,
    CANVAS_SUMMARY_TIME,
)


MAX_SUMMARY_CHARS = 1900


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _summary_clock(value: str = CANVAS_SUMMARY_TIME) -> time:
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
        return time(hour, minute)
    except (TypeError, ValueError):
        raise CanvasError("CANVAS_SUMMARY_TIME must be HH:MM in 24-hour time") from None


def next_summary_at(
    now: datetime,
    timezone_name: str,
    *,
    summary_time: str = CANVAS_SUMMARY_TIME,
) -> datetime:
    zone = load_timezone(timezone_name)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local_now = now.astimezone(zone)
    candidate = datetime.combine(local_now.date(), _summary_clock(summary_time), zone)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


@dataclass(frozen=True)
class UpcomingAssignments:
    connection: CanvasConnection
    events: tuple[CanvasEvent, ...]
    days: int
    stale: bool = False
    refresh_error: str | None = None


class CanvasService:
    def __init__(
        self,
        store: CanvasStore = canvas_store,
        client: CanvasClient = canvas_client,
        *,
        cache_seconds: int = CANVAS_CACHE_SECONDS,
        summary_time: str = CANVAS_SUMMARY_TIME,
    ) -> None:
        self.store = store
        self.client = client
        self.cache_seconds = cache_seconds
        self.summary_time = summary_time

    async def connect(
        self,
        user_id: int,
        feed_url: str,
        *,
        timezone_name: str = CANVAS_DEFAULT_TIMEZONE,
        lookahead_days: int = CANVAS_DEFAULT_LOOKAHEAD_DAYS,
        now: datetime | None = None,
    ) -> CanvasConnection:
        now = now or _utcnow()
        zone = load_timezone(timezone_name)
        result = await self.client.fetch(feed_url, zone.key, now=now)
        connection = self.store.upsert_connection(
            user_id=user_id,
            feed_url=feed_url.strip(),
            timezone_name=zone.key,
            lookahead_days=lookahead_days,
            next_summary_at=next_summary_at(
                now,
                zone.key,
                summary_time=self.summary_time,
            ),
        )
        self.store.replace_events(
            user_id,
            result.events,
            fetched_at=now,
            etag=result.etag,
            last_modified=result.last_modified,
        )
        return self.store.get_connection(user_id) or connection

    def configure(
        self,
        user_id: int,
        *,
        timezone_name: str,
        lookahead_days: int,
        now: datetime | None = None,
    ) -> CanvasConnection:
        now = now or _utcnow()
        zone = load_timezone(timezone_name)
        return self.store.configure(
            user_id,
            timezone_name=zone.key,
            lookahead_days=lookahead_days,
            next_summary_at=next_summary_at(
                now,
                zone.key,
                summary_time=self.summary_time,
            ),
        )

    async def refresh(
        self,
        user_id: int,
        *,
        force: bool = False,
        now: datetime | None = None,
    ) -> CanvasConnection:
        now = now or _utcnow()
        connection = self.store.get_connection(user_id)
        if connection is None:
            raise CanvasError("connect a Canvas calendar first")
        if (
            not force
            and connection.last_refresh_at is not None
            and (now - connection.last_refresh_at).total_seconds() < self.cache_seconds
        ):
            return connection
        try:
            result = await self.client.fetch(
                connection.feed_url,
                connection.timezone,
                etag=connection.etag,
                last_modified=connection.last_modified,
                now=now,
            )
            if result.not_modified:
                self.store.mark_not_modified(
                    user_id,
                    fetched_at=now,
                    etag=result.etag,
                    last_modified=result.last_modified,
                )
            else:
                self.store.replace_events(
                    user_id,
                    result.events,
                    fetched_at=now,
                    etag=result.etag,
                    last_modified=result.last_modified,
                )
        except CanvasFetchError as error:
            self.store.record_refresh_error(user_id, str(error))
            raise
        updated = self.store.get_connection(user_id)
        assert updated is not None
        return updated

    async def upcoming(
        self,
        user_id: int,
        *,
        days: int | None = None,
        force_refresh: bool = False,
        now: datetime | None = None,
    ) -> UpcomingAssignments:
        now = now or _utcnow()
        connection = self.store.get_connection(user_id)
        if connection is None:
            raise CanvasError("connect a Canvas calendar first")
        try:
            requested_days = connection.lookahead_days if days is None else int(days)
        except (TypeError, ValueError):
            raise CanvasError("days must be an integer") from None
        if not 1 <= requested_days <= self.store.max_lookahead_days:
            raise CanvasError(
                f"days must be between 1 and {self.store.max_lookahead_days}"
            )

        stale = False
        refresh_error = None
        try:
            connection = await self.refresh(
                user_id,
                force=force_refresh,
                now=now,
            )
        except CanvasFetchError as error:
            connection = self.store.get_connection(user_id) or connection
            if connection.last_refresh_at is None:
                raise
            stale = True
            refresh_error = str(error)

        zone = load_timezone(connection.timezone)
        start = now.astimezone(zone).date()
        stop = start + timedelta(days=requested_days)
        matching = [
            event
            for event in self.store.list_events(user_id)
            if start <= event.local_date(zone) < stop
        ]
        matching.sort(key=lambda event: _event_sort_key(event, zone))
        return UpcomingAssignments(
            connection=connection,
            events=tuple(matching),
            days=requested_days,
            stale=stale,
            refresh_error=refresh_error,
        )

    def disconnect(self, user_id: int) -> bool:
        return self.store.disconnect(user_id)


def _event_sort_key(event: CanvasEvent, zone: ZoneInfo):
    if event.due_at is not None:
        moment = event.due_at.astimezone(zone)
    else:
        moment = datetime.combine(event.local_date(zone), time.min, zone)
    return (moment, 0 if event.kind == "assignment" else 1, event.title.lower())


def _event_line(event: CanvasEvent, zone: ZoneInfo) -> str:
    clock = event.local_time(zone)
    details = []
    if clock:
        details.append(clock)
    if event.course:
        details.append(event.course)
    suffix = f" — {' · '.join(details)}" if details else ""
    title = event.title.replace("\n", " ").strip()
    title = title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    if event.url:
        return f"• [{title}]({event.url}){suffix}"
    return f"• {title}{suffix}"


def format_summary(
    result: UpcomingAssignments,
    *,
    now: datetime | None = None,
) -> str:
    now = now or _utcnow()
    zone = load_timezone(result.connection.timezone)
    today = now.astimezone(zone).date()
    assignments = [event for event in result.events if event.kind == "assignment"]
    other_events = [event for event in result.events if event.kind != "assignment"]
    if result.days == 1:
        lines = ["**Assignments due today:**"]
    else:
        lines = [f"**Assignments due in the next {result.days} days:**"]
    _append_grouped(
        lines,
        assignments,
        zone,
        today,
        show_dates=result.days > 1,
    )
    if not assignments:
        lines.append("None.")
    if other_events:
        lines.extend(["", "**Other Canvas events:**"])
        _append_grouped(
            lines,
            other_events,
            zone,
            today,
            show_dates=result.days > 1,
        )
    if result.stale:
        lines.extend(
            [
                "",
                "_Canvas could not be refreshed; this summary uses the last saved feed._",
            ]
        )
    text = "\n".join(lines)
    if len(text) > MAX_SUMMARY_CHARS:
        text = text[: MAX_SUMMARY_CHARS - 35].rstrip() + "\n…additional items omitted."
    return text


def _append_grouped(
    lines: list[str],
    events: list[CanvasEvent],
    zone: ZoneInfo,
    today: date,
    *,
    show_dates: bool,
) -> None:
    current_date = None
    for event in events:
        event_date = event.local_date(zone)
        if event_date != current_date:
            if show_dates:
                label = (
                    "Today"
                    if event_date == today
                    else event_date.strftime("%A, %b %d").replace(" 0", " ")
                )
                lines.append(f"__{label}__")
            current_date = event_date
        lines.append(_event_line(event, zone))


def upcoming_payload(result: UpcomingAssignments) -> dict:
    zone = load_timezone(result.connection.timezone)
    return {
        "days": result.days,
        "timezone": result.connection.timezone,
        "stale": result.stale,
        "refreshed_at": (
            result.connection.last_refresh_at.isoformat()
            if result.connection.last_refresh_at
            else None
        ),
        "items": [
            {
                "title": event.title,
                "course": event.course,
                "kind": event.kind,
                "due_at": (
                    event.due_at.astimezone(zone).isoformat()
                    if event.due_at is not None
                    else event.due_date.isoformat()
                ),
                "all_day": event.all_day,
                "url": event.url,
            }
            for event in result.events
        ],
    }


canvas_service = CanvasService()

