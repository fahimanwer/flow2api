"""Account order (2026-09-22): images try Pro -> Free -> Ultra, videos Ultra -> Pro -> Free, unless 'balanced'."""
import asyncio
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import admin
from src.core import client_policy as cp
from src.core.config import config
from src.core.database import Database
from src.core.models import Token
from src.services.load_balancer import LoadBalancer

FREE, PRO, ULT = "PAYGATE_TIER_NOT_PAID", "PAYGATE_TIER_ONE", "PAYGATE_TIER_TWO"


def _token(tid, tier, image_enabled=True, video_enabled=True):
    return Token(id=tid, st=f"st{tid}", at=f"at{tid}", email=f"t{tid}@x", user_paygate_tier=tier,
                 image_enabled=image_enabled, video_enabled=video_enabled)


class FakeTokenManager:
    def __init__(self, tokens):
        self.tokens = tokens
        self.db = None
    async def _ensure_quota_loaded(self): pass
    async def get_active_tokens(self): return list(self.tokens)
    def is_recaptcha_cooldown(self, tid): return False
    def is_health_cooldown(self, tid, *a): return False
    def health_cooldown_reason(self, tid): return ""
    def is_model_quota_exhausted(self, tid, model): return False
    def needs_at_refresh(self, token): return False
    async def ensure_valid_token(self, token): return token


def _pick(tokens, **kw):
    lb = LoadBalancer(FakeTokenManager(tokens), concurrency_manager=None)
    return asyncio.run(lb.select_token(reserve=False, enforce_concurrency_filter=False, **kw))


class PickerOrderTests(unittest.TestCase):
    def setUp(self):
        self._saved = (config.tier_order, config.call_logic_mode, config.captcha_method)
        config.set_tier_order("save_ultra")
        config.set_call_logic_mode("default")
        config.set_captcha_method("yescaptcha")  # no extension route checks
        self._saved_store = cp.client_policy_store._policies
        cp.client_policy_store.replace([
            {"client": "default", "image_tier": "any", "video_tier": "any"},
            {"client": "pinterest-factory", "image_tier": "ultra", "video_tier": "ultra"},
        ])

    def tearDown(self):
        cp.client_policy_store._policies = self._saved_store
        config.set_tier_order(self._saved[0])
        config.set_call_logic_mode(self._saved[1])
        config.set_captcha_method(self._saved[2])

    def test_images_try_pro_then_free_then_ultra(self):
        pool = [_token(1, ULT), _token(2, FREE), _token(3, PRO)]
        for _ in range(10):
            self.assertEqual(_pick(pool, for_image_generation=True).id, 3)
        self.assertEqual(_pick([_token(1, ULT), _token(2, FREE)], for_image_generation=True).id, 2)
        self.assertEqual(_pick([_token(1, ULT)], for_image_generation=True).id, 1)

    def test_videos_try_ultra_then_pro_then_free(self):
        pool = [_token(1, FREE), _token(2, PRO), _token(3, ULT)]
        for _ in range(10):
            self.assertEqual(_pick(pool, for_video_generation=True).id, 3)
        self.assertEqual(_pick([_token(1, FREE), _token(2, PRO)], for_video_generation=True).id, 2)

    def test_disabled_or_full_cheaper_accounts_let_ultra_through(self):
        pool = [_token(1, ULT), _token(2, PRO, image_enabled=False), _token(3, FREE, image_enabled=False)]
        self.assertEqual(_pick(pool, for_image_generation=True).id, 1)

    def test_sequential_rotation_keeps_its_order_inside_each_tier(self):
        config.set_call_logic_mode("polling")
        pool = [_token(1, ULT), _token(2, PRO), _token(3, PRO), _token(4, FREE)]
        picks = [_pick(pool, for_image_generation=True).id for _ in range(4)]
        self.assertTrue(all(p in (2, 3) for p in picks), picks)

    def test_balanced_mode_does_not_force_the_order(self):
        config.set_tier_order("balanced")
        config.set_call_logic_mode("polling")  # deterministic: round-robin by id
        lb = LoadBalancer(FakeTokenManager([_token(1, ULT), _token(2, PRO)]), concurrency_manager=None)
        picks = {asyncio.run(lb.select_token(for_image_generation=True, reserve=False,
                                             enforce_concurrency_filter=False)).id for _ in range(6)}
        self.assertEqual(picks, {1, 2})

    def test_pinterest_still_gets_ultra_only(self):
        pool = [_token(1, ULT), _token(2, PRO), _token(3, FREE)]
        self.assertEqual(_pick(pool, for_image_generation=True, client="pinterest-factory").id, 1)


class TierOrderStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = f"{self._tmp.name}/flow.db"
        self.db = Database(db_path=self.db_path)
        self._saved = (config.tier_order, config.call_logic_mode)

    async def asyncTearDown(self):
        config.set_tier_order(self._saved[0])
        config.set_call_logic_mode(self._saved[1])
        self._tmp.cleanup()

    async def test_fresh_database_defaults_to_save_ultra(self):
        await self.db.init_db()
        await self.db.init_config_from_toml({}, is_first_startup=True)
        self.assertEqual((await self.db.get_call_logic_config()).tier_order, "save_ultra")

    async def test_first_start_reads_toml(self):
        await self.db.init_db()
        await self.db.init_config_from_toml({"call_logic": {"call_mode": "polling", "tier_order": "balanced"}},
                                            is_first_startup=True)
        saved = await self.db.get_call_logic_config()
        self.assertEqual((saved.call_mode, saved.tier_order), ("polling", "balanced"))

    async def test_existing_database_gets_the_column(self):
        import sqlite3
        con = sqlite3.connect(self.db_path)
        con.execute("CREATE TABLE call_logic_config (id INTEGER PRIMARY KEY DEFAULT 1, call_mode TEXT DEFAULT 'default', "
                    "polling_mode_enabled BOOLEAN DEFAULT 0, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        con.execute("INSERT INTO call_logic_config (id, call_mode, polling_mode_enabled) VALUES (1, 'polling', 1)")
        con.commit(); con.close()
        await self.db.init_db()
        await self.db.check_and_migrate_db({})
        saved = await self.db.get_call_logic_config()
        self.assertEqual((saved.call_mode, saved.tier_order), ("polling", "save_ultra"))

    async def test_saving_rotation_mode_keeps_tier_order_and_reloads(self):
        await self.db.init_db()
        await self.db.init_config_from_toml({}, is_first_startup=True)
        await self.db.update_call_logic_config(tier_order="balanced")
        await self.db.update_call_logic_config(call_mode="polling")
        saved = await self.db.get_call_logic_config()
        self.assertEqual((saved.call_mode, saved.tier_order), ("polling", "balanced"))
        await self.db.reload_config_to_memory()
        self.assertEqual((config.call_logic_mode, config.tier_order), ("polling", "balanced"))


class TierOrderAdminApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(db_path=f"{self._tmp.name}/flow.db")
        asyncio.run(self.db.init_db())
        asyncio.run(self.db.init_config_from_toml({}, is_first_startup=True))
        self._saved = (config.tier_order, config.call_logic_mode)
        app = FastAPI()
        app.include_router(admin.router)
        app.dependency_overrides[admin.verify_admin_token] = lambda: "admin"
        self._db_patch = patch.object(admin, "db", self.db)
        self._db_patch.start()
        self.client = TestClient(app)

    def tearDown(self):
        self._db_patch.stop()
        config.set_tier_order(self._saved[0])
        config.set_call_logic_mode(self._saved[1])
        self._tmp.cleanup()

    def test_round_trip_and_validation(self):
        self.assertEqual(self.client.get("/api/call-logic/config").json()["config"]["tier_order"], "save_ultra")
        r = self.client.post("/api/call-logic/config", json={"call_mode": "default", "tier_order": "balanced"})
        self.assertEqual(r.json()["config"]["tier_order"], "balanced")
        self.assertEqual(config.tier_order, "balanced")
        # old clients that only send call_mode must not reset the order
        self.client.post("/api/call-logic/config", json={"call_mode": "polling"})
        self.assertEqual(self.client.get("/api/call-logic/config").json()["config"],
                         {"call_mode": "polling", "polling_mode_enabled": True, "tier_order": "balanced"})
        self.assertEqual(self.client.post("/api/call-logic/config", json={"tier_order": "nope"}).status_code, 400)
        self.assertEqual(self.client.post("/api/call-logic/config", json={}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
