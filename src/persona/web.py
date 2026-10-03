from __future__ import annotations

import asyncio
import hmac
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from src.persona.store import (
    PersonaConflictError,
    PersonaStore,
    PersonaValidationError,
)


STATIC_DIR = Path(__file__).with_name("static")
SESSION_COOKIE = "nabemono_dashboard"
SESSION_SECONDS = 8 * 60 * 60
LOGIN_WINDOW_SECONDS = 60
LOGIN_MAX_FAILURES = 5


@dataclass
class DashboardSession:
    csrf_token: str
    expires_at: float


def _public_origin(base_url: str) -> str | None:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


class PersonaDashboard:
    def __init__(
        self,
        store: PersonaStore,
        password: str,
        base_url: str,
    ) -> None:
        self.store = store
        self.password = password
        self.public_origin = _public_origin(base_url)
        self.secure_cookie = urlsplit(base_url).scheme == "https"
        self.sessions: dict[str, DashboardSession] = {}
        self.login_failures: dict[str, deque[float]] = defaultdict(deque)

    def register(self, app: web.Application) -> None:
        app.router.add_get("/", self.index)
        app.router.add_get("/login", self.login_page)
        app.router.add_post("/login", self.login)
        app.router.add_post("/logout", self.logout)
        app.router.add_get("/api/persona", self.list_facts)
        app.router.add_post("/api/persona", self.create_fact)
        app.router.add_put("/api/persona/{fact_id}", self.update_fact)
        app.router.add_delete("/api/persona/{fact_id}", self.delete_fact)
        app.router.add_get("/assets/persona.css", self.stylesheet)
        app.router.add_get("/assets/persona.js", self.javascript)
        app.router.add_get("/assets/persona-login.js", self.login_javascript)

    def _disabled(self) -> None:
        if not self.password:
            raise web.HTTPServiceUnavailable(
                text="The persona dashboard is not configured."
            )

    def _expected_origins(self, request: web.Request) -> set[str]:
        if self.public_origin is not None:
            return {self.public_origin}
        return {
            f"http://{request.host}",
            f"https://{request.host}",
        }

    def _validate_origin(self, request: web.Request) -> None:
        if request.headers.get("Origin") not in self._expected_origins(request):
            raise web.HTTPForbidden(text="Cross-origin requests are not allowed.")

    def _prune_sessions(self) -> None:
        now = time.monotonic()
        expired = [
            session_id
            for session_id, session in self.sessions.items()
            if session.expires_at <= now
        ]
        for session_id in expired:
            self.sessions.pop(session_id, None)

    def _session(self, request: web.Request) -> DashboardSession | None:
        self._prune_sessions()
        session_id = request.cookies.get(SESSION_COOKIE)
        if not session_id:
            return None
        return self.sessions.get(session_id)

    def _require_session(self, request: web.Request) -> DashboardSession:
        self._disabled()
        session = self._session(request)
        if session is None:
            raise web.HTTPUnauthorized(text="Authentication required.")
        return session

    def _validate_mutation(self, request: web.Request) -> DashboardSession:
        session = self._require_session(request)
        self._validate_origin(request)
        csrf_token = request.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(csrf_token, session.csrf_token):
            raise web.HTTPForbidden(text="Invalid CSRF token.")
        return session

    async def _json(self, request: web.Request) -> dict[str, object]:
        try:
            payload = await request.json()
        except (ValueError, web.HTTPBadRequest) as error:
            raise web.HTTPBadRequest(text="Expected a JSON object.") from error
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="Expected a JSON object.")
        return payload

    @staticmethod
    def _fact_id(request: web.Request) -> int:
        try:
            return int(request.match_info["fact_id"])
        except (KeyError, TypeError, ValueError) as error:
            raise web.HTTPBadRequest(text="Invalid fact id.") from error

    async def index(self, request: web.Request) -> web.StreamResponse:
        self._disabled()
        if self._session(request) is None:
            raise web.HTTPFound("/login")
        return web.FileResponse(STATIC_DIR / "index.html")

    async def login_page(self, request: web.Request) -> web.StreamResponse:
        self._disabled()
        if self._session(request) is not None:
            raise web.HTTPFound("/")
        return web.FileResponse(STATIC_DIR / "login.html")

    def _check_login_limit(self, request: web.Request) -> deque[float]:
        remote = request.remote or "unknown"
        failures = self.login_failures[remote]
        threshold = time.monotonic() - LOGIN_WINDOW_SECONDS
        while failures and failures[0] < threshold:
            failures.popleft()
        if len(failures) >= LOGIN_MAX_FAILURES:
            raise web.HTTPTooManyRequests(
                text="Too many login attempts. Try again shortly."
            )
        return failures

    async def login(self, request: web.Request) -> web.StreamResponse:
        self._disabled()
        self._validate_origin(request)
        failures = self._check_login_limit(request)
        data = await request.post()
        password = str(data.get("password", ""))
        if not hmac.compare_digest(
            password.encode("utf-8"),
            self.password.encode("utf-8"),
        ):
            failures.append(time.monotonic())
            raise web.HTTPUnauthorized(text="Incorrect password.")

        failures.clear()
        session_id = secrets.token_urlsafe(32)
        self.sessions[session_id] = DashboardSession(
            csrf_token=secrets.token_urlsafe(32),
            expires_at=time.monotonic() + SESSION_SECONDS,
        )
        response = web.HTTPFound("/")
        response.set_cookie(
            SESSION_COOKIE,
            session_id,
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=self.secure_cookie,
            samesite="Strict",
            path="/",
        )
        raise response

    async def logout(self, request: web.Request) -> web.StreamResponse:
        self._validate_mutation(request)
        session_id = request.cookies.get(SESSION_COOKIE)
        if session_id:
            self.sessions.pop(session_id, None)
        response = web.json_response({"status": "logged_out"})
        response.del_cookie(
            SESSION_COOKIE,
            path="/",
        )
        return response

    async def list_facts(self, request: web.Request) -> web.Response:
        session = self._require_session(request)
        query = request.query.get("q", "").strip()
        if query:
            facts = await asyncio.to_thread(self.store.search, query, 20)
        else:
            facts = await asyncio.to_thread(self.store.list_facts)
        return web.json_response(
            {
                "facts": [fact.public_payload() for fact in facts],
                "csrf_token": session.csrf_token,
            }
        )

    async def create_fact(self, request: web.Request) -> web.Response:
        self._validate_mutation(request)
        payload = await self._json(request)
        try:
            fact = await asyncio.to_thread(
                self.store.create,
                key=payload.get("key"),
                value=payload.get("value"),
                aliases=payload.get("aliases"),
            )
        except PersonaValidationError as error:
            raise web.HTTPBadRequest(text=str(error)) from error
        except PersonaConflictError as error:
            raise web.HTTPConflict(text=str(error)) from error
        return web.json_response(fact.public_payload(), status=201)

    async def update_fact(self, request: web.Request) -> web.Response:
        self._validate_mutation(request)
        payload = await self._json(request)
        try:
            fact = await asyncio.to_thread(
                self.store.update,
                self._fact_id(request),
                key=payload.get("key"),
                value=payload.get("value"),
                aliases=payload.get("aliases"),
            )
        except PersonaValidationError as error:
            raise web.HTTPBadRequest(text=str(error)) from error
        except PersonaConflictError as error:
            raise web.HTTPConflict(text=str(error)) from error
        if fact is None:
            raise web.HTTPNotFound(text="Persona fact not found.")
        return web.json_response(fact.public_payload())

    async def delete_fact(self, request: web.Request) -> web.Response:
        self._validate_mutation(request)
        deleted = await asyncio.to_thread(
            self.store.delete,
            self._fact_id(request),
        )
        if not deleted:
            raise web.HTTPNotFound(text="Persona fact not found.")
        return web.json_response({"deleted": True})

    async def stylesheet(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "persona.css")

    async def javascript(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "persona.js")

    async def login_javascript(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "login.js")
