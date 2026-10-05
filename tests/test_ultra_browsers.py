"""Ultra browsers managed from the dashboard (tmp/ultra_browsers_plan.md, Slices A + B, rev 3).

Covers: Ultra port inventory (reservation blockers, pool removal, stale-pool allocation), the ownership guard
on session pushes and cookie clears, the onboarding hold and its release, the job queue (one mutating job,
leases, uncertain logins never replayed, secrets only in the poll answer), challenge replies, the drain before
a restart, the alert outbox and the health derivation."""
import asyncio
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import admin as admin_module
from src.core import vault
from src.core.database import Database
from src.core.models import Token
from src.services import ultra_browsers as ub
from src.services.load_balancer import LoadBalancer
from src.services.token_manager import RefreshOutcome, TokenManager
from src.services.ultra_browsers import UltraError, UltraService, derive_health

KEY = base64.b64encode(b"k" * 32).decode()
HOST = "disp.oxylabs.io"
POOL = {"host": HOST, "user": "user-x", "pass": "pw-pool", "ports": [8001, 8002, 8003, 8011]}
CONN = "conn-token-xyz"
PASSWORD = "Sup3r-Secret-Passw0rd!"


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(db_path=f"{self._tmp.name}/flow.db")
        await self.db.init_db()
        await self.db.update_plugin_config(CONN, auto_enable_on_update=False, ext_proxy_pool=json.dumps(POOL))
        self.clock = Clock()
        self.svc = UltraService(self.db, now=self.clock, vault_key=KEY)
        self._env = patch.dict(os.environ, {"ULTRA_BROWSERS_ENABLED": "1", "ULTRA_HOST_ID": "h1",
                                            "ULTRA_AGENT_TOKEN": "agent-tok"}, clear=False)
        self._env.start()
        for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            os.environ.pop(k, None)

    async def asyncTearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    async def _sql(self, sql, args=()):
        async with self.db.connect(write=True) as c:
            cur = await c.execute(sql, args)
            rows = await cur.fetchall()
            await c.commit()
            return rows

    async def _token(self, email, *, port=8003, route_key=None, tier="PAYGATE_TIER_TWO", active=True, **kw):
        tid = await self.db.add_token(Token(st=f"st-{email}", at="at", email=email, user_paygate_tier=tier, is_active=active, **kw))
        await self.db.update_token(tid, redeem_proxy_url=f"http://user-x:pw-pool@{HOST}:{port}" if port else None,
                                   extension_route_key=route_key)
        return tid

    async def _managed(self, email="new@x.com", port=8011, client="pinterest-factory"):
        await self.svc.reserve_port(port, city="NYC", tz="America/New_York")
        return await self.svc.add_account(email=email, password=PASSWORD, port=port, reserved_client=client)

    async def _job(self, name, kind):
        rows = await self._sql("SELECT id, state FROM ultra_jobs WHERE browser = ? AND kind = ? ORDER BY created_at DESC", (name, kind))
        return rows[0] if rows else None

    async def _run_job(self, kind, outcome="done", result=None):
        job = await self.svc.lease("h1")
        self.assertIsNotNone(job, f"expected a {kind} job")
        self.assertEqual(job["kind"], kind)
        await self.svc.handle_result("h1", job["id"], outcome, result or {})
        return job


class PortTests(Base):
    async def test_reserve_removes_from_pool_and_keeps_connection_token(self):
        out = await self.svc.reserve_port(8011)
        self.assertTrue(out["removed_from_pool"])
        pc = await self.db.get_plugin_config()
        self.assertEqual(pc.connection_token, CONN)
        self.assertFalse(pc.auto_enable_on_update)
        pool = json.loads(pc.ext_proxy_pool)
        self.assertEqual(pool["ports"], [8001, 8002, 8003])
        self.assertEqual(pool["pass"], "pw-pool")

    async def test_reserve_refused_with_blockers(self):
        await self._token("a@x", port=8002)
        await self._sql("INSERT INTO device_port_assignments (route_key, port, assigned_at, last_seen) VALUES ('rk', 8001, 'x', 'x')")
        await self._sql("INSERT INTO port_migrations (route_key, token_id, from_port, to_port, state) VALUES ('rk2', 9, 8001, 8003, 'pending')")
        disabled = await self._token("b@x", port=8004, active=False)
        for port in (8001, 8002, 8003, 8004):
            with self.assertRaises(UltraError) as e:
                await self.svc.reserve_port(port)
            self.assertTrue(e.exception.blockers, port)
        self.assertIn(f"token {disabled}", " ".join((await self.svc.check_port(8004))["blockers"]))
        self.assertEqual(json.loads((await self.db.get_plugin_config()).ext_proxy_pool)["ports"], POOL["ports"])

    async def test_pool_config_refuses_an_ultra_port(self):
        await self.svc.reserve_port(8011)
        with self.assertRaises(ValueError):
            await self.db.update_plugin_config(CONN, True, json.dumps(dict(POOL, ports=[8001, 8011])))
        await self.db.update_plugin_config(CONN, True, json.dumps(dict(POOL, ports=[8001])))

    async def test_stale_pool_snapshot_never_assigns_an_ultra_port(self):
        """REGRESSION (R3-2): a device request read the pool before the reservation committed."""
        stale_snapshot = list(POOL["ports"])
        await self.svc.reserve_port(8011)
        for i in range(8):
            await self._sql("INSERT INTO device_port_assignments (route_key, port, assigned_at, last_seen) VALUES (?, ?, ?, ?)",
                            (f"busy{i}", [8001, 8002, 8003][i % 3], "2099-01-01", "2099-01-01"))
        got = await self.db.assign_device_port("newdev", stale_snapshot)
        self.assertNotEqual(got, 8011)
        self.assertIsNone(await self.db.assign_device_port("only-ultra", [8011]))
        self.assertIsNone(await self.db.pick_free_port([8011]))
        tid = await self._token("u@x", route_key="rk9")
        self.assertFalse(await self.db.queue_port_migration("rk9", tid, 8011))

    async def test_unrelated_account_cannot_redeem_through_an_ultra_port(self):
        await self.svc.reserve_port(8011)
        tid = await self._token("someone@x", port=8003, route_key="rkA")
        self.assertFalse(await self.db.write_redeem_proxy(tid, f"http://user-x:pw-pool@{HOST}:8011", "rkA", False))
        self.assertFalse(await self.db.apply_push_routing(tid, "rkA", f"http://user-x:pw-pool@{HOST}:8011", False))
        self.assertTrue((await self.db.get_token(tid)).redeem_proxy_url.endswith(":8003"))


class ObserveOnlyTests(Base):
    async def test_observe_only_registration_has_no_guard_hold_or_lifecycle(self):
        tid = await self._token("tuba@x", port=8016, route_key="auto-01")
        await self.svc.register_observed(name="flow-ultra-01", container="flow-ultra-01", port=8016, token_id=tid)
        row, refusal = await self.svc.push_guard("tuba@x", "some-other-route", f"http://u:p@{HOST}:8003", tid)
        self.assertIsNone(row)
        self.assertIsNone(refusal)
        with self.assertRaises(UltraError):
            await self.svc.request_job("flow-ultra-01", "restart")
        out = await self.svc.request_job("flow-ultra-01", "screenshot")
        self.assertEqual(out["state"], "queued")
        self.assertEqual((await self.db.get_token(tid)).ultra_hold, "")
        # its own redeem on its own Ultra port is still accepted; another account's is not
        self.assertTrue(await self.db.write_redeem_proxy(tid, f"http://user-x:pw-pool@{HOST}:8016", "auto-01", False))
        other = await self._token("o@x", port=8003)
        self.assertFalse(await self.db.write_redeem_proxy(other, f"http://user-x:pw-pool@{HOST}:8016", None, False))

    async def test_observe_refused_when_port_in_pool(self):
        with self.assertRaises(UltraError):
            await self.svc.register_observed(name="flow-ultra-02", container="flow-ultra-02", port=8001, token_id=None)


class PushTests(Base):
    """HTTP-level session pushes against the real database, Google mocked."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.tm = MagicMock()
        self.tm.flow_client.st_to_at = AsyncMock(side_effect=self._st_to_at)
        self.tm.flow_client.get_credits = AsyncMock(return_value={"credits": 1, "userPaygateTier": "PAYGATE_TIER_TWO"})
        self.tm.validate_and_promote = AsyncMock(return_value=RefreshOutcome(True, "ok", verified=True))
        self.tm.update_token = AsyncMock()
        self.tm.enable_token = AsyncMock()
        self.tm.AUTH_DISABLE_REASONS = TokenManager.AUTH_DISABLE_REASONS
        self.tm._is_auth_error = lambda e: "401" in str(e)
        self.tm.add_token = AsyncMock(side_effect=self._add_token)
        self.emails = {}
        self._saved = (admin_module.db, admin_module.token_manager, admin_module.ultra_service)
        admin_module.db, admin_module.token_manager = self.db, self.tm
        admin_module.set_ultra_service(self.svc)
        app = FastAPI()
        app.include_router(admin_module.router)
        self.client = TestClient(app)

    async def asyncTearDown(self):
        admin_module.db, admin_module.token_manager, admin_module.ultra_service = self._saved
        await super().asyncTearDown()

    async def _st_to_at(self, st):
        return {"access_token": "at-" + st, "expires": None, "user": {"email": self.emails.get(st, "x@x")}}

    async def _add_token(self, st, **kw):
        tok = Token(st=st, at="at", email=self.emails[st], user_paygate_tier="PAYGATE_TIER_TWO",
                    current_project_id=kw.get("project_id"), is_active=kw.get("is_active", True),
                    ban_reason=kw.get("ban_reason"), reserved_client=kw.get("reserved_client", ""),
                    ultra_hold=kw.get("ultra_hold", ""))
        tok.id = await self.db.add_token(tok, push_route_key=kw.get("push_route_key"))
        return tok

    def _push(self, st, email, route_key, port=8011, **extra):
        self.emails[st] = email
        body = {"session_token": st, "route_key": route_key, "proxy_url": f"http://user-x:pw-pool@{HOST}:{port}",
                "ext_version": "3.7.5", "project_id": "11111111-2222-3333-4444-555555555555", **extra}
        return self.client.post("/api/plugin/update-token", json=body, headers={"Authorization": f"Bearer {CONN}"})

    async def test_old_mac_push_for_a_managed_account_is_refused_before_any_write(self):
        """REGRESSION: competing old-browser push (laptop profile still signed in)."""
        tid = await self._token("new@x.com", port=8003, route_key="auto-mac")
        out = await self._managed()
        self.assertEqual(out["token_id"], tid)
        self.assertEqual((await self.db.get_token(tid)).ultra_hold, "onboarding")
        r = await asyncio.to_thread(self._push, "st-mac", "new@x.com", "auto-mac", port=8003)
        self.assertEqual(r.status_code, 409, r.text)
        self.tm.validate_and_promote.assert_not_awaited()
        t = await self.db.get_token(tid)
        self.assertEqual((t.extension_route_key, t.redeem_proxy_url[-4:]), ("auto-mac", "8003"))

    async def test_wrong_email_from_a_managed_route_creates_nothing(self):
        """REGRESSION (R3-1): the managed browser signed in to a different Google account."""
        out = await self._managed()
        r = await asyncio.to_thread(self._push, "st-wrong", "someone-else@x.com", out["route_key"])
        self.assertEqual(r.status_code, 409, r.text)
        self.tm.add_token.assert_not_awaited()
        self.assertEqual(await self._sql("SELECT COUNT(*) FROM tokens"), [(0,)])

    async def test_right_route_with_another_proxy_is_refused(self):
        out = await self._managed()
        r = await asyncio.to_thread(self._push, "st-1", "new@x.com", out["route_key"], port=8003)
        self.assertEqual(r.status_code, 409, r.text)
        self.tm.add_token.assert_not_awaited()

    async def test_onboarding_push_creates_a_held_restricted_token_then_release(self):
        out = await self._managed()
        name = out["name"]
        r = await asyncio.to_thread(self._push, "st-1", "New@X.com", out["route_key"])
        self.assertEqual(r.status_code, 200, r.text)
        tid = r.json()["token_id"]
        t = await self.db.get_token(tid)
        self.assertEqual((t.is_active, t.ban_reason, t.ultra_hold, t.reserved_client),
                         (False, "ultra_onboarding", "onboarding", "pinterest-factory"))
        self.assertEqual(t.extension_route_key, out["route_key"])
        b = await self.svc.get_browser(name)
        self.assertEqual(b["token_id"], tid)
        # not released yet: the extension was never acknowledged (bootstrap)
        ok, missing = await self.svc.finalize_onboarding(name)
        self.assertFalse(ok)
        self.assertIn("route key", missing)
        await self._sql("UPDATE ultra_browsers SET route_acked = 1, state = 'onboarding' WHERE name = ?", (name,))
        await self._sql("UPDATE ultra_jobs SET state = 'done'")
        await self.svc.note_verified(tid)
        t = await self.db.get_token(tid)
        self.assertEqual((t.is_active, t.ban_reason, t.ultra_hold), (True, None, ""))
        self.assertEqual((await self.svc.get_browser(name))["state"], "ok")

    async def test_existing_account_moves_to_the_managed_route_even_over_an_explicit_key(self):
        tid = await self._token("new@x.com", port=8003, route_key="mac-explicit-key")
        out = await self._managed()
        r = await asyncio.to_thread(self._push, "st-1", "new@x.com", out["route_key"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["token_id"], tid)
        t = await self.db.get_token(tid)
        self.assertEqual((t.extension_route_key, t.redeem_proxy_url[-4:], t.ultra_hold), (out["route_key"], "8011", "onboarding"))
        self.tm.validate_and_promote.assert_awaited()
        self.assertEqual(self.tm.validate_and_promote.await_args.kwargs["push_route_key"], out["route_key"])

    async def test_promotion_decides_ownership_in_its_own_transaction(self):
        """REGRESSION (code review #1): a laptop push that passed every earlier check, then the account became
        managed before its credential write — the write itself refuses it."""
        tid = await self._token("Late@X.com ", port=8003, route_key="auto-mac")
        out = await self._managed(email="late@x.com")
        await self._sql("UPDATE ultra_browsers SET token_id = NULL")   # as if management landed after the pre-check
        await self._sql("UPDATE tokens SET ultra_hold = '', reserved_client = ''")
        self.assertFalse(await self.db.promote_credential_guarded(tid, "auto-mac", st="st-laptop"))
        self.assertEqual((await self.db.get_token(tid)).st, "st-Late@X.com ")
        # the browser's own route binds the row and holds + restricts it in the same write
        self.assertTrue(await self.db.promote_credential_guarded(tid, out["route_key"], st="st-browser"))
        t = await self.db.get_token(tid)
        self.assertEqual((t.st, t.ultra_hold, t.reserved_client), ("st-browser", "onboarding", "pinterest-factory"))
        self.assertEqual((await self.svc.get_browser(out["name"]))["token_id"], tid)
        # an unmanaged account is written as before
        other = await self._token("plain@x", port=8002)
        self.assertTrue(await self.db.promote_credential_guarded(other, "", st="st-new"))

    async def test_new_account_insert_decides_ownership_in_its_own_transaction(self):
        """REGRESSION (code review #1): management registered after push_guard, before the INSERT."""
        out = await self._managed()
        with self.assertRaises(Exception) as e:
            await self.db.add_token(Token(st="st-laptop", at="at", email="NEW@x.com", is_active=True), push_route_key="auto-mac")
        self.assertEqual(type(e.exception).__name__, "UltraOwnershipError")
        self.assertEqual(await self._sql("SELECT COUNT(*) FROM tokens"), [(0,)])
        with self.assertRaises(Exception):
            await self.db.add_token(Token(st="st-x", at="at", email="other@x.com"), push_route_key=out["route_key"])
        tid = await self.db.add_token(Token(st="st-ok", at="at", email="new@x.com", is_active=True), push_route_key=out["route_key"])
        t = await self.db.get_token(tid)
        self.assertEqual((t.is_active, t.ban_reason, t.ultra_hold, t.reserved_client),
                         (False, "ultra_onboarding", "onboarding", "pinterest-factory"))
        self.assertEqual((await self.svc.get_browser(out["name"]))["token_id"], tid)
        # and the HTTP path turns the race into a 409 with nothing written
        with patch.object(self.svc, "push_guard", AsyncMock(return_value=(None, None))):
            r = await asyncio.to_thread(self._push, "st-race", "second@x.com", out["route_key"])
        self.assertEqual(r.status_code, 409, r.text)

    async def test_cookie_clear_requires_the_managed_route(self):
        """REGRESSION (R3-1): cookie clear carries route_key; managed accounts require it."""
        out = await self._managed()
        tid = await self._token("new@x.com", port=8011, route_key=out["route_key"])
        await self.svc.bind_token(out["name"], tid)
        await self.db.update_token(tid, google_cookies="[1]", google_cookies_seq=1)
        hdr = {"Authorization": f"Bearer {CONN}"}
        r = await asyncio.to_thread(self.client.post, "/api/plugin/cookie-sync",
                                    json={"action": "clear", "token_id": tid, "cookie_sync_seq": 5}, headers=hdr)
        self.assertEqual(r.status_code, 409, r.text)
        r = await asyncio.to_thread(self.client.post, "/api/plugin/cookie-sync",
                                    json={"action": "clear", "token_id": tid, "cookie_sync_seq": 5, "route_key": "auto-mac"}, headers=hdr)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual((await self.db.get_token(tid)).google_cookies, "[1]")
        r = await asyncio.to_thread(self.client.post, "/api/plugin/cookie-sync",
                                    json={"action": "clear", "token_id": tid, "cookie_sync_seq": 5, "route_key": out["route_key"]}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((await self.db.get_token(tid)).google_cookies, "")
        # an unmanaged account clears exactly as before, with no route key
        other = await self._token("plain@x", port=8003)
        await self.db.update_token(other, google_cookies="[1]", google_cookies_seq=1)
        r = await asyncio.to_thread(self.client.post, "/api/plugin/cookie-sync",
                                    json={"action": "clear", "token_id": other, "cookie_sync_seq": 5}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((await self.db.get_token(other)).google_cookies, "")

    async def test_commit_time_checks_refuse_a_foreign_route(self):
        out = await self._managed()
        tid = await self._token("new@x.com", port=8011, route_key=out["route_key"])
        await self.svc.bind_token(out["name"], tid)
        self.assertFalse(await self.db.ultra_route_allows(tid, "auto-mac"))
        self.assertFalse(await self.db.ultra_route_allows(tid, ""))
        self.assertTrue(await self.db.ultra_route_allows(tid, out["route_key"]))
        self.assertFalse(await self.db.apply_push_routing(tid, "auto-mac", f"http://user-x:pw-pool@{HOST}:8003", True))
        self.assertEqual(await self.db.update_token_cookie_sync(tid, 99, route_key="auto-mac", google_cookies=""), 0)
        # a migration ack can never move a managed account onto the shared pool
        await self._sql("INSERT INTO port_migrations (route_key, token_id, from_port, to_port, state, mig_id) VALUES (?, ?, 8011, 8002, 'offered', 'm1')",
                        (out["route_key"], tid))
        ok, reason, _ = await self.db.complete_port_migration(out["route_key"], "m1", 8002)
        self.assertFalse(ok)
        self.assertTrue((await self.db.get_token(tid)).redeem_proxy_url.endswith(":8011"))


class HoldTests(Base):
    def _lb(self, tokens):
        from tests.test_tier_order import FakeTokenManager
        tm = FakeTokenManager(tokens)
        tm.db = self.db
        return LoadBalancer(tm, concurrency_manager=None)

    async def _pick(self, lb):
        from src.core import client_policy as cp
        from src.core.config import config
        saved = (config.captcha_method, cp.client_policy_store._policies)
        config.set_captcha_method("yescaptcha")
        cp.client_policy_store.replace([{"client": "default", "image_tier": "any", "video_tier": "any"}])
        try:
            return await lb.select_token(for_image_generation=True, reserve=False, enforce_concurrency_filter=False, client="pinterest-factory")
        finally:
            config.set_captcha_method(saved[0])
            cp.client_policy_store._policies = saved[1]

    async def test_enable_during_onboarding_does_not_serve(self):
        """REGRESSION (R3-3): the ordinary Enable button during onboarding."""
        out = await self._managed()
        tid = await self._token("new@x.com", port=8011, route_key=out["route_key"], active=False,
                                ban_reason="ultra_onboarding", ultra_hold="onboarding")
        tm = TokenManager.__new__(TokenManager)
        tm.db = self.db
        tm._health_cd = {}
        await TokenManager.enable_token(tm, tid)
        t = await self.db.get_token(tid)
        self.assertTrue(t.is_active)
        self.assertEqual(t.ultra_hold, "onboarding")
        self.assertIsNone(await self._pick(self._lb([t])))

    async def test_hold_set_during_validation_is_caught_and_db_errors_fail_closed(self):
        tid = await self._token("u@x", port=8003)
        t = await self.db.get_token(tid)
        lb = self._lb([t])
        self.assertEqual((await self._pick(lb)).id, tid)
        await self.db.update_token(tid, ultra_hold="restart")  # set after the token list was read
        self.assertIsNone(await self._pick(lb))
        await self.db.update_token(tid, ultra_hold="")
        with patch.object(self.db, "get_ultra_hold", AsyncMock(side_effect=RuntimeError("db locked"))):
            self.assertIsNone(await self._pick(lb))


class JobTests(Base):
    async def _onboard_to_login(self):
        out = await self._managed()
        await self._run_job("create", result={"ready": True})
        job = await self.svc.lease("h1")
        self.assertEqual(job["kind"], "bootstrap")
        self.assertEqual(job["secrets"]["connection_token"], CONN)
        self.assertEqual(job["secrets"]["route_key"], out["route_key"])
        await self.svc.handle_result("h1", job["id"], "done", {"route_key": out["route_key"]})
        return out

    async def test_add_account_refused_when_slice_b_is_off_or_vault_missing(self):
        await self.svc.reserve_port(8011)
        with patch.dict(os.environ, {"ULTRA_BROWSERS_ENABLED": "0"}):
            with self.assertRaises(UltraError) as e:
                await self.svc.add_account(email="a@x.com", password="p", port=8011)
            self.assertEqual(e.exception.status, 403)
        svc = UltraService(self.db, now=self.clock, vault_key="")
        with self.assertRaises(UltraError) as e:
            await svc.add_account(email="a@x.com", password="p", port=8011)
        self.assertEqual(e.exception.status, 503)

    async def test_fresh_profile_bootstrap_then_login_and_secrets_never_stored(self):
        """REGRESSION (R3-4): login starts only after the extension's route key is read back and acked."""
        out = await self._managed()
        name = out["name"]
        job = await self.svc.lease("h1")
        self.assertEqual(job["kind"], "create")
        self.assertIn(f"{HOST}:8011", job["secrets"]["site"]["proxyUrl"])
        self.assertIsNone(await self.svc.lease("h1"))  # one mutating job at a time
        await self.svc.handle_result("h1", job["id"], "done", {"ready": True})
        boot = await self.svc.lease("h1")
        self.assertEqual(boot["kind"], "bootstrap")
        with self.assertRaises(UltraError):
            await self.svc.request_job(name, "login")  # not acked yet
        await self.svc.handle_result("h1", boot["id"], "done", {"route_key": "auto-something-else"})
        self.assertEqual((await self.svc.get_browser(name))["state"], "error")
        self.assertIsNone(await self.svc.lease("h1"))
        # a person retries the bootstrap path: here, simulate a correct read-back
        await self._sql("UPDATE ultra_browsers SET state = 'bootstrapping' WHERE name = ?", (name,))
        await self._sql("INSERT INTO ultra_jobs (id, browser, kind, mutating, args, state, created_at, updated_at) VALUES ('b2', ?, 'bootstrap', 1, '{}', 'queued', 'z', 'z')", (name,))
        boot = await self.svc.lease("h1")
        await self.svc.handle_result("h1", boot["id"], "done", {"route_key": out["route_key"]})
        self.assertEqual((await self.svc.get_browser(name))["route_acked"], 1)
        login = await self.svc.lease("h1")
        self.assertEqual(login["kind"], "login")
        self.assertEqual(login["secrets"]["password"], PASSWORD)
        self.assertIsNotNone(login["attempt_id"])
        # nothing in the database holds the password or the connection token in clear
        async with self.db.connect() as c:
            for table in ("ultra_jobs", "ultra_browsers", "ultra_login_attempts", "ultra_messages", "ultra_alerts"):
                rows = await (await c.execute(f"SELECT * FROM {table}")).fetchall()
                blob = json.dumps(rows, default=str)
                self.assertNotIn(PASSWORD, blob, table)
                self.assertNotIn(CONN, blob, table)

    async def test_login_lease_expiry_is_uncertain_and_never_replayed(self):
        out = await self._onboard_to_login()
        login = await self.svc.lease("h1")
        self.assertEqual(login["kind"], "login")
        await self.svc.handle_result("h1", login["id"], "progress", {"step": "password_sent"})
        self.clock.advance(ub.LEASE_S["login"] + 5)
        self.assertEqual(await self.svc.expire_leases(), 1)
        self.assertEqual((await self._job(out["name"], "login"))[1], "uncertain")
        b = await self.svc.get_browser(out["name"])
        self.assertEqual(b["state"], "needs_you")
        self.assertIsNone(await self.svc.lease("h1"))
        attempt = await self._sql("SELECT state, password_sent FROM ultra_login_attempts")
        self.assertEqual(attempt, [("uncertain", 1)])
        # a late answer from the lost agent changes nothing
        r = await self.svc.handle_result("h1", login["id"], "done", {"step": "signed_in"})
        self.assertTrue(r.get("stop"))
        self.assertEqual((await self.svc.get_browser(out["name"]))["state"], "needs_you")
        alerts = await self._sql("SELECT kind FROM ultra_alerts")
        self.assertEqual(alerts, [("needs_you",)])

    async def test_lease_renewal_keeps_a_waiting_login_alive(self):
        await self._onboard_to_login()
        login = await self.svc.lease("h1")
        for _ in range(10):
            self.clock.advance(60)
            await self.svc.renew([login["id"]])
            self.assertEqual(await self.svc.expire_leases(), 0)

    async def test_challenge_replies_are_single_use_and_tied_to_the_current_challenge(self):
        out = await self._onboard_to_login()
        name = out["name"]
        login = await self.svc.lease("h1")
        r = await self.svc.handle_result("h1", login["id"], "progress", {"step": "challenge", "challenge": {"kind": "code", "hint": "SMS to ••42"}})
        ch1 = r["challenge_id"]
        b = await self.svc.get_browser(name)
        self.assertEqual(b["state"], "needs_you")
        with self.assertRaises(UltraError):
            await self.svc.reply_challenge(name, login["attempt_id"], "stale-id", "code", "123456")
        await self.svc.reply_challenge(name, login["attempt_id"], ch1, "code", "123456")
        with self.assertRaises(UltraError):
            await self.svc.reply_challenge(name, login["attempt_id"], ch1, "code", "654321")
        stored = await self._sql("SELECT payload FROM ultra_messages")
        self.assertNotIn("123456", json.dumps(stored))
        msgs = await self.svc.take_messages("h1")
        self.assertEqual([(m["kind"], m.get("code")) for m in msgs], [("code", "123456")])
        self.assertEqual(await self.svc.take_messages("h1"), [])
        self.assertEqual(await self._sql("SELECT state, payload FROM ultra_messages"), [("delivered", None)])
        # a new challenge gets a new id; the old one is dead
        r = await self.svc.handle_result("h1", login["id"], "progress", {"step": "challenge", "challenge": {"kind": "number", "number": 42, "device": "Pixel"}})
        self.assertNotEqual(r["challenge_id"], ch1)
        with self.assertRaises(UltraError):
            await self.svc.reply_challenge(name, login["attempt_id"], ch1, "tapped")
        await self.svc.reply_challenge(name, login["attempt_id"], r["challenge_id"], "tapped")
        # expiry
        self.clock.advance(ub.NUMBER_TTL_S + 1)
        with self.assertRaises(UltraError):
            await self.svc.reply_challenge(name, login["attempt_id"], r["challenge_id"], "resend")
        self.assertEqual(await self.svc.take_messages("h1"), [])

    async def test_signed_in_moves_to_onboarding(self):
        out = await self._onboard_to_login()
        login = await self.svc.lease("h1")
        await self.svc.handle_result("h1", login["id"], "done", {"step": "signed_in", "ultra": True, "project_opened": True})
        self.assertEqual((await self.svc.get_browser(out["name"]))["state"], "onboarding")


class DrainTests(Base):
    async def _live(self):
        out = await self._managed()
        tid = await self._token("new@x.com", port=8011, route_key=out["route_key"])
        await self.svc.bind_token(out["name"], tid)
        await self._sql("UPDATE ultra_browsers SET onboarding_hold = 0, route_acked = 1, state = 'ok' WHERE name = ?", (out["name"],))
        await self._sql("UPDATE ultra_jobs SET state = 'done'")
        return out["name"], tid

    async def _log(self, tid, status="started"):
        await self._sql("INSERT INTO request_logs (token_id, operation, status_code, duration, status_text, created_at) "
                        "VALUES (?, 'generate', 102, 0, ?, '2020-01-01 00:00:00')", (tid, status))

    async def test_restart_in_the_selection_to_log_gap_waits_for_the_request(self):
        """REGRESSION (R3-5): a request picked just before the hold logs itself only after the hold."""
        name, tid = await self._live()
        out = await self.svc.request_job(name, "restart")
        self.assertEqual(out["state"], "draining")
        self.assertEqual((await self.db.get_token(tid)).ultra_hold, "restart")
        await self.svc.process_drains()       # settle time not over: still draining, even with nothing logged
        self.assertEqual((await self._job(name, "restart"))[1], "draining")
        await self._log(tid)                  # the in-gap request appears (old created_at: no 15-min cutoff)
        self.clock.advance(ub.DRAIN_SETTLE_S + 1)
        await self.svc.process_drains()
        self.assertEqual((await self._job(name, "restart"))[1], "draining")
        await self._sql("UPDATE request_logs SET status_text = 'completed'")
        await self.svc.process_drains()
        self.assertEqual((await self._job(name, "restart"))[1], "queued")
        job = await self.svc.lease("h1")
        await self.svc.handle_result("h1", job["id"], "done", {"ready": True})
        self.assertEqual((await self.db.get_token(tid)).ultra_hold, "")

    async def test_drain_defers_at_the_limit_and_never_forces(self):
        name, tid = await self._live()
        await self.svc.request_job(name, "restart")
        await self._log(tid)
        self.clock.advance(ub.DRAIN_MAX_S + 1)
        await self.svc.process_drains()
        self.assertEqual((await self._job(name, "restart"))[1], "deferred")
        self.assertEqual((await self.db.get_token(tid)).ultra_hold, "")
        self.assertIsNone(await self.svc.lease("h1"))

    async def test_failed_restart_keeps_the_hold_and_stop_stays_excluded(self):
        name, tid = await self._live()
        await self.svc.request_job(name, "restart")
        self.clock.advance(ub.DRAIN_SETTLE_S + 1)
        await self.svc.process_drains()
        job = await self.svc.lease("h1")
        await self.svc.handle_result("h1", job["id"], "failed", {"error": "container did not come up"})
        self.assertEqual((await self.db.get_token(tid)).ultra_hold, "restart")
        self.assertEqual((await self.svc.get_browser(name))["state"], "error")
        await self.db.update_token(tid, ultra_hold="")
        await self._sql("UPDATE ultra_browsers SET state = 'ok' WHERE name = ?", (name,))
        await self.svc.request_job(name, "stop")
        self.clock.advance(ub.DRAIN_SETTLE_S + 1)
        await self.svc.process_drains()
        job = await self.svc.lease("h1")
        await self.svc.handle_result("h1", job["id"], "done", {})
        self.assertEqual((await self.db.get_token(tid)).ultra_hold, "stopped")
        b = await self.svc.get_browser(name)
        self.assertEqual((b["state"], b["desired_state"]), ("stopped", "stopped"))
        await self.svc.request_job(name, "start")
        job = await self.svc.lease("h1")
        await self.svc.handle_result("h1", job["id"], "done", {"ready": False})
        self.assertEqual((await self.db.get_token(tid)).ultra_hold, "stopped")
        await self._sql("UPDATE ultra_browsers SET state = 'stopped' WHERE name = ?", (name,))
        await self.svc.request_job(name, "start")
        job = await self.svc.lease("h1")
        await self.svc.handle_result("h1", job["id"], "done", {"ready": True})
        self.assertEqual((await self.db.get_token(tid)).ultra_hold, "")

    async def test_one_mutating_job_per_browser(self):
        name, _ = await self._live()
        await self.svc.request_job(name, "restart")
        with self.assertRaises(UltraError):
            await self.svc.request_job(name, "stop")
        await self.svc.request_job(name, "screenshot")  # read-only may run beside it


class LifecycleReleaseTests(Base):
    async def test_late_push_never_releases_a_stopped_onboarding_browser(self):
        """REGRESSION (code review #2): stop during onboarding, then a verified push arrives."""
        out = await self._managed()
        name = out["name"]
        tid = await self._token("new@x.com", port=8011, route_key=out["route_key"], active=False,
                                ban_reason="ultra_onboarding", ultra_hold="onboarding", reserved_client="pinterest-factory",
                                current_project_id="p1")
        await self.svc.bind_token(name, tid)
        await self._sql("UPDATE ultra_browsers SET route_acked = 1, state = 'onboarding' WHERE name = ?", (name,))
        await self._sql("UPDATE ultra_jobs SET state = 'done'")
        await self.svc.request_job(name, "stop")
        ok, missing = await self.svc.finalize_onboarding(name)       # stop requested, not done yet
        self.assertFalse(ok)
        job = await self.svc.lease("h1")
        await self.svc.handle_result("h1", job["id"], "done", {})
        await self.svc.note_verified(tid)                             # the late push
        t = await self.db.get_token(tid)
        self.assertEqual((t.is_active, t.ultra_hold), (False, "onboarding"))
        b = await self.svc.get_browser(name)
        self.assertEqual((b["state"], b["desired_state"]), ("stopped", "stopped"))
        await self.svc.request_job(name, "start")                     # desired running, start not verified yet
        await self.svc.note_verified(tid)
        self.assertEqual((await self.db.get_token(tid)).ultra_hold, "onboarding")
        job = await self.svc.lease("h1")
        await self.svc.handle_result("h1", job["id"], "done", {"ready": True})
        self.assertEqual((await self.svc.get_browser(name))["state"], "onboarding")
        await self.svc.note_verified(tid)
        t = await self.db.get_token(tid)
        self.assertEqual((t.is_active, t.ultra_hold), (True, ""))

    async def test_login_ends_at_the_server_backstop_even_with_renewals(self):
        out = await self._managed()
        await self._sql("UPDATE ultra_browsers SET route_acked = 1 WHERE name = ?", (out["name"],))
        await self._sql("UPDATE ultra_jobs SET state = 'done'")
        await self.svc.request_job(out["name"], "login")
        login = await self.svc.lease("h1")
        for _ in range(ub.LOGIN_MAX_S // 60 + 1):
            self.clock.advance(60)
            await self.svc.renew([login["id"]])
            await self.svc.expire_leases()
        self.assertEqual((await self._job(out["name"], "login"))[1], "uncertain")

    async def test_updates_run_one_browser_at_a_time(self):
        names = []
        for i, port in enumerate((8011, 8002)):
            await self._sql("UPDATE plugin_config SET ext_proxy_pool = ?", (json.dumps(dict(POOL, ports=[8001, 8003])),))
            out = await self._managed(email=f"u{i}@x.com", port=port)
            names.append(out["name"])
        obs = json.dumps({"ext_version": "3.7.4"})
        await self._sql("UPDATE ultra_browsers SET state = 'ok', onboarding_hold = 0, obs = ?", (obs,))
        await self._sql("UPDATE ultra_jobs SET state = 'done', updated_at = '2000-01-01'")
        self.svc._published_version = lambda: "3.7.5"
        await self.svc.schedule_updates()
        await self.svc.schedule_updates()
        rows = await self._sql("SELECT browser FROM ultra_jobs WHERE kind = 'update' AND state IN ('draining', 'queued')")
        self.assertEqual(len(rows), 1)
        await self._sql("UPDATE ultra_jobs SET state = 'done' WHERE kind = 'update'")
        await self.svc.schedule_updates()
        rows = await self._sql("SELECT browser FROM ultra_jobs WHERE kind = 'update' AND state IN ('draining', 'queued')")
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0][0], (await self._sql("SELECT browser FROM ultra_jobs WHERE kind = 'update' AND state = 'done'"))[0][0])


class DownloadAuthTests(Base):
    async def test_header_auth_download_and_old_query_form(self):
        """REGRESSION (code review #4): the agent authenticates the zip download with a header."""
        from src.api import ext_update
        tmp = Path(self._tmp.name)
        z = tmp / "worker-latest.zip"
        z.write_bytes(b"PK\x05\x06" + b"\0" * 18)
        ext_update.set_dependencies(self.db, admin_module.verify_admin_token)
        app = FastAPI()
        app.include_router(ext_update.router)
        client = TestClient(app)
        with patch.object(ext_update, "EXT_ZIP", z):
            r = await asyncio.to_thread(client.get, "/download/worker-latest.zip", headers={"Authorization": f"Bearer {CONN}"})
            self.assertEqual(r.status_code, 200)
            r = await asyncio.to_thread(client.get, f"/download/worker-latest.zip?token={CONN}")
            self.assertEqual(r.status_code, 200)
            for bad in ({"Authorization": "Bearer nope"}, {}):
                r = await asyncio.to_thread(client.get, "/download/worker-latest.zip", headers=bad)
                self.assertEqual(r.status_code, 401)


class AlertTests(Base):
    async def test_alert_rows_follow_state_changes_once(self):
        await self.svc.register_observed(name="flow-ultra-02", container="flow-ultra-02", port=8019, token_id=None)
        self.assertTrue(await self.svc.set_state("flow-ultra-02", "error", "boom"))
        self.assertFalse(await self.svc.set_state("flow-ultra-02", "error", "boom again"))
        await self.svc.set_state("flow-ultra-02", "degraded", "x")
        await self.svc.set_state("flow-ultra-02", "ok")
        await self.svc.set_state("flow-ultra-02", "ok")
        self.assertEqual([r[0] for r in await self._sql("SELECT kind FROM ultra_alerts ORDER BY id")], ["error"])
        await self.svc.set_state("flow-ultra-02", "logged_out")
        await self.svc.set_state("flow-ultra-02", "ok")
        self.assertEqual([r[0] for r in await self._sql("SELECT kind FROM ultra_alerts ORDER BY id")], ["error", "logged_out", "ok"])

    async def test_outbox_waits_without_telegram_and_retries_with_backoff(self):
        await self.svc.register_observed(name="flow-ultra-02", container="flow-ultra-02", port=8019, token_id=None)
        await self.svc.set_state("flow-ultra-02", "error", "boom")
        self.assertEqual(await self.svc.send_pending_alerts(), 0)  # no env: stays in the outbox
        self.assertEqual(await self._sql("SELECT tries, sent_at FROM ultra_alerts"), [(0, None)])
        failing = AsyncMock(side_effect=RuntimeError("telegram down"))
        self.assertEqual(await self.svc.send_pending_alerts(failing), 0)
        self.assertEqual(await self.svc.send_pending_alerts(failing), 0)  # backoff: not due yet
        self.assertEqual(failing.await_count, 1)
        self.clock.advance(60)
        ok = AsyncMock()
        self.assertEqual(await self.svc.send_pending_alerts(ok), 1)
        self.assertEqual(await self.svc.send_pending_alerts(ok), 0)
        self.assertEqual(ok.await_count, 1)

    async def test_host_silent_alert_and_recovery(self):
        await self.svc.agent_poll("h1", "1.0", {}, [])
        self.clock.advance(ub.HOST_SILENT_S + 1)
        await self.svc.check_hosts()
        await self.svc.check_hosts()
        await self.svc.agent_poll("h1", "1.0", {}, [])
        self.assertEqual([r[0] for r in await self._sql("SELECT kind FROM ultra_alerts ORDER BY id")], ["host_silent", "host_ok"])


class HealthTests(unittest.TestCase):
    NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)

    def _obs(self, **kw):
        base = {"at": self.NOW.isoformat(), "container_up": True, "egress_ip": "1.2.3.4", "egress_at": self.NOW.isoformat(),
                "google_cookies": True, "cookies_at": self.NOW.isoformat()}
        base.update(kw)
        return base

    def test_network_trouble_is_degraded_not_logged_out(self):
        b = {"expected_egress_ip": "1.2.3.4"}
        tok = {"is_active": True, "ban_reason": None}
        self.assertEqual(derive_health(b, tok, self._obs(), True, self.NOW)[0], "ok")
        self.assertEqual(derive_health(b, tok, self._obs(egress_ip=None, google_cookies=False), True, self.NOW)[0], "degraded")
        self.assertEqual(derive_health(b, tok, self._obs(egress_ip="9.9.9.9"), True, self.NOW)[0], "degraded")
        self.assertEqual(derive_health(b, tok, self._obs(), False, self.NOW)[0], "degraded")
        self.assertEqual(derive_health(b, tok, self._obs(container_up=False), True, self.NOW)[0], "degraded")

    def test_logged_out_needs_two_reads_ten_minutes_apart(self):
        b = {"expected_egress_ip": ""}
        tok = {"is_active": True, "ban_reason": None}
        st, _, marks = derive_health(b, tok, self._obs(google_cookies=False), True, self.NOW)
        self.assertEqual(st, "degraded")
        b.update(marks)
        later = self.NOW + timedelta(minutes=5)
        self.assertEqual(derive_health(b, tok, self._obs(google_cookies=False, cookies_at=later.isoformat(), at=later.isoformat()), True, later)[0], "degraded")
        later = self.NOW + timedelta(minutes=11)
        self.assertEqual(derive_health(b, tok, self._obs(google_cookies=False, cookies_at=later.isoformat(), at=later.isoformat()), True, later)[0], "logged_out")

    def test_cookies_alone_never_make_ok_and_dead_labs_is_separate(self):
        b = {"expected_egress_ip": ""}
        self.assertEqual(derive_health(b, None, self._obs(), True, self.NOW)[0], "degraded")
        tok = {"is_active": False, "ban_reason": "auto_at_stale"}
        st, _, marks = derive_health(b, tok, self._obs(), True, self.NOW)
        b.update(marks)
        later = self.NOW + timedelta(minutes=31)
        self.assertEqual(derive_health(b, tok, self._obs(cookies_at=later.isoformat(), at=later.isoformat()), True, later)[0], "labs_dead")

    def test_stale_or_unknown_observations_are_degraded(self):
        """Code review (non-blocking): expired/unknown observations never give ok."""
        b = {"expected_egress_ip": ""}
        tok = {"is_active": True, "ban_reason": None}
        old = (self.NOW - timedelta(minutes=5)).isoformat()
        self.assertEqual(derive_health(b, tok, self._obs(at=old), True, self.NOW)[0], "degraded")
        self.assertEqual(derive_health(b, tok, self._obs(at=None), True, self.NOW)[0], "degraded")
        very_old = (self.NOW - timedelta(hours=2)).isoformat()
        self.assertEqual(derive_health(b, tok, self._obs(egress_at=very_old), True, self.NOW)[0], "degraded")
        self.assertEqual(derive_health(b, tok, self._obs(cookies_at=very_old), True, self.NOW)[0], "degraded")
        self.assertEqual(derive_health(b, tok, self._obs(), None, self.NOW)[0], "degraded")
        # an old "cookies absent" read is not evidence of a logout
        st, _, marks = derive_health(b, tok, self._obs(google_cookies=False, cookies_at=very_old), True, self.NOW)
        self.assertEqual((st, marks["cookies_absent_since"]), ("degraded", None))


class VaultTests(unittest.TestCase):
    def test_roundtrip_context_and_missing_key(self):
        s = vault.seal("hello", "ctx-a", raw_key=KEY)
        self.assertTrue(s.startswith("v1:"))
        self.assertNotIn("hello", s)
        self.assertEqual(vault.open_sealed(s, "ctx-a", raw_key=KEY), "hello")
        with self.assertRaises(ValueError):
            vault.open_sealed(s, "ctx-b", raw_key=KEY)
        with self.assertRaises(vault.VaultUnavailable):
            vault.seal("x", "c", raw_key="")
        with self.assertRaises(vault.VaultUnavailable):
            vault.seal("x", "c", raw_key=base64.b64encode(b"short").decode())
        self.assertFalse(vault.available(""))


class StatusTests(Base):
    async def test_status_never_carries_the_sealed_password_and_health_refresh_runs(self):
        tid = await self._token("new@x.com", port=8003)   # the account existed before (e.g. on a laptop)
        out = await self._managed()
        self.assertEqual(out["token_id"], tid)
        await self.svc.agent_poll("h1", "1.0", {out["name"]: {"container_up": True, "egress_ip": "1.2.3.4"}}, [], accept_jobs=False)
        st = await self.svc.status()
        self.assertTrue(st["slice_b_enabled"] and st["vault_ready"])
        b = st["browsers"][0]
        self.assertNotIn("sealed_password", b)
        self.assertNotIn("v1:", json.dumps(st, default=str))
        self.assertEqual(b["obs"]["egress_ip"], "1.2.3.4")
        self.assertEqual(b["token"]["hold"], "onboarding")
        self.assertEqual(st["ports"][0]["browser_name"], out["name"])
        await self._sql("UPDATE ultra_browsers SET state = 'degraded', onboarding_hold = 0 WHERE name = ?", (out["name"],))
        await self.svc.tick()
        self.assertIn((await self.svc.get_browser(out["name"]))["state"], ("degraded", "ok"))

    async def test_dry_run_poll_takes_no_job(self):
        out = await self._managed()
        r = await self.svc.agent_poll("h1", "1.0", {}, [], accept_jobs=False)
        self.assertIsNone(r["job"])
        self.assertEqual([b["name"] for b in r["browsers"]], [out["name"]])
        self.assertEqual((await self._job(out["name"], "create"))[1], "queued")


class AgentApiTests(Base):
    async def test_agent_endpoints_need_the_agent_token(self):
        from src.api import ultra as ultra_api
        ultra_api.set_dependencies(self.svc, admin_module.verify_admin_token)
        app = FastAPI()
        app.include_router(ultra_api.router)
        client = TestClient(app)
        r = await asyncio.to_thread(client.post, "/api/ultra/agent/poll", json={"host_id": "h1"}, headers={"Authorization": f"Bearer {CONN}"})
        self.assertEqual(r.status_code, 401)
        r = await asyncio.to_thread(client.post, "/api/ultra/agent/poll", json={"host_id": "h1"}, headers={"Authorization": "Bearer agent-tok"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["job"], None)
        r = await asyncio.to_thread(client.get, "/api/ultra/status")
        self.assertEqual(r.status_code, 401)
        with patch.dict(os.environ, {"ULTRA_AGENT_TOKEN": ""}):
            r = await asyncio.to_thread(client.post, "/api/ultra/agent/poll", json={"host_id": "h1"}, headers={"Authorization": "Bearer "})
            self.assertEqual(r.status_code, 503)


if __name__ == "__main__":
    unittest.main()


class AgentServerIntegrationTests(Base):
    """The real agent (fake docker) against the real API in-process: create → bootstrap → sign-in queued, and a
    screenshot, with the protocol exactly as deployed."""

    async def test_agent_and_server_speak_the_same_protocol(self):
        import importlib.util
        import io
        import threading
        import zipfile
        from pathlib import Path
        from src.api import ultra as ultra_api
        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location("ultra_agent_it", root / "docker/flow-ultra/agent/ultra_agent.py")
        ua = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ua)

        ultra_api.set_dependencies(self.svc, admin_module.verify_admin_token)
        app = FastAPI()
        app.include_router(ultra_api.router)
        client = TestClient(app)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("manifest.json", json.dumps({"version": "3.7.5"}))
            for f in ("background.js", "options.html", "options.js"):
                z.writestr(f, "//")
        loop = asyncio.get_running_loop()

        class InProcHttp:
            def post(self, path, body, timeout=30):
                r = client.post(path, json=body, headers={"Authorization": "Bearer agent-tok"})
                assert r.status_code == 200, r.text
                return r.json()

            def get_bytes(self, url, headers=None, timeout=60):
                return json.dumps({"version": "3.7.5"}).encode() if url.endswith("/ext-version") else buf.getvalue()

        out = await self._managed()
        name, rk = out["name"], out["route_key"]
        state = {"up": False}

        class Runner:
            def __init__(self):
                self.calls = []

            def run(self, argv, input_text=None, timeout=60):
                self.calls.append((argv, input_text))
                if argv[:2] == ["docker", "ps"]:
                    return 0, (name + "\n") if state["up"] else "", ""
                if argv[:2] == ["docker", "run"]:
                    state["up"] = True
                    return 0, "cid\n", ""
                if argv[:3] == ["docker", "exec", "-i"]:
                    cmd = json.loads(input_text)["cmd"]
                    res = {"ext_bootstrap": {"ok": True, "routeKey": rk, "via": "flowBootstrap"},
                           "screenshot": {"png_b64": base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()},
                           "cookies": {"google_signed_in": False}}.get(cmd, {})
                    return 0, json.dumps({"ok": True, "result": res}) + "\n", ""
                if argv[:2] == ["docker", "exec"] and "cat" in argv:
                    return 0, json.dumps({"version": "3.7.5"}), ""
                return 0, "", ""

        tmp = self._tmp.name
        cfg = ua.Config({"FLOW_BASE": "https://flow.example.com", "ULTRA_AGENT_TOKEN": "agent-tok", "HOST_ID": "h1",
                         "FU_ROOT": tmp + "/srv", "FU_ETC": tmp + "/etc"})
        runner = Runner()
        agent = ua.Agent(cfg, runner=runner, http=InProcHttp(), sleep=lambda s: None)
        agent.docker._src = "#"

        def drive(rounds):
            for _ in range(rounds):
                agent.poll_once()
                for t in list(agent.running.values()):
                    t.join(10)

        await asyncio.to_thread(drive, 2)
        b = await self.svc.get_browser(name)
        self.assertEqual(b["route_acked"], 1, b["state_detail"])
        self.assertEqual(b["state"], "signing_in")
        jobs = {k: s for k, s in await self._sql("SELECT kind, state FROM ultra_jobs")}
        self.assertEqual(jobs["create"], "done")
        self.assertEqual(jobs["bootstrap"], "done")
        self.assertEqual(jobs["login"], "queued")   # the sign-in waits for the next poll
        # the proxy password and the connection token never reached a command line
        for argv, _ in runner.calls:
            self.assertNotIn("pw-pool", " ".join(argv))
            self.assertNotIn(CONN, " ".join(argv))
        boot_stdin = next(i for a, i in runner.calls if i and '"ext_bootstrap"' in i)
        self.assertIn(CONN, boot_stdin)
        await self._sql("UPDATE ultra_jobs SET state = 'cancelled' WHERE kind = 'login'")
        shot = await self.svc.request_job(name, "screenshot")
        await asyncio.to_thread(drive, 1)
        self.assertTrue(self.svc.get_screenshot(shot["job_id"]).startswith(b"\x89PNG"))
