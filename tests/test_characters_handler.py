"""Flow Characters through the generation handler: validation before setup, setup before
upload/generation, request payload shape, model-type rules."""
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.characters import CharacterService, CharacterSetupError, LoadedCharacter
from src.services.generation_handler import MODEL_CONFIG, GenerationHandler

IMAGE_MODEL = {"model_name": "HARBOR_SEAL", "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE"}


async def _collect(agen):
    chunks = []
    try:
        async for chunk in agen:
            chunks.append(chunk)
    except RuntimeError as exc:
        if str(exc) != "stop here":
            raise
    return chunks


def _char(name="Maya", n=1):
    return LoadedCharacter(name=name, images=[f"{name}-{i}".encode() for i in range(n)])


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.handler = GenerationHandler(
            flow_client=MagicMock(), token_manager=MagicMock(), load_balancer=MagicMock(),
            db=MagicMock(), concurrency_manager=MagicMock(), proxy_manager=None,
        )
        self.handler._update_request_log_progress = AsyncMock()
        self.handler.flow_client.upload_image = AsyncMock(side_effect=lambda at, img, ar, project_id=None: "media-" + img.decode())
        self.handler.flow_client.generate_image = AsyncMock(side_effect=RuntimeError("stop here"))
        self.handler.flow_client.generate_video_reference_images = AsyncMock(side_effect=RuntimeError("stop here"))
        self.handler.flow_client.generate_video_start_end = AsyncMock(side_effect=RuntimeError("stop here"))
        self.handler.flow_client.generate_video_start_image = AsyncMock(side_effect=RuntimeError("stop here"))
        self.ensure = AsyncMock(return_value={"Maya": "ent-maya"})
        self.svc_patch = patch.object(CharacterService, "get_instance", return_value=MagicMock(ensure=self.ensure))
        self.svc_patch.start()

    async def asyncTearDown(self):
        self.svc_patch.stop()

    def _token(self):
        return types.SimpleNamespace(id=7, at="at", st="st", user_paygate_tier="PAYGATE_TIER_ONE", image_concurrency=1, video_concurrency=1)

    async def _image(self, prompt, characters, images=None):
        result = self.handler._create_generation_result()
        chunks = await _collect(self.handler._handle_image_generation(
            self._token(), "proj", IMAGE_MODEL, prompt, images, False,
            generation_result=result, response_state=self.handler._create_response_state(), characters=characters,
        ))
        return chunks, result

    async def _video(self, model, prompt, characters, images=None):
        result = self.handler._create_generation_result()
        chunks = await _collect(self.handler._handle_video_generation(
            self._token(), "proj", MODEL_CONFIG[model], prompt, images, False,
            generation_result=result, response_state=self.handler._create_response_state(), characters=characters,
        ))
        return chunks, result


class ImageTests(_Base):
    async def test_character_request_carries_entities_and_prompt_parts(self):
        await self._image("A photo of @Maya at the beach", [_char("Maya", 2)])
        self.ensure.assert_awaited_once()
        kwargs = self.handler.flow_client.generate_image.await_args.kwargs
        self.assertEqual(kwargs["reference_entities"], ["ent-maya"])
        self.assertEqual(kwargs["prompt_parts"], [
            {"text": "A photo of "},
            {"reference": {"entity": {"entityId": "ent-maya", "handle": "Maya"}}},
            {"text": " at the beach"},
        ])
        self.assertEqual(kwargs["image_inputs"], [])  # character photos are NOT reference images

    async def test_without_characters_payload_is_unchanged(self):
        await self._image("A photo", None)
        kwargs = self.handler.flow_client.generate_image.await_args.kwargs
        self.assertIsNone(kwargs["prompt_parts"])
        self.assertIsNone(kwargs["reference_entities"])
        self.ensure.assert_not_awaited()

    async def test_invalid_character_is_refused_before_any_setup_or_upload(self):
        chunks, result = await self._image("x", [_char("@bad")], images=[b"ref"])
        self.assertFalse(result["success"])
        self.assertIn('"status_code": 400', str(chunks))
        self.ensure.assert_not_awaited()
        self.handler.flow_client.upload_image.assert_not_awaited()
        self.handler.flow_client.generate_image.assert_not_awaited()

    async def test_too_many_characters_for_images(self):
        chunks, result = await self._image("x", [_char(f"C{i}") for i in range(11)])
        self.assertIn("At most 10", str(chunks))

    async def test_setup_failure_returns_its_status_and_submits_nothing(self):
        self.ensure.side_effect = CharacterSetupError("Flow could not create character", 502)
        chunks, result = await self._image("@Maya", [_char()], images=[b"ref"])
        self.assertFalse(result["success"])
        self.assertIn('"status_code": 502', str(chunks))
        self.handler.flow_client.upload_image.assert_not_awaited()
        self.handler.flow_client.generate_image.assert_not_awaited()


class VideoTests(_Base):
    async def test_frame_models_refuse_characters(self):
        for model in ("veo_3_1_i2v_s_fast_fl", "veo_3_1_t2v_fast_landscape", "veo_3_1_extend"):
            with self.subTest(model=model):
                chunks, result = await self._video(model, "@Maya waves", [_char()], images=[b"a"] if "i2v" in model else None)
                self.assertFalse(result["success"])
                self.assertIn("ingredients model", str(chunks))
        self.ensure.assert_not_awaited()

    async def test_veo_r2v_with_character_only_uses_the_reference_route(self):
        await self._video("veo_3_1_r2v_fast", "@Maya walks on a beach", [_char()])
        kwargs = self.handler.flow_client.generate_video_reference_images.await_args.kwargs
        self.assertEqual(kwargs["reference_entities"], ["ent-maya"])
        self.assertEqual(kwargs["reference_images"], [])
        self.assertEqual(kwargs["prompt_parts"][0], {"reference": {"entity": {"entityId": "ent-maya", "handle": "Maya"}}})

    async def test_omni_with_character_and_two_images_skips_the_frame_route(self):
        await self._video("omni", "@Maya dances", [_char()], images=[b"a", b"b"])
        self.handler.flow_client.generate_video_start_end.assert_not_awaited()
        kwargs = self.handler.flow_client.generate_video_reference_images.await_args.kwargs
        self.assertEqual(kwargs["model_key"], "abra_r2v_8s")
        self.assertEqual([r["mediaId"] for r in kwargs["reference_images"]], ["media-a", "media-b"])
        self.assertEqual(kwargs["reference_entities"], ["ent-maya"])
        self.assertNotIn("reference_only", MODEL_CONFIG["omni"])  # the shared config was not mutated

    async def test_omni_character_only_works(self):
        await self._video("omni_4s", "@Maya dances", [_char()])
        kwargs = self.handler.flow_client.generate_video_reference_images.await_args.kwargs
        self.assertEqual(kwargs["model_key"], "abra_r2v_4s")
        self.assertEqual(kwargs["reference_images"], [])

    async def test_video_character_limit_is_three(self):
        chunks, result = await self._video("veo_3_1_r2v_fast", "x", [_char(f"C{i}") for i in range(4)])
        self.assertIn("At most 3", str(chunks))
        self.ensure.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
