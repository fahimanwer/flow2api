"""Reference-image limits match Flow's own model list (2026-09-22):
images 10, Omni ingredients 7, Veo Fast/Lite ingredients 3."""
import asyncio
import types
import unittest
from unittest.mock import AsyncMock, MagicMock

from src.core.model_resolver import resolve_model_name
from src.services.generation_handler import (
    FLOW_IMAGE_MAX_REFERENCES,
    MODEL_CONFIG,
    REFERENCE_UPLOAD_CONCURRENCY,
    GenerationHandler,
)

IMAGE_MODEL = {"model_name": "GEM_PIX_2", "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE"}


def _request(**generation_config):
    return types.SimpleNamespace(generationConfig=types.SimpleNamespace(**generation_config))


class ResolverTests(unittest.TestCase):
    def _resolve(self, model, images=0, **generation_config):
        return resolve_model_name(
            model,
            request=_request(**generation_config),
            model_config=MODEL_CONFIG,
            images=[b"x"] * images,
        )

    def test_lite_alias_picks_mode_from_image_count(self):
        self.assertEqual(self._resolve("Veo 3.1 - Lite", 1), "veo_3_1_i2v_lite_landscape")
        self.assertEqual(self._resolve("Veo 3.1 - Lite", 2), "veo_3_1_interpolation_lite_landscape")
        self.assertEqual(self._resolve("Veo 3.1 - Lite", 3), "veo_3_1_r2v_lite_landscape")

    def test_lite_ingredients_names(self):
        self.assertEqual(self._resolve("veo-r2v-lite", 1, aspectRatio="portrait"), "veo_3_1_r2v_lite_portrait")
        self.assertEqual(self._resolve("veo_3_1_r2v_lite", 2), "veo_3_1_r2v_lite_landscape")

    def test_omni_ingredients_names_and_duration(self):
        self.assertEqual(self._resolve("omni-r2v", 1), "omni_r2v")
        self.assertEqual(
            self._resolve("omni_r2v", 2, aspectRatio="portrait", durationSeconds=6),
            "omni_r2v_6s_portrait",
        )

    def test_omni_alias_with_seven_images_stays_on_omni(self):
        self.assertEqual(self._resolve("Omni 1.1 Flash", 7), "omni")


class ModelConfigTests(unittest.TestCase):
    def test_limits_match_flow_model_list(self):
        for key in ("omni", "omni_portrait", "omni-flash", "omni_10s_portrait", "omni_r2v_4s"):
            self.assertEqual(MODEL_CONFIG[key]["max_images"], 7, key)
        for key in ("veo_3_1_r2v_fast", "veo_3_1_r2v_lite_landscape", "veo_3_1_r2v_lite_portrait"):
            self.assertEqual(MODEL_CONFIG[key]["max_images"], 3, key)
        self.assertEqual(FLOW_IMAGE_MAX_REFERENCES, 10)

    def test_omni_ingredients_models_use_reference_keys(self):
        cfg = MODEL_CONFIG["omni_r2v_6s_portrait"]
        self.assertTrue(cfg["reference_only"])
        self.assertEqual(cfg["min_images"], 1)
        self.assertEqual(cfg["reference_model_key"], "abra_r2v_6s")
        self.assertEqual(cfg["aspect_ratio"], "VIDEO_ASPECT_RATIO_PORTRAIT")
        self.assertNotIn("reference_only", MODEL_CONFIG["omni"])

    def test_lite_ingredients_never_upgrade_to_a_fake_ultra_key(self):
        cfg = MODEL_CONFIG["veo_3_1_r2v_lite_landscape"]
        self.assertEqual(cfg["model_key"], "veo_3_1_r2v_lite")
        self.assertFalse(cfg["allow_tier_upgrade"])


async def _collect(agen):
    """Chunks until the fake upstream call raises "stop here"."""
    chunks = []
    try:
        async for chunk in agen:
            chunks.append(chunk)
    except RuntimeError as exc:
        if str(exc) != "stop here":
            raise
    return chunks


class _HandlerTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.handler = GenerationHandler(
            flow_client=MagicMock(), token_manager=MagicMock(), load_balancer=MagicMock(),
            db=MagicMock(), concurrency_manager=MagicMock(), proxy_manager=None,
        )
        self.handler._update_request_log_progress = AsyncMock()
        self.in_flight = 0
        self.max_in_flight = 0

        async def fake_upload(at, image_bytes, aspect_ratio, project_id=None):
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            await asyncio.sleep(0.01 if image_bytes[-1] % 2 else 0.002)
            self.in_flight -= 1
            return f"media-{image_bytes.decode()}"

        self.handler.flow_client.upload_image = AsyncMock(side_effect=fake_upload)

    def _token(self, tier="PAYGATE_TIER_ONE"):
        return types.SimpleNamespace(
            id=7, at="at", st="st", user_paygate_tier=tier, image_concurrency=1, video_concurrency=1,
        )


class ImageLimitTests(_HandlerTestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.handler.flow_client.generate_image = AsyncMock(side_effect=RuntimeError("stop here"))

    async def _run(self, count):
        result = self.handler._create_generation_result()
        images = [str(i).encode() for i in range(count)]
        chunks = await _collect(self.handler._handle_image_generation(
            self._token(), "project", IMAGE_MODEL, "prompt", images, False,
            generation_result=result, response_state=self.handler._create_response_state(),
        ))
        return chunks, result

    async def test_more_than_ten_images_is_refused_before_upload(self):
        chunks, result = await self._run(11)
        self.assertFalse(result["success"])
        self.assertIn("at most 10 reference images", str(chunks))
        self.handler.flow_client.upload_image.assert_not_awaited()
        self.handler.flow_client.generate_image.assert_not_awaited()

    async def test_ten_images_upload_in_order_a_few_at_a_time(self):
        await self._run(10)
        inputs = self.handler.flow_client.generate_image.await_args.kwargs["image_inputs"]
        self.assertEqual([i["name"] for i in inputs], [f"media-{i}" for i in range(10)])
        self.assertEqual(self.max_in_flight, REFERENCE_UPLOAD_CONCURRENCY)


class UploadFailureTests(_HandlerTestBase):
    async def test_one_failed_upload_cancels_the_rest_and_raises_its_error(self):
        started = []

        async def flaky_upload(at, image_bytes, aspect_ratio, project_id=None):
            started.append(image_bytes)
            if image_bytes == b"1":
                raise ValueError("upload 1 failed")
            await asyncio.sleep(10)
            return "never"

        self.handler.flow_client.upload_image = AsyncMock(side_effect=flaky_upload)
        with self.assertRaisesRegex(ValueError, "upload 1 failed"):
            await asyncio.wait_for(
                self.handler._upload_reference_images(
                    self._token(), [str(i).encode() for i in range(6)], "IMAGE_ASPECT_RATIO_LANDSCAPE", "p"
                ),
                timeout=2,
            )
        # The failed slot may let one more start; the rest of the queue is cancelled.
        self.assertLessEqual(len(started), REFERENCE_UPLOAD_CONCURRENCY + 1)


class OmniRoutingTests(_HandlerTestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        stop = RuntimeError("stop here")
        self.handler.flow_client.generate_video_reference_images = AsyncMock(side_effect=stop)
        self.handler.flow_client.generate_video_start_end = AsyncMock(side_effect=stop)
        self.handler.flow_client.generate_video_start_image = AsyncMock(side_effect=stop)

    async def _run(self, model, count):
        result = self.handler._create_generation_result()
        images = [str(i).encode() for i in range(count)]
        chunks = await _collect(self.handler._handle_video_generation(
            self._token(), "project", MODEL_CONFIG[model], "prompt", images, False,
            generation_result=result, response_state=self.handler._create_response_state(),
        ))
        return chunks, result

    def _refs(self):
        kwargs = self.handler.flow_client.generate_video_reference_images.await_args.kwargs
        return kwargs["model_key"], [r["mediaId"] for r in kwargs["reference_images"]]

    async def test_seven_images_go_to_omni_ingredients(self):
        await self._run("omni", 7)
        model_key, refs = self._refs()
        self.assertEqual(model_key, "abra_r2v_8s")
        self.assertEqual(refs, [f"media-{i}" for i in range(7)])

    async def test_eight_images_are_refused(self):
        chunks, result = await self._run("omni", 8)
        self.assertFalse(result["success"])
        self.assertIn("at most 7 reference images", str(chunks))
        self.handler.flow_client.upload_image.assert_not_awaited()

    async def test_plain_omni_keeps_two_images_as_first_and_last_frame(self):
        await self._run("omni", 2)
        self.handler.flow_client.generate_video_start_end.assert_awaited_once()
        self.handler.flow_client.generate_video_reference_images.assert_not_awaited()

    async def test_omni_ingredients_model_sends_one_or_two_images_as_references(self):
        for count in (1, 2):
            with self.subTest(count=count):
                self.handler.flow_client.generate_video_reference_images.reset_mock()
                await self._run("omni_r2v_6s", count)
                model_key, refs = self._refs()
                self.assertEqual(model_key, "abra_r2v_6s")
                self.assertEqual(len(refs), count)
        self.handler.flow_client.generate_video_start_end.assert_not_awaited()
        self.handler.flow_client.generate_video_start_image.assert_not_awaited()

    async def test_omni_ingredients_model_needs_an_image(self):
        chunks, result = await self._run("omni_r2v", 0)
        self.assertFalse(result["success"])
        self.assertIn("at least 1 reference image", str(chunks))

    async def test_lite_ingredients_send_the_lite_key(self):
        await self._run("veo_3_1_r2v_lite_portrait", 3)
        kwargs = self.handler.flow_client.generate_video_reference_images.await_args.kwargs
        self.assertEqual(kwargs["model_key"], "veo_3_1_r2v_lite")
        self.assertEqual(kwargs["aspect_ratio"], "VIDEO_ASPECT_RATIO_PORTRAIT")


if __name__ == "__main__":
    unittest.main()
