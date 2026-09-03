"""Regression tests for the 2026-09-03 production fixes.

  1. A BOUND account (non-empty route_key) is eligible / mintable ONLY when its own
     browser is online. The old non-strict lookup fell back to any browser, so the
     load balancer's route check was a no-op and offline accounts kept being picked
     (their reCAPTCHA minted on another employee's device).
  2. "Failed to obtain reCAPTCHA token" (extension could not mint at all) is a device
     failure: a short flat pause, NO account strike, NO escalation. It used to match
     the "recaptcha" environmental marker and bench the account for up to 2 h.
  3. An access token that is already expired and whose refresh fails for a
     non-transient reason ("unknown") is disabled (recoverable auto_st_expired)
     instead of staying is_active=1 forever while every request skips it.
"""
import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from src.services.browser_captcha_extension import (
    ExtensionCaptchaService,
    ExtensionConnection,
)
from src.services.token_manager import (
    RefreshOutcome,
    TokenManager,
    _is_captcha_mint_failure,
    _is_environmental_token_error,
)


def _svc(*route_keys):
    svc = ExtensionCaptchaService.__new__(ExtensionCaptchaService)
    ExtensionCaptchaService.__init__(svc, db=None)
    svc.db = None
    svc.active_connections = [ExtensionConnection(websocket=MagicMock(), route_key=k) for k in route_keys]
    svc.pending_requests = {}
    svc._rr_index = 0
    return svc


class RouteStrictness(unittest.TestCase):
    def test_bound_account_needs_its_own_browser(self):
        svc = _svc("auto-aaa", "auto-bbb")
        self.assertTrue(svc._has_connection_for_route_key("auto-aaa"))
        # Offline bound account: must NOT be reported as connected just because others are.
        self.assertFalse(svc._has_connection_for_route_key("auto-zzz"))

    def test_unbound_account_uses_shared_pool_browsers_only(self):
        svc = _svc("auto-aaa")           # only a BOUND browser online
        self.assertFalse(svc._has_connection_for_route_key(""))
        self.assertFalse(svc._has_connection_for_route_key(None))
        svc = _svc("auto-aaa", "")       # plus one shared-pool browser
        self.assertTrue(svc._has_connection_for_route_key(""))

    def test_no_browsers_at_all(self):
        svc = _svc()
        self.assertFalse(svc._has_connection_for_route_key("auto-aaa"))
        self.assertFalse(svc._has_connection_for_route_key(""))

    def test_get_token_refuses_to_mint_on_another_device(self):
        svc = _svc("auto-aaa")
        svc._resolve_route_key = AsyncMock(return_value="auto-zzz")
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(svc.get_token("proj", token_id=7))
        self.assertIn("No Chrome Extension connection matches", str(ctx.exception))
        # and nothing was sent to the unrelated browser
        svc.active_connections[0].websocket.send_text.assert_not_called()


def _make_tm():
    tm = TokenManager.__new__(TokenManager)
    tm.db = MagicMock()
    tm.db.update_token = AsyncMock()
    tm.db.upsert_recaptcha_cooldown = AsyncMock()
    tm.db.delete_recaptcha_cooldown = AsyncMock()
    tm.db.increment_token_stats = AsyncMock()
    tm.db.get_token_stats = AsyncMock()
    tm.db.get_admin_config = AsyncMock()
    tm._recaptcha_cd = {}
    tm._health_cd = {}
    tm._model_quota_until = {}
    tm._quota_loaded = True
    return tm


class MintFailureClassification(unittest.TestCase):
    def test_marker_classification(self):
        self.assertTrue(_is_captcha_mint_failure("Failed to obtain reCAPTCHA token"))
        self.assertTrue(_is_captcha_mint_failure("No Chrome Extension connection matches token_id=5 route_key='x'"))
        # Real Google rejections are still environmental, not mint failures
        self.assertFalse(_is_captcha_mint_failure("PUBLIC_ERROR_UNUSUAL_ACTIVITY: reCAPTCHA evaluation failed"))
        self.assertTrue(_is_environmental_token_error("PUBLIC_ERROR_UNUSUAL_ACTIVITY: reCAPTCHA evaluation failed"))

    def test_mint_failure_is_flat_pause_no_strike(self):
        tm = _make_tm()
        asyncio.run(tm.record_error(46, "Failed to obtain reCAPTCHA token", "gemini-3.0-pro-image-landscape-2k"))
        until, strikes = tm._recaptcha_cd[46]
        self.assertEqual(strikes, 0)
        remaining = (until - datetime.now(timezone.utc)).total_seconds()
        self.assertLessEqual(remaining, TokenManager.MINT_FAILURE_PAUSE_SECONDS)
        self.assertGreater(remaining, TokenManager.MINT_FAILURE_PAUSE_SECONDS - 5)
        self.assertTrue(tm.is_recaptcha_cooldown(46))
        tm.db.increment_token_stats.assert_not_called()  # not an auto-disable error

    def test_repeated_mint_failures_do_not_escalate(self):
        tm = _make_tm()
        for _ in range(10):
            asyncio.run(tm.mark_mint_failure(46))
        until, strikes = tm._recaptcha_cd[46]
        self.assertEqual(strikes, 0)
        self.assertLessEqual((until - datetime.now(timezone.utc)).total_seconds(), TokenManager.MINT_FAILURE_PAUSE_SECONDS)

    def test_mint_failure_never_shortens_a_real_cooldown(self):
        tm = _make_tm()
        long_until = datetime.now(timezone.utc) + timedelta(hours=1)
        tm._recaptcha_cd[46] = (long_until, 5)
        asyncio.run(tm.mark_mint_failure(46))
        self.assertEqual(tm._recaptcha_cd[46], (long_until, 5))

    def test_real_google_rejection_still_escalates(self):
        tm = _make_tm()
        asyncio.run(tm.record_error(46, "PUBLIC_ERROR_UNUSUAL_ACTIVITY: reCAPTCHA evaluation failed", None))
        self.assertEqual(tm._recaptcha_cd[46][1], 1)


class DeadSessionDisable(unittest.TestCase):
    def _tm(self, at_expires, outcome_reason):
        tm = _make_tm()
        tm.is_health_cooldown = lambda *a, **k: False
        tm._should_refresh_at = lambda token: True
        tm._refresh_at = AsyncMock(return_value=RefreshOutcome(False, outcome_reason))
        tm.disable_token = AsyncMock()
        tok = MagicMock(); tok.id = 75; tok.at = "at"; tok.at_expires = at_expires
        return tm, tok

    def test_expired_at_unknown_failure_disables(self):
        tm, tok = self._tm(datetime.now(timezone.utc) - timedelta(hours=4), "unknown")
        self.assertIsNone(asyncio.run(tm.ensure_valid_token(tok)))
        tm.disable_token.assert_awaited_once_with(75, reason="auto_st_expired")

    def test_live_at_unknown_failure_does_not_disable(self):
        tm, tok = self._tm(datetime.now(timezone.utc) + timedelta(hours=4), "unknown")
        self.assertIsNone(asyncio.run(tm.ensure_valid_token(tok)))
        tm.disable_token.assert_not_awaited()

    def test_expired_at_network_failure_does_not_disable(self):
        tm, tok = self._tm(datetime.now(timezone.utc) - timedelta(hours=4), "network")
        self.assertIsNone(asyncio.run(tm.ensure_valid_token(tok)))
        tm.disable_token.assert_not_awaited()

    def test_manual_path_never_disables(self):
        tm, tok = self._tm(datetime.now(timezone.utc) - timedelta(hours=4), "unknown")
        self.assertIsNone(asyncio.run(tm.ensure_valid_token(tok, disable_on_failure=False)))
        tm.disable_token.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()


class PromptRejectionNotAccountError(unittest.TestCase):
    """Google safety-filter rejections are prompt faults: no strike, no disable count."""

    def test_unsafe_generation_not_counted(self):
        from src.services.token_manager import _is_prompt_rejection
        msg = "Generation failed: PUBLIC_ERROR_UNSAFE_GENERATION: Request contains an invalid argument."
        self.assertTrue(_is_prompt_rejection(msg))
        tm = _make_tm()
        asyncio.run(tm.record_error(30, msg, "gemini-3.1-flash-image-landscape"))
        tm.db.increment_token_stats.assert_not_called()
        self.assertNotIn(30, tm._recaptcha_cd)
        tm.db.update_token.assert_awaited()  # last_error_at stamped

    def test_real_errors_still_count(self):
        from src.services.token_manager import _is_prompt_rejection
        self.assertFalse(_is_prompt_rejection("HTTP Error 404: Requested entity was not found."))
        self.assertFalse(_is_prompt_rejection("PUBLIC_ERROR_UNUSUAL_ACTIVITY: reCAPTCHA evaluation failed"))


class UnboundAccountsUseOnlySharedBrowsers(unittest.TestCase):
    """An account with no route key may only mint on browsers that registered with
    no route key (shared pool). Bound browsers belong to one employee's account."""

    def test_unbound_account_with_only_bound_browsers_is_not_connected(self):
        svc = _svc("auto-aaa", "auto-bbb")
        self.assertFalse(svc._has_connection_for_route_key(""))
        self.assertIsNone(svc._select_connection(""))

    def test_unbound_account_uses_shared_browser(self):
        svc = _svc("auto-aaa", "", "auto-bbb")
        conn = svc._select_connection("")
        self.assertIsNotNone(conn)
        self.assertEqual(conn.route_key, "")
        self.assertTrue(svc._has_connection_for_route_key(""))

    def test_bound_offline_account_never_borrows_a_bound_browser(self):
        svc = _svc("auto-aaa")
        self.assertIsNone(svc._select_connection("auto-zzz"))          # non-strict path
        self.assertIsNone(svc._select_connection("auto-zzz", strict=True))


class RoutePauseAfterMintFailure(unittest.TestCase):
    """After one mint fails on a browser, requests queued on that route fail fast
    instead of reopening the Flow tab every few seconds."""

    def test_queued_requests_fail_fast_after_a_failed_mint(self):
        from src.core.config import config
        captcha = config._config.setdefault("captcha", {})
        saved = {k: captcha.get(k) for k in ("extension_route_min_interval_seconds", "extension_global_min_interval_seconds")}
        captcha["extension_route_min_interval_seconds"] = 0
        captcha["extension_global_min_interval_seconds"] = 0
        try:
            svc = _svc("auto-aaa")
            svc._resolve_route_key = AsyncMock(return_value="auto-aaa")
            svc._dispatch_token_request = AsyncMock(return_value=None)  # browser could not mint

            async def run():
                first = await svc.get_token("proj", token_id=7)
                self.assertIsNone(first)
                with self.assertRaises(RuntimeError) as ctx:
                    await svc.get_token("proj", token_id=7)
                self.assertIn("route paused after mint failure", str(ctx.exception))
            asyncio.run(run())
            self.assertEqual(svc._dispatch_token_request.await_count, 1)  # second never reached the browser
            from src.services.token_manager import _is_captcha_mint_failure
            self.assertTrue(_is_captcha_mint_failure("Extension route paused after mint failure (88s left)"))
        finally:
            for k, v in saved.items():
                if v is None:
                    captcha.pop(k, None)
                else:
                    captcha[k] = v

    def test_successful_mint_clears_the_pause(self):
        svc = _svc("auto-aaa")
        svc._route_paused_until["auto-aaa"] = 0.0
        svc._resolve_route_key = AsyncMock(return_value="auto-aaa")
        svc._dispatch_token_request = AsyncMock(return_value="tok")
        from src.core.config import config
        captcha = config._config.setdefault("captcha", {})
        captcha["extension_route_min_interval_seconds"] = 0
        captcha["extension_global_min_interval_seconds"] = 0
        self.assertEqual(asyncio.run(svc.get_token("proj", token_id=7)), "tok")
        self.assertNotIn("auto-aaa", svc._route_paused_until)
