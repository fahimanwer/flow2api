"""2K/4K enlarge step (26 Sep 2026, tmp/upscale_honest_plan.md): one layer owns retries, the account's
enlarge limit is remembered, and a failed enlarge is delivered as 1K with a note (flow_upscale)."""
import base64
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.config import config
from src.services import generation_handler as gh
from src.services.flow_client import FlowAPIError, FlowClient, classify_upsample_error
from src.services.generation_handler import GenerationHandler
from src.services.load_balancer import LoadBalancer
from src.services.token_manager import model_quota_key, upsample_quota_key, upsample_resolution_for_model

GOOGLE_URL = "https://flow-content.google/image/abc?Expires=1&Signature=x"
MODEL_2K = {"model_name": "GEM_PIX_2", "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT",
            "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"}
MODEL_4K = {**MODEL_2K, "upsample": "UPSAMPLE_IMAGE_RESOLUTION_4K"}
RESET = datetime(2026, 9, 27, 7, 0, tzinfo=timezone.utc)

QUOTA = FlowAPIError(429, "HTTP Error 429: Quota exceeded: PUBLIC_ERROR_PER_MODEL_DAILY_QUOTA_REACHED",
                     "PUBLIC_ERROR_PER_MODEL_DAILY_QUOTA_REACHED")
TRAFFIC = FlowAPIError(429, "HTTP Error 429: reCAPTCHA evaluation failed",
                       "PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC")
ACCESS = FlowAPIError(403, "HTTP Error 403: PUBLIC_ERROR_MODEL_ACCESS_DENIED", "PUBLIC_ERROR_MODEL_ACCESS_DENIED")


class ClassifyTests(unittest.TestCase):
    def test_live_reasons(self):
        self.assertEqual(classify_upsample_error(QUOTA), "quota")
        self.assertEqual(classify_upsample_error(TRAFFIC), "traffic")
        self.assertEqual(classify_upsample_error(ACCESS), "access")
        self.assertEqual(classify_upsample_error(Exception("403 PUBLIC_ERROR_UNUSUAL_ACTIVITY: reCAPTCHA evaluation failed")), "traffic")
        self.assertEqual(classify_upsample_error(Exception("400 PUBLIC_ERROR_UNSAFE_GENERATION")), "refused")
        self.assertEqual(classify_upsample_error(Exception("HTTP Error 500: internal")), "other")
        self.assertEqual(classify_upsample_error(TimeoutError("timed out")), "other")

    def test_upscale_keys_never_collide(self):
        self.assertEqual(upsample_quota_key("2K"), "upsample_image@2k")
        self.assertEqual(upsample_quota_key("UPSAMPLE_IMAGE_RESOLUTION_4K"), "upsample_image@4k")
        # model_quota_key keeps them apart from each other and from the generation family
        self.assertEqual(model_quota_key("upsample_image@2k"), "upsample_image@2k")
        self.assertEqual(model_quota_key("upsample_image@4k"), "upsample_image@4k")
        self.assertEqual(model_quota_key("gemini-3.1-flash-image-portrait-2k"), "gemini-3.1-flash-image")

    def test_resolution_for_model(self):
        self.assertEqual(upsample_resolution_for_model("gemini-3.0-pro-image-three-four-4k"), "4k")
        self.assertEqual(upsample_resolution_for_model("gemini-3.1-flash-image-portrait-2k"), "2k")
        self.assertIsNone(upsample_resolution_for_model("gemini-3.1-flash-image-landscape"))
        self.assertIsNone(upsample_resolution_for_model(None))


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = (config.remove_watermark, config.cache_enabled, config.cache_base_url)
        config.set_remove_watermark(False)
        config.set_cache_enabled(False)
        config.set_cache_base_url("http://flow.test")
        self.tm = MagicMock()
        self.tm.mark_model_quota_exhausted = AsyncMock()
        self.tm._next_pt_daily_reset = MagicMock(return_value=RESET)
        self.cooled = set()
        self.tm.is_model_quota_exhausted = MagicMock(side_effect=lambda tid, key: (tid, key) in self.cooled)
        self.handler = GenerationHandler(
            flow_client=MagicMock(), token_manager=self.tm, load_balancer=MagicMock(),
            db=MagicMock(), concurrency_manager=MagicMock(), proxy_manager=None,
        )
        self.handler.file_cache.cache_dir = Path(self._tmp.name)
        self.handler._update_request_log_progress = AsyncMock()
        self.handler.flow_client.generate_image = AsyncMock(return_value=(
            {"media": [{"image": {"generatedImage": {"fifeUrl": GOOGLE_URL}}, "name": "media-1"}]}, "session", {},
        ))
        self.sleep = patch.object(gh.asyncio, "sleep", AsyncMock())
        self.sleep_mock = self.sleep.start()

    async def asyncTearDown(self):
        self.sleep.stop()
        config.set_remove_watermark(self._saved[0])
        config.set_cache_enabled(self._saved[1])
        config.set_cache_base_url(self._saved[2])
        self._tmp.cleanup()

    async def _run(self, model_config=MODEL_2K, stream=False, tier="PAYGATE_TIER_TWO"):
        token = SimpleNamespace(id=77, at="at", user_paygate_tier=tier, image_concurrency=1)
        state = self.handler._create_response_state()
        result = self.handler._create_generation_result()
        chunks = [c async for c in self.handler._handle_image_generation(
            token, "project", model_config, "a prompt", None, stream,
            perf_trace={"request_id": "gen-test"}, generation_result=result, response_state=state,
        )]
        self.assertTrue(result["success"], chunks)
        return state, chunks

    async def test_quota_is_one_call_1k_with_note_and_rests_until_reset(self):
        self.handler.flow_client.upsample_image = AsyncMock(side_effect=QUOTA)
        state, chunks = await self._run()
        self.assertEqual(self.handler.flow_client.upsample_image.await_count, 1)
        body = json.loads(chunks[-1])
        self.assertEqual(body["flow_upscale"], {"requested": "2K", "delivered": "1K", "reason": "quota"})
        self.assertIn(GOOGLE_URL, body["choices"][0]["message"]["content"])
        self.assertEqual(state["url"], GOOGLE_URL)
        self.assertEqual(state["generated_assets"]["upscale"]["reason"], "quota")
        args, kwargs = self.tm.mark_model_quota_exhausted.await_args
        self.assertEqual(args[:2], (77, "upsample_image@2k"))
        self.assertEqual(kwargs["until"], RESET)
        self.tm.record_error.assert_not_called()

    async def test_4k_access_denied_rests_the_4k_key_only(self):
        self.handler.flow_client.upsample_image = AsyncMock(side_effect=ACCESS)
        _, chunks = await self._run(MODEL_4K, tier="PAYGATE_TIER_TWO")
        self.assertEqual(json.loads(chunks[-1])["flow_upscale"]["reason"], "access")
        self.assertEqual(self.tm.mark_model_quota_exhausted.await_args[0][1], "upsample_image@4k")

    async def test_traffic_gets_one_more_try_after_a_pause_then_short_rest(self):
        self.handler.flow_client.upsample_image = AsyncMock(side_effect=[TRAFFIC, TRAFFIC])
        _, chunks = await self._run()
        self.assertEqual(self.handler.flow_client.upsample_image.await_count, gh.UPSAMPLE_TRAFFIC_ATTEMPTS)
        self.sleep_mock.assert_any_await(gh.UPSAMPLE_TRAFFIC_RETRY_DELAY_S)
        self.assertEqual(json.loads(chunks[-1])["flow_upscale"]["reason"], "traffic")
        until = self.tm.mark_model_quota_exhausted.await_args.kwargs["until"]
        left = until - datetime.now(timezone.utc)
        self.assertTrue(timedelta(minutes=gh.UPSAMPLE_TRAFFIC_COOLDOWN_MIN - 1) < left <= timedelta(minutes=gh.UPSAMPLE_TRAFFIC_COOLDOWN_MIN))

    async def test_traffic_then_success_delivers_2k(self):
        png = base64.b64encode(b"\x89PNG fake").decode()
        self.handler.flow_client.upsample_image = AsyncMock(side_effect=[TRAFFIC, png])
        with patch.object(self.handler.file_cache, "cache_base64_image", AsyncMock(return_value="x.jpg")):
            state, chunks = await self._run()
        body = json.loads(chunks[-1])
        self.assertEqual(body["flow_upscale"], {"requested": "2K", "delivered": "2K"})
        self.assertEqual(state["url"], "http://flow.test/tmp/x.jpg")
        self.tm.mark_model_quota_exhausted.assert_not_awaited()

    async def test_empty_result_and_refusal_do_not_rest_the_account(self):
        for side in ("", Exception("400 PUBLIC_ERROR_UNSAFE_GENERATION")):
            with self.subTest(side=side):
                self.tm.mark_model_quota_exhausted.reset_mock()
                self.handler.flow_client.upsample_image = AsyncMock(side_effect=[side] if isinstance(side, Exception) else None, return_value=side)
                _, chunks = await self._run()
                self.assertEqual(self.handler.flow_client.upsample_image.await_count, 1)
                self.assertEqual(json.loads(chunks[-1])["flow_upscale"]["delivered"], "1K")
                self.tm.mark_model_quota_exhausted.assert_not_awaited()

    async def test_missing_media_id_is_a_noted_1k(self):
        self.handler.flow_client.generate_image = AsyncMock(return_value=(
            {"media": [{"image": {"generatedImage": {"fifeUrl": GOOGLE_URL}}}]}, "session", {},
        ))
        self.handler.flow_client.upsample_image = AsyncMock()
        _, chunks = await self._run()
        self.handler.flow_client.upsample_image.assert_not_awaited()
        self.assertEqual(json.loads(chunks[-1])["flow_upscale"]["reason"], "no_media_id")

    async def test_stream_says_not_enlarged_and_final_chunk_carries_the_note(self):
        self.handler.flow_client.upsample_image = AsyncMock(side_effect=QUOTA)
        _, chunks = await self._run(stream=True)
        text = "".join(chunks)
        self.assertIn("Not enlarged", text)
        final = json.loads(chunks[-1][len("data: "):])
        self.assertEqual(final["flow_upscale"]["delivered"], "1K")
        self.assertEqual(final["choices"][0]["finish_reason"], "stop")

    async def test_resting_account_makes_no_enlarge_call(self):
        self.cooled = {(77, "upsample_image@2k")}
        self.handler.flow_client.upsample_image = AsyncMock()
        _, chunks = await self._run()
        self.handler.flow_client.upsample_image.assert_not_awaited()
        self.assertEqual(json.loads(chunks[-1])["flow_upscale"], {"requested": "2K", "delivered": "1K", "reason": "resting"})
        self.tm.mark_model_quota_exhausted.assert_not_awaited()  # the rest is not renewed

    async def test_4k_below_ultra_is_not_sent_to_google(self):
        self.handler.flow_client.upsample_image = AsyncMock()
        _, chunks = await self._run(MODEL_4K, tier="PAYGATE_TIER_ONE")
        self.handler.flow_client.upsample_image.assert_not_awaited()
        self.assertEqual(json.loads(chunks[-1])["flow_upscale"]["reason"], "needs_ultra")

    async def test_1k_models_have_no_note(self):
        cfg = {k: v for k, v in MODEL_2K.items() if k != "upsample"}
        self.handler.flow_client.upsample_image = AsyncMock()
        _, chunks = await self._run(cfg)
        self.assertNotIn("flow_upscale", json.loads(chunks[-1]))


class FlowClientNoNestedRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_quota_is_raised_after_one_request(self):
        client = FlowClient.__new__(FlowClient)
        client.api_base_url = "https://x"
        client._get_recaptcha_token = AsyncMock(return_value=("tok", None))
        client._make_request = AsyncMock(side_effect=QUOTA)
        client._maybe_switch_to_server_mint = AsyncMock()
        client._notify_browser_captcha_request_finished = AsyncMock()
        client._handle_retryable_generation_error = AsyncMock(return_value=True)
        with self.assertRaises(FlowAPIError):
            await client.upsample_image("at", "project", "media", "UPSAMPLE_IMAGE_RESOLUTION_2K")
        self.assertEqual(client._make_request.await_count, 1)
        client._handle_retryable_generation_error.assert_not_awaited()
        client._notify_browser_captcha_request_finished.assert_awaited()

    async def test_network_error_keeps_the_inner_retry(self):
        client = FlowClient.__new__(FlowClient)
        client.api_base_url = "https://x"
        client._get_recaptcha_token = AsyncMock(return_value=("tok", None))
        client._make_request = AsyncMock(side_effect=[Exception("HTTP Error 500: internal"), {"encodedImage": "abc"}])
        client._notify_browser_captcha_request_finished = AsyncMock()
        client._handle_retryable_generation_error = AsyncMock(return_value=True)
        self.assertEqual(await client.upsample_image("at", "project", "media", "UPSAMPLE_IMAGE_RESOLUTION_2K"), "abc")
        self.assertEqual(client._make_request.await_count, 2)


class LoadBalancerPreferenceTests(unittest.TestCase):
    def _lb(self, cooled):
        tm = MagicMock()
        tm.is_model_quota_exhausted = lambda tid, key: (tid, key) in cooled
        lb = LoadBalancer.__new__(LoadBalancer)
        lb.token_manager = tm
        return lb

    def test_can_enlarge(self):
        lb = self._lb({(77, "upsample_image@2k")})
        ultra = SimpleNamespace(id=93, user_paygate_tier="PAYGATE_TIER_TWO")
        spent = SimpleNamespace(id=77, user_paygate_tier="PAYGATE_TIER_TWO")
        pro = SimpleNamespace(id=88, user_paygate_tier="PAYGATE_TIER_ONE")
        self.assertTrue(lb._can_enlarge(ultra, "2k"))
        self.assertFalse(lb._can_enlarge(spent, "2k"))
        self.assertTrue(lb._can_enlarge(spent, "4k"))  # a 2K limit does not block 4K
        self.assertTrue(lb._can_enlarge(pro, "2k"))
        self.assertFalse(lb._can_enlarge(pro, "4k"))  # 4K needs Ultra


class SelectTokenPreferenceTests(unittest.TestCase):
    """The real picker: capable accounts first, but never a full one, and never a filter."""

    def setUp(self):
        from src.core import client_policy as cp
        from tests.test_tier_order import FakeTokenManager, _token, FREE, PRO, ULT
        self.cp, self.FTM, self._token = cp, FakeTokenManager, _token
        self.PRO, self.ULT = PRO, ULT
        self._saved = (config.tier_order, config.call_logic_mode, config.captcha_method)
        config.set_tier_order("save_ultra")
        config.set_call_logic_mode("default")
        config.set_captcha_method("yescaptcha")
        self._store = cp.client_policy_store._policies
        cp.client_policy_store.replace([{"client": "default", "image_tier": "any", "video_tier": "any"}])

    def tearDown(self):
        self.cp.client_policy_store._policies = self._store
        config.set_tier_order(self._saved[0])
        config.set_call_logic_mode(self._saved[1])
        config.set_captcha_method(self._saved[2])

    def _pick(self, tokens, model, cooled=(), load=None):
        import asyncio
        tm = self.FTM(tokens)
        tm.is_model_quota_exhausted = lambda tid, key: (tid, key) in set(cooled)
        lb = LoadBalancer(tm, concurrency_manager=None)
        if load:
            async def _load(tid, *a, **kw):
                return load.get(tid, (0, None))
            lb._get_token_load = _load
        return asyncio.run(lb.select_token(for_image_generation=True, model=model, reserve=False,
                                           enforce_concurrency_filter=False)).id

    def test_4k_goes_to_ultra_even_with_save_ultra_for_last(self):
        pool = [self._token(1, self.PRO), self._token(2, self.ULT)]
        for _ in range(5):
            self.assertEqual(self._pick(pool, "gemini-3.0-pro-image-three-four-4k"), 2)
        # 1K keeps "save Ultra for last"
        self.assertEqual(self._pick(pool, "gemini-3.0-pro-image-three-four"), 1)

    def test_spent_account_is_skipped_for_2k_but_still_used_when_alone(self):
        pool = [self._token(1, self.PRO), self._token(2, self.PRO)]
        cooled = {(1, "upsample_image@2k")}
        for _ in range(5):
            self.assertEqual(self._pick(pool, "gemini-3.1-flash-image-portrait-2k", cooled), 2)
        self.assertEqual(self._pick([self._token(1, self.PRO)], "gemini-3.1-flash-image-portrait-2k", cooled), 1)

    def test_full_capable_account_does_not_take_everything(self):
        pool = [self._token(1, self.PRO), self._token(2, self.PRO)]
        cooled = {(2, "upsample_image@2k")}
        load = {1: (100, 0), 2: (0, 10)}
        for _ in range(5):
            self.assertEqual(self._pick(pool, "gemini-3.1-flash-image-portrait-2k", cooled, load), 2)
        load = {1: (5, None), 2: (0, None)}  # no cap: in-flight threshold
        self.assertEqual(self._pick(pool, "gemini-3.1-flash-image-portrait-2k", cooled, load), 2)


if __name__ == "__main__":
    unittest.main()
