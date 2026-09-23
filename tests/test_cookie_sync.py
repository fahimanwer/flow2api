"""Cookie sync (worker extension 3.7.0, docs/cookie-sync.md): the worker shares its Google
login; the server stores it, derives a Labs session from it when the pushed one is dead,
renews sessions through the account's proxy only, and never leaks the values."""
import asyncio
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


class PluginPushTests(unittest.TestCase):
    """HTTP-level: the real endpoint with a fake db / token manager."""

    def setUp(self):
        self.db = _FakeDB(_token())
        self.tm = MagicMock()
        self.tm.flow_client.st_to_at = AsyncMock(return_value={"access_token": "at-new", "expires": None, "user": {"email": "me@x.com"}})
        self.tm.validate_and_promote = AsyncMock(return_value=RefreshOutcome(True, "ok", verified=True))
        self.tm.update_token = AsyncMock()
        self.tm.enable_token = AsyncMock()
        self.tm.AUTH_DISABLE_REASONS = TokenManager.AUTH_DISABLE_REASONS
        self.tm.cookie_login_proxy = TokenManager.cookie_login_proxy
        self.tm.cookie_login = AsyncMock(return_value={"success": True, "session_token": "st-derived", "reason": "ok"})
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
        self.tm.cookie_login.assert_not_awaited()  # session was fine: no Google login replay

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
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["cookie_sync"]["stale"])
        self.assertNotIn("google_cookies", self._stored())

    def test_dead_session_is_replaced_by_one_derived_from_the_cookies(self):
        self.tm.flow_client.st_to_at = AsyncMock(side_effect=[Exception("HTTP 401 UNAUTHENTICATED"),
                                                              {"access_token": "at-new", "expires": None, "user": {"email": "me@x.com"}}])
        r = self._push({"session_token": "st-dead", "google_cookies": COOKIES, "proxy_url": "http://u:p@disp.example:8004", "cookie_sync_seq": 1})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["cookie_sync"]["derived_from_cookies"])
        kw = self.tm.cookie_login.await_args.kwargs
        self.assertEqual(kw["google_cookies"], COOKIES)
        self.assertEqual(kw["proxy"], "http://u:p@disp.example:8004")
        self.tm.validate_and_promote.assert_awaited_with(55, "st-derived", source="plugin_push")
        self.assertEqual(self._stored()["google_cookies"], COOKIES)

    def test_no_session_token_but_cookies_derives_one(self):
        r = self._push({"google_cookies": COOKIES, "proxy_url": "http://u:p@disp.example:8004", "token_id": 55})
        self.assertEqual(r.status_code, 200, r.text)
        self.tm.cookie_login.assert_awaited_once()
        self.assertTrue(r.json()["cookie_sync"]["derived_from_cookies"])

    def test_dead_session_and_failed_cookie_login_stores_nothing(self):
        self.tm.flow_client.st_to_at = AsyncMock(side_effect=Exception("HTTP 401"))
        self.tm.cookie_login = AsyncMock(return_value={"success": False, "reason": "rejected", "error": "signin/rejected"})
        r = self._push({"session_token": "st-dead", "google_cookies": COOKIES})
        self.assertEqual(r.status_code, 400)
        self.assertIn("rejected", r.json()["detail"])
        self.assertEqual(self.db.updates, [])
        # transport failure → 503 so the worker retries instead of showing "logged out"
        self.tm.cookie_login = AsyncMock(return_value={"success": False, "reason": "network", "error": "proxy down"})
        r = self._push({"session_token": "st-dead", "google_cookies": COOKIES})
        self.assertEqual(r.status_code, 503)

    def test_promote_st_expired_falls_back_to_cookies(self):
        self.tm.validate_and_promote = AsyncMock(side_effect=[RefreshOutcome(False, "st_expired"), RefreshOutcome(True, "ok", verified=True)])
        r = self._push({"session_token": "st-new", "google_cookies": COOKIES})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["cookie_sync"]["derived_from_cookies"])
        self.assertEqual(self.tm.validate_and_promote.await_args_list[1].args, (55, "st-derived"))

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
            r = self._push({"session_token": "st-new", "google_cookies": COOKIES})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(SENTINEL, r.text)
        self.assertFalse(any(SENTINEL in m for m in seen), seen)


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

    async def test_cookie_login_refuses_without_proxy_and_passes_proxy_when_present(self):
        tm = self._tm()
        r = await tm.cookie_login(_token(google_cookies=COOKIES, redeem_proxy_url=""))
        self.assertEqual(r["reason"], "no_proxy")
        with patch.object(pl.protocol_loginer, "login", AsyncMock(return_value={"success": True, "session_token": "st2"})) as login:
            r = await tm.cookie_login(_token(google_cookies=COOKIES))
        self.assertTrue(r["success"])
        self.assertEqual(login.await_args.kwargs["proxy"], "http://u:p@disp.example:8004")
        self.assertEqual(login.await_args.kwargs["email"], "me@x.com")

    async def test_cookie_login_timeout_is_a_structured_reason(self):
        tm = self._tm()
        tm.COOKIE_LOGIN_TIMEOUT_SECONDS = 0.01

        async def slow(*a, **k):
            await asyncio.sleep(1)
        with patch.object(pl.protocol_loginer, "login", slow):
            r = await tm.cookie_login(_token(google_cookies=COOKIES))
        self.assertEqual(r["reason"], "timeout")

    async def test_protocol_refresh_uses_the_redeem_proxy(self):
        tm = self._tm()
        tok = _token(protocol_mode="protocol", google_cookies=COOKIES)
        with patch.object(pl.protocol_loginer, "login", AsyncMock(return_value={"success": True, "session_token": "st2"})) as login:
            st = await tm._try_protocol_refresh_st(55, tok)
        self.assertEqual(st, "st2")
        self.assertEqual(login.await_args.kwargs["proxy"], "http://u:p@disp.example:8004")

    async def test_identity_guard_blocks_another_accounts_session(self):
        tm = self._tm()
        tm.flow_client.st_to_at = AsyncMock(return_value={"access_token": "at2", "expires": None, "user": {"email": "other@x.com"}})
        tm._flow_call_for_token = lambda token, call: call()
        tm._credential_fingerprint = lambda at: "fp"
        out = await tm._do_refresh_at(55, "st2", _token())
        self.assertFalse(out.success)
        self.assertEqual(out.reason, "account_mismatch")
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

    def test_google_host_and_exact_callback_matching(self):
        self.assertTrue(pl._is_google_host("https://accounts.google.com/o/oauth2/auth?x=1"))
        self.assertTrue(pl._is_google_host("https://accounts.google.co.uk.google.com/"))
        self.assertFalse(pl._is_google_host("https://evil.io/accounts.google.com/"))
        self.assertFalse(pl._is_google_host("https://google.com.evil.io/"))
        self.assertTrue(pl._is_labs_callback("https://labs.google/fx/api/auth/callback/google?code=1"))
        self.assertFalse(pl._is_labs_callback("https://evil.io/labs.google/fx/api/auth/callback/google"))
        self.assertFalse(pl._is_labs_callback("http://labs.google/fx/api/auth/callback/google"))

    def test_cookies_are_never_sent_to_a_foreign_redirect(self):
        sent = []

        class Resp:
            def __init__(self, status, headers=None, body="", js=None):
                self.status_code = status; self.headers = headers or {}; self.text = body; self._js = js
            def json(self):
                return self._js

        class Session:
            def __init__(self, **kw):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def get(self, url, headers=None, allow_redirects=False):
                sent.append((url, (headers or {}).get("Cookie", "")))
                if url.endswith("/api/auth/csrf"):
                    return Resp(200, js={"csrfToken": "c"})
                if "accounts.google.com" in url:
                    return Resp(302, {"location": "https://evil.io/steal"})
                return Resp(200)
            async def post(self, url, data=None, headers=None, allow_redirects=False):
                return Resp(200, js={"redirect": "https://accounts.google.com/o/oauth2/auth?client_id=1"})

        with patch.object(pl, "AsyncSession", Session):
            r = asyncio.run(pl.protocol_loginer.login(COOKIES, proxy="http://u:p@h:1"))
        self.assertFalse(r["success"])
        self.assertEqual(r["reason"], "unexpected_redirect")
        self.assertFalse(any("evil.io" in u for u, _ in sent))
        self.assertFalse(any(SENTINEL in c and "google.com" not in u for u, c in sent))


if __name__ == "__main__":
    unittest.main()
