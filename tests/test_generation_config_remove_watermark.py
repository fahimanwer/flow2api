import asyncio
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import admin
from src.core.config import config
from src.core.database import Database


class RemoveWatermarkConfigTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = f"{self._temp_dir.name}/flow.db"
        self.db = Database(db_path=self.db_path)
        self._original = config.remove_watermark

    async def asyncTearDown(self):
        config.set_remove_watermark(self._original)
        self._temp_dir.cleanup()

    async def test_fresh_database_defaults_to_on(self):
        await self.db.init_db()
        await self.db.init_config_from_toml({"generation": {"image_timeout": 300}}, is_first_startup=True)
        config.set_remove_watermark(False)

        self.assertTrue((await self.db.get_generation_config()).remove_watermark)
        await self.db.reload_config_to_memory()
        self.assertTrue(config.remove_watermark)

    async def test_first_start_reads_setting_toml(self):
        await self.db.init_db()
        await self.db.init_config_from_toml({"generation": {"remove_watermark": False}}, is_first_startup=True)
        self.assertFalse((await self.db.get_generation_config()).remove_watermark)

    async def test_existing_database_gets_column_switched_on(self):
        con = sqlite3.connect(self.db_path)
        con.execute(
            "CREATE TABLE generation_config (id INTEGER PRIMARY KEY DEFAULT 1, image_timeout INTEGER DEFAULT 300, "
            "video_timeout INTEGER DEFAULT 1500, max_retries INTEGER DEFAULT 3, "
            "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        con.execute("INSERT INTO generation_config (id, image_timeout, video_timeout, max_retries) VALUES (1, 111, 222, 4)")
        con.commit()
        con.close()

        await self.db.init_db()
        await self.db.check_and_migrate_db({})

        generation_config = await self.db.get_generation_config()
        self.assertTrue(generation_config.remove_watermark)
        self.assertEqual((generation_config.image_timeout, generation_config.max_retries), (111, 4))

    async def test_toggle_persists_and_timeout_save_keeps_it(self):
        await self.db.init_db()
        await self.db.init_config_from_toml({}, is_first_startup=True)

        await self.db.update_generation_config(remove_watermark=False)
        await self.db.reload_config_to_memory()
        self.assertFalse(config.remove_watermark)

        await self.db.update_generation_config(image_timeout=400, video_timeout=1600, max_retries=5)
        await self.db.reload_config_to_memory()
        self.assertFalse(config.remove_watermark)
        self.assertEqual(config.image_timeout, 400)

        await self.db.update_generation_config(remove_watermark=True)
        await self.db.reload_config_to_memory()
        self.assertTrue(config.remove_watermark)


class RemoveWatermarkAdminApiTests(unittest.TestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(db_path=f"{self._temp_dir.name}/flow.db")
        asyncio.run(self.db.init_db())
        asyncio.run(self.db.init_config_from_toml({}, is_first_startup=True))
        self._original = config.remove_watermark
        app = FastAPI()
        app.include_router(admin.router)
        app.dependency_overrides[admin.verify_admin_token] = lambda: "admin"
        self._db_patch = patch.object(admin, "db", self.db)
        self._db_patch.start()
        self.client = TestClient(app)

    def tearDown(self):
        self._db_patch.stop()
        config.set_remove_watermark(self._original)
        self._temp_dir.cleanup()

    def test_both_generation_endpoints_read_and_write_the_switch(self):
        for path in ("/api/generation/timeout", "/api/config/generation"):
            with self.subTest(path=path):
                self.assertTrue(self.client.get(path).json()["config"]["remove_watermark"])

                saved = self.client.post(path, json={"image_timeout": 300, "video_timeout": 1500,
                                                     "max_retries": 3, "remove_watermark": False})
                self.assertTrue(saved.json()["success"])
                self.assertFalse(self.client.get(path).json()["config"]["remove_watermark"])
                self.assertFalse(config.remove_watermark)

                self.client.post(path, json={"remove_watermark": True})
                self.assertTrue(self.client.get(path).json()["config"]["remove_watermark"])


if __name__ == "__main__":
    unittest.main()
