"""Video aliases / Omni 1.1 durations and the per-browser reCAPTCHA mint throttle
(adapted from Danborad/flow2api and Gurumigun/flow2api, 2026-09-03)."""
import asyncio
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.core import model_resolver as mr
from src.services.generation_handler import MODEL_CONFIG, _estimate_video_credit_cost_for_log
from src.services.browser_captcha_extension import ExtensionCaptchaService, ExtensionConnection


def _req(duration=None, aspect=None, images=0, image_size=None):
    gc = {}
    if duration is not None:
        gc["durationSeconds"] = duration
    if aspect:
        gc["aspectRatio"] = aspect
    if image_size:
        gc["imageSize"] = image_size
    content = [{"type": "image_url", "image_url": {"url": "data:x"}} for _ in range(images)]
    content.append({"type": "text", "text": "hi"})
    return SimpleNamespace(generationConfig=gc, messages=[{"role": "user", "content": content}])


class OmniDurationsAndAliases(unittest.TestCase):
    def test_duration_families_exist_with_i2v_keys(self):
        for d in (4, 6, 8, 10):
            for k in (f"omni_{d}s", f"omni_{d}s_portrait", f"omni-flash_{d}s", f"omni-flash_{d}s_portrait"):
                cfg = MODEL_CONFIG[k]
                self.assertEqual(cfg["model_key"], f"abra_t2v_{d}s")
                self.assertEqual(cfg["first_frame_model_key"], f"abra_i2v_{d}s")
                self.assertEqual(cfg["start_end_model_key"], f"abra_i2v_{d}s")
                self.assertEqual(cfg["reference_model_key"], f"abra_r2v_{d}s")
                self.assertEqual(cfg["reference_duration"], d)
        self.assertEqual(MODEL_CONFIG["omni-flash_6s"]["output_resolution"], "VIDEO_RESOLUTION_720P")
        self.assertEqual(MODEL_CONFIG["omni"]["first_frame_model_key"], "abra_i2v_8s")

    def test_omni_flash_alias_with_duration(self):
        self.assertEqual(mr.resolve_model_name("Omni Flash", _req(duration=6), MODEL_CONFIG), "omni_6s")
        self.assertEqual(mr.resolve_model_name("Omni 1.1 Flash", _req(duration="4s", aspect="9:16"), MODEL_CONFIG), "omni_4s_portrait")
        self.assertEqual(mr.resolve_model_name("omni-flash", _req(duration=10), MODEL_CONFIG), "omni-flash_10s")
        # unsupported duration falls back to the 8 s default family
        self.assertEqual(mr.resolve_model_name("omni", _req(duration=5), MODEL_CONFIG), "omni")
        self.assertEqual(mr.resolve_model_name("omni", None, MODEL_CONFIG), "omni")

    def test_veo_aliases_pick_variant_from_image_count(self):
        self.assertEqual(mr.resolve_model_name("Veo 3.1 - Fast", _req(images=0), MODEL_CONFIG), "veo_3_1_t2v_fast_landscape")
        self.assertEqual(mr.resolve_model_name("Veo 3.1 - Fast", _req(images=2), MODEL_CONFIG), "veo_3_1_i2v_s_fast_fl")
        self.assertEqual(mr.resolve_model_name("Veo 3.1 - Lite", _req(images=2, aspect="portrait"), MODEL_CONFIG), "veo_3_1_interpolation_lite_portrait")
        self.assertEqual(mr.resolve_model_name("Veo 3.1 - Quality", _req(images=1), MODEL_CONFIG), "veo_3_1_i2v_s_landscape")

    def test_extract_params_returns_duration(self):
        self.assertEqual(mr._extract_generation_params(_req(duration=6)), (None, None, 6))
        self.assertEqual(mr._normalize_duration("6 seconds"), 6)
        self.assertIsNone(mr._normalize_duration(7))
        self.assertIsNone(mr._normalize_duration(True))

    def test_credit_estimate(self):
        self.assertEqual(_estimate_video_credit_cost_for_log("omni_4s", MODEL_CONFIG["omni_4s"]), 7)
        self.assertEqual(_estimate_video_credit_cost_for_log("omni", MODEL_CONFIG["omni"]), 12)
        self.assertEqual(_estimate_video_credit_cost_for_log("gemini-3.0-pro-image-square", MODEL_CONFIG["gemini-3.0-pro-image-square"]), 0)

    def test_friendly_aliases_listed(self):
        aliases = mr.get_base_model_aliases()
        self.assertIn("Omni 1.1 Flash", aliases)
        self.assertIn("Veo 3.1 - Fast", aliases)


class _FakeWS:
    """Answers every get_token immediately with a success payload."""
    def __init__(self, svc):
        self.svc = svc
        self.sent = []

    async def send_text(self, text):
        data = json.loads(text)
        self.sent.append(data)
        fut = self.svc.pending_requests[data["req_id"]]
        fut.set_result({"status": "success", "token": "tok-" + data["req_id"][-4:]})


class MintThrottle(unittest.TestCase):
    def _svc(self, route):
        svc = ExtensionCaptchaService.__new__(ExtensionCaptchaService)
        ExtensionCaptchaService.__init__(svc, db=None)
        svc.db = None
        svc.pending_requests = {}
        svc._rr_index = 0
        svc._route_locks = {}
        svc._route_last_dispatch_at = {}
        svc._global_dispatch_lock = asyncio.Lock()
        svc._global_last_dispatch_at = 0.0
        ws = _FakeWS(svc)
        svc.active_connections = [ExtensionConnection(websocket=ws, route_key=route)]
        svc._resolve_route_key = AsyncMock(return_value=route)
        return svc, ws

    def test_same_route_mints_are_spaced(self):
        from src.core.config import config
        svc, ws = self._svc("auto-aaa")
        orig_r, orig_g = config._config.get("captcha", {}).get("extension_route_min_interval_seconds"), config._config.get("captcha", {}).get("extension_global_min_interval_seconds")
        config._config.setdefault("captcha", {})["extension_route_min_interval_seconds"] = 0.3
        config._config["captcha"]["extension_global_min_interval_seconds"] = 0.0
        try:
            async def run():
                t0 = time.monotonic()
                r = await asyncio.gather(svc.get_token("p", token_id=1), svc.get_token("p", token_id=1))
                return r, time.monotonic() - t0
            (a, b), elapsed = asyncio.run(run())
            self.assertTrue(a.startswith("tok-") and b.startswith("tok-"))
            self.assertGreaterEqual(elapsed, 0.28)  # second mint waited for the interval
            self.assertEqual(len(ws.sent), 2)
        finally:
            config._config["captcha"]["extension_route_min_interval_seconds"] = orig_r if orig_r is not None else 3.0
            config._config["captcha"]["extension_global_min_interval_seconds"] = orig_g if orig_g is not None else 1.0

    def test_bound_route_offline_still_refused(self):
        svc, ws = self._svc("auto-aaa")
        svc._resolve_route_key = AsyncMock(return_value="auto-zzz")
        with self.assertRaises(RuntimeError):
            asyncio.run(svc.get_token("p", token_id=2))
        self.assertEqual(ws.sent, [])


class RecaptchaRetryBudget(unittest.TestCase):
    def test_recaptcha_failure_uses_small_budget(self):
        from src.services.flow_client import FlowClient
        from src.core.config import config
        fc = FlowClient.__new__(FlowClient)
        captcha = config._config.setdefault("captcha", {})
        had = "browser_captcha_generation_retries" in captcha
        prev = captcha.get("browser_captcha_generation_retries")
        captcha.pop("browser_captcha_generation_retries", None)  # exercise the code default
        try:
            self.assertEqual(config.browser_captcha_generation_retries, 2)
            # dedicated small budget, NOT max(generic, budget) as before
            self.assertEqual(fc._resolve_generation_retry_budget(6, Exception("PUBLIC_ERROR_UNUSUAL_ACTIVITY: reCAPTCHA evaluation failed")), 2)
            self.assertEqual(fc._resolve_generation_retry_budget(3, Exception("HTTP 500")), 3)
        finally:
            if had:
                captcha["browser_captcha_generation_retries"] = prev


if __name__ == "__main__":
    unittest.main()
