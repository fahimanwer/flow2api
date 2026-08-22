"""Regression tests for manual/automatic AT-refresh behavior.

Covers the production fix:
  - failures are classified (st_expired / network / unknown) via structured
    FlowAPIError status/reason, not brittle substring matching;
  - only a confirmed credential failure (st_expired) disables a token, and only
    on the automatic pool path (disable_on_failure=True);
  - manual admin paths never disable;
  - the browser/extension ST refresh is attempted only for st_expired, never
    for a transient network error;
  - _refresh_at_inner is side-effect-free (it never disables).
"""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from src.services.token_manager import TokenManager, RefreshOutcome
from src.services.flow_client import FlowAPIError


def _make_tm():
    tm = TokenManager.__new__(TokenManager)
    tm.db = MagicMock()
    tm.flow_client = MagicMock()
    tm.flow_client._is_timeout_error = lambda e: "timed out" in str(e).lower() or "timeout" in str(e).lower()
    tm.flow_client._is_proxy_connection_error = lambda e: "proxy" in str(e).lower()
    tm.db.update_token = AsyncMock()
    tm.db.get_token = AsyncMock()
    tm._refresh_locks = {}
    tm._refresh_lock_guard = MagicMock()
    tm._get_token_lock = AsyncMock(return_value=asyncio.Lock())
    # Post-merge state: upstream's AT-validation cache (set in __init__, which
    # this fixture bypasses via __new__).
    tm._at_validation_cache = {}
    tm._health_cd = {}
    tm.db.delete_token_health_cooldown = AsyncMock()
    tm.db.upsert_token_health_cooldown = AsyncMock()
    return tm


class RefreshClassificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_st_to_at_401_is_st_expired(self):
        tm = _make_tm()
        tm.flow_client.st_to_at = AsyncMock(
            side_effect=FlowAPIError(401, "HTTP Error 401: x", "UNAUTHENTICATED")
        )
        outcome = await tm._do_refresh_at(1, "st")
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.reason, "st_expired")

    async def test_st_to_at_timeout_is_network(self):
        tm = _make_tm()
        tm.flow_client.st_to_at = AsyncMock(
            side_effect=Exception("Flow API request failed: connection timed out")
        )
        outcome = await tm._do_refresh_at(1, "st")
        self.assertEqual(outcome.reason, "network")

    async def test_verify_401_is_at_stale_and_writes_nothing(self):
        # Session alive (st_to_at returns a user) but the API rejects the embedded
        # access token: Google stopped renewing the grant. This is at_stale, NOT
        # st_expired — and validate-then-promote must leave stored credentials alone.
        tm = _make_tm()
        tm.flow_client.st_to_at = AsyncMock(return_value={"access_token": "AT", "expires": "2026-08-08T02:41:57.000Z"})
        tm.flow_client.get_credits = AsyncMock(
            side_effect=FlowAPIError(401, "HTTP Error 401", "UNAUTHENTICATED")
        )
        outcome = await tm._do_refresh_at(1, "st")
        self.assertEqual(outcome.reason, "at_stale")
        self.assertEqual(tm.db.update_token.await_count, 0)

    async def test_verify_ok_promotes_st_at_and_credits_in_one_write(self):
        tm = _make_tm()
        tm.flow_client.st_to_at = AsyncMock(return_value={"access_token": "AT2", "expires": None})
        tm.flow_client.get_credits = AsyncMock(return_value={"credits": 50, "userPaygateTier": "T"})
        outcome = await tm._do_refresh_at(1, "st2")
        self.assertTrue(outcome.success)
        self.assertEqual(tm.db.update_token.await_count, 1)
        kwargs = tm.db.update_token.await_args.kwargs
        self.assertEqual(kwargs["st"], "st2")
        self.assertEqual(kwargs["at"], "AT2")
        self.assertEqual(kwargs["credits"], 50)

    async def test_verify_non_auth_error_still_success(self):
        # A transient non-auth verify error must not fail the refresh: the AT is
        # promoted anyway (preserves long-standing behavior).
        tm = _make_tm()
        tm.flow_client.st_to_at = AsyncMock(return_value={"access_token": "AT", "expires": None})
        tm.flow_client.get_credits = AsyncMock(side_effect=Exception("temporary 500 blip"))
        outcome = await tm._do_refresh_at(1, "st")
        self.assertTrue(outcome.success)
        self.assertEqual(tm.db.update_token.await_count, 1)


class DisablePolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_automatic_st_expired_disables(self):
        tm = _make_tm()
        tm._should_refresh_at = lambda t: True
        tm._refresh_at = AsyncMock(return_value=RefreshOutcome(False, "st_expired"))
        tm.disable_token = AsyncMock()
        tok = MagicMock()
        tok.id = 7
        res = await tm.ensure_valid_token(tok, disable_on_failure=True)
        self.assertIsNone(res)
        self.assertEqual(tm.disable_token.await_count, 1)

    async def test_automatic_network_does_not_disable(self):
        tm = _make_tm()
        tm._should_refresh_at = lambda t: True
        tm._refresh_at = AsyncMock(return_value=RefreshOutcome(False, "network"))
        tm.disable_token = AsyncMock()
        tok = MagicMock()
        tok.id = 8
        res = await tm.ensure_valid_token(tok, disable_on_failure=True)
        self.assertIsNone(res)
        self.assertEqual(tm.disable_token.await_count, 0)

    async def test_automatic_at_stale_never_disables_here(self):
        # at_stale is owned by the refresh leader (pause → reload → threshold); the
        # pool path must NOT disable on it.
        tm = _make_tm()
        tm._should_refresh_at = lambda t: True
        tm._refresh_at = AsyncMock(return_value=RefreshOutcome(False, "at_stale"))
        tm.disable_token = AsyncMock()
        tok = MagicMock()
        tok.id = 10
        res = await tm.ensure_valid_token(tok, disable_on_failure=True)
        self.assertIsNone(res)
        self.assertEqual(tm.disable_token.await_count, 0)

    async def test_at_stale_pause_skips_token_without_api_call(self):
        tm = _make_tm()
        from datetime import datetime, timezone, timedelta
        tm._health_cd[(11, "at_stale")] = {"until": datetime.now(timezone.utc) + timedelta(minutes=5), "strikes": 1,
                                           "first_failure_at": None, "last_failure_at": None, "fingerprint": "x"}
        tm._refresh_at = AsyncMock()
        tok = MagicMock()
        tok.id = 11
        res = await tm.ensure_valid_token(tok, disable_on_failure=True)
        self.assertIsNone(res)
        self.assertEqual(tm._refresh_at.await_count, 0)
        self.assertTrue(tm.is_health_cooldown(11))

    async def test_manual_path_never_disables(self):
        tm = _make_tm()
        tm._should_refresh_at = lambda t: True
        tm._refresh_at = AsyncMock(return_value=RefreshOutcome(False, "st_expired"))
        tm.disable_token = AsyncMock()
        tok = MagicMock()
        tok.id = 9
        res = await tm.ensure_valid_token(tok, disable_on_failure=False)
        self.assertIsNone(res)
        self.assertEqual(tm.disable_token.await_count, 0)


class RefreshInnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_inner_is_side_effect_free_on_network(self):
        tm = _make_tm()
        tm.db.get_token = AsyncMock(return_value=MagicMock(st="st"))
        tm._do_refresh_at = AsyncMock(return_value=RefreshOutcome(False, "network"))
        tm._try_refresh_st = AsyncMock(return_value="newst")
        tm.disable_token = AsyncMock()
        outcome = await tm._refresh_at_inner(5)
        self.assertEqual(outcome.reason, "network")
        # network must NOT trigger a session refresh and must NOT disable.
        self.assertEqual(tm._try_refresh_st.await_count, 0)
        self.assertEqual(tm.disable_token.await_count, 0)

    async def test_inner_st_expired_triggers_st_refresh_then_succeeds(self):
        tm = _make_tm()
        tm.db.get_token = AsyncMock(return_value=MagicMock(st="st"))
        tm._do_refresh_at = AsyncMock(
            side_effect=[RefreshOutcome(False, "st_expired"), RefreshOutcome(True, "ok")]
        )
        tm._try_refresh_st = AsyncMock(return_value="newst")
        tm.disable_token = AsyncMock()
        outcome = await tm._refresh_at_inner(5)
        self.assertTrue(outcome.success)
        self.assertEqual(tm._try_refresh_st.await_count, 1)
        self.assertEqual(tm.disable_token.await_count, 0)


class FlowAPIErrorTests(unittest.TestCase):
    def test_str_reproduces_legacy_message(self):
        err = FlowAPIError(401, "HTTP Error 401: nope", "UNAUTHENTICATED")
        self.assertEqual(str(err), "HTTP Error 401: nope")
        self.assertEqual(err.status_code, 401)
        self.assertEqual(err.reason, "UNAUTHENTICATED")


if __name__ == "__main__":
    unittest.main()


class AtStaleLeaderTests(unittest.IsolatedAsyncioTestCase):
    """The single-flight leader owns the at_stale strike / device reload / CAS disable."""

    def _tm(self):
        from datetime import datetime, timezone
        tm = _make_tm()
        tm._health_cd = {}
        tm.db.upsert_token_health_cooldown = AsyncMock()
        tm.db.delete_token_health_cooldown = AsyncMock()
        tm._try_protocol_refresh_st = AsyncMock(return_value=None)
        tm._try_refresh_st_via_extension = AsyncMock(return_value=None)
        tm.disable_token = AsyncMock()
        return tm

    async def test_first_strike_pauses_and_forces_device_reload_no_disable(self):
        tm = self._tm()
        tok = MagicMock(id=20, at="DEADAT", st="st")
        tm.db.get_token = AsyncMock(return_value=tok)
        from unittest.mock import patch
        with patch("src.services.token_manager.config") as cfg:
            cfg.captcha_method = "extension"
            out = await tm._handle_at_stale(20, tok)
        self.assertEqual(out.reason, "at_stale")
        self.assertTrue(tm.is_health_cooldown(20, "at_stale"))
        tm._try_refresh_st_via_extension.assert_awaited_once_with(20, tok, reload=True)
        self.assertEqual(tm.disable_token.await_count, 0)
        self.assertEqual(tm._health_cd[(20, "at_stale")]["strikes"], 1)

    async def test_concurrent_failure_inside_pause_does_not_escalate(self):
        tm = self._tm()
        tok = MagicMock(id=21, at="DEADAT", st="st")
        tm.db.get_token = AsyncMock(return_value=tok)
        from unittest.mock import patch
        with patch("src.services.token_manager.config") as cfg:
            cfg.captcha_method = "extension"
            await tm._handle_at_stale(21, tok)
            await tm._handle_at_stale(21, tok)
        self.assertEqual(tm._health_cd[(21, "at_stale")]["strikes"], 1)
        self.assertEqual(tm._try_refresh_st_via_extension.await_count, 1)

    async def test_threshold_disables_only_if_credential_unchanged(self):
        from datetime import datetime, timezone, timedelta
        tm = self._tm()
        tok = MagicMock(id=22, at="DEADAT", st="st")
        # Two prior strikes, pause already lapsed, inside the 2h window.
        now = datetime.now(timezone.utc)
        tm._health_cd[(22, "at_stale")] = {"until": now - timedelta(seconds=1), "strikes": 2,
                                           "first_failure_at": now - timedelta(minutes=50),
                                           "last_failure_at": now - timedelta(minutes=20),
                                           "fingerprint": tm._credential_fingerprint("DEADAT")}
        # Case A: a NEWER credential landed meanwhile (valid push) → CAS skips the disable.
        newer = MagicMock(id=22, at="FRESHAT", st="st2", is_active=True)
        tm.db.get_token = AsyncMock(return_value=newer)
        from unittest.mock import patch
        with patch("src.services.token_manager.config") as cfg:
            cfg.captcha_method = "extension"
            await tm._handle_at_stale(22, tok)
        self.assertEqual(tm.disable_token.await_count, 0)
        # Case B: same dead credential still serving → disable with auto_at_stale.
        tm2 = self._tm()
        tm2._health_cd[(23, "at_stale")] = {"until": now - timedelta(seconds=1), "strikes": 2,
                                            "first_failure_at": now - timedelta(minutes=50),
                                            "last_failure_at": now - timedelta(minutes=20),
                                            "fingerprint": tm2._credential_fingerprint("DEADAT")}
        tok2 = MagicMock(id=23, at="DEADAT", st="st", is_active=True)
        tm2.db.get_token = AsyncMock(return_value=tok2)
        with patch("src.services.token_manager.config") as cfg:
            cfg.captcha_method = "extension"
            await tm2._handle_at_stale(23, tok2)
        tm2.disable_token.assert_awaited_once_with(23, reason="auto_at_stale")

    async def test_old_window_resets_strikes(self):
        from datetime import datetime, timezone, timedelta
        tm = self._tm()
        now = datetime.now(timezone.utc)
        tm._health_cd[(24, "at_stale")] = {"until": now - timedelta(hours=1), "strikes": 2,
                                           "first_failure_at": now - timedelta(hours=5),
                                           "last_failure_at": now - timedelta(hours=3), "fingerprint": "old"}
        entry = await tm._record_at_stale(24, "AT")
        self.assertEqual(entry["strikes"], 1)
        self.assertFalse(entry["disable_due"])

    async def test_verified_newer_credential_clears_but_same_dead_one_does_not(self):
        from datetime import datetime, timezone, timedelta
        tm = self._tm()
        tm._health_cd[(25, "at_stale")] = {"until": datetime.now(timezone.utc) + timedelta(minutes=5), "strikes": 1,
                                           "first_failure_at": None, "last_failure_at": None,
                                           "fingerprint": tm._credential_fingerprint("DEADAT")}
        await tm._note_verified_at(25, "DEADAT")      # older in-flight success on the dead AT
        self.assertTrue(tm.is_health_cooldown(25, "at_stale"))
        await tm._note_verified_at(25, "FRESHAT")     # newer credential verified
        self.assertFalse(tm.is_health_cooldown(25, "at_stale"))
