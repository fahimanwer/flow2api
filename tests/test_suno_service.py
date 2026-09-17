"""Service-level tests for the Suno provider.

Real SQLite (a temp file, through the production ``Database`` class) with a
fake Suno transport, so the job store, dispatcher, poller and recovery rules are
exercised for real without touching Suno or needing an account.

The invariants under test are the expensive ones to get wrong: a job that was
never submitted must not hold account capacity, a job that may have been
submitted must never be retried, and a finished job must not be settled while
its A/B sibling is still rendering.
"""

import asyncio
import time
import os
import tempfile
import unittest

from src.core import suno_models as sm
from src.core.database import Database
from src.core.suno_models import SunoConflictError, SunoValidationError
from src.services.suno_client import (
    SunoAPIError,
    SunoAuthError,
    SunoRateLimited,
    SunoSession,
    parse_cookie_string,
)
from src.services.suno_service import SunoService


class FakeClient:
    """Stand-in for SunoClient. Records calls and replays scripted answers."""

    JWT_TTL_SECONDS = 45

    def __init__(self):
        self.captcha = {"required": False, "captcha_version": None}
        self.generate_result = [
            {"id": "clip-a", "status": "submitted", "metadata": {}},
            {"id": "clip-b", "status": "submitted", "metadata": {}},
        ]
        self.generate_error = None
        self.feed_result = []
        self.download_result = {"url": "https://cdn1.suno.ai/clip-a.mp3"}
        self.download_error = None
        self.session_info_result = {"user_id": "user-1"}
        self.refresh_error = None
        self.generate_calls = []
        self.refresh_calls = 0
        self.refresh_delay = 0.0
        self.download_calls = []

    async def refresh_session(self, session: SunoSession):
        self.refresh_calls += 1
        if self.refresh_delay:
            await asyncio.sleep(self.refresh_delay)
        if self.refresh_error:
            raise self.refresh_error
        session.sid = session.sid or "sid-1"
        session.jwt = f"jwt-{self.refresh_calls}"
        session.jwt_obtained_at = 10 ** 12  # far future: never considered stale
        # Clerk rotates the cookie on every exchange.
        session.cookies["__client"] = f"rotated-{self.refresh_calls}"
        return session

    async def ensure_token(self, session):
        return session.jwt or "jwt"

    async def session_info(self, session):
        return self.session_info_result

    async def billing_info(self, session):
        return {"total_credits_left": 500, "subscription_type": "pro"}

    async def captcha_check(self, session, ctype="generation"):
        return dict(self.captcha)

    async def generate(self, session, payload):
        self.generate_calls.append(payload)
        if self.generate_error:
            raise self.generate_error
        return self.generate_result

    async def feed(self, session, clip_ids):
        return [c for c in self.feed_result if c.get("id") in set(clip_ids)]

    async def get_clip(self, session, clip_id):
        return next((c for c in self.feed_result if c.get("id") == clip_id), {})

    async def download_url(self, session, clip_id, fmt="mp3"):
        self.download_calls.append((clip_id, fmt))
        if self.download_error:
            raise self.download_error
        return self.download_result

    async def stream_download(self, url, **kwargs):
        yield b"ID3audio"


class SunoServiceTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(self.db_path)
        self.client = FakeClient()
        self.service = SunoService(self.db, proxy_manager=None, client=self.client)
        # Start the schema and recovery without the background loops, so each
        # test drives dispatch and polling deterministically.
        async with self.db.connect(write=True) as conn:
            await conn.executescript(__import__(
                "src.services.suno_service", fromlist=["_SCHEMA"]
            )._SCHEMA)
            await conn.commit()
        self.service._started = True

    async def asyncTearDown(self):
        self.service._started = False
        for task in list(self.service._refresh_tasks.values()):
            task.cancel()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    # ------------------------------------------------------------- helpers

    async def _add_account(self, **overrides):
        account = await self.service.import_account("__client=abc; other=1", "test")
        if overrides:
            await self.service._update_account(account["id"], **overrides)
        return account

    async def _submit(self, **body):
        body.setdefault("prompt", "a gentle piano piece")
        return await self.service.submit(body)

    async def _state(self, job_id):
        row = await self.service._fetch_job(job_id)
        return row["state"] if row else None


class AccountTests(SunoServiceTestCase):
    async def test_import_requires_client_cookie(self):
        with self.assertRaises(SunoValidationError) as ctx:
            await self.service.import_account("session=nope")
        self.assertEqual(ctx.exception.code, "invalid_cookie")

    async def test_import_persists_rotated_cookie(self):
        account = await self._add_account()
        row = await self.service._fetch_account(account["id"])
        # Clerk rotated __client during the exchange; the stored jar must carry
        # the new value or the next refresh fails.
        self.assertIn("rotated-", row["cookies"])
        self.assertEqual(row["clerk_sid"], "sid-1")

    async def test_mid_call_cookie_rotation_is_persisted_on_next_use(self):
        """A cookie rotated by an ordinary API response (not a refresh) must
        still reach the account row, or a restart strands the account."""
        account = await self._add_account()
        session = await self.service._session_for(account["id"])
        # Simulate what the client does when an upstream response carries a
        # new __client cookie.
        session.cookies["__client"] = "rotated-mid-call"
        session.dirty = True

        again = await self.service._session_for(account["id"])
        self.assertIs(again, session)
        self.assertFalse(session.dirty)
        row = await self.service._fetch_account(account["id"])
        self.assertIn("__client=rotated-mid-call", row["cookies"])

    async def test_close_persists_rotated_cookie(self):
        account = await self._add_account()
        session = await self.service._session_for(account["id"])
        session.cookies["__client"] = "rotated-at-shutdown"
        session.dirty = True
        await self.service.close()
        row = await self.service._fetch_account(account["id"])
        self.assertIn("__client=rotated-at-shutdown", row["cookies"])

    async def test_service_refreshes_before_client_ttl(self):
        """The service must re-mint inside the client's TTL, so the client's
        own unlocked in-place refresh never has to run."""
        account = await self._add_account()
        session = await self.service._session_for(account["id"])
        refreshes = self.client.refresh_calls
        # Age the token to just inside the client TTL but past the margin.
        session.jwt_obtained_at = time.time() - (self.client.JWT_TTL_SECONDS - 5)
        await self.service._session_for(account["id"])
        self.assertEqual(self.client.refresh_calls, refreshes + 1)

    async def test_public_account_hides_credentials(self):
        await self._add_account()
        accounts = await self.service.list_accounts()
        blob = repr(accounts)
        for secret in ("cookies", "clerk_sid", "jwt", "rotated-", "__client"):
            self.assertNotIn(secret, blob)

    async def test_duplicate_upstream_user_rejected(self):
        await self._add_account()
        with self.assertRaises(SunoConflictError) as ctx:
            await self.service.import_account("__client=def")
        self.assertEqual(ctx.exception.code, "duplicate_account")

    async def test_replacement_rejects_a_different_suno_user(self):
        account = await self._add_account()
        self.client.session_info_result = {"user_id": "someone-else"}
        with self.assertRaises(SunoConflictError) as ctx:
            await self.service.import_account("__client=xyz", account_id=account["id"])
        self.assertEqual(ctx.exception.code, "different_user")

    async def test_unknown_provider_cap_limits_to_one(self):
        account = await self._add_account()
        row = await self.service._fetch_account(account["id"])
        self.assertIsNone(row["provider_cap"])
        self.assertEqual(SunoService._effective_limit(row), 1)

        await self.service.set_account_limit(account["id"], operator_limit=4, provider_cap=2)
        row = await self.service._fetch_account(account["id"])
        self.assertEqual(SunoService._effective_limit(row), 2)

    async def test_operator_disable_is_independent_of_health(self):
        account = await self._add_account()
        await self.service.set_account_enabled(account["id"], False)
        row = await self.service._fetch_account(account["id"])
        self.assertTrue(row["operator_disabled"])
        # A successful refresh must not quietly put it back into rotation.
        await self.service._refresh_worker(account["id"])
        row = await self.service._fetch_account(account["id"])
        self.assertTrue(row["operator_disabled"])
        self.assertIsNone(await self.service._claim_next_job())


class SubmissionTests(SunoServiceTestCase):
    async def test_submit_returns_queued_job(self):
        job = await self._submit()
        self.assertEqual(job["state"], sm.QUEUED)
        self.assertTrue(job["job_id"].startswith("sj_"))

    async def test_idempotency_replays_the_same_job(self):
        first = await self._submit(idempotency_key="k1")
        second = await self._submit(idempotency_key="k1")
        self.assertEqual(first["job_id"], second["job_id"])

    async def test_idempotency_conflict_on_changed_body(self):
        await self._submit(idempotency_key="k2", prompt="one")
        with self.assertRaises(SunoConflictError) as ctx:
            await self._submit(idempotency_key="k2", prompt="two")
        self.assertEqual(ctx.exception.code, "idempotency_conflict")

    async def test_concurrent_identical_idempotent_submits_create_one_job(self):
        results = await asyncio.gather(*[
            self._submit(idempotency_key="race") for _ in range(5)
        ])
        self.assertEqual(len({r["job_id"] for r in results}), 1)

    async def test_queue_is_globally_bounded(self):
        self.service.MAX_ADMITTED_JOBS = 3
        for _ in range(3):
            await self._submit()
        with self.assertRaises(SunoValidationError) as ctx:
            await self._submit()
        self.assertEqual(ctx.exception.code, "queue_full")

    async def test_pinned_unknown_account_rejected(self):
        with self.assertRaises(SunoValidationError) as ctx:
            await self._submit(account_id=999)
        self.assertEqual(ctx.exception.code, "unknown_account")


class DispatchTests(SunoServiceTestCase):
    async def test_claim_marks_submitting_before_any_network_call(self):
        await self._add_account(provider_cap=2, operator_limit=2)
        job = await self._submit()
        claim = await self.service._claim_next_job()
        self.assertIsNotNone(claim)
        row = await self.service._fetch_job(job["job_id"])
        self.assertEqual(row["state"], sm.SUBMITTING)
        # The attempt and its upstream correlation id exist before we talk to
        # Suno, so a lost reply still leaves a record of what was tried.
        self.assertTrue(row["attempt_id"])
        self.assertTrue(row["transaction_uuid"])
        self.assertEqual(self.client.generate_calls, [])

    async def test_capacity_is_respected(self):
        await self._add_account(provider_cap=1, operator_limit=1)
        await self._submit()
        await self._submit()
        self.assertIsNotNone(await self.service._claim_next_job())
        # One slot, one in flight: the second job waits.
        self.assertIsNone(await self.service._claim_next_job())

    async def test_cooldown_blocks_dispatch(self):
        import time as _time
        await self._add_account(provider_cap=2, cooldown_until=_time.time() + 60)
        await self._submit()
        self.assertIsNone(await self.service._claim_next_job())

    async def test_successful_run_records_clip_ids(self):
        await self._add_account(provider_cap=2)
        job = await self._submit()
        job_id, account_id = await self.service._claim_next_job()
        await self.service._run_job(job_id, account_id)
        row = await self.service._fetch_job(job_id)
        self.assertEqual(row["state"], sm.SUBMITTED)
        self.assertEqual(sorted(__import__("json").loads(row["clip_ids"])), ["clip-a", "clip-b"])

    async def test_generate_payload_omits_captcha_token_when_not_required(self):
        await self._add_account(provider_cap=2)
        await self._submit()
        job_id, account_id = await self.service._claim_next_job()
        await self.service._run_job(job_id, account_id)
        payload = self.client.generate_calls[0]
        self.assertIsNone(payload["token"])
        self.assertEqual(payload["mv"], "chirp-crow")


class CaptchaGateTests(SunoServiceTestCase):
    async def test_blocked_job_releases_the_account_slot(self):
        self.client.captcha = {"required": True, "captcha_version": 1}
        await self._add_account(provider_cap=1, operator_limit=1)
        first = await self._submit()
        second = await self._submit()

        job_id, account_id = await self.service._claim_next_job()
        await self.service._run_job(job_id, account_id)

        self.assertEqual(await self._state(first["job_id"]), sm.BLOCKED_CAPTCHA)
        # Nothing reached Suno, so nothing was charged and no clip exists.
        self.assertEqual(self.client.generate_calls, [])
        row = await self.service._fetch_job(first["job_id"])
        self.assertIsNone(row["account_id"])

        # The slot is free again; the account is only paused, not consumed.
        await self.service._update_account(account_id, captcha_paused_until=0)
        claim = await self.service._claim_next_job()
        self.assertIsNotNone(claim)
        self.assertEqual(claim[0], second["job_id"])

    async def test_account_is_paused_after_a_block(self):
        import time as _time
        self.client.captcha = {"required": True, "captcha_version": 1}
        account = await self._add_account(provider_cap=1)
        await self._submit()
        job_id, account_id = await self.service._claim_next_job()
        await self.service._run_job(job_id, account_id)
        row = await self.service._fetch_account(account["id"])
        self.assertGreater(row["captcha_paused_until"], _time.time())

    async def test_blocked_job_can_be_retried(self):
        self.client.captcha = {"required": True, "captcha_version": 1}
        await self._add_account(provider_cap=1)
        job = await self._submit()
        job_id, account_id = await self.service._claim_next_job()
        await self.service._run_job(job_id, account_id)

        self.client.captcha = {"required": False, "captcha_version": None}
        await self.service.retry_blocked(job["job_id"])
        self.assertEqual(await self._state(job["job_id"]), sm.QUEUED)

    async def test_blocked_job_can_be_cancelled(self):
        self.client.captcha = {"required": True, "captcha_version": 1}
        await self._add_account(provider_cap=1)
        job = await self._submit()
        job_id, account_id = await self.service._claim_next_job()
        await self.service._run_job(job_id, account_id)
        await self.service.cancel(job["job_id"])
        self.assertEqual(await self._state(job["job_id"]), sm.CANCELLED)

    async def test_captcha_provider_token_is_used_when_available(self):
        class Provider:
            async def mint(self, version=None, site_key=None):
                return "minted-token"

        self.service.captcha_provider = Provider()
        self.client.captcha = {"required": True, "captcha_version": 1}
        await self._add_account(provider_cap=1)
        await self._submit()
        job_id, account_id = await self.service._claim_next_job()
        await self.service._run_job(job_id, account_id)
        self.assertEqual(self.client.generate_calls[0]["token"], "minted-token")
        self.assertEqual(self.client.generate_calls[0]["token_provider"], 1)


class AmbiguousSubmitTests(SunoServiceTestCase):
    async def test_timeout_becomes_needs_review_and_holds_the_slot(self):
        self.client.generate_error = SunoAPIError(0, "timed out", "transport_error")
        await self._add_account(provider_cap=1, operator_limit=1)
        job = await self._submit()
        await self._submit()

        job_id, account_id = await self.service._claim_next_job()
        await self.service._run_job(job_id, account_id)

        self.assertEqual(await self._state(job["job_id"]), sm.NEEDS_REVIEW)
        # The song may exist and be billed, so the slot stays reserved and the
        # second job must not take it.
        self.assertIsNone(await self.service._claim_next_job())

    async def test_server_error_is_also_ambiguous(self):
        self.client.generate_error = SunoAPIError(503, "upstream down")
        await self._add_account(provider_cap=1)
        job = await self._submit()
        job_id, account_id = await self.service._claim_next_job()
        await self.service._run_job(job_id, account_id)
        self.assertEqual(await self._state(job["job_id"]), sm.NEEDS_REVIEW)

    async def test_explicit_rejection_fails_without_review(self):
        self.client.generate_error = SunoAPIError(422, "token_validation_failed")
        await self._add_account(provider_cap=1)
        job = await self._submit()
        job_id, account_id = await self.service._claim_next_job()
        await self.service._run_job(job_id, account_id)
        self.assertEqual(await self._state(job["job_id"]), sm.FAILED)

    async def test_rate_limit_requeues_and_cools_the_account(self):
        import time as _time
        self.client.generate_error = SunoRateLimited(429, "slow down")
        account = await self._add_account(provider_cap=1)
        job = await self._submit()
        job_id, account_id = await self.service._claim_next_job()
        await self.service._run_job(job_id, account_id)
        self.assertEqual(await self._state(job["job_id"]), sm.QUEUED)
        row = await self.service._fetch_account(account["id"])
        self.assertGreater(row["cooldown_until"], _time.time())

    async def test_expired_session_requeues_and_flags_the_account(self):
        self.client.generate_error = SunoAuthError(401, "session expired")
        account = await self._add_account(provider_cap=1)
        job = await self._submit()
        job_id, account_id = await self.service._claim_next_job()
        await self.service._run_job(job_id, account_id)
        self.assertEqual(await self._state(job["job_id"]), sm.QUEUED)
        row = await self.service._fetch_account(account["id"])
        self.assertEqual(row["status"], sm.ACCOUNT_NEEDS_LOGIN)

    async def test_needs_review_cannot_be_cancelled_by_the_caller(self):
        self.client.generate_error = SunoAPIError(0, "timed out")
        await self._add_account(provider_cap=1)
        job = await self._submit()
        job_id, account_id = await self.service._claim_next_job()
        await self.service._run_job(job_id, account_id)
        with self.assertRaises(SunoConflictError) as ctx:
            await self.service.cancel(job["job_id"])
        self.assertEqual(ctx.exception.code, "not_cancellable")

    async def test_resolve_fail_requires_explicit_confirmation(self):
        self.client.generate_error = SunoAPIError(0, "timed out")
        await self._add_account(provider_cap=1)
        job = await self._submit()
        job_id, account_id = await self.service._claim_next_job()
        await self.service._run_job(job_id, account_id)

        with self.assertRaises(SunoValidationError) as ctx:
            await self.service.resolve(job["job_id"], "fail")
        self.assertEqual(ctx.exception.code, "confirmation_required")

        await self.service.resolve(job["job_id"], "fail", confirm_no_upstream_work=True)
        self.assertEqual(await self._state(job["job_id"]), sm.FAILED)

    async def test_resolve_resume_adopts_operator_supplied_clips(self):
        self.client.generate_error = SunoAPIError(0, "timed out")
        await self._add_account(provider_cap=1)
        job = await self._submit()
        job_id, account_id = await self.service._claim_next_job()
        await self.service._run_job(job_id, account_id)

        await self.service.resolve(job["job_id"], "resume", clip_ids=["found-1"])
        row = await self.service._fetch_job(job["job_id"])
        self.assertEqual(row["state"], sm.SUBMITTED)
        self.assertEqual(__import__("json").loads(row["clip_ids"]), ["found-1"])

    async def test_late_failure_cannot_overwrite_an_operator_resolution(self):
        """A straggler from the old attempt must not clobber the resolution."""
        self.client.generate_error = SunoAPIError(0, "timed out")
        await self._add_account(provider_cap=1)
        job = await self._submit()
        job_id, account_id = await self.service._claim_next_job()
        row = await self.service._fetch_job(job_id)
        stale_attempt = row["attempt_id"]
        await self.service._run_job(job_id, account_id)
        await self.service.resolve(job["job_id"], "fail", confirm_no_upstream_work=True)

        # The old attempt reports in after the operator already settled it.
        await self.service._settle_failed(job["job_id"], stale_attempt, "late error")
        await self.service._needs_review(job["job_id"], stale_attempt, "late review")
        self.assertEqual(await self._state(job["job_id"]), sm.FAILED)


class PollingTests(SunoServiceTestCase):
    async def _submitted_job(self):
        await self._add_account(provider_cap=2)
        await self._submit()
        job_id, account_id = await self.service._claim_next_job()
        await self.service._run_job(job_id, account_id)
        return job_id, account_id

    async def test_job_waits_while_one_clip_still_renders(self):
        job_id, account_id = await self._submitted_job()
        self.client.feed_result = [
            {"id": "clip-a", "status": "complete", "metadata": {}},
            {"id": "clip-b", "status": "streaming", "metadata": {}},
        ]
        job = await self.service._fetch_job(job_id)
        await self.service._poll_job(job, account_id)
        # streaming is playable but not terminal, so the job is still running.
        self.assertEqual(await self._state(job_id), sm.SUBMITTED)

    async def test_partial_success_when_one_clip_errors(self):
        job_id, account_id = await self._submitted_job()
        self.client.feed_result = [
            {"id": "clip-a", "status": "complete", "metadata": {}},
            {"id": "clip-b", "status": "error", "metadata": {"error_message": "bad prompt"}},
        ]
        job = await self.service._fetch_job(job_id)
        await self.service._poll_job(job, account_id)
        self.assertEqual(await self._state(job_id), sm.SUCCEEDED)

    async def test_all_clips_failing_fails_the_job(self):
        job_id, account_id = await self._submitted_job()
        self.client.feed_result = [
            {"id": "clip-a", "status": "error", "metadata": {"error_message": "nope"}},
            {"id": "clip-b", "status": "error", "metadata": {"error_message": "nope"}},
        ]
        job = await self.service._fetch_job(job_id)
        await self.service._poll_job(job, account_id)
        row = await self.service._fetch_job(job_id)
        self.assertEqual(row["state"], sm.FAILED)
        self.assertIn("nope", row["error"])

    async def test_success_requires_a_resolvable_download(self):
        """`status: complete` is not evidence of playable audio."""
        job_id, account_id = await self._submitted_job()
        self.client.feed_result = [
            {"id": "clip-a", "status": "complete", "metadata": {}},
            {"id": "clip-b", "status": "complete", "metadata": {}},
        ]
        self.client.download_result = {"status": "processing"}
        job = await self.service._fetch_job(job_id)
        await self.service._poll_job(job, account_id)
        self.assertEqual(await self._state(job_id), sm.FINALIZING)

        self.client.download_result = {"url": "https://cdn1.suno.ai/clip-a.mp3"}
        await self.service._finalize(job_id, account_id, ["clip-a"])
        self.assertEqual(await self._state(job_id), sm.SUCCEEDED)

    async def test_unresolvable_download_escalates_instead_of_regenerating(self):
        job_id, account_id = await self._submitted_job()
        self.client.download_result = {"status": "processing"}
        self.service.MAX_FINALIZE_ATTEMPTS = 2
        async with self.db.connect(write=True) as conn:
            await conn.execute(
                "UPDATE suno_jobs SET state = ? WHERE id = ?", (sm.FINALIZING, job_id)
            )
            await conn.commit()
        for _ in range(2):
            await self.service._finalize(job_id, account_id, ["clip-a"])
        # The songs exist and were paid for: escalate, never re-run.
        self.assertEqual(await self._state(job_id), sm.NEEDS_REVIEW)
        self.assertEqual(self.client.generate_calls and len(self.client.generate_calls), 1)

    async def test_poll_failure_is_not_a_job_failure(self):
        job_id, account_id = await self._submitted_job()

        async def boom(session, clip_ids):
            raise SunoAPIError(503, "feed down")

        self.client.feed = boom
        await self.service._poll_once()
        self.assertEqual(await self._state(job_id), sm.SUBMITTED)


class SessionRefreshTests(SunoServiceTestCase):
    async def test_concurrent_callers_share_one_refresh(self):
        account = await self._add_account()
        self.service._sessions.clear()
        self.client.refresh_calls = 0
        self.client.refresh_delay = 0.05
        await asyncio.gather(*[self.service._session_for(account["id"]) for _ in range(5)])
        self.assertEqual(self.client.refresh_calls, 1)

    async def test_cancelled_waiter_does_not_abort_the_refresh(self):
        account = await self._add_account()
        self.service._sessions.clear()
        self.client.refresh_calls = 0
        self.client.refresh_delay = 0.1

        waiter = asyncio.create_task(self.service._session_for(account["id"]))
        await asyncio.sleep(0.01)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter

        # The shared task owns the lock through refresh AND the durable save,
        # so the rotated cookie still lands in the database.
        task = self.service._refresh_tasks[account["id"]]
        await task
        row = await self.service._fetch_account(account["id"])
        self.assertIn("rotated-", row["cookies"])

    async def test_rejected_cookie_marks_the_account_needs_login(self):
        account = await self._add_account()
        self.service._sessions.clear()
        self.client.refresh_error = SunoAuthError(401, "bad cookie")
        with self.assertRaises(SunoAuthError):
            await self.service._session_for(account["id"])
        row = await self.service._fetch_account(account["id"])
        self.assertEqual(row["status"], sm.ACCOUNT_NEEDS_LOGIN)

    async def test_credential_replacement_serializes_with_refresh(self):
        account = await self._add_account()
        self.service._sessions.clear()
        self.client.refresh_delay = 0.05
        refresh = asyncio.create_task(self.service._session_for(account["id"]))
        await asyncio.sleep(0.01)
        replace = asyncio.create_task(
            self.service.import_account("__client=new", account_id=account["id"])
        )
        await asyncio.gather(refresh, replace)
        row = await self.service._fetch_account(account["id"])
        # The replacement ran last and therefore wins; no interleaved write.
        self.assertIn("rotated-", row["cookies"])
        self.assertEqual(row["status"], sm.ACCOUNT_READY)


class AccountDeletionTests(SunoServiceTestCase):
    async def test_delete_refused_while_jobs_are_in_flight(self):
        await self._add_account(provider_cap=1)
        await self._submit()
        job_id, account_id = await self.service._claim_next_job()
        await self.service._run_job(job_id, account_id)
        with self.assertRaises(SunoConflictError) as ctx:
            await self.service.delete_account(account_id)
        self.assertEqual(ctx.exception.code, "jobs_in_flight")

    async def test_delete_refused_while_finished_jobs_serve_audio(self):
        account_id, job_id = await self._finish_a_job()
        with self.assertRaises(SunoConflictError) as ctx:
            await self.service.delete_account(account_id)
        self.assertEqual(ctx.exception.code, "audio_depends_on_account")

    async def test_retire_audio_deletes_and_marks_jobs(self):
        account_id, job_id = await self._finish_a_job()
        result = await self.service.delete_account(account_id, retire_audio=True)
        self.assertTrue(result["audio_retired"])
        row = await self.service._fetch_job(job_id)
        self.assertIsNone(row["account_id"])
        self.assertEqual(row["state"], sm.SUCCEEDED)  # the song still happened

    async def _finish_a_job(self):
        await self._add_account(provider_cap=2)
        await self._submit()
        job_id, account_id = await self.service._claim_next_job()
        await self.service._run_job(job_id, account_id)
        self.client.feed_result = [
            {"id": "clip-a", "status": "complete", "metadata": {}},
            {"id": "clip-b", "status": "complete", "metadata": {}},
        ]
        job = await self.service._fetch_job(job_id)
        await self.service._poll_job(job, account_id)
        self.assertEqual(await self._state(job_id), sm.SUCCEEDED)
        return account_id, job_id


class AudioTests(SunoServiceTestCase):
    async def _ready_job(self):
        await self._add_account(provider_cap=2)
        await self._submit()
        job_id, account_id = await self.service._claim_next_job()
        await self.service._run_job(job_id, account_id)
        return job_id

    async def test_streams_audio_for_a_clip_of_that_job(self):
        job_id = await self._ready_job()
        chunks = [c async for c in self.service.stream_audio(job_id, "clip-a")]
        self.assertEqual(b"".join(chunks), b"ID3audio")

    async def test_rejects_a_clip_from_another_job(self):
        job_id = await self._ready_job()
        with self.assertRaises(SunoValidationError) as ctx:
            async for _ in self.service.stream_audio(job_id, "someone-elses-clip"):
                pass
        self.assertEqual(ctx.exception.code, "clip_not_in_job")

    async def test_retired_audio_reports_410(self):
        job_id = await self._ready_job()
        async with self.db.connect(write=True) as conn:
            await conn.execute("UPDATE suno_jobs SET account_id = NULL WHERE id = ?", (job_id,))
            await conn.commit()
        with self.assertRaises(SunoConflictError) as ctx:
            async for _ in self.service.stream_audio(job_id, "clip-a"):
                pass
        self.assertEqual(ctx.exception.code, "audio_retired")
        self.assertEqual(ctx.exception.extra.get("http_status"), 410)

    async def test_processing_download_is_reported_not_streamed(self):
        job_id = await self._ready_job()
        self.client.download_result = {"status": "processing"}
        with self.assertRaises(SunoConflictError) as ctx:
            async for _ in self.service.stream_audio(job_id, "clip-a"):
                pass
        self.assertEqual(ctx.exception.code, "audio_processing")

    async def test_concurrency_is_bounded(self):
        self.assertEqual(self.service._audio_slots._value, self.service.MAX_AUDIO_STREAMS)


class RestartRecoveryTests(SunoServiceTestCase):
    async def test_submitting_jobs_become_needs_review_after_restart(self):
        await self._add_account(provider_cap=1)
        job = await self._submit()
        await self.service._claim_next_job()  # leaves the job in submitting

        stats = await self.service._recover_after_restart()
        self.assertEqual(stats["requeued"], 1)
        # The generate call may have landed before the process died, so it is a
        # review, not a re-queue.
        self.assertEqual(await self._state(job["job_id"]), sm.NEEDS_REVIEW)

    async def test_submitted_jobs_are_resumed_not_resubmitted(self):
        await self._add_account(provider_cap=2)
        await self._submit()
        job_id, account_id = await self.service._claim_next_job()
        await self.service._run_job(job_id, account_id)

        stats = await self.service._recover_after_restart()
        self.assertEqual(stats["resumed"], 1)
        self.assertEqual(await self._state(job_id), sm.SUBMITTED)
        self.assertEqual(len(self.client.generate_calls), 1)


class CookieParsingTests(unittest.TestCase):
    def test_parses_a_pasted_devtools_header(self):
        jar = parse_cookie_string("Cookie: __client=abc; ajs_anonymous_id=xyz;  extra=1 ")
        self.assertEqual(jar["__client"], "abc")
        self.assertEqual(jar["ajs_anonymous_id"], "xyz")
        self.assertEqual(jar["extra"], "1")

    def test_tolerates_newlines_and_empty_segments(self):
        jar = parse_cookie_string("__client=abc;\n\n; broken; k=v")
        self.assertEqual(jar, {"__client": "abc", "k": "v"})

    def test_session_cookie_roundtrip(self):
        session = SunoSession({"__client": "abc", "k": "v"})
        self.assertIn("__client=abc", session.cookie_string())
        self.assertEqual(session.client_cookie, "abc")


if __name__ == "__main__":
    unittest.main()
