"""upload_image on a cookie session (stage 2, 2026-11): Flow web upload (maseQ) with its own UPLOAD_IMAGE captcha.
The legacy :uploadUserImage REST fallback was deleted with the REST transport."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.services.flow_client import FlowAPIError, FlowClient


JPEG_BYTES = b"\xff\xd8\xff" + b"0" * 16
HANDLE = "FLOWCK.t.1"


def _client(response):
    client = FlowClient(proxy_manager=None)
    client._cookie_session_for_handle = AsyncMock(return_value=SimpleNamespace(token_id=1, email="a@x"))
    client._get_recaptcha_token = AsyncMock(return_value=("tok", None))
    calls = []

    async def fake_make_request(**kwargs):
        calls.append(kwargs)
        return response

    client._make_request = AsyncMock(side_effect=fake_make_request)
    return client, calls


class FlowClientUploadImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_project_scoped_upload_uses_the_flow_web_upload_with_its_own_captcha(self):
        client, calls = _client({"media": {"name": "new-media-id"}})

        media_id = await client.upload_image(
            at=HANDLE,
            image_bytes=JPEG_BYTES,
            aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
            project_id="project-123",
        )

        self.assertEqual(media_id, "new-media-id")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["url"].endswith("/flow/uploadImage"))
        context = calls[0]["json_data"]["clientContext"]
        self.assertEqual(context["projectId"], "project-123")
        self.assertIn("sessionId", context)
        self.assertEqual(context["recaptchaContext"]["token"], "tok")
        self.assertEqual(calls[0]["at_token"], HANDLE)
        client._get_recaptcha_token.assert_awaited_once_with("project-123", action="UPLOAD_IMAGE", token_id=1)

    async def test_project_scoped_upload_accepts_media_list_response(self):
        client, calls = _client({"media": [{"name": "new-media-id", "projectId": "project-123"}]})

        media_id = await client.upload_image(
            at=HANDLE,
            image_bytes=JPEG_BYTES,
            aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
            project_id="project-123",
        )

        self.assertEqual(media_id, "new-media-id")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["url"].endswith("/flow/uploadImage"))

    async def test_upload_without_a_project_id_is_refused_before_any_call(self):
        client, calls = _client({"media": {"name": "unused"}})

        with self.assertRaisesRegex(RuntimeError, "needs a project id"):
            await client.upload_image(at=HANDLE, image_bytes=JPEG_BYTES, project_id=None)

        self.assertEqual(calls, [])
        client._get_recaptcha_token.assert_not_awaited()

    async def test_upload_without_a_cookie_session_is_a_local_401(self):
        client, calls = _client({"media": {"name": "unused"}})

        with self.assertRaises(FlowAPIError) as ctx:
            await client.upload_image(at="ya29.legacy", image_bytes=JPEG_BYTES, project_id="project-123")

        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.reason, "UNAUTHENTICATED")
        # No cookie login, no captcha mint and no request for a dead credential.
        client._cookie_session_for_handle.assert_not_awaited()
        client._get_recaptcha_token.assert_not_awaited()
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
