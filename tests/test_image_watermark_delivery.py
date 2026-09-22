import base64
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
from PIL import Image

from src.core.config import config
from src.services import watermark_remover as wm
from src.services.generation_handler import GenerationHandler

GOOGLE_URL = "https://flow-content.google/image/abc?Expires=1&Signature=x"
MODEL_1K = {"model_name": "GEM_PIX_2", "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE"}
MODEL_2K = {"model_name": "GEM_PIX_2", "aspect_ratio": "IMAGE_ASPECT_RATIO_PORTRAIT",
            "upsample": "UPSAMPLE_IMAGE_RESOLUTION_2K"}


def _jpeg(width: int, height: int, stamped: bool) -> bytes:
    yy, xx = np.mgrid[0:height, 0:width]
    rgb = np.stack([90 + 80 * xx / width, 120 + 50 * yy / height, 100 + 0 * xx], axis=-1)
    if stamped:
        spec = wm.KNOWN_SPECS[(width, height)]
        s = spec.logo_size
        x, y = width - spec.margin_right - s, height - spec.margin_bottom - s
        a = (wm._mask(spec.mask) * spec.gain)[..., None]
        rgb[y:y + s, x:x + s] = a * 255 + (1 - a) * rgb[y:y + s, x:x + s]
    buf = io.BytesIO()
    Image.fromarray(np.clip(np.round(rgb), 0, 255).astype(np.uint8)).save(buf, "JPEG", quality=92)
    return buf.getvalue()


def _logo_edge_match(data: bytes) -> float:
    im = Image.open(io.BytesIO(data))
    w, h = im.size
    spec = wm.KNOWN_SPECS[(w, h)]
    s = spec.logo_size
    x, y = w - spec.margin_right - s, h - spec.margin_bottom - s
    rgb = np.asarray(im.convert("RGB"), dtype=np.float64)
    return wm._edge_match(rgb[y:y + s, x:x + s], wm._mask(spec.mask))


class ImageWatermarkDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self._saved = (config.remove_watermark, config.cache_enabled, config.cache_base_url)
        config.set_remove_watermark(True)
        config.set_cache_enabled(False)
        config.set_cache_base_url("http://flow.test")

        self.handler = GenerationHandler(
            flow_client=MagicMock(), token_manager=MagicMock(), load_balancer=MagicMock(),
            db=MagicMock(), concurrency_manager=MagicMock(), proxy_manager=None,
        )
        self.handler.file_cache.cache_dir = Path(self._temp_dir.name)
        self.handler._update_request_log_progress = AsyncMock()
        self.handler.flow_client.generate_image = AsyncMock(return_value=(
            {"media": [{"image": {"generatedImage": {"fifeUrl": GOOGLE_URL}}, "name": "media-1"}]}, "session", {},
        ))
        self.fetch = AsyncMock(return_value=_jpeg(1376, 768, stamped=True))
        self.handler.file_cache.fetch_image_bytes = self.fetch

    async def asyncTearDown(self):
        config.set_remove_watermark(self._saved[0])
        config.set_cache_enabled(self._saved[1])
        config.set_cache_base_url(self._saved[2])
        self._temp_dir.cleanup()

    async def _run(self, tier: str, model_config=MODEL_1K, stream: bool = False):
        token = SimpleNamespace(id=7, at="at", user_paygate_tier=tier, image_concurrency=1)
        state = self.handler._create_response_state()
        result = self.handler._create_generation_result()
        perf = {}
        chunks = [chunk async for chunk in self.handler._handle_image_generation(
            token, "project", model_config, "a prompt", None, stream,
            perf_trace=perf, generation_result=result, response_state=state,
        )]
        self.assertTrue(result["success"], chunks)
        return state, chunks, perf

    def _cached_file(self, url: str) -> bytes:
        self.assertTrue(url.startswith("http://flow.test/tmp/"), url)
        return (Path(self._temp_dir.name) / url.split("/tmp/")[-1]).read_bytes()

    async def test_free_and_pro_1k_images_are_cleaned_and_served_locally(self):
        for tier in ("PAYGATE_TIER_NOT_PAID", "PAYGATE_TIER_ONE"):
            with self.subTest(tier=tier):
                state, _, perf = await self._run(tier)
                data = self._cached_file(state["url"])
                self.assertLess(_logo_edge_match(data), 0.3)
                self.assertTrue(state["generated_assets"]["watermark"]["applied"])
                self.assertEqual(state["generated_assets"]["origin_image_url"], GOOGLE_URL)
                self.assertIn("watermark_ms", perf["image_generation"])
        self.fetch.assert_awaited_with(GOOGLE_URL)

    async def test_ultra_keeps_the_direct_link_without_downloading(self):
        state, _, _ = await self._run("PAYGATE_TIER_TWO")
        self.assertEqual(state["url"], GOOGLE_URL)
        self.fetch.assert_not_awaited()
        self.assertNotIn("watermark", state["generated_assets"])

    async def test_switch_off_restores_todays_behaviour(self):
        config.set_remove_watermark(False)
        state, _, _ = await self._run("PAYGATE_TIER_ONE")
        self.assertEqual(state["url"], GOOGLE_URL)
        self.fetch.assert_not_awaited()

    async def test_download_failure_falls_back_to_the_source_link(self):
        self.fetch.side_effect = Exception("boom")
        state, _, _ = await self._run("PAYGATE_TIER_ONE")
        self.assertEqual(state["url"], GOOGLE_URL)
        self.assertEqual(state["generated_assets"]["watermark"]["reason"], "fetch-failed")

    async def test_unwatermarked_1k_image_keeps_the_source_link(self):
        self.fetch.return_value = _jpeg(1376, 768, stamped=False)
        state, _, _ = await self._run("PAYGATE_TIER_NOT_PAID")
        self.assertEqual(state["url"], GOOGLE_URL)
        self.assertEqual(state["generated_assets"]["watermark"]["reason"], "not-detected")
        self.assertEqual(list(Path(self._temp_dir.name).iterdir()), [])

    async def test_stream_announces_removal_once_and_returns_local_link(self):
        state, chunks, _ = await self._run("PAYGATE_TIER_ONE", stream=True)
        text = "".join(chunks)
        self.assertEqual(text.count("Watermark removed"), 1)
        self.assertIn(state["url"], text)

    async def test_2k_upscale_is_cleaned_for_any_tier(self):
        stamped = _jpeg(1536, 2752, stamped=True)
        self.handler.flow_client.upsample_image = AsyncMock(return_value=base64.b64encode(stamped).decode())
        state, _, _ = await self._run("PAYGATE_TIER_TWO", model_config=MODEL_2K)
        data = self._cached_file(state["url"])
        self.assertLess(_logo_edge_match(data), 0.3)
        self.assertTrue(state["generated_assets"]["watermark"]["applied"])
        self.fetch.assert_not_awaited()

    async def test_clean_2k_upscale_is_stored_byte_identical(self):
        clean = _jpeg(1536, 2752, stamped=False)
        self.handler.flow_client.upsample_image = AsyncMock(return_value=base64.b64encode(clean).decode())
        state, _, _ = await self._run("PAYGATE_TIER_TWO", model_config=MODEL_2K)
        self.assertEqual(self._cached_file(state["url"]), clean)

    async def test_inline_fallback_carries_the_cleaned_image(self):
        stamped = _jpeg(1536, 2752, stamped=True)
        self.handler.flow_client.upsample_image = AsyncMock(return_value=base64.b64encode(stamped).decode())
        with patch.object(self.handler.file_cache, "cache_base64_image", AsyncMock(side_effect=Exception("disk"))):
            state, chunks, _ = await self._run("PAYGATE_TIER_ONE", model_config=MODEL_2K)
        text = "".join(chunks)
        encoded = text.split("data:image/jpeg;base64,")[1].split('"')[0].split(")")[0].split("\\")[0]
        self.assertLess(_logo_edge_match(base64.b64decode(encoded)), 0.3)


if __name__ == "__main__":
    unittest.main()
