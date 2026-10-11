"""Google refusing an uploaded photo (2026-10-11): /flow/uploadImage answered HTTP 400
PUBLIC_ERROR_PROMINENT_PEOPLE_UPLOAD for one AI-made face ("Nia" front shot) on every Ultra
account, while her other photos uploaded fine. The refusal is permanent for that photo, so:
no retries, Google's reason in the caller's error, a 400 the caller can act on ("use a
different face"), and no strike against the account that carried the request."""
import json
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.api import routes
from src.services.characters import CharacterService, CharacterSetupError, LoadedCharacter
from src.services.flow_client import (
    FlowAPIError,
    FlowClient,
    FlowPhotoRefusedError,
    flow_api_error_from_body,
    photo_refusal_reason,
)
from src.services.generation_handler import GenerationHandler

JPEG_BYTES = b"\xff\xd8\xff" + b"0" * 16
GOOGLE_TEXT = "PUBLIC_ERROR_PROMINENT_PEOPLE_UPLOAD: Request contains an invalid argument."


def _prominent():
    return FlowAPIError(400, GOOGLE_TEXT, "PUBLIC_ERROR_PROMINENT_PEOPLE_UPLOAD")


class UploadImageRefusalTests(unittest.IsolatedAsyncioTestCase):
    async def _upload(self, error, project_id="project-123"):
        client = FlowClient(proxy_manager=None)
        calls = []

        async def fake_make_request(**kwargs):
            calls.append(kwargs["url"])
            raise error

        client._make_request = AsyncMock(side_effect=fake_make_request)
        with patch("src.services.flow_client.config") as cfg, patch("asyncio.sleep", AsyncMock()):
            cfg.flow_max_retries = 3
            cfg.captcha_method = "api"
            try:
                await client.upload_image("at", JPEG_BYTES, "IMAGE_ASPECT_RATIO_PORTRAIT", project_id=project_id)
            except Exception as exc:  # the result under test
                return exc, calls
        self.fail("upload_image should have raised")

    async def test_prominent_person_refusal_is_not_retried_and_names_googles_reason(self):
        exc, calls = await self._upload(_prominent())
        self.assertIsInstance(exc, FlowPhotoRefusedError)
        self.assertEqual(exc.reason, "PUBLIC_ERROR_PROMINENT_PEOPLE_UPLOAD")
        self.assertIn(GOOGLE_TEXT, str(exc))
        self.assertIn("real or famous person", str(exc))
        self.assertEqual(len(calls), 1)  # one attempt, not three

    async def test_refusal_without_project_does_not_try_the_legacy_endpoint(self):
        exc, calls = await self._upload(_prominent(), project_id=None)
        self.assertIsInstance(exc, FlowPhotoRefusedError)
        self.assertEqual(len(calls), 1)

    async def test_reason_is_read_from_the_text_when_the_structured_reason_is_missing(self):
        exc, calls = await self._upload(FlowAPIError(400, f"HTTP Error 400: {{\"reason\": \"PUBLIC_ERROR_PROMINENT_PEOPLE_UPLOAD\"}}"))
        self.assertIsInstance(exc, FlowPhotoRefusedError)
        self.assertEqual(exc.reason, "PUBLIC_ERROR_PROMINENT_PEOPLE_UPLOAD")

    async def test_urllib_path_keeps_the_reason_from_a_full_google_envelope(self):
        # The real envelope puts the reason after ~200 characters; cutting first lost it.
        body = json.dumps({"error": {
            "code": 400, "message": "Request contains an invalid argument.", "status": "INVALID_ARGUMENT",
            "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                         "reason": "PUBLIC_ERROR_PROMINENT_PEOPLE_UPLOAD", "domain": "aisandbox-pa.googleapis.com"}],
        }})
        self.assertGreater(body.index("PUBLIC_ERROR_PROMINENT_PEOPLE_UPLOAD"), 150)
        error = flow_api_error_from_body(400, body)
        self.assertEqual(error.reason, "PUBLIC_ERROR_PROMINENT_PEOPLE_UPLOAD")
        self.assertEqual(str(error), f"HTTP Error 400: {GOOGLE_TEXT}")
        self.assertEqual(photo_refusal_reason(error), "PUBLIC_ERROR_PROMINENT_PEOPLE_UPLOAD")
        self.assertEqual(str(flow_api_error_from_body(502, "<html>bad gateway</html>")), "HTTP Error 502: <html>bad gateway</html>")

    async def test_urllib_path_keeps_retrying_structured_server_errors(self):
        body = json.dumps({"error": {"code": 500, "message": "backend unavailable",
                                     "details": [{"reason": "INTERNAL"}]}})
        error = flow_api_error_from_body(500, body)
        self.assertEqual(error.reason, "INTERNAL")
        self.assertEqual(FlowClient(proxy_manager=None)._get_retry_reason(str(error)), "500/internal error")

    async def test_quota_and_traffic_refusals_are_not_photo_refusals(self):
        for reason in ("PUBLIC_ERROR_PER_MODEL_DAILY_QUOTA_REACHED", "PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC"):
            self.assertIsNone(photo_refusal_reason(FlowAPIError(429, reason, reason)), reason)
            self.assertIsNone(photo_refusal_reason(FlowAPIError(400, reason, reason)), reason)
        self.assertIsNone(photo_refusal_reason(FlowAPIError(500, GOOGLE_TEXT, "PUBLIC_ERROR_PROMINENT_PEOPLE_UPLOAD")))

    async def test_other_project_scoped_failures_still_say_what_google_answered(self):
        exc, calls = await self._upload(FlowAPIError(500, "HTTP Error 500: backend exploded"))
        self.assertNotIsInstance(exc, FlowPhotoRefusedError)
        self.assertIn("legacy :uploadUserImage fallback is disabled", str(exc))
        self.assertIn("HTTP Error 500: backend exploded", str(exc))
        self.assertEqual(len(calls), 3)  # a 500 is still worth retrying


def _flow_with_refused_photo(refused_index):
    fc = MagicMock()
    fc.create_character_entity = AsyncMock(return_value="ent-1")
    seen = {"n": 0}

    async def upload(at, img, ar, project_id=None):
        seen["n"] += 1
        if seen["n"] == refused_index:
            raise FlowPhotoRefusedError("PUBLIC_ERROR_PROMINENT_PEOPLE_UPLOAD", GOOGLE_TEXT)
        return f"media-{seen['n']}"

    fc.upload_image = AsyncMock(side_effect=upload)
    fc.copy_project_media_to_character_slot = AsyncMock(return_value={})
    fc.batch_get_entities = AsyncMock(return_value=[])
    return fc


class _NoRowsDB:
    def __init__(self):
        self.rows = {}

    async def get_flow_character(self, *a):
        return None

    async def upsert_flow_character(self, token_id, project_id, name, digest, entity_id):
        self.rows[name] = entity_id


class CharacterPhotoRefusalTests(unittest.IsolatedAsyncioTestCase):
    async def test_refused_character_photo_is_a_400_that_names_the_photo_and_asks_for_another_face(self):
        fc = _flow_with_refused_photo(2)
        db = _NoRowsDB()
        nia = LoadedCharacter(name="Nia", images=[b"a", b"b", b"c"])
        with self.assertRaises(CharacterSetupError) as ctx:
            await CharacterService().ensure(fc, db, MagicMock(id=55, at="at"), "proj", [nia])
        err = ctx.exception
        self.assertEqual(err.status_code, 400)
        self.assertIn("photo 2 of character 'Nia'", str(err))
        self.assertIn("different face", str(err))
        self.assertIn(GOOGLE_TEXT, str(err))
        self.assertEqual(err.extra["code"], "photo_refused_by_google")
        self.assertEqual(err.extra["google_reason"], "PUBLIC_ERROR_PROMINENT_PEOPLE_UPLOAD")
        self.assertEqual(err.extra["photo_index"], 2)
        self.assertEqual(err.extra["character"], "Nia")
        self.assertEqual(db.rows, {})


class HandlerPhotoRefusalTests(unittest.IsolatedAsyncioTestCase):
    """Through handle_generation: the caller gets a 400 with a stable code, the account gets no strike."""

    async def asyncSetUp(self):
        self.token = types.SimpleNamespace(
            id=55, at="at", st="st", email="ultra@example.com", user_paygate_tier="PAYGATE_TIER_TWO",
            image_concurrency=1, video_concurrency=1,
        )
        lb = MagicMock()
        lb.select_token = AsyncMock(return_value=self.token)
        lb.release_pending = AsyncMock()
        tm = MagicMock()
        tm.ensure_valid_token = AsyncMock(return_value=self.token)
        tm.ensure_project_exists = AsyncMock(return_value="proj")
        tm.record_error = AsyncMock()
        db = MagicMock()
        db.find_project_with_characters = AsyncMock(return_value=None)
        self.handler = GenerationHandler(
            flow_client=MagicMock(), token_manager=tm, load_balancer=lb,
            db=db, concurrency_manager=MagicMock(), proxy_manager=None,
        )
        self.handler._log_request = AsyncMock(return_value=1)
        self.handler._update_request_log_progress = AsyncMock()
        self.handler.flow_client.prefill_remote_browser_pool = AsyncMock()
        self.handler.flow_client.generate_video_reference_images = AsyncMock(side_effect=AssertionError("must not submit"))
        self.handler.flow_client.generate_image = AsyncMock(side_effect=AssertionError("must not submit"))

    async def _run(self, model, images=None, characters=None):
        chunks = []
        async for chunk in self.handler.handle_generation(model, "a woman reads the news", images=images, characters=characters):
            chunks.append(chunk)
        return json.loads(chunks[-1])["error"]

    def _refuse_upload(self, refused_index):
        seen = {"n": 0}

        async def upload(at, img, ar, project_id=None):
            seen["n"] += 1
            if img == b"bad":
                raise FlowPhotoRefusedError("PUBLIC_ERROR_PROMINENT_PEOPLE_UPLOAD", GOOGLE_TEXT)
            return f"media-{seen['n']}"

        self.handler.flow_client.upload_image = AsyncMock(side_effect=upload)

    async def test_refused_reference_image_on_veo_r2v_is_a_400_with_the_photo_number(self):
        self._refuse_upload(2)
        error = await self._run("veo_3_1_r2v_fast_portrait_ultra", images=[b"ok1", b"bad", b"ok3"])
        self.assertEqual(error["status_code"], 400)
        self.assertEqual(error["code"], "photo_refused_by_google")
        self.assertEqual(error["photo_index"], 2)
        self.assertIn("reference image 2", error["message"])
        self.assertIn("different face", error["message"])
        self.assertIn(GOOGLE_TEXT, error["message"])
        self.handler.token_manager.record_error.assert_not_awaited()
        self.handler._log_request.assert_awaited()
        self.assertEqual(self.handler._log_request.await_args_list[-1].args[4], 400)

    async def test_refused_reference_image_on_an_image_model_is_a_400(self):
        self._refuse_upload(1)
        error = await self._run("gemini-3.1-flash-image-portrait", images=[b"bad"])
        self.assertEqual(error["status_code"], 400)
        self.assertEqual(error["code"], "photo_refused_by_google")
        self.handler.token_manager.record_error.assert_not_awaited()

    async def test_refused_character_photo_is_a_400_and_no_account_strike(self):
        self._refuse_upload(1)
        self.handler.flow_client.create_character_entity = AsyncMock(return_value="ent-1")
        self.handler.flow_client.copy_project_media_to_character_slot = AsyncMock(return_value={})
        nia = LoadedCharacter(name="Nia", images=[b"bad", b"ok2"])
        with patch.object(CharacterService, "get_instance", return_value=CharacterService()):
            self.handler.db.get_flow_character = AsyncMock(return_value=None)
            error = await self._run("omni_r2v_8s_portrait", characters=[nia])
        self.assertEqual(error["status_code"], 400)
        self.assertEqual(error["code"], "photo_refused_by_google")
        self.assertEqual(error["character"], "Nia")
        self.assertIn("photo 1 of character 'Nia'", error["message"])
        self.handler.token_manager.record_error.assert_not_awaited()

    async def test_characters_on_a_frame_model_are_a_400_and_no_account_strike(self):
        error = await self._run("veo_3_1_t2v_fast_portrait_ultra", characters=[LoadedCharacter(name="Nia", images=[b"ok"])])
        self.assertEqual(error["status_code"], 400)
        self.handler.token_manager.record_error.assert_not_awaited()


class GeminiErrorShapeTests(unittest.IsolatedAsyncioTestCase):
    def test_photo_refusal_fields_travel_as_error_info(self):
        payload = routes._build_gemini_error_payload(400, "Google refused photo 1", {
            "message": "Google refused photo 1", "type": "invalid_request_error", "status_code": 400,
            "code": "photo_refused_by_google", "google_reason": "PUBLIC_ERROR_PROMINENT_PEOPLE_UPLOAD",
            "photo_index": 1, "character": "Nia",
        })
        self.assertEqual(payload["error"]["code"], 400)
        detail = payload["error"]["details"][0]
        self.assertEqual(detail["reason"], "PHOTO_REFUSED_BY_GOOGLE")
        self.assertEqual(detail["metadata"], {
            "google_reason": "PUBLIC_ERROR_PROMINENT_PEOPLE_UPLOAD", "photo_index": "1", "character": "Nia",
        })

    def test_generic_errors_keep_the_old_shape(self):
        payload = routes._build_gemini_error_payload(500, "boom", {"message": "boom", "code": "generation_failed"})
        self.assertNotIn("details", payload["error"])

    async def test_stream_drains_the_handler_after_an_error(self):
        finished = {"accounting": False}

        async def fake_generation(**kwargs):
            yield json.dumps({"error": {"message": "nope", "status_code": 400, "code": "photo_refused_by_google"}})
            finished["accounting"] = True  # the handler's own request-log update after the error

        handler = MagicMock(handle_generation=fake_generation)
        normalized = routes.NormalizedGenerationRequest(model="m", prompt="p", images=[])
        with patch.object(routes, "_ensure_generation_handler", return_value=handler):
            events = [e async for e in routes._iterate_gemini_stream(normalized, "m")]
        self.assertTrue(finished["accounting"])
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0][6:])["error"]["details"][0]["reason"], "PHOTO_REFUSED_BY_GOOGLE")


if __name__ == "__main__":
    unittest.main()
