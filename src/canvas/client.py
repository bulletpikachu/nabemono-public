from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import socket
from datetime import date, datetime, timezone
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
from icalendar import Calendar

from src.canvas.models import CanvasEvent, FetchResult
from src.config import CANVAS_FETCH_MAX_BYTES, CANVAS_FETCH_TIMEOUT


class CanvasFetchError(ValueError):
    pass


_COURSE_SUFFIX = re.compile(r"^(.*?)\s+\[([^\[\]]+)\]\s*$")
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 3


def load_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo((name or "").strip())
    except (ZoneInfoNotFoundError, ValueError):
        raise CanvasFetchError(f"unknown timezone: {name}") from None


def _is_public_address(address: str) -> bool:
    try:
        value = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (
        value.is_private
        or value.is_loopback
        or value.is_link_local
        or value.is_multicast
        or value.is_reserved
        or value.is_unspecified
    )


async def validate_feed_url(url: str) -> str:
    value = (url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise CanvasFetchError("the Canvas Calendar Feed must be an HTTPS URL")
    if parsed.username or parsed.password:
        raise CanvasFetchError("the Calendar Feed URL must not contain credentials")
    if parsed.fragment:
        raise CanvasFetchError("the Calendar Feed URL must not contain a fragment")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost") or hostname.endswith(".local"):
        raise CanvasFetchError("the Calendar Feed URL cannot point to a local address")
    try:
        port = parsed.port or 443
    except ValueError:
        raise CanvasFetchError("the Calendar Feed URL has an invalid port") from None
    try:
        records = await asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror:
        raise CanvasFetchError("the Calendar Feed host could not be resolved") from None
    addresses = {record[4][0] for record in records}
    if not addresses or any(not _is_public_address(address) for address in addresses):
        raise CanvasFetchError("the Calendar Feed URL cannot point to a local address")
    return value


def _safe_event_url(value) -> str | None:
    if value is None:
        return None
    url = str(value).strip()
    parsed = urlparse(url)
    return url if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _title_and_course(summary: str) -> tuple[str, str | None]:
    title = re.sub(r"\s+", " ", summary).strip() or "Untitled Canvas item"
    match = _COURSE_SUFFIX.match(title)
    if not match:
        return title[:300], None
    return match.group(1).strip()[:300], match.group(2).strip()[:200]


def _event_kind(uid: str, url: str | None) -> str:
    path = urlparse(url).path.lower() if url else ""
    return "assignment" if "/assignments/" in path or "assignment" in uid.lower() else "event"


def _decoded(component, name: str):
    try:
        return component.decoded(name)
    except (KeyError, ValueError, TypeError):
        return None


def parse_calendar(
    body: bytes,
    timezone_name: str,
    *,
    now: datetime | None = None,
) -> tuple[CanvasEvent, ...]:
    zone = load_timezone(timezone_name)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local_today = now.astimezone(zone).date()
    try:
        calendar = Calendar.from_ical(body)
    except Exception as error:
        raise CanvasFetchError("Canvas returned a malformed calendar feed") from error

    events: list[CanvasEvent] = []
    seen: set[str] = set()
    for component in calendar.walk("VEVENT"):
        if str(component.get("STATUS", "")).strip().upper() == "CANCELLED":
            continue
        start = _decoded(component, "DTSTART")
        if start is None:
            continue

        all_day = not isinstance(start, datetime)
        due_at: datetime | None = None
        due_date: date | None = None
        if isinstance(start, datetime):
            if start.tzinfo is None:
                start = start.replace(tzinfo=zone)
            due_at = start.astimezone(timezone.utc)
            if due_at < now:
                continue
        elif isinstance(start, date):
            due_date = start
            if due_date < local_today:
                continue
        else:
            continue

        summary = str(component.get("SUMMARY", ""))
        title, course = _title_and_course(summary)
        url = _safe_event_url(component.get("URL"))
        uid = str(component.get("UID", "")).strip()
        if not uid:
            fingerprint = f"{title}|{due_at}|{due_date}|{url}".encode("utf-8")
            uid = hashlib.sha256(fingerprint).hexdigest()
        recurrence_id = _decoded(component, "RECURRENCE-ID")
        if recurrence_id is not None:
            uid = f"{uid}:{recurrence_id}"
        unique_uid = uid
        suffix = 2
        while unique_uid in seen:
            unique_uid = f"{uid}:{suffix}"
            suffix += 1
        seen.add(unique_uid)
        events.append(
            CanvasEvent(
                uid=unique_uid[:500],
                title=title,
                due_at=due_at,
                due_date=due_date,
                all_day=all_day,
                url=url,
                course=course,
                kind=_event_kind(uid, url),
            )
        )
    return tuple(events)


class CanvasClient:
    def __init__(
        self,
        *,
        timeout: float = CANVAS_FETCH_TIMEOUT,
        max_bytes: int = CANVAS_FETCH_MAX_BYTES,
    ) -> None:
        self.timeout = timeout
        self.max_bytes = max_bytes

    async def fetch(
        self,
        feed_url: str,
        timezone_name: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        now: datetime | None = None,
    ) -> FetchResult:
        current_url = await validate_feed_url(feed_url)
        headers = {
            "Accept": "text/calendar, text/plain;q=0.8",
            "User-Agent": "nabemono/1.0 (Canvas calendar sync)",
        }
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for redirect_count in range(_MAX_REDIRECTS + 1):
                try:
                    async with session.get(
                        current_url,
                        headers=headers,
                        allow_redirects=False,
                    ) as response:
                        self._validate_peer(response)
                        if response.status in _REDIRECT_STATUSES:
                            if redirect_count == _MAX_REDIRECTS:
                                raise CanvasFetchError(
                                    "the Calendar Feed redirected too many times"
                                )
                            location = response.headers.get("Location")
                            if not location:
                                raise CanvasFetchError(
                                    "the Calendar Feed returned an invalid redirect"
                                )
                            current_url = await validate_feed_url(
                                urljoin(current_url, location)
                            )
                            continue
                        if response.status == 304:
                            return FetchResult(
                                events=(),
                                etag=response.headers.get("ETag") or etag,
                                last_modified=(
                                    response.headers.get("Last-Modified") or last_modified
                                ),
                                not_modified=True,
                            )
                        if response.status in {401, 403}:
                            raise CanvasFetchError(
                                "Canvas rejected the Calendar Feed URL; copy a new link"
                            )
                        if response.status >= 400:
                            raise CanvasFetchError(
                                f"Canvas Calendar Feed request failed (HTTP {response.status})"
                            )
                        raw = await response.content.read(self.max_bytes + 1)
                        if len(raw) > self.max_bytes:
                            raise CanvasFetchError("the Canvas Calendar Feed is too large")
                        events = parse_calendar(raw, timezone_name, now=now)
                        return FetchResult(
                            events=events,
                            etag=response.headers.get("ETag"),
                            last_modified=response.headers.get("Last-Modified"),
                        )
                except CanvasFetchError:
                    raise
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    raise CanvasFetchError(
                        "the Canvas Calendar Feed could not be reached"
                    ) from None
        raise CanvasFetchError("the Canvas Calendar Feed could not be reached")

    @staticmethod
    def _validate_peer(response: aiohttp.ClientResponse) -> None:
        connection = response.connection
        transport = getattr(connection, "transport", None) if connection else None
        peer = transport.get_extra_info("peername") if transport else None
        if peer and not _is_public_address(str(peer[0])):
            raise CanvasFetchError(
                "the Calendar Feed URL cannot point to a local address"
            )


canvas_client = CanvasClient()

