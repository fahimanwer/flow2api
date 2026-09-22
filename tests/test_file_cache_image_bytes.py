import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.services.file_cache import FileCache


class _FakeSession:
    def __init__(self, responses, calls):
        self._responses = responses
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        self._calls.append((url, kwargs.get("proxy"), kwargs.get("timeout")))
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FetchImageBytesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.cache = FileCache(cache_dir=self._temp_dir.name, default_timeout=7200)
        self.cache._resolve_download_proxy = AsyncMock(return_value="socks5://warp-proxy:1080")
        self.calls = []

    async def asyncTearDown(self):
        self._temp_dir.cleanup()

    def _session(self, *responses):
        responses = list(responses)
        return patch("src.services.file_cache.AsyncSession", lambda: _FakeSession(responses, self.calls))

    async def test_flow_cdn_link_is_fetched_direct_first(self):
        with self._session(SimpleNamespace(status_code=200, content=b"jpeg")):
            data = await self.cache.fetch_image_bytes("https://flow-content.google/image/x?Signature=s")
        self.assertEqual(data, b"jpeg")
        self.assertEqual([(proxy, timeout) for _, proxy, timeout in self.calls], [(None, 15)])

    async def test_direct_failure_retries_through_the_media_proxy(self):
        with self._session(Exception("reset"), SimpleNamespace(status_code=200, content=b"jpeg")):
            data = await self.cache.fetch_image_bytes("https://flow-content.google/image/x")
        self.assertEqual(data, b"jpeg")
        self.assertEqual([proxy for _, proxy, _ in self.calls], [None, "socks5://warp-proxy:1080"])

    async def test_other_hosts_always_use_the_media_proxy(self):
        with self._session(SimpleNamespace(status_code=200, content=b"jpeg")):
            await self.cache.fetch_image_bytes("https://storage.googleapis.com/some/image")
        self.assertEqual([proxy for _, proxy, _ in self.calls], ["socks5://warp-proxy:1080"])

    async def test_raises_when_every_attempt_fails(self):
        with self._session(SimpleNamespace(status_code=403, content=b""), SimpleNamespace(status_code=500, content=b"")):
            with self.assertRaises(Exception) as ctx:
                await self.cache.fetch_image_bytes("https://flow-content.google/image/x")
        self.assertIn("HTTP 500", str(ctx.exception))

    async def test_cache_image_bytes_names_by_format(self):
        jpg = await self.cache.cache_image_bytes(b"\xff\xd8\xff\xe0jpegdata", "1K")
        png = await self.cache.cache_image_bytes(b"\x89PNG\r\n\x1a\npngdata", "2K")
        self.assertTrue(jpg.endswith("_1K.jpg"))
        self.assertTrue(png.endswith("_2K.png"))
        self.assertEqual(self.cache.get_cache_path(jpg).read_bytes(), b"\xff\xd8\xff\xe0jpegdata")
        self.assertEqual(sorted(p.suffix for p in self.cache.cache_dir.iterdir()), [".jpg", ".png"])


if __name__ == "__main__":
    unittest.main()
