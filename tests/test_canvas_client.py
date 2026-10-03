import socket
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

from src.canvas.client import (
    CanvasFetchError,
    parse_calendar,
    validate_feed_url,
)


NOW = datetime(2026, 9, 26, 14, 0, tzinfo=timezone.utc)
FEED = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Canvas//EN
BEGIN:VEVENT
UID:assignment_10
DTSTART:20260926T230000Z
SUMMARY:Essay Draft [English 101]
URL:https://canvas.example.edu/courses/1/assignments/10
END:VEVENT
BEGIN:VEVENT
UID:event_20
DTSTART;VALUE=DATE:20260927
SUMMARY:Study Group [English 101]
URL:https://canvas.example.edu/calendar?event_id=20
END:VEVENT
BEGIN:VEVENT
UID:assignment_old
DTSTART:20260925T230000Z
SUMMARY:Old Homework [Math]
URL:https://canvas.example.edu/courses/2/assignments/9
END:VEVENT
BEGIN:VEVENT
UID:assignment_cancelled
DTSTART:20260928T230000Z
SUMMARY:Cancelled Homework [Math]
STATUS:CANCELLED
URL:https://canvas.example.edu/courses/2/assignments/11
END:VEVENT
END:VCALENDAR
"""


class CalendarParsingTests(unittest.TestCase):
    def test_parses_assignments_events_and_course_suffixes(self):
        events = parse_calendar(FEED, "America/Los_Angeles", now=NOW)

        self.assertEqual(2, len(events))
        assignment, event = events
        self.assertEqual("Essay Draft", assignment.title)
        self.assertEqual("English 101", assignment.course)
        self.assertEqual("assignment", assignment.kind)
        self.assertEqual(
            datetime(2026, 9, 26, 23, 0, tzinfo=timezone.utc),
            assignment.due_at,
        )
        self.assertFalse(assignment.all_day)
        self.assertEqual("event", event.kind)
        self.assertEqual(date(2026, 9, 27), event.due_date)
        self.assertTrue(event.all_day)

    def test_floating_times_use_the_users_timezone(self):
        feed = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:assignment_floating
DTSTART:20260926T090000
SUMMARY:Morning Quiz
END:VEVENT
END:VCALENDAR
"""

        event = parse_calendar(feed, "America/Los_Angeles", now=NOW)[0]

        self.assertEqual(
            datetime(2026, 9, 26, 16, 0, tzinfo=timezone.utc),
            event.due_at,
        )

    def test_rejects_malformed_calendar(self):
        with self.assertRaises(CanvasFetchError):
            parse_calendar(b"BEGIN:VCALENDAR\nBROKEN", "UTC", now=NOW)


class FeedUrlValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_requires_https(self):
        with self.assertRaisesRegex(CanvasFetchError, "HTTPS"):
            await validate_feed_url("http://canvas.example.edu/feed.ics")

    async def test_rejects_localhost_without_dns_lookup(self):
        with self.assertRaisesRegex(CanvasFetchError, "local address"):
            await validate_feed_url("https://localhost/feed.ics")

    async def test_rejects_hosts_resolving_to_private_addresses(self):
        records = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", 443),
            )
        ]
        with patch("src.canvas.client.socket.getaddrinfo", return_value=records):
            with self.assertRaisesRegex(CanvasFetchError, "local address"):
                await validate_feed_url("https://canvas.example.edu/feed.ics")

    async def test_accepts_public_custom_canvas_domains(self):
        records = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("8.8.8.8", 443),
            )
        ]
        with patch("src.canvas.client.socket.getaddrinfo", return_value=records):
            result = await validate_feed_url(
                "https://learn.example.edu/feeds/calendars/user_secret.ics"
            )
        self.assertEqual(
            "https://learn.example.edu/feeds/calendars/user_secret.ics",
            result,
        )


if __name__ == "__main__":
    unittest.main()

