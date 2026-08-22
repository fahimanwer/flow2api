"""Named multi-key authentication: principals, legacy fallback, rotation, fail-closed."""
import ast
import asyncio
import importlib
import inspect
import json
import tempfile
import textwrap
import types
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from src.api import routes
from src.core import auth as auth_module
from src.core.auth import (
    AUTH_NOT_CONFIGURED_DETAIL,
    AuthManager,
    verify_api_key_flexible,
    verify_api_key_header,
)
from src.core.config import LEGACY_PRINCIPAL, config
from src.core.database import Database
from src.core.models import AsyncGenerationRequest, AsyncTask
from src.services.async_task_manager import AsyncTaskManager

# `src.core` re-exports the config *instance* under the same name as the module,
# so attribute access would hand back the instance; go through the module table.
config_module = importlib.import_module("src.core.config")

REELS_KEY = "sk-reels-0123456789abcdef"
FACTORY_KEY = "sk-factory-0123456789abcdef"
EXTENSION_KEY = "sk-extension-0123456789abcdef"
LEGACY_KEY = "old-single-key"

NAMED_KEYS = {
    "ai-reels": REELS_KEY,
    "content-factory": FACTORY_KEY,
    "extension": EXTENSION_KEY,
}


def _bearer(key: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=key)


async def _resolve(key: str) -> str:
    """Run the real HTTP auth dependency for a bearer key."""
    return await verify_api_key_flexible(credentials=_bearer(key), x_goog_api_key=None, key=None)


class _ConfigSandbox:
    """Swap the process-wide config's keys for a test and restore them after."""

    def __init__(self, legacy: str = "", named: dict = None):
        self.legacy = legacy
        self.named = named or {}

    def __enter__(self):
        self._saved_legacy = config.api_key
        self._saved_named = config.api_keys
        config.api_key = self.legacy
        config.set_api_keys(self.named)
        return self

    def __exit__(self, *exc):
        config.api_key = self._saved_legacy
        config.set_api_keys(self._saved_named)
        return False


class PrincipalResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_named_key_resolves_to_its_principal(self):
        with _ConfigSandbox(named=NAMED_KEYS):
            self.assertEqual(await _resolve(REELS_KEY), "ai-reels")
            self.assertEqual(await _resolve(FACTORY_KEY), "content-factory")
            self.assertEqual(await _resolve(EXTENSION_KEY), "extension")

    async def test_legacy_single_key_resolves_to_the_legacy_principal(self):
        with _ConfigSandbox(legacy=LEGACY_KEY, named=NAMED_KEYS):
            self.assertEqual(await _resolve(LEGACY_KEY), LEGACY_PRINCIPAL)
            self.assertEqual(
                await verify_api_key_header(credentials=_bearer(LEGACY_KEY)), LEGACY_PRINCIPAL
            )

    async def test_all_three_transports_resolve_the_principal(self):
        with _ConfigSandbox(named=NAMED_KEYS):
            via_header = await verify_api_key_flexible(
                credentials=None, x_goog_api_key=REELS_KEY, key=None
            )
            via_query = await verify_api_key_flexible(credentials=None, x_goog_api_key=None, key=REELS_KEY)
        self.assertEqual(via_header, "ai-reels")
        self.assertEqual(via_query, "ai-reels")

    async def test_unknown_key_is_401_when_keys_are_configured(self):
        with _ConfigSandbox(named=NAMED_KEYS):
            with self.assertRaises(HTTPException) as ctx:
                await _resolve("not-a-key")
            self.assertEqual(ctx.exception.status_code, 401)

            with self.assertRaises(HTTPException) as ctx:
                await verify_api_key_flexible(credentials=None, x_goog_api_key=None, key=None)
            self.assertEqual(ctx.exception.status_code, 401)

    async def test_unconfigured_auth_refuses_everything_with_503(self):
        with _ConfigSandbox(legacy="", named={}):
            self.assertFalse(config.auth_configured)

            for presented in ("", "han1234", REELS_KEY):
                with self.subTest(presented=presented):
                    with self.assertRaises(HTTPException) as ctx:
                        await verify_api_key_flexible(
                            credentials=_bearer(presented) if presented else None,
                            x_goog_api_key=None,
                            key=None,
                        )
                    self.assertEqual(ctx.exception.status_code, 503)
                    self.assertEqual(ctx.exception.detail, AUTH_NOT_CONFIGURED_DETAIL)

            with self.assertRaises(HTTPException) as ctx:
                await verify_api_key_header(credentials=_bearer("han1234"))
            self.assertEqual(ctx.exception.status_code, 503)

            self.assertFalse(AuthManager.verify_api_key("han1234"))
            self.assertIsNone(AuthManager.resolve_principal(""))

    async def test_whitespace_only_keys_do_not_count_as_configured(self):
        with _ConfigSandbox(legacy="   ", named={"ai-reels": "  ", " ": REELS_KEY}):
            self.assertFalse(config.auth_configured)
            self.assertEqual(config.api_keys, {})

    async def test_legacy_name_is_reserved_in_the_named_table(self):
        with _ConfigSandbox(legacy="", named={"legacy": "sneaky", "ai-reels": REELS_KEY}):
            self.assertEqual(config.api_keys, {"ai-reels": REELS_KEY})
            self.assertIsNone(AuthManager.resolve_principal("sneaky"))

    async def test_duplicate_key_string_resolves_to_the_first_principal(self):
        with _ConfigSandbox(named={"first": REELS_KEY, "second": REELS_KEY}):
            self.assertEqual(AuthManager.resolve_principal(REELS_KEY), "first")

    async def test_rotating_a_key_keeps_the_principal_and_retires_the_old_key(self):
        with _ConfigSandbox(named=NAMED_KEYS):
            self.assertEqual(await _resolve(REELS_KEY), "ai-reels")

            rotated = dict(NAMED_KEYS, **{"ai-reels": "sk-reels-rotated"})
            config.set_api_keys(rotated)

            self.assertEqual(await _resolve("sk-reels-rotated"), "ai-reels")
            with self.assertRaises(HTTPException) as ctx:
                await _resolve(REELS_KEY)
            self.assertEqual(ctx.exception.status_code, 401)


class ConstantTimeComparisonTests(unittest.TestCase):
    def test_resolve_principal_uses_hmac_compare_digest_for_every_candidate(self):
        calls = []
        real = config_module.hmac.compare_digest

        def spy(a, b):
            calls.append((a, b))
            return real(a, b)

        with _ConfigSandbox(legacy=LEGACY_KEY, named=NAMED_KEYS):
            with patch.object(config_module.hmac, "compare_digest", spy):
                self.assertEqual(config.resolve_principal(REELS_KEY), "ai-reels")
                # A hit never short-circuits: every configured key is compared.
                self.assertEqual(len(calls), 1 + len(NAMED_KEYS))

                calls.clear()
                self.assertIsNone(config.resolve_principal("nope"))
                self.assertEqual(len(calls), 1 + len(NAMED_KEYS))

    def test_admin_verification_uses_hmac_compare_digest(self):
        with patch.object(auth_module.hmac, "compare_digest", wraps=auth_module.hmac.compare_digest) as spy:
            AuthManager.verify_admin(config.admin_username, config.admin_password)
        self.assertGreaterEqual(spy.call_count, 2)

    def test_no_equality_operator_on_secrets(self):
        """auth.py and resolve_principal must not compare secrets with ==/!=."""
        sources = {
            "src/core/auth.py": inspect.getsource(auth_module),
            "Config.resolve_principal": inspect.getsource(config_module.Config.resolve_principal),
            "Config.auth_configured": inspect.getsource(config_module.Config.auth_configured.fget),
        }
        for name, source in sources.items():
            with self.subTest(source=name):
                tree = ast.parse(textwrap.dedent(source))
                offenders = [
                    node.lineno
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Compare)
                    and any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops)
                ]
                self.assertEqual(offenders, [], f"== / != used in {name} at lines {offenders}")


class FakeGenerationHandler:
    def __init__(self):
        self.calls = []

    async def handle_generation(self, **kwargs):
        self.calls.append(kwargs)
        yield json.dumps(
            {
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "model": "veo_3_1_t2v",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
            }
        )


def _fake_request() -> types.SimpleNamespace:
    return types.SimpleNamespace(headers={"host": "localhost:8000"}, url=types.SimpleNamespace(scheme="http"))


class PrincipalTaskOwnershipTests(unittest.IsolatedAsyncioTestCase):
    """Async tasks are owned by principal; the legacy key also reads its old hash rows."""

    async def asyncSetUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(db_path=f"{self._temp_dir.name}/flow.db")
        await self.db.init_db()
        self.manager = AsyncTaskManager(self.db)
        self._previous_handler = routes.generation_handler
        self._previous_manager = routes.async_task_manager
        routes.set_generation_handler(FakeGenerationHandler())
        routes.set_async_task_manager(self.manager)
        self._sandbox = _ConfigSandbox(legacy=LEGACY_KEY, named=NAMED_KEYS)
        self._sandbox.__enter__()

    async def asyncTearDown(self):
        self._sandbox.__exit__(None, None, None)
        await self.manager.shutdown()
        routes.set_generation_handler(self._previous_handler)
        routes.set_async_task_manager(self._previous_manager)
        self._temp_dir.cleanup()

    async def _submit_as(self, key: str, idempotency_key: str = None) -> str:
        principal = await _resolve(key)
        response = await routes.submit_async_chat_completion(
            request=AsyncGenerationRequest(
                model="veo_3_1_t2v", messages=[{"role": "user", "content": "a cat"}]
            ),
            raw_request=_fake_request(),
            idempotency_key=idempotency_key,
            principal=principal,
        )
        return json.loads(response.body)["task_id"]

    async def _status_as(self, key: str, task_id: str):
        principal = await _resolve(key)
        return await routes.get_async_task_status(task_id=task_id, principal=principal)

    async def _await_terminal(self, task_id: str, principal: str):
        for _ in range(200):
            task = await self.manager.get(task_id, principal)
            if task and task.status in ("succeeded", "failed"):
                return task
            await asyncio.sleep(0.01)
        self.fail(f"Task {task_id} never finished")

    async def test_two_principals_each_see_only_their_own_tasks(self):
        reels_task = await self._submit_as(REELS_KEY)
        factory_task = await self._submit_as(FACTORY_KEY)
        await self._await_terminal(reels_task, "ai-reels")
        await self._await_terminal(factory_task, "content-factory")

        self.assertEqual((await self._status_as(REELS_KEY, reels_task))["task_id"], reels_task)
        self.assertEqual((await self._status_as(FACTORY_KEY, factory_task))["task_id"], factory_task)

        for key, foreign in ((REELS_KEY, factory_task), (FACTORY_KEY, reels_task), (LEGACY_KEY, reels_task)):
            with self.subTest(key=key, task=foreign):
                with self.assertRaises(HTTPException) as ctx:
                    await self._status_as(key, foreign)
                self.assertEqual(ctx.exception.status_code, 404)

        # Ownership is recorded by principal name, never by a key hash.
        stored = await self.db.get_async_task(reels_task, "principal:ai-reels")
        self.assertIsNotNone(stored)
        self.assertNotIn(REELS_KEY, stored.api_key_hash)

    async def test_idempotency_keys_are_scoped_per_principal(self):
        first = await self._submit_as(REELS_KEY, idempotency_key="scene-1")
        other = await self._submit_as(FACTORY_KEY, idempotency_key="scene-1")
        replay = await self._submit_as(REELS_KEY, idempotency_key="scene-1")

        self.assertNotEqual(first, other)
        self.assertEqual(replay, first)

    async def test_legacy_key_finds_a_pre_migration_row_by_raw_key_hash(self):
        pre_migration = AsyncTask(
            task_id="gen_premigration",
            api_key_hash=AsyncTaskManager.hash_api_key(LEGACY_KEY),
            idempotency_key="old-job",
            status="succeeded",
            model="veo_3_1_t2v",
            prompt="a cat",
            result_body="{}",
        )
        await self.db.create_async_task(pre_migration)

        status = await self._status_as(LEGACY_KEY, "gen_premigration")
        self.assertEqual(status["task_id"], "gen_premigration")

        # Idempotent re-submit under the legacy key replays the old row too.
        replay = await self._submit_as(LEGACY_KEY, idempotency_key="old-job")
        self.assertEqual(replay, "gen_premigration")

        # A named principal never inherits hash-owned rows, even with the same raw key value.
        with _ConfigSandbox(legacy="", named={"ai-reels": LEGACY_KEY}):
            with self.assertRaises(HTTPException) as ctx:
                await self._status_as(LEGACY_KEY, "gen_premigration")
            self.assertEqual(ctx.exception.status_code, 404)

    async def test_legacy_principal_writes_new_rows_by_principal(self):
        task_id = await self._submit_as(LEGACY_KEY)
        await self._await_terminal(task_id, LEGACY_PRINCIPAL)

        self.assertIsNotNone(await self.db.get_async_task(task_id, "principal:legacy"))
        self.assertIsNone(
            await self.db.get_async_task(task_id, AsyncTaskManager.hash_api_key(LEGACY_KEY))
        )

    async def test_rotating_a_principals_key_keeps_its_tasks(self):
        task_id = await self._submit_as(REELS_KEY, idempotency_key="scene-7")
        await self._await_terminal(task_id, "ai-reels")

        config.set_api_keys(dict(NAMED_KEYS, **{"ai-reels": "sk-reels-rotated"}))

        status = await self._status_as("sk-reels-rotated", task_id)
        self.assertEqual(status["task_id"], task_id)
        self.assertEqual(status["status"], "succeeded")
        replay = await self._submit_as("sk-reels-rotated", idempotency_key="scene-7")
        self.assertEqual(replay, task_id)

        with self.assertRaises(HTTPException) as ctx:
            await self._status_as(REELS_KEY, task_id)
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_manager_requires_a_principal(self):
        with self.assertRaises(ValueError):
            AsyncTaskManager.principal_owner("")


class _FakeWebSocket:
    def __init__(self, query: dict = None, headers: dict = None):
        self.query_params = query or {}
        self.headers = headers or {}
        self.closed_with = None

    async def close(self, code: int = 1000):
        self.closed_with = code


class CaptchaWebSocketAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_key_is_refused(self):
        with _ConfigSandbox(named=NAMED_KEYS):
            ws = _FakeWebSocket(query={"key": "wrong"})
            await routes.captcha_websocket_endpoint(ws)
        self.assertEqual(ws.closed_with, 1008)

    async def test_unconfigured_auth_refuses_even_the_old_default(self):
        with _ConfigSandbox(legacy="", named={}):
            ws = _FakeWebSocket(headers={"authorization": "Bearer han1234"})
            await routes.captcha_websocket_endpoint(ws)
        self.assertEqual(ws.closed_with, 1008)

    async def test_extension_principal_is_accepted(self):
        accepted = {}

        class _Service:
            async def connect(self, websocket):
                accepted["ws"] = websocket

            def disconnect(self, websocket):
                pass

        async def get_instance(db=None):
            return _Service()

        with _ConfigSandbox(named=NAMED_KEYS):
            ws = _FakeWebSocket(headers={"authorization": f"Bearer {EXTENSION_KEY}"})

            async def receive_text():
                raise routes.WebSocketDisconnect()

            ws.receive_text = receive_text
            with patch.object(routes.ExtensionCaptchaService, "get_instance", get_instance):
                await routes.captcha_websocket_endpoint(ws)

        self.assertIsNone(ws.closed_with)
        self.assertIs(accepted["ws"], ws)


class DatabaseSeedingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(db_path=f"{self._temp_dir.name}/flow.db")
        self._saved = (config.api_key, config.api_keys)

    async def asyncTearDown(self):
        config.api_key = self._saved[0]
        config.set_api_keys(self._saved[1])
        self._temp_dir.cleanup()

    async def test_fresh_database_without_toml_keys_seeds_nothing(self):
        await self.db.init_db()
        await self.db.init_config_from_toml({"global": {"admin_username": "admin", "admin_password": "x"}})

        row = await self.db.get_admin_config()
        self.assertEqual(row.api_key, "")
        self.assertEqual(row.named_api_keys(), {})

        await self.db.reload_config_to_memory()
        self.assertFalse(config.auth_configured)

    async def test_fresh_database_seeds_named_keys_from_toml(self):
        await self.db.init_db()
        await self.db.init_config_from_toml({"global": {"api_key": "", "api_keys": NAMED_KEYS}})

        await self.db.reload_config_to_memory()
        self.assertEqual(config.api_key, "")
        self.assertEqual(config.api_keys, NAMED_KEYS)
        self.assertEqual(config.resolve_principal(FACTORY_KEY), "content-factory")

    async def test_existing_database_gains_the_column_and_syncs_toml_keys(self):
        # A database from before named keys: admin_config without api_keys, seeded
        # with the upstream public default.
        async with self.db._connect(write=True) as conn:
            await conn.execute(
                """
                CREATE TABLE admin_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    username TEXT DEFAULT 'admin',
                    password TEXT DEFAULT 'admin',
                    api_key TEXT DEFAULT 'han1234',
                    error_ban_threshold INTEGER DEFAULT 3,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await conn.execute("INSERT INTO admin_config (id, api_key) VALUES (1, 'han1234')")
            await conn.commit()

        await self.db.init_db()
        await self.db.check_and_migrate_db({"global": {"api_keys": NAMED_KEYS}})

        row = await self.db.get_admin_config()
        self.assertEqual(row.api_key, "han1234")  # existing legacy key is left alone
        self.assertEqual(row.named_api_keys(), {})  # migration itself writes no keys

        self.assertTrue(await self.db.sync_named_api_keys_from_toml({"global": {"api_keys": NAMED_KEYS}}))
        self.assertEqual((await self.db.get_admin_config()).named_api_keys(), NAMED_KEYS)
        # Idempotent on the next restart.
        self.assertFalse(await self.db.sync_named_api_keys_from_toml({"global": {"api_keys": NAMED_KEYS}}))

    async def test_toml_without_named_keys_leaves_database_keys_alone(self):
        await self.db.init_db()
        await self.db.init_config_from_toml({"global": {"api_keys": NAMED_KEYS}})

        self.assertFalse(await self.db.sync_named_api_keys_from_toml({"global": {}}))
        self.assertFalse(await self.db.sync_named_api_keys_from_toml({}))
        self.assertEqual((await self.db.get_admin_config()).named_api_keys(), NAMED_KEYS)

    async def test_corrupt_api_keys_column_reads_as_no_named_keys(self):
        await self.db.init_db()
        await self.db.init_config_from_toml({"global": {"api_keys": NAMED_KEYS}})
        await self.db.update_admin_config(api_keys="not json")

        self.assertEqual((await self.db.get_admin_config()).named_api_keys(), {})

    async def test_default_schema_has_no_public_bootstrap_key(self):
        await self.db.init_db()
        async with self.db._connect(write=True) as conn:
            await conn.execute("INSERT INTO admin_config (id) VALUES (1)")
            await conn.commit()

        row = await self.db.get_admin_config()
        self.assertEqual(row.api_key, "")
        self.assertEqual(row.named_api_keys(), {})


if __name__ == "__main__":
    unittest.main()
