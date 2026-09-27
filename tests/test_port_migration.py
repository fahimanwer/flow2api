"""One-time move of an Ultra account to its own proxy IP, only when its browser runs worker >= 3.7.4
(owner 27 Sep 2026). pending → (running work) draining → offered (browser gets the port; the account
takes no new work; redeem unchanged) → done (browser confirmed; assignment + redeem switch together)."""
import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from src.api import admin
from src.core.database import Database, proxy_port, with_proxy_port
from src.core.models import Token
from src.services.load_balancer import LoadBalancer

POOL = [8001, 8002, 8003, 8004, 8011, 8012]
HOST = "disp.oxylabs.io"
BASE = f"http://user-x:pass@{HOST}"


class Helpers(unittest.TestCase):
    def test_port_helpers_and_versions(self):
        self.assertEqual(proxy_port(f"{BASE}:8003"), 8003)
        self.assertEqual(with_proxy_port(f"{BASE}:8003", 8011), f"{BASE}:8011")
        self.assertTrue(admin._version_at_least("3.7.4", "3.7.4"))
        self.assertTrue(admin._version_at_least("3.10.0", "3.7.4"))
        self.assertFalse(admin._version_at_least("3.7.3", "3.7.4"))
        self.assertFalse(admin._version_at_least(None, "3.7.4"))
        self.assertTrue(admin._is_pool_endpoint(f"{BASE}:8003", POOL, HOST))
        self.assertFalse(admin._is_pool_endpoint(f"{BASE}:8016", POOL, HOST))
        self.assertFalse(admin._is_pool_endpoint("http://u:p@other.host:8003", POOL, HOST))


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(db_path=f"{self._tmp.name}/flow.db")
        await self.db.init_db()
        pc = MagicMock()
        pc.ext_proxy_pool = '{"host": "%s", "user": "user-x", "pass": "pass", "ports": %s}' % (HOST, POOL)
        pc.connection_token = "x"

        async def _pc():
            return pc
        self.db.get_plugin_config = _pc
        self._p = [patch.object(admin, "db", self.db), patch.object(admin, "token_manager", MagicMock()),
                   patch.object(admin, "_verify_plugin_connection_token", return_value=None)]
        for p in self._p:
            p.start()

    async def asyncTearDown(self):
        for p in self._p:
            p.stop()
        self._tmp.cleanup()

    async def _seed(self, route_key, port, minutes_ago=1):
        seen = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
        async with self.db._connect(write=True) as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO device_port_assignments (route_key, port, assigned_at, last_seen) VALUES (?, ?, ?, ?)",
                (route_key, port, seen, seen))
            await conn.commit()

    async def _token(self, email, tier="PAYGATE_TIER_TWO", port=8003, route_key=None):
        tid = await self.db.add_token(Token(st=f"st-{email}", at="at", email=email, user_paygate_tier=tier))
        await self.db.update_token(tid, redeem_proxy_url=f"{BASE}:{port}", extension_route_key=route_key)
        return tid

    async def _running(self, tid, n=1):
        async with self.db._connect(write=True) as conn:
            for _ in range(n):
                await conn.execute(
                    "INSERT INTO request_logs (token_id, operation, status_code, duration, status_text, created_at) "
                    "VALUES (?, 'generate', 102, 0, 'started', ?)",
                    (tid, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")))
            await conn.commit()

    async def _finish_all(self, tid):
        async with self.db._connect(write=True) as conn:
            await conn.execute("UPDATE request_logs SET status_text = 'completed' WHERE token_id = ?", (tid,))
            await conn.commit()

    async def _age_drain(self, seconds=30):
        t = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
        async with self.db._connect(write=True) as conn:
            await conn.execute("UPDATE port_migrations SET offered_at = ? WHERE state = 'draining'", (t,))
            await conn.commit()

    async def _offer_ready(self):
        """Block first, let the settle time pass, then offer (the real sequence)."""
        first = await self._offer()
        if first and first["state"] == "draining":
            await self._age_drain()
            return await self._offer()
        return first

    async def _state(self, rk, tid):
        return (await self.db.get_device_assignment(rk),
                proxy_port((await self.db.get_token(tid)).redeem_proxy_url),
                ((await self.db.get_port_migration(rk)) or {}).get("state"))

    async def _seeded(self):
        tid = await self._token("u@x", route_key="rk")
        await self._seed("rk", 8003)
        await self._seed("other", 8003)
        self.assertTrue(await self.db.queue_port_migration("rk", tid, 8011))
        return tid

    async def _offer(self, ver="3.7.4", manual=None):
        return await admin._offer_port_migration("rk", POOL, ver, manual)

    async def _ack(self, mig_id, port=8011, ok=True):
        return await admin.plugin_port_migration_ack({"route_key": "rk", "migration_id": mig_id, "port": port, "ok": ok}, "Bearer x")

    async def test_old_version_or_manual_is_never_offered(self):
        tid = await self._seeded()
        for ver, manual in (("3.7.3", None), (None, None), ("3.7.4", "1")):
            self.assertIsNone(await self._offer(ver, manual))
        self.assertEqual(await self._state("rk", tid), (8003, 8003, "pending"))
        self.assertEqual(await self.db.get_ip_move_blocked_token_ids(), set())

    async def test_proxy_pool_endpoint_offers_only_when_ready(self):
        tid = await self._seeded()
        await self._running(tid)
        call = lambda ver="3.7.4": admin.plugin_proxy_pool(route_key="rk", ext_version=ver, manual=None, authorization="Bearer x")
        out = await call("3.7.3")
        self.assertEqual(out["assigned_port"], 8003)
        self.assertNotIn("migration_id", out)
        out = await call()
        self.assertEqual((out["assigned_port"], out.get("migration_retry_in")), (8003, admin.PORT_MIGRATION_RETRY_SECONDS))
        await self._finish_all(tid)
        await self._age_drain()
        out = await call()
        self.assertEqual(out["assigned_port"], 8011)
        self.assertTrue(out["migration_id"])
        self.assertEqual(await self._state("rk", tid), (8003, 8003, "offered"))

    async def test_running_work_drains_first_and_blocks_new_work(self):
        tid = await self._seeded()
        await self._running(tid)
        m = await self._offer()
        self.assertEqual(m["state"], "draining")
        self.assertIn(tid, await self.db.get_ip_move_blocked_token_ids())
        self.assertEqual(await self._state("rk", tid), (8003, 8003, "draining"))
        await self._finish_all(tid)
        self.assertEqual((await self._offer())["state"], "draining")  # settle time not passed yet
        await self._age_drain()
        m = await self._offer()
        self.assertEqual(m["state"], "offered")
        self.assertEqual(await self._state("rk", tid), (8003, 8003, "offered"))  # redeem not switched yet

    async def test_offered_blocks_until_confirmed_with_no_timer(self):
        tid = await self._seeded()
        m = await self._offer_ready()
        self.assertIn(tid, await self.db.get_ip_move_blocked_token_ids())
        async with self.db._connect(write=True) as conn:
            await conn.execute("UPDATE port_migrations SET offered_at = '2026-01-01T00:00:00+00:00'")
            await conn.commit()
        self.assertIn(tid, await self.db.get_ip_move_blocked_token_ids())  # an old offer still blocks
        res = await self._ack(m["mig_id"])
        self.assertEqual(res["state"], "done")
        self.assertEqual(await self._state("rk", tid), (8011, 8011, "done"))
        self.assertEqual(await self.db.get_ip_move_blocked_token_ids(), set())
        self.assertTrue((await self._ack(m["mig_id"]))["success"])  # idempotent

    async def test_stale_draining_lapses(self):
        tid = await self._seeded()
        await self._running(tid)
        await self._offer()
        async with self.db._connect(write=True) as conn:
            await conn.execute("UPDATE port_migrations SET offered_at = '2026-01-01T00:00:00+00:00'")
            await conn.commit()
        self.assertEqual(await self.db.get_ip_move_blocked_token_ids(), set())

    async def test_rejected_ack_keeps_the_block(self):
        tid = await self._seeded()
        m = await self._offer_ready()
        for body in (("nope", 8011, True), (m["mig_id"], 8012, True), ("nope", 8011, False)):
            self.assertFalse((await self._ack(*body))["success"])
        self.assertEqual(await self._state("rk", tid), (8003, 8003, "offered"))
        self.assertIn(tid, await self.db.get_ip_move_blocked_token_ids())

    async def test_browser_failure_goes_back_to_pending_and_unblocks(self):
        tid = await self._seeded()
        m = await self._offer_ready()
        self.assertTrue((await self._ack(m["mig_id"], ok=False))["success"])
        self.assertEqual(await self._state("rk", tid), (8003, 8003, "pending"))
        self.assertEqual(await self.db.get_ip_move_blocked_token_ids(), set())

    async def test_late_reports_never_overwrite_redeem(self):
        tid = await self._seeded()
        m = await self._offer_ready()
        self.assertFalse(await admin._write_reported_redeem(tid, "rk", f"{BASE}:8011"))  # not confirmed yet
        self.assertTrue(await admin._write_reported_redeem(tid, "rk", f"{BASE}:8003"))
        await self._ack(m["mig_id"])
        self.assertFalse(await admin._write_reported_redeem(tid, "rk", f"{BASE}:8003"))  # late old push
        self.assertEqual(await self._state("rk", tid), (8011, 8011, "done"))
        self.assertTrue(await admin._write_reported_redeem(tid, "rk", f"{BASE}:8011"))
        self.assertTrue(await admin._write_reported_redeem(tid, "rk", f"{BASE}:8016"))  # private endpoint
        plain = await self._token("p@x", route_key="plain")
        await self._seed("plain", 8002)
        self.assertTrue(await admin._write_reported_redeem(plain, "plain", f"{BASE}:8004"))  # no migration

    async def test_account_on_another_device_voids_the_move(self):
        tid = await self._seeded()
        m = await self._offer_ready()
        await self.db.update_token(tid, extension_route_key="rk-B")
        self.assertFalse((await self._ack(m["mig_id"]))["success"])
        self.assertIsNone(await self.db.get_port_migration("rk"))
        self.assertEqual(proxy_port((await self.db.get_token(tid)).redeem_proxy_url), 8003)
        await self.db.queue_port_migration("rk", tid, 8011)
        self.assertIsNone(await self._offer())  # the old device no longer owns the account
        self.assertEqual(await self.db.drop_foreign_migrations(tid, "rk-B"), 1)

    async def test_reserved_port_is_not_handed_out_and_blocks_a_clashing_offer(self):
        await self._seeded()
        for i in range(6):
            self.assertNotEqual(await self.db.assign_device_port(f"new{i}", POOL), 8011)
        await self._seed("squatter", 8011)
        self.assertIsNone(await self._offer())

    async def test_done_migration_restores_the_port_after_pruning(self):
        await self._seeded()
        m = await self._offer_ready()
        await self._ack(m["mig_id"])
        async with self.db._connect(write=True) as conn:
            await conn.execute("DELETE FROM device_port_assignments WHERE route_key = 'rk'")
            await conn.commit()
        self.assertEqual(await self.db.assign_device_port("rk", POOL), 8011)

    async def test_new_ultra_queue_and_alone_reservation(self):
        req = lambda ver="3.7.4", port=8003: {"ext_version": ver, "proxy_url": f"{BASE}:{port}"}
        ult = await self._token("u@x", route_key="rk-u")
        await self._seed("rk-u", 8003)
        await self._seed("busy", 8003)
        self.assertTrue(await admin._ultra_migration_pending("rk-u", ult, req()))
        mig = await self.db.get_port_migration("rk-u")
        self.assertEqual((mig["state"], mig["to_port"]), ("pending", 8001))
        alone = await self._token("a@x", port=8004, route_key="rk-a")
        await self._seed("rk-a", 8004)
        self.assertFalse(await admin._ultra_migration_pending("rk-a", alone, req(port=8004)))
        self.assertEqual((await self.db.get_port_migration("rk-a"))["state"], "done")  # 8004 now reserved
        self.assertNotEqual(await self.db.assign_device_port("newcomer", [8004, 8002]), 8004)
        old = await self._token("o@x", route_key="rk-o")
        await self._seed("rk-o", 8003)
        self.assertFalse(await admin._ultra_migration_pending("rk-o", old, req(ver="3.7.3")))
        man = await self._token("m@x", route_key="rk-m")
        await self._seed("rk-m", 8003)
        self.assertFalse(await admin._ultra_migration_pending("rk-m", man, req(port=8016)))
        pro = await self._token("p@x", tier="PAYGATE_TIER_ONE", route_key="rk-p")
        await self._seed("rk-p", 8003)
        self.assertFalse(await admin._ultra_migration_pending("rk-p", pro, req()))


    # ---- Codex round-2 reproductions ----
    async def test_rebinding_and_completion_cannot_interleave(self):
        tid = await self._token("u@x", route_key="auto-A")
        await self._seed("auto-A", 8003)
        await self._seed("other", 8003)
        await self.db.queue_port_migration("auto-A", tid, 8011)
        first = await admin._offer_port_migration("auto-A", POOL, "3.7.4", None)
        await self._age_drain()
        m = await admin._offer_port_migration("auto-A", POOL, "3.7.4", None)
        self.assertEqual(m["state"], "offered")
        # device B pushes: binding + its redeem land in one transaction, so A's later ack is refused
        await self.db.apply_push_routing(tid, "auto-B", f"{BASE}:8004", True)
        res = await admin.plugin_port_migration_ack({"route_key": "auto-A", "migration_id": m["mig_id"], "port": 8011, "ok": True}, "Bearer x")
        self.assertFalse(res["success"])
        t = await self.db.get_token(tid)
        self.assertEqual((proxy_port(t.redeem_proxy_url), t.extension_route_key), (8004, "auto-B"))
        self.assertIsNone(await self.db.get_port_migration("auto-A"))

    async def test_manual_binding_is_kept(self):
        tid = await self._token("m@x", route_key="staff-laptop")
        await self.db.apply_push_routing(tid, "auto-X", f"{BASE}:8002", True)
        self.assertEqual((await self.db.get_token(tid)).extension_route_key, "staff-laptop")

    async def test_dormant_device_blocks_alone_reservation(self):
        ult = await self._token("u@x", port=8004, route_key="rk-u")
        await self._seed("rk-u", 8004)
        await self._seed("offline", 8004, minutes_ago=24 * 60)
        self.assertFalse(await self.db.reserve_current_port("rk-u", ult, 8004))
        # and it counts as sharing, so the Ultra is queued to move
        req = {"ext_version": "3.7.4", "proxy_url": f"{BASE}:8004"}
        self.assertTrue(await admin._ultra_migration_pending("rk-u", ult, req))

    async def test_stale_drain_restarts_its_settle_time(self):
        tid = await self._seeded()
        await self._offer()
        await self._age_drain(seconds=11 * 60)  # older than the 10-min lapse
        self.assertEqual((await self._offer())["state"], "draining")  # re-blocks, does not offer at once
        await self._age_drain()
        self.assertEqual((await self._offer())["state"], "offered")

    async def test_away_mode_account_on_the_port_blocks_alone_reservation(self):
        ult = await self._token("u@x", port=8004, route_key="rk-u")
        await self._seed("rk-u", 8004)
        await self._token("away@x", port=8004)  # redeems on 8004 without a device row
        self.assertFalse(await self.db.reserve_current_port("rk-u", ult, 8004))
        req = {"ext_version": "3.7.4", "proxy_url": f"{BASE}:8004"}
        self.assertTrue(await admin._ultra_migration_pending("rk-u", ult, req))  # queued to move instead

    async def test_free_port_skips_ports_used_by_active_accounts(self):
        await self._token("away@x", port=8001)  # redeems on 8001, no device row
        self.assertNotEqual(await self.db.pick_free_port(POOL), 8001)


class PickerGateTests(unittest.TestCase):
    def test_blocked_account_gets_no_new_work(self):
        from src.core import client_policy as cp
        from src.core.config import config
        from tests.test_tier_order import FakeTokenManager, _token, ULT
        saved = (config.captcha_method, cp.client_policy_store._policies)
        config.set_captcha_method("yescaptcha")
        cp.client_policy_store.replace([{"client": "default", "image_tier": "any", "video_tier": "any"}])
        try:
            tm = FakeTokenManager([_token(1, ULT), _token(2, ULT)])
            tm.db = MagicMock()

            async def blocked():
                return {1}
            tm.db.get_ip_move_blocked_token_ids = blocked
            lb = LoadBalancer(tm, concurrency_manager=None)
            for _ in range(5):
                t = asyncio.run(lb.select_token(for_image_generation=True, reserve=False, enforce_concurrency_filter=False))
                self.assertEqual(t.id, 2)
            # a block that starts while the pick is validating is caught by the second check
            calls = {"n": 0}

            async def late_block():
                calls["n"] += 1
                return set() if calls["n"] == 1 else {1, 2}
            tm.db.get_ip_move_blocked_token_ids = late_block
            self.assertIsNone(asyncio.run(lb.select_token(for_image_generation=True, reserve=False, enforce_concurrency_filter=False)))
        finally:
            config.set_captcha_method(saved[0])
            cp.client_policy_store._policies = saved[1]


if __name__ == "__main__":
    unittest.main()
