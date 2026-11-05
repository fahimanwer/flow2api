"""Cookie sync (worker extension 3.7.0, docs/cookie-sync.md): the worker shares its Google
login; the server stores it, signs the account in to Flow with it when the pushed session is dead,
renews sessions through the account's proxy only, and never leaks the values."""
import json
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import admin as admin_module
from src.core.models import Token
from src.services import protocol_login as pl
from src.services.token_manager import RefreshOutcome, TokenManager

SENTINEL = "SECRET-COOKIE-VALUE-7f3a"
COOKIES = json.dumps([{"name": "SID", "value": SENTINEL}, {"name": "HSID", "value": "h"}, {"name": "NID", "value": "n"}])
CONN = "conn-token"


def _token(**kw):
    base = dict(id=55, st="st-old", at="at-old", email="me@x.com", is_active=True, protocol_mode="session",
                google_cookies="", login_account="", proxy_url="", redeem_proxy_url="http://u:p@disp.example:8004",
                auto_refresh_enabled=True, google_cookies_seq=0)
    base.update(kw)
    return Token(**base)


class _FakeDB:
    def __init__(self, token=None):
        self.token = token
        self.updates = []
        self.plugin_config = types.SimpleNamespace(connection_token=CONN, auto_enable_on_update=False)

    async def get_plugin_config(self):
        return self.plugin_config

    async def get_token(self, token_id):
        return self.token if self.token and self.token.id == int(token_id) else None

    async def get_token_by_email(self, email):
        return self.token if self.token and self.token.email == email else None

    async def update_token(self, token_id, **fields):
        self.updates.append((token_id, fields))
        if self.token and self.token.id == token_id:
            for k, v in fields.items():
                setattr(self.token, k, v)

    async def apply_push_routing(self, token_id, route_key, redeem_url, pool_managed):
        fields = {}
        if redeem_url:
            fields["redeem_proxy_url"] = redeem_url
        if route_key:
            fields["extension_route_key"] = route_key
        if fields:
            await self.update_token(token_id, **fields)
        return True

    async def write_redeem_proxy(self, token_id, url, route_key, pool_managed):
        await self.update_token(token_id, redeem_proxy_url=url)
        return True

    async def update_token_cookie_sync(self, token_id, seq, route_key=None, **fields):
        if not self.token or self.token.id != token_id or int(self.token.google_cookies_seq or 0) >= seq:
            return 0
        fields = dict(fields, google_cookies_seq=seq)
        await self.update_token(token_id, **fields)
        return 1


# Since 2026-10-23 the pushed Google cookies ARE the credential (flow.google.com cookie sessions); the Labs
# login replay was deleted in stage 2 (2026-11). A push re-validates the row with its cookies (set_cookie_hint).
class PluginPushTests(unittest.TestCase):
    """HTTP-level: the real endpoint with a fake db / token manager."""

    def setUp(self):
        self.db = _FakeDB(_token())
        self.tm = MagicMock()
        self.tm.flow_client.set_cookie_hint = MagicMock()  # admin calls it with (google_cookies, proxy, user_agent)
        self.tm.flow_client.st_to_at = AsyncMock(return_value={"access_token": "at-new", "expires": None, "user": {"email": "me@x.com"}})
        self.tm.validate_and_promote = AsyncMock(return_value=RefreshOutcome(True, "ok", verified=True))
        self.tm.update_token = AsyncMock()
        self.tm.enable_token = AsyncMock()
        self.tm.AUTH_DISABLE_REASONS = TokenManager.AUTH_DISABLE_REASONS
        self.tm.cookie_login_proxy = TokenManager.cookie_login_proxy
        self.tm._is_auth_error = lambda e: "401" in str(e)
        self.tm.flow_client._is_timeout_error = lambda e: isinstance(e, TimeoutError)
        self.tm.flow_client._is_proxy_connection_error = lambda e: False
        self._saved = (admin_module.db, admin_module.token_manager)
        admin_module.db = self.db
        admin_module.token_manager = self.tm
        app = FastAPI()
        app.include_router(admin_module.router)
        self.client = TestClient(app)

    def tearDown(self):
        admin_module.db, admin_module.token_manager = self._saved

    def _push(self, body):
        return self.client.post("/api/plugin/update-token", json=body, headers={"Authorization": f"Bearer {CONN}"})

    def _stored(self):
        merged = {}
        for _, f in self.db.updates:
            merged.update(f)
        return merged

    def test_valid_session_plus_cookies_are_stored_as_protocol_mode(self):
        r = self._push({"session_token": "st-new", "google_cookies": COOKIES, "cookie_sync_seq": 100, "ext_version": "3.7.0"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["token_id"], 55)
        self.assertEqual(body["cookie_sync"], {"stored": True, "cleared": False, "stale": False, "cookies": 3, "derived_from_cookies": False})
        st = self._stored()
        self.assertEqual(st["google_cookies"], COOKIES)
        self.assertEqual(st["protocol_mode"], "protocol")
        self.assertEqual(st["login_account"], "me@x.com")
        self.assertEqual(st["google_cookies_seq"], 100)
        self.assertIsNotNone(st["google_cookies_updated_at"])

    def test_empty_cookies_clear_the_server_copy(self):
        self.db.token.google_cookies = COOKIES
        self.db.token.protocol_mode = "protocol"
        r = self._push({"session_token": "st-new", "google_cookies": "", "cookie_sync_seq": 200})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["cookie_sync"]["cleared"])
        st = self._stored()
        self.assertEqual(st["google_cookies"], "")
        self.assertEqual(st["protocol_mode"], "session")
        self.assertEqual(st["login_account"], "")

    def test_absent_key_leaves_stored_cookies_alone(self):
        self.db.token.google_cookies = COOKIES
        self.db.token.protocol_mode = "protocol"
        r = self._push({"session_token": "st-new"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["cookie_sync"], {"stored": False, "cleared": False, "stale": False, "cookies": 0, "derived_from_cookies": False})
        self.assertNotIn("google_cookies", self._stored())

    def test_stale_write_cannot_undo_a_newer_clear(self):
        self.db.token.google_cookies_seq = 500  # an OFF landed with seq 500
        r = self._push({"session_token": "st-new", "google_cookies": COOKIES, "cookie_sync_seq": 400})
        self.assertEqual(r.status_code, 503, r.text)  # refused before any write: older than the stored state
        self.assertIn("stale cookie push", r.json()["detail"])
        self.assertEqual(self.db.updates, [])
        # equal sequence = the same write again: idempotent report, no second mutation
        self.db.token.google_cookies = ""
        r = self._push({"session_token": "st-new", "google_cookies": "", "cookie_sync_seq": 500})
        self.assertTrue(r.json()["cookie_sync"]["cleared"])
        self.assertNotIn("google_cookies", self._stored())
        # no sequence at all = unordered: refused, nothing written
        r = self._push({"session_token": "st-new", "google_cookies": COOKIES})
        self.assertEqual(r.json()["cookie_sync"]["reason"], "no_seq")
        self.assertNotIn("google_cookies", self._stored())

    def test_dead_session_is_revalidated_with_the_pushed_cookies(self):
        self.tm.flow_client.st_to_at = AsyncMock(side_effect=[Exception("HTTP 401 UNAUTHENTICATED"),
                                                              {"access_token": "at-new", "expires": None, "user": {"email": "me@x.com"}}])
        self.db.token.st = "st-dead"  # the row holds the same dead session: nothing to reuse
        r = self._push({"session_token": "st-dead", "google_cookies": COOKIES, "proxy_url": "http://u:p@disp.example:8004", "cookie_sync_seq": 1, "token_id": 55})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["cookie_sync"]["derived_from_cookies"])
        self.tm.flow_client.set_cookie_hint.assert_called_once_with(COOKIES, "http://u:p@disp.example:8004", None)
        self.tm.validate_and_promote.assert_awaited_with(55, "st-dead", source="plugin_push", push_route_key="",
                                                         cookie_seq=1)
        self.assertEqual(self._stored()["google_cookies"], COOKIES)

    def test_no_session_token_but_cookies_derives_one(self):
        self.db.token.st = ""  # nothing stored to reuse
        r = self._push({"google_cookies": COOKIES, "proxy_url": "http://u:p@disp.example:8004", "token_id": 55})
        self.assertEqual(r.status_code, 200, r.text)
        self.tm.flow_client.set_cookie_hint.assert_called_once()
        self.assertTrue(r.json()["cookie_sync"]["derived_from_cookies"])
        self.assertTrue(self.tm.validate_and_promote.await_args.args[1].startswith("cookie:"))  # placeholder row key

    def test_promote_st_expired_falls_back_to_cookies(self):
        self.tm.validate_and_promote = AsyncMock(side_effect=[RefreshOutcome(False, "st_expired"), RefreshOutcome(True, "ok", verified=True)])
        self.db.token.st = "st-new"  # the row holds nothing newer than the pushed session
        r = self._push({"session_token": "st-new", "google_cookies": COOKIES, "cookie_sync_seq": 2})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["cookie_sync"]["derived_from_cookies"])
        self.assertEqual(self.tm.validate_and_promote.await_args_list[1].args, (55, "st-new"))
        self.assertEqual(self.tm.validate_and_promote.await_args_list[1].kwargs["source"], "plugin_push_cookies")

    def test_at_stale_push_still_stores_the_cookie_backup(self):
        # The push proved the email but Google is not renewing its grant, and re-validating with the
        # row key does not help either. Store the cookies anyway: the healer retries.
        self.tm.validate_and_promote = AsyncMock(return_value=RefreshOutcome(False, "at_stale", verified=False))
        self.db.token.st = "st-new"
        r = self._push({"session_token": "st-new", "google_cookies": COOKIES, "ext_version": "3.7.0", "cookie_sync_seq": 4})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["action"], "relogin_required")
        self.assertTrue(r.json()["cookie_sync"]["stored"])
        self.assertEqual(self._stored()["google_cookies"], COOKIES)
        self.assertEqual(self.tm.validate_and_promote.await_count, 2)  # at_stale, then the row key again

    def test_transport_failure_at_first_conversion_is_503_and_no_google_login(self):
        self.tm._is_auth_error = lambda e: False
        self.tm.flow_client._is_timeout_error = lambda e: isinstance(e, TimeoutError)
        self.tm.flow_client._is_proxy_connection_error = lambda e: False
        self.tm.flow_client.st_to_at = AsyncMock(side_effect=TimeoutError("session endpoint timeout"))
        r = self._push({"session_token": "st-new", "google_cookies": COOKIES, "cookie_sync_seq": 5})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(self.db.updates, [])

    def test_stored_server_session_is_verified_before_any_new_google_login(self):
        # The row already holds a server-derived session (from an earlier push/healer) that
        # differs from the browser's stale one: verify it, do NOT replay the login.
        self.db.token.st = "st-server"
        self.tm.validate_and_promote = AsyncMock(side_effect=[RefreshOutcome(False, "at_stale", verified=False), RefreshOutcome(True, "ok", verified=True)])
        r = self._push({"session_token": "st-browser", "google_cookies": COOKIES, "cookie_sync_seq": 6, "token_id": 55})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.tm.validate_and_promote.await_args_list[1].args, (55, "st-server"))
        self.assertTrue(r.json()["credential_verified"])
        self.assertFalse(r.json()["cookie_sync"]["derived_from_cookies"])

    def test_dead_browser_session_reuses_a_live_stored_one_when_bound(self):
        self.db.token.st = "st-server"
        calls = []

        async def st_to_at(st):
            calls.append(st)
            if st == "st-browser":
                raise Exception("HTTP 401 UNAUTHENTICATED")
            return {"access_token": "at-new", "expires": None, "user": {"email": "me@x.com"}}
        self.tm.flow_client.st_to_at = AsyncMock(side_effect=st_to_at)
        r = self._push({"session_token": "st-browser", "google_cookies": COOKIES, "cookie_sync_seq": 7, "token_id": 55})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(calls, ["st-browser", "st-server"])

    def test_account_mismatch_from_promote_is_409(self):
        self.tm.validate_and_promote = AsyncMock(return_value=RefreshOutcome(False, "account_mismatch"))
        r = self._push({"session_token": "st-new"})
        self.assertEqual(r.status_code, 409)

    def test_unusable_or_oversize_cookies_are_refused(self):
        r = self._push({"session_token": "st-new", "google_cookies": json.dumps([{"name": "NID", "value": "x"}])})
        self.assertEqual(r.status_code, 400)
        self.assertIn("unusable", r.json()["detail"])
        r = self._push({"session_token": "st-new", "google_cookies": "SID=" + "x" * (300 * 1024)})
        self.assertEqual(r.status_code, 413)
        r = self._push({"google_cookies": ""})
        self.assertEqual(r.status_code, 400)

    def test_clear_endpoint_needs_no_google_round_trip(self):
        self.db.token.google_cookies = COOKIES
        r = self.client.post("/api/plugin/cookie-sync", json={"action": "clear", "token_id": 55, "cookie_sync_seq": 9},
                             headers={"Authorization": f"Bearer {CONN}"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["cookie_sync"]["cleared"])
        self.assertEqual(self._stored()["google_cookies"], "")
        self.tm.flow_client.st_to_at.assert_not_awaited()
        r = self.client.post("/api/plugin/cookie-sync", json={"action": "clear", "token_id": 99}, headers={"Authorization": f"Bearer {CONN}"})
        self.assertEqual(r.status_code, 404)
        r = self.client.post("/api/plugin/cookie-sync", json={"action": "clear", "token_id": 55}, headers={"Authorization": "Bearer wrong"})
        self.assertEqual(r.status_code, 401)

    def test_cookie_values_never_reach_logs_or_responses(self):
        seen = []
        with patch.object(admin_module.debug_logger, "event", side_effect=lambda m, *a, **k: seen.append(m)), \
             patch.object(admin_module.debug_logger, "log_info", side_effect=lambda m, *a, **k: seen.append(m)):
            r = self._push({"session_token": "st-new", "google_cookies": COOKIES, "cookie_sync_seq": 3})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(SENTINEL, r.text)
        self.assertFalse(any(SENTINEL in m for m in seen), seen)

    def test_the_labs_cookie_login_is_gone(self):
        # stage 2 (2026-11): the pushed cookies are the credential; there is no Labs login replay left
        self.assertFalse(hasattr(TokenManager, "cookie_login"))


class AdminTokenListMasksSecrets(unittest.TestCase):
    def test_get_tokens_hides_cookie_and_password_values(self):
        # The row-dict builder inside GET /api/tokens is not importable on its own; check
        # the shipped source keeps both values blank and exposes only the flag.
        src = open(admin_module.__file__).read()
        self.assertIn('"google_cookies": "",', src)
        self.assertIn('"login_password": "",', src)
        self.assertIn('"google_cookies_set"', src)


class TokenManagerCookieLoginTests(unittest.IsolatedAsyncioTestCase):
    def _tm(self):
        tm = TokenManager.__new__(TokenManager)
        tm.db = MagicMock()
        tm.db.update_token = AsyncMock()
        tm.flow_client = MagicMock()
        return tm

    def test_proxy_resolution_prefers_protocol_proxy_then_redeem_proxy_never_direct(self):
        self.assertEqual(TokenManager.cookie_login_proxy(_token(proxy_url="http://a:b@p1:1")), "http://a:b@p1:1")
        self.assertEqual(TokenManager.cookie_login_proxy(_token()), "http://u:p@disp.example:8004")
        self.assertIsNone(TokenManager.cookie_login_proxy(_token(redeem_proxy_url="")))

    async def test_protocol_refresh_rechecks_the_stored_cookies(self):
        tm = self._tm()
        tm._st_to_at_for_token = AsyncMock(return_value={"access_token": "FLOWCK.x.y"})
        tok = _token(protocol_mode="protocol", google_cookies=COOKIES)
        self.assertEqual(await tm._try_protocol_refresh_st(55, tok), tok.st)
        tm._st_to_at_for_token.assert_awaited_once_with(tok, tok.st)
        # the stored cookies no longer sign in: None, so _try_refresh_st asks the worker browser
        tm._st_to_at_for_token = AsyncMock(side_effect=Exception("401"))
        self.assertIsNone(await tm._try_protocol_refresh_st(55, tok))

    async def test_identity_guard_blocks_another_accounts_session(self):
        tm = self._tm()
        tm.flow_client.st_to_at = AsyncMock(return_value={"access_token": "at2", "expires": None, "user": {"email": "other@x.com"}})
        tm._flow_call_for_token = lambda token, call: call()
        tm._credential_fingerprint = lambda at: "fp"
        out = await tm._do_refresh_at(55, "st2", _token())
        self.assertFalse(out.success)
        self.assertEqual(out.reason, "account_mismatch")
        tm.db.update_token.assert_not_awaited()

    async def test_empty_session_answer_is_st_expired_not_unknown(self):
        # Labs/NextAuth answers 200 {} for a dead session token; that must trigger the ST
        # refresh path (cookie login / extension), not end as "unknown".
        tm = self._tm()
        tm.flow_client.st_to_at = AsyncMock(return_value={})
        tm._flow_call_for_token = lambda token, call: call()
        out = await tm._do_refresh_at(55, "dead", _token())
        self.assertEqual(out.reason, "st_expired")
        tm.db.update_token.assert_not_awaited()

    async def test_healer_only_touches_auth_disabled_rows(self):
        tm = self._tm()
        tm.db.get_token_refresh_config = AsyncMock(return_value=types.SimpleNamespace(enabled=True, refresh_interval_minutes=120))
        rows = [
            _token(id=1, is_active=True, protocol_mode="protocol", google_cookies=COOKIES),
            _token(id=2, is_active=False, ban_reason="auto_st_expired", protocol_mode="protocol", google_cookies=COOKIES),
            _token(id=3, is_active=False, ban_reason="auto_at_stale", protocol_mode="protocol", google_cookies=COOKIES),
            _token(id=4, is_active=False, ban_reason="auto_error", protocol_mode="protocol", google_cookies=COOKIES),
        ]
        tm.db.get_all_tokens = AsyncMock(return_value=rows)
        tm._refresh_protocol_token = AsyncMock()
        await tm.run_protocol_refresh_once()
        self.assertEqual(sorted(c.args[0].id for c in tm._refresh_protocol_token.await_args_list), [2, 3])

    async def test_healer_re_enables_an_expired_session_account_after_a_verified_login(self):
        tm = self._tm()
        tok = _token(id=2, is_active=False, ban_reason="auto_st_expired", protocol_mode="protocol", google_cookies=COOKIES)
        tm.db.get_token = AsyncMock(return_value=tok)
        tm._try_protocol_refresh_st = AsyncMock(return_value="st2")
        tm._locked_refresh = AsyncMock(return_value=RefreshOutcome(True, "ok", verified=True))
        tm.enable_token = AsyncMock()
        from datetime import datetime, timezone
        await tm._refresh_protocol_token(tok, datetime.now(timezone.utc))
        tm.enable_token.assert_awaited_once_with(2)
        # the row was disabled by an admin (ban_reason cleared) while the credential was
        # being verified: promoted (same account, valid session) but NOT re-enabled
        tm.enable_token.reset_mock()
        tm.db.get_token = AsyncMock(side_effect=[tok, tok, _token(id=2, is_active=False, ban_reason=None, protocol_mode="protocol", google_cookies=COOKIES)])
        await tm._refresh_protocol_token(tok, datetime.now(timezone.utc))
        tm.enable_token.assert_not_awaited()
        # the switch was turned OFF during the login: nothing promoted
        tm.enable_token.reset_mock(); tm._locked_refresh.reset_mock()
        tm.db.get_token = AsyncMock(side_effect=[tok, _token(id=2, is_active=False, ban_reason="auto_st_expired", protocol_mode="session", google_cookies="")])
        await tm._refresh_protocol_token(tok, datetime.now(timezone.utc))
        tm._locked_refresh.assert_not_awaited()


class ProtocolLoginSafetyTests(unittest.TestCase):
    def test_usable_cookie_guard_matches_the_login(self):
        self.assertTrue(pl.google_cookies_usable(COOKIES))
        self.assertTrue(pl.google_cookies_usable("SAPISID=x; NID=y"))
        self.assertFalse(pl.google_cookies_usable(json.dumps([{"name": "__Secure-1PSID", "value": "x"}])))
        self.assertFalse(pl.google_cookies_usable(""))


if __name__ == "__main__":
    unittest.main()
