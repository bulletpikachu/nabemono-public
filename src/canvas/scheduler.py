from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord

from src.canvas.client import CanvasFetchError, load_timezone
from src.canvas.models import CanvasConnection
from src.canvas.service import CanvasService, canvas_service, format_summary, next_summary_at
from src.canvas.store import CanvasStore, canvas_store


logger = logging.getLogger(__name__)
MAX_SLEEP_SECONDS = 300
RETRY_SECONDS = 900


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CanvasScheduler:
    def __init__(
        self,
        store: CanvasStore = canvas_store,
        service: CanvasService = canvas_service,
    ) -> None:
        self.store = store
        self.service = service
        self.client: discord.Client | None = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self, client: discord.Client) -> None:
        self.client = client
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    def wake(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self.run_due()
                next_time = self.store.next_due_time()
            except Exception:
                logger.exception("Canvas summary scheduler iteration failed")
                next_time = None
            timeout = MAX_SLEEP_SECONDS
            if next_time is not None:
                timeout = min(
                    MAX_SLEEP_SECONDS,
                    max(0.0, (next_time - _utcnow()).total_seconds()),
                )
            try:
                await asyncio.wait_for(self._wake.wait(), timeout)
            except asyncio.TimeoutError:
                pass

    async def run_due(self, now: datetime | None = None) -> None:
        now = now or _utcnow()
        for connection in self.store.due(now):
            try:
                await self.deliver(connection, now=now)
            except Exception:
                logger.exception(
                    "Failed to process Canvas summary for user %s",
                    connection.user_id,
                )

    async def deliver(
        self,
        connection: CanvasConnection,
        *,
        now: datetime | None = None,
    ) -> None:
        now = now or _utcnow()
        zone = load_timezone(connection.timezone)
        local_date = now.astimezone(zone).date()
        if connection.last_summary_date == local_date:
            self.store.reschedule(
                connection.user_id,
                next_summary_at(
                    now,
                    connection.timezone,
                    summary_time=self.service.summary_time,
                ),
            )
            return

        try:
            result = await self.service.upcoming(
                connection.user_id,
                force_refresh=True,
                now=now,
            )
        except CanvasFetchError as error:
            logger.warning(
                "Canvas feed refresh failed for user %s: %s",
                connection.user_id,
                error,
            )
            local_now = now.astimezone(zone)
            if local_now.hour < 18:
                retry_at = now + timedelta(seconds=RETRY_SECONDS)
            else:
                retry_at = next_summary_at(
                    now,
                    connection.timezone,
                    summary_time=self.service.summary_time,
                )
            self.store.reschedule(connection.user_id, retry_at)
            return

        try:
            user = await self._resolve_user(connection.user_id)
            await user.send(
                format_summary(result, now=now),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as error:
            logger.warning(
                "Canvas summary DM failed for user %s: %s",
                connection.user_id,
                error,
            )

        self.store.reschedule(
            connection.user_id,
            next_summary_at(
                now,
                connection.timezone,
                summary_time=self.service.summary_time,
            ),
            last_summary_date=local_date,
        )

    async def _resolve_user(self, user_id: int):
        if self.client is None:
            raise RuntimeError("Canvas scheduler has not been started")
        return self.client.get_user(user_id) or await self.client.fetch_user(user_id)


canvas_scheduler = CanvasScheduler()

