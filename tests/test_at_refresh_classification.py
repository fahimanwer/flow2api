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

    async def test_verify_401_is_st_expired(self):
        tm = _make_tm()
        tm.flow_client.st_to_at = AsyncMock(return_value={"access_token": "AT", "expires": None})
        tm.flow_client.get_credits = AsyncMock(
            side_effect=FlowAPIError(401, "HTTP Error 401", "UNAUTHENTICATED")
        )
        outcome = await tm._do_refresh_at(1, "st")
        self.assertEqual(outcome.reason, "st_expired")

    async def test_verify_non_auth_error_still_success(self):
        # AT was already written; a transient non-auth verify error must not
        # fail the refresh (preserves long-standing behavior).
        tm = _make_tm()
        tm.flow_client.st_to_at = AsyncMock(return_value={"access_token": "AT", "expires": None})
        tm.flow_client.get_credits = AsyncMock(side_effect=Exception("temporary 500 blip"))
        outcome = await tm._do_refresh_at(1, "st")
        self.assertTrue(outcome.success)


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
