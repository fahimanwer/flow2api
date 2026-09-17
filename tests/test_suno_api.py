"""HTTP-level tests for the Suno router.

Mounted on a bare FastAPI app with a stub service, so these check routing,
status codes and the authentication split without booting the whole backend.

The split matters: generation runs on the shared Flow2API key, but importing or
deleting account credentials requires an admin session. A shared key must never
reach the credential endpoints.
"""

import unittest
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import admin as admin_module
from src.api import suno as suno_router
from src.core import suno_models as sm
from src.core.auth import verify_api_key_flexible
from src.core.suno_models import SunoConflictError, SunoValidationError


class StubService:
    def __init__(self):
        self.submitted: List[Dict[str, Any]] = []
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.raise_on_submit: Optional[Exception] = None
        self.audio_error: Optional[Exception] = None
        self.deleted: List[int] = []

    async def submit(self, body):
        if self.raise_on_submit:
            raise self.raise_on_submit
        self.submitted.append(body)
        job = {"job_id": "sj_test", "state": sm.QUEUED, "clip_ids": []}
        self.jobs["sj_test"] = job
        return job

    async def get_job(self, job_id):
        return self.jobs.get(job_id)

    async def list_jobs(self, limit=50, offset=0, state=None):
        return {"jobs": list(self.jobs.values()), "total": len(self.jobs),
                "limit": limit, "offset": offset}

    async def cancel(self, job_id):
        return {"job_id": job_id, "state": sm.CANCELLED}

    async def retry_blocked(self, job_id):
        return {"job_id": job_id, "state": sm.QUEUED}

    async def list_accounts(self):
        return [{"id": 1, "status": sm.ACCOUNT_READY, "credits": 100}]

    async def import_account(self, cookie, display_name="", account_id=None):
        return {"id": 1, "status": sm.ACCOUNT_READY}

    async def set_account_limit(self, account_id, operator_limit=None, provider_cap=None):
        return {"id": account_id, "operator_limit": operator_limit, "provider_cap": provider_cap}

    async def set_account_enabled(self, account_id, enabled):
        return {"id": account_id, "operator_disabled": not enabled}

    async def delete_account(self, account_id, retire_audio=False):
        self.deleted.append(account_id)
        return {"deleted": account_id, "audio_retired": retire_audio}

    async def refresh_account_billing(self, account_id):
        return {}

    async def resolve(self, job_id, action, clip_ids=None,
                      confirm_no_upstream_work=False, note=""):
        return {"job_id": job_id, "state": sm.FAILED if action == "fail" else sm.SUBMITTED}

    async def stream_audio(self, job_id, clip_id, fmt="mp3"):
        if self.audio_error:
            raise self.audio_error
        yield b"audio-bytes"


ADMIN_TOKEN = "admin-session-token"


class SunoApiTestCase(unittest.TestCase):
    def setUp(self):
        self.service = StubService()
        suno_router.set_service(self.service)

        app = FastAPI()
        app.include_router(suno_router.router)
        # Accept a fixed API key, and register a real admin session token so the
        # production admin dependency is exercised rather than stubbed away.
        app.dependency_overrides[verify_api_key_flexible] = lambda: "api-key"
        admin_module.active_admin_tokens.add(ADMIN_TOKEN)
        self.client = TestClient(app)

    def tearDown(self):
        suno_router.set_service(None)
        admin_module.active_admin_tokens.discard(ADMIN_TOKEN)

    # ------------------------------------------------------------ caller side

    def test_models_endpoint_lists_the_catalogue(self):
        response = self.client.get("/v1/suno/models")
        self.assertEqual(response.status_code, 200)
        ids = [m["id"] for m in response.json()["data"]]
        self.assertIn("suno/v5", ids)

    def test_generate_returns_202_with_a_job(self):
        response = self.client.post("/v1/suno/music/generations", json={"prompt": "lo-fi"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["state"], sm.QUEUED)

    def test_idempotency_header_is_folded_into_the_body(self):
        self.client.post(
            "/v1/suno/music/generations",
            json={"prompt": "lo-fi"},
            headers={"Idempotency-Key": "abc123"},
        )
        self.assertEqual(self.service.submitted[-1]["idempotency_key"], "abc123")

    def test_validation_error_is_a_400(self):
        self.service.raise_on_submit = SunoValidationError("missing_input", "'prompt' is required.")
        response = self.client.post("/v1/suno/music/generations", json={})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"]["error"]["code"], "missing_input")

    def test_conflict_error_is_a_409(self):
        self.service.raise_on_submit = SunoConflictError(
            "idempotency_conflict", "Key reused with a different body."
        )
        response = self.client.post("/v1/suno/music/generations", json={"prompt": "x"})
        self.assertEqual(response.status_code, 409)

    def test_unknown_job_is_a_404(self):
        self.assertEqual(self.client.get("/v1/suno/jobs/nope").status_code, 404)

    def test_audio_streams_bytes(self):
        response = self.client.get("/v1/suno/jobs/sj_test/audio/clip-a")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"audio-bytes")
        self.assertEqual(response.headers["content-type"], "audio/mpeg")

    def test_retired_audio_returns_410(self):
        self.service.audio_error = SunoConflictError(
            "audio_retired", "Account deleted.", http_status=410
        )
        response = self.client.get("/v1/suno/jobs/sj_test/audio/clip-a")
        self.assertEqual(response.status_code, 410)

    def test_audio_processing_returns_409(self):
        self.service.audio_error = SunoConflictError("audio_processing", "Still packaging.")
        response = self.client.get("/v1/suno/jobs/sj_test/audio/clip-a")
        self.assertEqual(response.status_code, 409)

    def test_accounts_listing_has_no_credentials(self):
        response = self.client.get("/v1/suno/accounts")
        self.assertEqual(response.status_code, 200)
        body = response.text
        for secret in ("cookie", "__client", "jwt", "clerk_sid"):
            self.assertNotIn(secret, body)

    # ------------------------------------------------------------- admin side

    def test_account_import_rejects_the_shared_api_key(self):
        """The generation key must not be able to install credentials."""
        response = self.client.post("/api/suno/accounts", json={"cookie": "__client=x"})
        self.assertIn(response.status_code, (401, 403))
        response = self.client.post(
            "/api/suno/accounts", json={"cookie": "__client=x"},
            headers={"Authorization": "Bearer api-key"},
        )
        self.assertIn(response.status_code, (401, 403))

    def test_account_import_accepts_an_admin_session(self):
        response = self.client.post(
            "/api/suno/accounts", json={"cookie": "__client=x"},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        self.assertEqual(response.status_code, 200)

    def test_account_delete_requires_admin(self):
        self.assertIn(self.client.delete("/api/suno/accounts/1").status_code, (401, 403))
        response = self.client.delete(
            "/api/suno/accounts/1?retire_audio=true",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["audio_retired"])
        self.assertEqual(self.service.deleted, [1])

    def test_resolve_requires_admin(self):
        self.assertIn(
            self.client.post("/api/suno/jobs/sj_test/resolve",
                             json={"action": "fail"}).status_code,
            (401, 403),
        )
        response = self.client.post(
            "/api/suno/jobs/sj_test/resolve",
            json={"action": "fail", "confirm_no_upstream_work": True},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        self.assertEqual(response.status_code, 200)

    def test_limits_require_admin(self):
        self.assertIn(
            self.client.put("/api/suno/accounts/1/limits",
                            json={"operator_limit": 2}).status_code,
            (401, 403),
        )
        response = self.client.put(
            "/api/suno/accounts/1/limits", json={"operator_limit": 2, "provider_cap": 2},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        self.assertEqual(response.status_code, 200)


class ServiceUnavailableTests(unittest.TestCase):
    def test_routes_report_503_without_a_service(self):
        suno_router.set_service(None)
        app = FastAPI()
        app.include_router(suno_router.router)
        app.dependency_overrides[verify_api_key_flexible] = lambda: "api-key"
        client = TestClient(app)
        response = client.post("/v1/suno/music/generations", json={"prompt": "x"})
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
