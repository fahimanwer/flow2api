"""Server-side reCAPTCHA fallback (2026-09-23): the server mints on flow.google.com
when the worker extension is offline, cannot mint, or minted a token Google refused."""
import asyncio
import time
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.config import config
from src.services import flow_page_captcha as fpc
from src.services.flow_client import FlowAPIError, FlowClient
from src.services.load_balancer import LoadBalancer


class _FakePage:
    def __init__(self, token="tok", verdict_ok=True, page_has_grecaptcha=False):
        self.url = "about:blank"
        self.token = token
        self.verdict_ok = verdict_ok
        self.page_has_grecaptcha = page_has_grecaptcha
        self.mouse = MagicMock(move=AsyncMock(), wheel=AsyncMock())
        self.execs = 0

    def is_closed(self):
        return False

    async def goto(self, url, **_):
        self.url = url

    async def wait_for_timeout(self, _ms):
        return None

    async def evaluate(self, script, arg=None):
        if script == "navigator.userAgent":
            return "Mozilla/5.0 (X11; Linux x86_64) Chrome/153"
        if script.startswith("!!(window.grecaptcha"):
            return True
        if "__f2aServerInjected" in script:  # inject
            if self.page_has_grecaptcha:
                return {"ok": False, "err": "page loaded reCAPTCHA itself (possible execute trap)"}
            return {"ok": True, "reused": False}
        self.execs += 1  # execute
        return {"ok": True, "token": self.token} if self.verdict_ok else {"ok": False, "err": "empty token"}


def _service(page_factory, headed=False):
    svc = fpc.FlowPageCaptchaService()
    svc._available = True

    async def fake_launch(slot):
        slot.browser = MagicMock(close=AsyncMock())
        slot.context = MagicMock(close=AsyncMock())
        slot.page = page_factory(slot.proxy_url)
        slot.dirty = True
        svc.stats["launches"] += 1

    svc._launch = fake_launch
    return svc


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._saved = (config.captcha_server_fallback_enabled, config.captcha_server_fallback_max_browsers)
        config.set_captcha_server_fallback_enabled(True)
        config.set_captcha_server_fallback_max_browsers(2)
        fpc.WARMUP_DWELL_SECONDS = 0

    async def asyncTearDown(self):
        config.set_captcha_server_fallback_enabled(self._saved[0])
        config.set_captcha_server_fallback_max_browsers(self._saved[1])

    async def test_mints_and_reuses_the_warm_page(self):
        pages = {}
        svc = _service(lambda p: pages.setdefault(p, _FakePage()))
        r1 = await svc.mint("IMAGE_GENERATION", "http://u:p@proxy:8003", token_id=1)
        r2 = await svc.mint("VIDEO_GENERATION", "http://u:p@proxy:8003", token_id=1)
        self.assertEqual(r1["token"], "tok")
        self.assertIn("Chrome/153", r1["user_agent"])
        self.assertEqual(svc.stats["launches"], 1)
        self.assertEqual(pages["http://u:p@proxy:8003"].execs, 2)
        self.assertEqual(r2["token"], "tok")

    async def test_one_browser_per_proxy_and_lru_eviction_at_the_cap(self):
        svc = _service(lambda p: _FakePage(token=p))
        await svc.mint("IMAGE_GENERATION", "http://u:p@a:1", token_id=1)
        await svc.mint("IMAGE_GENERATION", "http://u:p@b:2", token_id=2)
        self.assertEqual(len(svc._slots), 2)
        r = await svc.mint("IMAGE_GENERATION", "http://u:p@c:3", token_id=3)
        self.assertEqual(r["token"], "http://u:p@c:3")
        self.assertEqual(len(svc._slots), 2)
        self.assertNotIn("http://u:p@a:1", svc._slots)  # least recently used went

    async def test_page_provided_recaptcha_is_refused(self):
        svc = _service(lambda p: _FakePage(page_has_grecaptcha=True))
        self.assertIsNone(await svc.mint("IMAGE_GENERATION", "http://u:p@a:1", token_id=1))
        self.assertEqual(svc.stats["fail"], 1)

    async def test_deadline_returns_none_and_marks_page_dirty(self):
        class SlowPage(_FakePage):
            async def evaluate(self, script, arg=None):
                if script.startswith("!!(") or script == "navigator.userAgent" or "__f2aServerInjected" in script:
                    return await super().evaluate(script, arg)
                await asyncio.sleep(5)

        svc = _service(lambda p: SlowPage())
        r = await svc.mint("IMAGE_GENERATION", "http://u:p@a:1", token_id=1, deadline_at=time.monotonic() + 0.3)
        self.assertIsNone(r)
        self.assertEqual(svc.stats["timeout"], 1)
        self.assertTrue(svc._slots["http://u:p@a:1"].dirty)
        self.assertFalse(svc._slots["http://u:p@a:1"].lock.locked())

    async def test_disabled_or_unavailable_or_no_proxy_means_none(self):
        svc = _service(lambda p: _FakePage())
        self.assertIsNone(await svc.mint("IMAGE_GENERATION", "", token_id=1))
        config.set_captcha_server_fallback_enabled(False)
        self.assertIsNone(await svc.mint("IMAGE_GENERATION", "http://u:p@a:1", token_id=1))
        config.set_captcha_server_fallback_enabled(True)
        svc._available = False
        self.assertIsNone(await svc.mint("IMAGE_GENERATION", "http://u:p@a:1", token_id=1))
        self.assertEqual(svc.stats["launches"], 0)

    async def test_sweep_closes_idle_and_everything_when_disabled(self):
        svc = _service(lambda p: _FakePage())
        await svc.mint("IMAGE_GENERATION", "http://u:p@a:1", token_id=1)
        self.assertEqual(await svc.sweep_idle(ttl_seconds=10**6), 0)
        config.set_captcha_server_fallback_enabled(False)
        self.assertEqual(await svc.sweep_idle(ttl_seconds=10**6), 1)
        self.assertEqual(len(svc._slots), 0)


class _Row:
    def __init__(self, proxy):
        self.redeem_proxy_url = proxy
        self.browser_user_agent = "Mozilla/5.0 (Macintosh) Chrome/144"


def _client(proxy="http://u:p@disp:8004"):
    client = FlowClient.__new__(FlowClient)
    FlowClient.__init__(client, db=MagicMock(), proxy_manager=None)
    client.db.get_token = AsyncMock(return_value=_Row(proxy))
    return client


class WiringTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._saved = (config.captcha_method, config.captcha_server_fallback_enabled)
        config.set_captcha_method("extension")
        config.set_captcha_server_fallback_enabled(True)
        self.fallback = MagicMock()
        self.fallback.is_available = MagicMock(return_value=True)
        self.fallback.mint = AsyncMock(return_value={"token": "server-tok", "user_agent": "Linux Chrome/153", "ms": 12})
        self.svc_patch = patch.object(fpc.FlowPageCaptchaService, "get_instance", AsyncMock(return_value=self.fallback))
        self.svc_patch.start()

    async def asyncTearDown(self):
        self.svc_patch.stop()
        config.set_captcha_method(self._saved[0])
        config.set_captcha_server_fallback_enabled(self._saved[1])

    def _ext(self, client, behaviour):
        ext = MagicMock()
        ext.get_token = AsyncMock(side_effect=behaviour) if isinstance(behaviour, Exception) else AsyncMock(return_value=behaviour)
        return patch("src.services.browser_captcha_extension.ExtensionCaptchaService.get_instance", AsyncMock(return_value=ext))

    async def test_worker_offline_falls_back_and_redeem_uses_the_server_ua(self):
        client = _client()
        client.reset_mint_context()
        with self._ext(client, RuntimeError("No Chrome Extension connection matches token_id=5")):
            token, source = await client._get_recaptcha_token("proj", "IMAGE_GENERATION", token_id=5)
        self.assertEqual((token, source), ("server-tok", "server"))
        self.fallback.mint.assert_awaited_once()
        self.assertEqual(self.fallback.mint.await_args.args[:2], ("IMAGE_GENERATION", "http://u:p@disp:8004"))
        fp = client.get_request_fingerprint()
        self.assertEqual(fp["proxy_url"], "http://u:p@disp:8004")
        self.assertEqual(fp["user_agent"], "Linux Chrome/153")  # NOT the account's Mac UA
        self.assertEqual(client.last_mint_source(), "server")

    async def test_worker_mint_failed_falls_back(self):
        client = _client()
        client.reset_mint_context()
        with self._ext(client, None):
            token, _ = await client._get_recaptcha_token("proj", "VIDEO_GENERATION", token_id=5)
        self.assertEqual(token, "server-tok")

    async def test_healthy_worker_never_uses_the_fallback(self):
        client = _client()
        client.reset_mint_context()
        with self._ext(client, "ext-tok"):
            token, _ = await client._get_recaptcha_token("proj", "IMAGE_GENERATION", token_id=5)
        self.assertEqual(token, "ext-tok")
        self.fallback.mint.assert_not_awaited()
        self.assertEqual(client.get_request_fingerprint()["user_agent"], "Mozilla/5.0 (Macintosh) Chrome/144")
        self.assertEqual(client.last_mint_source(), "extension")

    async def test_flag_off_or_no_proxy_means_no_fallback(self):
        config.set_captcha_server_fallback_enabled(False)
        client = _client()
        client.reset_mint_context()
        with self._ext(client, None):
            self.assertEqual(await client._get_recaptcha_token("proj", "IMAGE_GENERATION", token_id=5), (None, None))
        config.set_captcha_server_fallback_enabled(True)
        client = _client(proxy="")
        with self._ext(client, None):
            self.assertEqual(await client._get_recaptcha_token("proj", "IMAGE_GENERATION", token_id=5), (None, None))
        self.fallback.mint.assert_not_awaited()

    async def test_rejected_extension_token_switches_the_rest_of_the_request_to_the_server(self):
        client = _client()
        client.reset_mint_context()
        with self._ext(client, "ext-tok") as ext_patch:
            await client._get_recaptcha_token("proj", "IMAGE_GENERATION", token_id=5)
            rejected = FlowAPIError(403, "PUBLIC_ERROR_UNUSUAL_ACTIVITY: reCAPTCHA evaluation failed", "PUBLIC_ERROR_UNUSUAL_ACTIVITY")
            rejected.flow_fail_class = "RECAPTCHA"
            client._notify_browser_captcha_error = AsyncMock()
            with patch("asyncio.sleep", AsyncMock()):
                should_retry = await client._handle_retryable_generation_error(
                    error=rejected, retry_attempt=0, max_retries=3, browser_id=None, project_id="proj", log_prefix="[T] ",
                )
            self.assertTrue(should_retry)
            # Both remaining mints of this request (e.g. Omni prompt + approval) go to the server.
            t1, s1 = await client._get_recaptcha_token("proj", "VIDEO_GENERATION", token_id=5)
            t2, s2 = await client._get_recaptcha_token("proj", "VIDEO_GENERATION", token_id=5)
        self.assertEqual((t1, s1, t2, s2), ("server-tok", "server", "server-tok", "server"))
        self.assertEqual(self.fallback.mint.await_count, 2)
        # A new request starts clean.
        client.reset_mint_context()
        self.assertIsNone(client._mint_override_ctx.get())

    async def test_only_a_transport_classified_recaptcha_rejection_switches(self):
        client = _client()
        client.reset_mint_context()
        client._mint_source_ctx.set("extension")
        client._notify_browser_captcha_error = AsyncMock()
        for err in (
            FlowAPIError(403, "some other 403", None),
            Exception("PUBLIC_ERROR_RESOURCE_EXHAUSTED: quota"),
            Exception("PUBLIC_ERROR_UNSAFE_GENERATION"),
        ):
            with patch("asyncio.sleep", AsyncMock()):
                await client._handle_retryable_generation_error(err, 0, 3, None, "proj", "[T] ")
            self.assertIsNone(client._mint_override_ctx.get(), str(err))

    def test_transport_classifier_tags_exceptions(self):
        self.assertEqual(FlowClient._classify_flow_fail(403, "PUBLIC_ERROR_UNUSUAL_ACTIVITY: reCAPTCHA evaluation failed"), "RECAPTCHA")
        self.assertEqual(FlowClient._classify_flow_fail(429, "RESOURCE_EXHAUSTED"), "QUOTA")
        self.assertEqual(FlowClient._classify_flow_fail(401, "x"), "AUTH/ST_EXPIRED")
        self.assertEqual(FlowClient._classify_flow_fail(400, "PUBLIC_ERROR_UNSAFE_GENERATION"), "HTTP")


class LoadBalancerEligibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._saved = (config.captcha_method, config.captcha_server_fallback_enabled)
        config.set_captcha_method("extension")
        config.set_captcha_server_fallback_enabled(True)
        self.lb = LoadBalancer.__new__(LoadBalancer)
        self.lb.token_manager = MagicMock(db=None)
        ext = MagicMock()
        ext.has_connection_for_token = AsyncMock(return_value=(False, "auto-x"))
        ext.describe_routes = MagicMock(return_value="none")
        self.ext_patch = patch("src.services.browser_captcha_extension.ExtensionCaptchaService.get_instance", AsyncMock(return_value=ext))
        self.ext_patch.start()
        self.fallback = MagicMock(is_available=MagicMock(return_value=True))
        self.svc_patch = patch.object(fpc.FlowPageCaptchaService, "get_instance", AsyncMock(return_value=self.fallback))
        self.svc_patch.start()

    async def asyncTearDown(self):
        self.ext_patch.stop()
        self.svc_patch.stop()
        config.set_captcha_method(self._saved[0])
        config.set_captcha_server_fallback_enabled(self._saved[1])

    async def test_offline_worker_is_eligible_only_with_flag_proxy_and_chromium(self):
        token = types.SimpleNamespace(id=5, redeem_proxy_url="http://u:p@disp:8004")
        self.assertEqual(await self.lb._check_extension_route(token), (True, ""))
        self.fallback.is_available.return_value = False
        self.assertFalse((await self.lb._check_extension_route(token))[0])
        self.fallback.is_available.return_value = True
        self.assertFalse((await self.lb._check_extension_route(types.SimpleNamespace(id=6, redeem_proxy_url="")))[0])
        config.set_captcha_server_fallback_enabled(False)
        self.assertFalse((await self.lb._check_extension_route(token))[0])


if __name__ == "__main__":
    unittest.main()


class OmniApprovalMintFailureTests(unittest.TestCase):
    def test_omni_approval_mint_failure_is_a_mint_failure_not_a_strike(self):
        from src.services.token_manager import _is_captcha_mint_failure
        self.assertTrue(_is_captcha_mint_failure("Failed to obtain reCAPTCHA token (Omni approval stage)"))


class ProxyListValidatorTests(unittest.TestCase):
    def test_comma_separated_proxy_list_is_valid(self):
        from src.api.admin import _validate_browser_proxy_url_local as v
        self.assertEqual(v("http://u:p@a.example:8003,http://u:p@a.example:8004")[0], True)
        self.assertEqual(v("http://u:p@a.example:8003")[0], True)
        self.assertEqual(v("http://u:p@a.example:8003, not a proxy")[0], False)
