"""Transport-level tests for the Suno client.

Focus on the parts that are security-relevant or easy to regress: which hosts a
resolved download URL may point at, that the audio fetch carries no credentials,
and that upstream error shapes map to the right exception class.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.suno_client import (
    SunoAPIError,
    SunoAuthError,
    SunoClient,
    SunoRateLimited,
    SunoSession,
    _merge_set_cookie,
)


class DownloadUrlValidationTests(unittest.TestCase):
    """A resolved download URL arrives in an upstream response body, so it is
    data, not an instruction about where our server should send a request."""

    def test_accepts_suno_and_its_cdns(self):
        for url in (
            "https://cdn1.suno.ai/clip.mp3",
            "https://cdn2.suno.ai/clip.mp3",
            "https://cdn-o.suno.com/clip.mp3",
            "https://d123.cloudfront.net/clip.mp3",
            "https://bucket.s3.amazonaws.com/clip.mp3",
        ):
            with self.subTest(url=url):
                self.assertEqual(SunoClient.validate_download_url(url), url)

    def test_rejects_foreign_hosts(self):
        for url in (
            "https://evil.example.com/clip.mp3",
            "https://suno.ai.evil.com/clip.mp3",
            "http://169.254.169.254/latest/meta-data/",
        ):
            with self.subTest(url=url), self.assertRaises(SunoAPIError) as ctx:
                SunoClient.validate_download_url(url)
            self.assertEqual(ctx.exception.code, "bad_download_host")

    def test_rejects_non_http_schemes(self):
        for url in ("file:///etc/passwd", "gopher://x/1", "data:audio/mp3;base64,AAA"):
            with self.subTest(url=url), self.assertRaises(SunoAPIError) as ctx:
                SunoClient.validate_download_url(url)
            self.assertEqual(ctx.exception.code, "bad_download_url")


class CookieJarTests(unittest.TestCase):
    def test_set_cookie_headers_update_the_jar(self):
        jar = {"__client": "old", "keep": "1"}
        _merge_set_cookie(jar, [
            "__client=new; Path=/; HttpOnly; Secure",
            "extra=2; Max-Age=60",
        ])
        self.assertEqual(jar["__client"], "new")
        self.assertEqual(jar["extra"], "2")
        self.assertEqual(jar["keep"], "1")

    def test_malformed_set_cookie_is_ignored(self):
        jar = {"a": "1"}
        _merge_set_cookie(jar, ["", "novalue", "; ;"])
        self.assertEqual(jar, {"a": "1"})


class _Response:
    def __init__(self, status_code=200, payload=None, set_cookie=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = MagicMock()
        self.headers.get_list = MagicMock(return_value=set_cookie or [])

    def json(self):
        return self._payload


class _Session:
    def __init__(self, response):
        self._response = response
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._response

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._response


class ErrorMappingTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, response):
        client = SunoClient()
        session = SunoSession({"__client": "abc"})
        session.jwt = "jwt"
        session.jwt_obtained_at = 10 ** 12
        fake = _Session(response)
        with patch("src.services.suno_client.AsyncSession", return_value=fake):
            return await client._request(session, "GET", "https://example.suno.com/api/x",
                                         bearer="jwt")

    async def test_401_becomes_auth_error(self):
        with self.assertRaises(SunoAuthError):
            await self._call(_Response(401, {"detail": "Unauthorized"}))

    async def test_429_becomes_rate_limited(self):
        with self.assertRaises(SunoRateLimited):
            await self._call(_Response(429, {"detail": "slow down"}))

    async def test_422_message_is_surfaced(self):
        with self.assertRaises(SunoAPIError) as ctx:
            await self._call(_Response(422, {"detail": "token_validation_failed"}))
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("token_validation_failed", ctx.exception.message)

    async def test_set_cookie_rotation_is_folded_back(self):
        client = SunoClient()
        session = SunoSession({"__client": "old"})
        session.jwt = "jwt"
        session.jwt_obtained_at = 10 ** 12
        fake = _Session(_Response(200, {"ok": True}, set_cookie=["__client=rotated; Path=/"]))
        with patch("src.services.suno_client.AsyncSession", return_value=fake):
            await client._request(session, "GET", "https://x.suno.com/api/y", bearer="jwt")
        self.assertEqual(session.cookies["__client"], "rotated")


class AuthFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_requires_an_active_clerk_session(self):
        client = SunoClient()
        session = SunoSession({"__client": "abc"})
        fake = _Session(_Response(200, {"response": {}}))
        with patch("src.services.suno_client.AsyncSession", return_value=fake):
            with self.assertRaises(SunoAuthError) as ctx:
                await client.refresh_session(session)
        self.assertEqual(ctx.exception.code, "no_session")

    async def test_missing_client_cookie_fails_fast(self):
        client = SunoClient()
        session = SunoSession({"other": "1"})
        with self.assertRaises(SunoAuthError) as ctx:
            await client._request(session, "GET", "https://auth.suno.com/v1/client",
                                  cookie_auth=True)
        self.assertEqual(ctx.exception.code, "no_client_cookie")


class GenerateTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_posts_to_the_v2_web_endpoint(self):
        """The retired /api/generate/v2/ is what returns 422 for the upstream
        project; Suno's own client posts to /api/generate/v2-web/."""
        client = SunoClient()
        session = SunoSession({"__client": "abc"})
        session.jwt = "jwt"
        session.jwt_obtained_at = 10 ** 12
        fake = _Session(_Response(200, {"clips": [{"id": "c1"}]}))
        with patch("src.services.suno_client.AsyncSession", return_value=fake):
            clips = await client.generate(session, {"mv": "chirp-crow"})
        self.assertEqual(clips, [{"id": "c1"}])
        method, url, _kwargs = fake.calls[-1]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/api/generate/v2-web/"))

    async def test_empty_clip_list_is_an_error(self):
        client = SunoClient()
        session = SunoSession({"__client": "abc"})
        session.jwt = "jwt"
        session.jwt_obtained_at = 10 ** 12
        fake = _Session(_Response(200, {"clips": []}))
        with patch("src.services.suno_client.AsyncSession", return_value=fake):
            with self.assertRaises(SunoAPIError) as ctx:
                await client.generate(session, {})
        self.assertEqual(ctx.exception.code, "no_clips")

    async def test_feed_filters_to_the_requested_clips(self):
        client = SunoClient()
        session = SunoSession({"__client": "abc"})
        session.jwt = "jwt"
        session.jwt_obtained_at = 10 ** 12
        fake = _Session(_Response(200, {"clips": [
            {"id": "wanted"}, {"id": "someone-elses"},
        ]}))
        with patch("src.services.suno_client.AsyncSession", return_value=fake):
            clips = await client.feed(session, ["wanted"])
        self.assertEqual(clips, [{"id": "wanted"}])
        _method, url, _kwargs = fake.calls[-1]
        self.assertTrue(url.endswith("/api/feed/v3"))


if __name__ == "__main__":
    unittest.main()
