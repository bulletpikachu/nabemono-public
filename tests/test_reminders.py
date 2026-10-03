import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from src.reminders import (
    ReminderError,
    ReminderScheduler,
    ReminderStore,
    build_schedule,
    cancel_reminder,
    create_reminder,
    list_reminders,
)

NOW = datetime(2026, 9, 23, 16, 0, tzinfo=timezone.utc)  # Wed 09:00 PDT


def make_message(user_id=1, channel_id=10, guild=True):
    return SimpleNamespace(
        author=SimpleNamespace(id=user_id),
        channel=SimpleNamespace(id=channel_id),
        guild=SimpleNamespace(id=5) if guild else None,
    )


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db_path = Path(directory.name) / "reminders.sqlite3"
        self.store = ReminderStore(self.db_path, max_per_user=3)
        self.scheduler = ReminderScheduler(self.store)

    def create(self, message=None, **kwargs):
        kwargs.setdefault("now", NOW)
        return create_reminder(
            message or make_message(),
            kwargs.pop("reminder_message", "drink water"),
            store=self.store,
            scheduler=self.scheduler,
            **kwargs,
        )


class BuildScheduleTests(unittest.TestCase):
    def test_relative_seconds(self):
        schedule = build_schedule(now=NOW, seconds=300)
        self.assertEqual(NOW + timedelta(seconds=300), schedule.first_fire_at)
        self.assertIsNone(schedule.repeat_seconds)

    def test_naive_at_uses_default_pacific_timezone(self):
        schedule = build_schedule(now=NOW, at="2026-09-23T17:00:00")
        self.assertEqual(
            datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc),
            schedule.first_fire_at,
        )
        self.assertEqual("America/Los_Angeles", schedule.timezone)

    def test_naive_at_uses_given_timezone(self):
        schedule = build_schedule(
            now=NOW,
            at="2026-09-23T17:00:00",
            timezone_name="America/New_York",
        )
        self.assertEqual(
            datetime(2026, 9, 23, 21, 0, tzinfo=timezone.utc),
            schedule.first_fire_at,
        )

    def test_at_with_offset_ignores_timezone(self):
        schedule = build_schedule(now=NOW, at="2026-09-23T18:00:00+00:00")
        self.assertEqual(
            datetime(2026, 9, 23, 18, 0, tzinfo=timezone.utc),
            schedule.first_fire_at,
        )

    def test_at_within_grace_window_fires_now(self):
        schedule = build_schedule(now=NOW, at="2026-09-23T08:59:45")
        self.assertEqual(NOW, schedule.first_fire_at)

    def test_rejects_at_past_grace_window(self):
        with self.assertRaisesRegex(ReminderError, "in the past"):
            build_schedule(now=NOW, at="2026-09-23T08:59:00")

    def test_rejects_conflicting_arguments(self):
        cases = [
            {"seconds": 5, "at": "2026-09-23T10:00:00"},
            {"seconds": 5, "repeat_seconds": 60, "repeat_time": "09:00"},
            {"seconds": 5, "repeat_seconds": 60, "repeat_weekdays": ["mon"]},
            {"repeat_seconds": 59},
            {"repeat_time": "25:00"},
            {"seconds": 5, "timezone_name": "Mars/Olympus"},
            {},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ReminderError):
                build_schedule(now=NOW, **kwargs)

    def test_repeat_time_alone_fires_at_next_match(self):
        schedule = build_schedule(now=NOW, repeat_time="09:00")
        self.assertEqual(
            datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc),
            schedule.first_fire_at,
        )

    def test_repeat_time_rolls_over_midnight(self):
        now = datetime(2026, 9, 24, 6, 50, tzinfo=timezone.utc)  # 23:50 PDT
        schedule = build_schedule(now=now, repeat_time="00:30")
        self.assertEqual(
            datetime(2026, 9, 24, 7, 30, tzinfo=timezone.utc),
            schedule.first_fire_at,
        )

    def test_weekdays_skip_the_weekend(self):
        friday = datetime(2026, 9, 25, 16, 0, tzinfo=timezone.utc)
        schedule = build_schedule(
            now=friday,
            repeat_time="09:00",
            repeat_weekdays=["mon", "tue", "wed", "thu", "fri"],
        )
        self.assertEqual(
            datetime(2026, 9, 28, 16, 0, tzinfo=timezone.utc),
            schedule.first_fire_at,
        )
        self.assertEqual(("mon", "tue", "wed", "thu", "fri"), schedule.repeat_weekdays)

    def test_repeat_seconds_alone_waits_one_interval(self):
        schedule = build_schedule(now=NOW, repeat_seconds=1800)
        self.assertEqual(NOW + timedelta(minutes=30), schedule.first_fire_at)


class RecurrenceTests(StoreTestCase):
    def test_calendar_rule_keeps_local_time_across_dst(self):
        before_dst = datetime(2026, 3, 7, 17, 0, tzinfo=timezone.utc)  # 09:00 PST
        payload = self.create(now=before_dst - timedelta(hours=1), repeat_time="09:00")
        reminder = self.store.get(payload["id"])
        self.assertEqual(before_dst, reminder.next_fire_at)

        self.assertEqual(
            datetime(2026, 3, 8, 16, 0, tzinfo=timezone.utc),  # 09:00 PDT
            reminder.next_after(before_dst),
        )

    def test_interval_catches_up_to_next_future_slot(self):
        payload = self.create(seconds=0, repeat_seconds=3600)
        reminder = self.store.get(payload["id"])
        later = NOW + timedelta(hours=3, minutes=30)
        self.assertEqual(NOW + timedelta(hours=4), reminder.next_after(later))


class DeliveryTargetTests(StoreTestCase):
    def test_server_request_is_a_dm(self):
        payload = self.create(seconds=5)
        self.assertEqual("dm", payload["delivery"])
        self.assertTrue(self.store.get(payload["id"]).deliver_dm)

    def test_dm_request_is_a_dm(self):
        payload = self.create(make_message(guild=False), seconds=5)
        self.assertEqual("dm", payload["delivery"])


class StoreTests(StoreTestCase):
    def test_reminders_survive_a_new_store_instance(self):
        payload = self.create(repeat_time="09:00", repeat_weekdays=["mon"])

        reopened = ReminderStore(self.db_path).get(payload["id"])

        self.assertEqual("drink water", reopened.message)
        self.assertEqual(("mon",), reopened.repeat_weekdays)
        self.assertEqual("09:00", reopened.repeat_time)
        self.assertEqual("active", reopened.status)

    def test_enforces_per_user_cap(self):
        for _ in range(3):
            self.create(seconds=5)
        with self.assertRaisesRegex(ReminderError, "limit is 3"):
            self.create(seconds=5)
        self.create(make_message(user_id=2), seconds=5)

    def test_only_the_owner_can_cancel(self):
        payload = self.create(seconds=5)

        with self.assertRaises(ReminderError):
            cancel_reminder(
                make_message(user_id=2),
                payload["id"],
                store=self.store,
                scheduler=self.scheduler,
            )
        result = cancel_reminder(
            make_message(),
            payload["id"],
            store=self.store,
            scheduler=self.scheduler,
        )

        self.assertEqual("cancelled", result["status"])
        self.assertEqual([], list_reminders(make_message(), store=self.store)["reminders"])

    def test_list_reports_every_reminder_as_a_dm(self):
        self.create(seconds=5)
        self.create(make_message(guild=False), seconds=10)

        reminders = list_reminders(make_message(), store=self.store)["reminders"]

        self.assertEqual(["dm", "dm"], [r["delivery"] for r in reminders])

    def test_create_wakes_the_scheduler(self):
        self.scheduler.wake = Mock()
        self.create(seconds=5)
        self.scheduler.wake.assert_called_once()


class SchedulerTests(StoreTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.channel = SimpleNamespace(send=AsyncMock())
        self.user = SimpleNamespace(send=AsyncMock())
        self.client = SimpleNamespace(
            get_user=Mock(return_value=None),
            fetch_user=AsyncMock(return_value=self.user),
            get_channel=Mock(return_value=None),
            fetch_channel=AsyncMock(return_value=self.channel),
        )
        self.scheduler.client = self.client

    async def test_one_shot_reminder_is_sent_as_a_dm(self):
        payload = self.create(seconds=0)

        await self.scheduler.run_due(NOW)

        self.client.fetch_user.assert_awaited_once_with(1)
        self.user.send.assert_awaited_once()
        self.assertEqual("drink water", self.user.send.await_args.args[0])
        self.channel.send.assert_not_awaited()
        self.assertEqual("done", self.store.get(payload["id"]).status)

    async def test_stored_channel_reminder_is_still_sent_as_a_dm(self):
        schedule = build_schedule(now=NOW, seconds=0)
        self.store.create(
            user_id=1,
            channel_id=10,
            from_guild=True,
            deliver_dm=False,
            message="drink water",
            schedule=schedule,
        )

        await self.scheduler.run_due(NOW)

        self.user.send.assert_awaited_once()
        self.channel.send.assert_not_awaited()

    async def test_recurring_reminder_is_rescheduled(self):
        payload = self.create(seconds=0, repeat_seconds=600)

        await self.scheduler.run_due(NOW)

        reminder = self.store.get(payload["id"])
        self.assertEqual("active", reminder.status)
        self.assertEqual(NOW + timedelta(minutes=10), reminder.next_fire_at)

    async def test_failed_dm_is_marked_failed_and_notifies_server_channel(self):
        self.user.send.side_effect = RuntimeError("DMs closed")
        payload = self.create(seconds=0, repeat_seconds=600)

        await self.scheduler.run_due(NOW)

        self.assertEqual("failed", self.store.get(payload["id"]).status)
        self.assertIn("couldn't DM you", self.channel.send.await_args.args[0])

    async def test_start_is_idempotent(self):
        self.scheduler.start(self.client)
        task = self.scheduler._task
        self.scheduler.start(self.client)

        self.assertIs(task, self.scheduler._task)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


if __name__ == "__main__":
    unittest.main()
