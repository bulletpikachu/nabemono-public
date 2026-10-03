from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

from src.config import REMINDERS_DB_PATH, REMINDERS_MAX_PER_USER

logger = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "America/Los_Angeles"
MIN_REPEAT_SECONDS = 60
PAST_GRACE_SECONDS = 30
MAX_MESSAGE_CHARS = 1800
MAX_SLEEP_SECONDS = 300
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_REPEAT_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


class ReminderError(ValueError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _load_zone(name: str | None) -> ZoneInfo:
    zone_name = (name or DEFAULT_TIMEZONE).strip()
    try:
        return ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ReminderError(f"unknown timezone: {zone_name}") from None


def _parse_repeat_time(value: str) -> time:
    match = _REPEAT_TIME_RE.match(value.strip())
    if not match:
        raise ReminderError("repeat_time must be HH:MM in 24-hour time")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ReminderError("repeat_time must be HH:MM in 24-hour time")
    return time(hour, minute)


def _normalize_weekdays(values) -> tuple[str, ...]:
    if not values:
        return ()
    if isinstance(values, str):
        values = values.split(",")
    requested = {str(value).strip().lower()[:3] for value in values}
    unknown = requested - set(WEEKDAYS)
    if unknown:
        raise ReminderError(f"unknown weekdays: {', '.join(sorted(unknown))}")
    return tuple(day for day in WEEKDAYS if day in requested)


def next_calendar_fire(
    after: datetime,
    repeat_time: str,
    weekdays: tuple[str, ...],
    zone: ZoneInfo,
) -> datetime:
    clock = _parse_repeat_time(repeat_time)
    local_start = after.astimezone(zone).date()
    for offset in range(8):
        day = local_start + timedelta(days=offset)
        if weekdays and WEEKDAYS[day.weekday()] not in weekdays:
            continue
        candidate = datetime.combine(day, clock, tzinfo=zone).astimezone(timezone.utc)
        if candidate > after:
            return candidate
    raise ReminderError("could not find the next time for this repeat rule")


@dataclass(frozen=True)
class Schedule:
    first_fire_at: datetime
    timezone: str
    repeat_seconds: int | None = None
    repeat_time: str | None = None
    repeat_weekdays: tuple[str, ...] = ()


def build_schedule(
    *,
    now: datetime | None = None,
    seconds: int | None = None,
    at: str | None = None,
    repeat_seconds: int | None = None,
    repeat_time: str | None = None,
    repeat_weekdays=None,
    timezone_name: str | None = None,
) -> Schedule:
    now = now or _utcnow()
    zone = _load_zone(timezone_name)

    if seconds is not None and at:
        raise ReminderError("give either seconds or at, not both")
    if repeat_seconds is not None and repeat_time:
        raise ReminderError("give either repeat_seconds or repeat_time, not both")
    weekdays = _normalize_weekdays(repeat_weekdays)
    if weekdays and not repeat_time:
        raise ReminderError("repeat_weekdays only works with repeat_time")
    if repeat_seconds is not None:
        repeat_seconds = int(repeat_seconds)
        if repeat_seconds < MIN_REPEAT_SECONDS:
            raise ReminderError(
                f"repeat_seconds must be at least {MIN_REPEAT_SECONDS}"
            )
    if repeat_time:
        clock = _parse_repeat_time(repeat_time)
        repeat_time = f"{clock.hour:02d}:{clock.minute:02d}"

    if seconds is not None:
        seconds = int(seconds)
        if seconds < 0:
            raise ReminderError("seconds must not be negative")
        first = now + timedelta(seconds=seconds)
    elif at:
        try:
            parsed = datetime.fromisoformat(at.strip())
        except ValueError:
            raise ReminderError("at must be an ISO 8601 datetime") from None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=zone)
        first = parsed.astimezone(timezone.utc)
        if first < now - timedelta(seconds=PAST_GRACE_SECONDS):
            raise ReminderError(
                f"at is in the past; the current time is {now.astimezone(zone).isoformat()}"
            )
        first = max(first, now)
    elif repeat_time:
        first = next_calendar_fire(now, repeat_time, weekdays, zone)
    elif repeat_seconds is not None:
        first = now + timedelta(seconds=repeat_seconds)
    else:
        raise ReminderError("give seconds, at, repeat_seconds, or repeat_time")

    return Schedule(
        first_fire_at=first,
        timezone=zone.key,
        repeat_seconds=repeat_seconds,
        repeat_time=repeat_time,
        repeat_weekdays=weekdays,
    )


@dataclass(frozen=True)
class Reminder:
    id: int
    user_id: int
    channel_id: int
    from_guild: bool
    deliver_dm: bool
    message: str
    timezone: str
    next_fire_at: datetime
    repeat_seconds: int | None
    repeat_time: str | None
    repeat_weekdays: tuple[str, ...]
    status: str

    @property
    def is_recurring(self) -> bool:
        return self.repeat_seconds is not None or self.repeat_time is not None

    def next_after(self, now: datetime) -> datetime:
        if self.repeat_seconds is not None:
            interval = timedelta(seconds=self.repeat_seconds)
            missed = max(0, int((now - self.next_fire_at) / interval))
            return self.next_fire_at + interval * (missed + 1)
        return next_calendar_fire(
            max(now, self.next_fire_at),
            self.repeat_time,
            self.repeat_weekdays,
            _load_zone(self.timezone),
        )

    def describe_repeat(self) -> str | None:
        if self.repeat_seconds is not None:
            return f"every {self.repeat_seconds} seconds"
        if self.repeat_time:
            days = ", ".join(self.repeat_weekdays) if self.repeat_weekdays else "every day"
            return f"{days} at {self.repeat_time} {self.timezone}"
        return None

    def to_payload(self) -> dict:
        return {
            "id": self.id,
            "message": self.message,
            "next_fire_at": self.next_fire_at.astimezone(
                _load_zone(self.timezone)
            ).isoformat(),
            "repeat": self.describe_repeat(),
            "delivery": "dm",
        }


class ReminderStore:
    def __init__(
        self,
        db_path: str | Path | None = None,
        max_per_user: int | None = None,
    ) -> None:
        self.db_path = Path(db_path or REMINDERS_DB_PATH)
        self.max_per_user = max_per_user if max_per_user is not None else REMINDERS_MAX_PER_USER
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
                    CREATE TABLE IF NOT EXISTS reminders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        channel_id INTEGER NOT NULL,
                        from_guild INTEGER NOT NULL,
                        deliver_dm INTEGER NOT NULL,
                        message TEXT NOT NULL,
                        timezone TEXT NOT NULL,
                        next_fire_at REAL NOT NULL,
                        repeat_seconds INTEGER,
                        repeat_time TEXT,
                        repeat_weekdays TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'active',
                        created_at TEXT NOT NULL DEFAULT (datetime('now'))
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_reminders_due
                    ON reminders(status, next_fire_at)
                    """
                )
            self._initialized = True

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Reminder:
        weekdays = row["repeat_weekdays"]
        return Reminder(
            id=row["id"],
            user_id=row["user_id"],
            channel_id=row["channel_id"],
            from_guild=bool(row["from_guild"]),
            deliver_dm=bool(row["deliver_dm"]),
            message=row["message"],
            timezone=row["timezone"],
            next_fire_at=datetime.fromtimestamp(row["next_fire_at"], timezone.utc),
            repeat_seconds=row["repeat_seconds"],
            repeat_time=row["repeat_time"],
            repeat_weekdays=tuple(weekdays.split(",")) if weekdays else (),
            status=row["status"],
        )

    def create(
        self,
        *,
        user_id: int,
        channel_id: int,
        from_guild: bool,
        deliver_dm: bool,
        message: str,
        schedule: Schedule,
    ) -> Reminder:
        self._ensure_db()
        with self._write_lock, closing(self._connect()) as connection, connection:
            active = connection.execute(
                "SELECT COUNT(*) FROM reminders WHERE user_id = ? AND status = 'active'",
                (user_id,),
            ).fetchone()[0]
            if active >= self.max_per_user:
                raise ReminderError(
                    f"you already have {active} active reminders; "
                    f"the limit is {self.max_per_user}. cancel one first"
                )
            cursor = connection.execute(
                """
                INSERT INTO reminders (
                    user_id, channel_id, from_guild, deliver_dm, message, timezone,
                    next_fire_at, repeat_seconds, repeat_time, repeat_weekdays
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    channel_id,
                    int(from_guild),
                    int(deliver_dm),
                    message,
                    schedule.timezone,
                    schedule.first_fire_at.timestamp(),
                    schedule.repeat_seconds,
                    schedule.repeat_time,
                    ",".join(schedule.repeat_weekdays),
                ),
            )
            row = connection.execute(
                "SELECT * FROM reminders WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        return self._from_row(row)

    def get(self, reminder_id: int) -> Reminder | None:
        self._ensure_db()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM reminders WHERE id = ?",
                (reminder_id,),
            ).fetchone()
        return self._from_row(row) if row else None

    def list_active(self, user_id: int) -> list[Reminder]:
        self._ensure_db()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM reminders
                WHERE user_id = ? AND status = 'active'
                ORDER BY next_fire_at
                """,
                (user_id,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def due(self, now: datetime | None = None) -> list[Reminder]:
        self._ensure_db()
        now = now or _utcnow()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM reminders
                WHERE status = 'active' AND next_fire_at <= ?
                ORDER BY next_fire_at
                """,
                (now.timestamp(),),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def next_due_time(self) -> datetime | None:
        self._ensure_db()
        with closing(self._connect()) as connection:
            value = connection.execute(
                "SELECT MIN(next_fire_at) FROM reminders WHERE status = 'active'"
            ).fetchone()[0]
        return datetime.fromtimestamp(value, timezone.utc) if value is not None else None

    def cancel(self, reminder_id: int, user_id: int) -> bool:
        return self._set_status(reminder_id, "cancelled", user_id=user_id)

    def complete(self, reminder_id: int) -> None:
        self._set_status(reminder_id, "done")

    def mark_failed(self, reminder_id: int) -> None:
        self._set_status(reminder_id, "failed")

    def reschedule(self, reminder_id: int, next_fire_at: datetime) -> None:
        self._ensure_db()
        with self._write_lock, closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE reminders SET next_fire_at = ? WHERE id = ? AND status = 'active'",
                (next_fire_at.timestamp(), reminder_id),
            )

    def _set_status(
        self,
        reminder_id: int,
        status: str,
        *,
        user_id: int | None = None,
    ) -> bool:
        self._ensure_db()
        query = "UPDATE reminders SET status = ? WHERE id = ? AND status = 'active'"
        params: list = [status, reminder_id]
        if user_id is not None:
            query += " AND user_id = ?"
            params.append(user_id)
        with self._write_lock, closing(self._connect()) as connection, connection:
            cursor = connection.execute(query, params)
        return cursor.rowcount > 0


class ReminderScheduler:
    def __init__(self, store: ReminderStore) -> None:
        self.store = store
        self.client: discord.Client | None = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self, client: discord.Client) -> None:
        self.client = client
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    def wake(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self.run_due()
                next_time = self.store.next_due_time()
            except Exception:
                logger.exception("Reminder scheduler iteration failed")
                next_time = None
            timeout = MAX_SLEEP_SECONDS
            if next_time is not None:
                remaining = (next_time - _utcnow()).total_seconds()
                timeout = min(MAX_SLEEP_SECONDS, max(0.0, remaining))
            try:
                await asyncio.wait_for(self._wake.wait(), timeout)
            except asyncio.TimeoutError:
                pass

    async def run_due(self, now: datetime | None = None) -> None:
        for reminder in self.store.due(now):
            try:
                await self.deliver(reminder, now)
            except Exception:
                logger.exception("Failed to process reminder %s", reminder.id)

    async def deliver(self, reminder: Reminder, now: datetime | None = None) -> None:
        try:
            await self._send(reminder)
        except Exception as error:
            logger.warning("Reminder %s could not be sent: %s", reminder.id, error)
            self.store.mark_failed(reminder.id)
            if reminder.from_guild:
                await self._notify_dm_failure(reminder)
            return

        if reminder.is_recurring:
            self.store.reschedule(reminder.id, reminder.next_after(now or _utcnow()))
        else:
            self.store.complete(reminder.id)

    async def _send(self, reminder: Reminder) -> None:
        user = await self._resolve_user(reminder.user_id)
        await user.send(
            reminder.message,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _notify_dm_failure(self, reminder: Reminder) -> None:
        try:
            channel = await self._resolve_channel(reminder.channel_id)
            await channel.send(
                f"<@{reminder.user_id}> i couldn't DM you a reminder. "
                "check that your DMs are open",
                allowed_mentions=_requester_only(reminder.user_id),
            )
        except Exception as error:
            logger.warning(
                "Could not post DM failure notice for reminder %s: %s",
                reminder.id,
                error,
            )

    async def _resolve_user(self, user_id: int):
        return self.client.get_user(user_id) or await self.client.fetch_user(user_id)

    async def _resolve_channel(self, channel_id: int):
        return self.client.get_channel(channel_id) or await self.client.fetch_channel(
            channel_id
        )


def _requester_only(user_id: int) -> discord.AllowedMentions:
    return discord.AllowedMentions(
        everyone=False,
        roles=False,
        users=[discord.Object(id=user_id)],
    )


reminder_store = ReminderStore()
reminder_scheduler = ReminderScheduler(reminder_store)


def create_reminder(
    original_message,
    reminder_message: str,
    *,
    seconds: int | None = None,
    at: str | None = None,
    repeat_seconds: int | None = None,
    repeat_time: str | None = None,
    repeat_weekdays=None,
    timezone: str | None = None,
    store: ReminderStore | None = None,
    scheduler: ReminderScheduler | None = None,
    now: datetime | None = None,
) -> dict:
    store = store or reminder_store
    scheduler = scheduler or reminder_scheduler
    text = (reminder_message or "").strip()
    if not text:
        raise ReminderError("reminder_message must not be empty")
    schedule = build_schedule(
        now=now,
        seconds=seconds,
        at=at,
        repeat_seconds=repeat_seconds,
        repeat_time=repeat_time,
        repeat_weekdays=repeat_weekdays,
        timezone_name=timezone,
    )
    from_guild = getattr(original_message, "guild", None) is not None
    reminder = store.create(
        user_id=original_message.author.id,
        channel_id=original_message.channel.id,
        from_guild=from_guild,
        deliver_dm=True,
        message=text[:MAX_MESSAGE_CHARS],
        schedule=schedule,
    )
    scheduler.wake()
    return {"status": "scheduled", **reminder.to_payload()}


def list_reminders(original_message, *, store: ReminderStore | None = None) -> dict:
    store = store or reminder_store
    reminders = store.list_active(original_message.author.id)
    return {"reminders": [reminder.to_payload() for reminder in reminders]}


def cancel_reminder(
    original_message,
    reminder_id: int,
    *,
    store: ReminderStore | None = None,
    scheduler: ReminderScheduler | None = None,
) -> dict:
    store = store or reminder_store
    scheduler = scheduler or reminder_scheduler
    if not store.cancel(int(reminder_id), original_message.author.id):
        raise ReminderError(f"you have no active reminder with id {reminder_id}")
    scheduler.wake()
    return {"status": "cancelled", "reminder_id": int(reminder_id)}
