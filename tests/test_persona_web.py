import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from src.music.player import MusicManager
from src.music.web.server import create_music_web_app
from src.persona.store import PersonaStore


class PersonaWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = PersonaStore(
            Path(self.temp_dir.name) / "persona.sqlite3"
        )
        app = create_music_web_app(
            MusicManager(),
            self.temp_dir.name,
            "http://example.test",
            self.store,
            "correct horse battery staple",
        )
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.origin = {"Origin": "http://example.test"}

    async def asyncTearDown(self):
        await self.client.close()
        self.temp_dir.cleanup()

    async def login(self):
        return await self.client.post(
            "/login",
            data={"password": "correct horse battery staple"},
            headers=self.origin,
            allow_redirects=False,
        )

    async def csrf_token(self):
        response = await self.client.get("/api/persona")
        self.assertEqual(200, response.status)
        return (await response.json())["csrf_token"]

    async def test_requires_authentication(self):
        response = await self.client.get("/", allow_redirects=False)
        self.assertEqual(302, response.status)
        self.assertEqual("/login", response.headers["Location"])

        response = await self.client.get("/api/persona")
        self.assertEqual(401, response.status)

    async def test_login_sets_hardened_session_cookie_and_logout_clears_it(self):
        response = await self.login()

        self.assertEqual(302, response.status)
        cookie = response.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)

        csrf = await self.csrf_token()
        response = await self.client.post(
            "/logout",
            headers={**self.origin, "X-CSRF-Token": csrf},
        )
        self.assertEqual(200, response.status)
        self.assertIn("Max-Age=0", response.headers["Set-Cookie"])
        self.assertEqual(
            401,
            (await self.client.get("/api/persona")).status,
        )

    async def test_rejects_bad_password_and_throttles_failures(self):
        for _ in range(5):
            response = await self.client.post(
                "/login",
                data={"password": "wrong"},
                headers=self.origin,
                allow_redirects=False,
            )
            self.assertEqual(401, response.status)

        response = await self.client.post(
            "/login",
            data={"password": "wrong"},
            headers=self.origin,
            allow_redirects=False,
        )
        self.assertEqual(429, response.status)

    async def test_crud_requires_csrf_and_updates_search_immediately(self):
        await self.login()
        csrf = await self.csrf_token()
        payload = {
            "key": "favorite color",
            "value": "Green.",
            "aliases": ["favorite colour"],
        }

        response = await self.client.post(
            "/api/persona",
            json=payload,
            headers=self.origin,
        )
        self.assertEqual(403, response.status)

        response = await self.client.post(
            "/api/persona",
            json=payload,
            headers={
                "Origin": "https://attacker.example",
                "X-CSRF-Token": csrf,
            },
        )
        self.assertEqual(403, response.status)

        response = await self.client.post(
            "/api/persona",
            json=payload,
            headers={**self.origin, "X-CSRF-Token": csrf},
        )
        self.assertEqual(201, response.status)
        fact = await response.json()
        self.assertEqual(
            "Green.",
            self.store.search("favorite colour")[0].value,
        )

        response = await self.client.put(
            f"/api/persona/{fact['id']}",
            json={
                "key": "favorite color",
                "value": "Blue.",
                "aliases": ["preferred color"],
            },
            headers={**self.origin, "X-CSRF-Token": csrf},
        )
        self.assertEqual(200, response.status)
        self.assertEqual(
            "Blue.",
            self.store.search("preferred color")[0].value,
        )

        response = await self.client.delete(
            f"/api/persona/{fact['id']}",
            headers={**self.origin, "X-CSRF-Token": csrf},
        )
        self.assertEqual(200, response.status)
        self.assertEqual([], self.store.search("favorite color"))

    async def test_dashboard_responses_include_security_headers(self):
        await self.login()
        response = await self.client.get("/")

        self.assertEqual("no-store", response.headers["Cache-Control"])
        self.assertEqual("DENY", response.headers["X-Frame-Options"])
        self.assertIn(
            "script-src 'self'",
            response.headers["Content-Security-Policy"],
        )

    async def test_missing_password_fails_closed(self):
        app = create_music_web_app(
            MusicManager(),
            self.temp_dir.name,
            "http://example.test",
            self.store,
            "",
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.get("/", allow_redirects=False)
            self.assertEqual(503, response.status)
        finally:
            await client.close()

    async def test_https_origin_marks_cookie_secure(self):
        app = create_music_web_app(
            MusicManager(),
            self.temp_dir.name,
            "https://nabemono.bulletmaji.me",
            self.store,
            "password",
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post(
                "/login",
                data={"password": "password"},
                headers={"Origin": "https://nabemono.bulletmaji.me"},
                allow_redirects=False,
            )
            self.assertIn("Secure", response.headers["Set-Cookie"])
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
