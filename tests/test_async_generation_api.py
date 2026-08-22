import asyncio
import inspect
import json
import tempfile
import types
import unittest

import aiosqlite

from src.api import routes
from src.core.auth import verify_api_key_flexible
from src.core.database import Database
from src.core.models import AsyncGenerationRequest, AsyncTask, GeminiGenerateContentRequest
from src.services.async_task_manager import AsyncTaskManager

# Callers are identified by principal name; the auth dependency resolves the
# presented key to one of these before any route code runs.
PRINCIPAL = "ai-reels"
OTHER_PRINCIPAL = "content-factory"
VIDEO_OUTPUT = '<video src="http://localhost:8000/tmp/clip.mp4" controls></video>'


def _openai_payload(content: str) -> str:
    return json.dumps(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "model": "veo_3_1_t2v",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        }
    )


def _error_payload(message: str, status_code: int) -> str:
    return json.dumps(
        {
            "error": {
                "message": message,
                "type": "server_error",
                "code": "generation_failed",
                "status_code": status_code,
            }
        }
    )


class FakeGenerationHandler:
    """Stands in for the real pipeline: records calls, yields a canned payload."""

    def __init__(self, payload: str = None, raises: Exception = None, gate: asyncio.Event = None):
        self.payload = payload if payload is not None else _openai_payload(VIDEO_OUTPUT)
        self.raises = raises
        self.gate = gate
        self.calls = []

    async def handle_generation(self, **kwargs):
        self.calls.append(kwargs)
        if self.gate is not None:
            await self.gate.wait()
        if self.raises is not None:
            raise self.raises
        yield self.payload


def _fake_request(headers: dict = None) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        headers=headers or {"host": "localhost:8000"},
        url=types.SimpleNamespace(scheme="http"),
    )


def _response_json(response) -> dict:
    return json.loads(response.body)


class AsyncTaskDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(db_path=f"{self._temp_dir.name}/flow.db")
        await self.db.init_db()

    async def asyncTearDown(self):
        self._temp_dir.cleanup()

    def _task(self, task_id: str, api_key_hash: str = "hash-a", idempotency_key: str = None) -> AsyncTask:
        return AsyncTask(
            task_id=task_id,
            api_key_hash=api_key_hash,
            idempotency_key=idempotency_key,
            status="queued",
            model="veo_3_1_t2v",
            prompt="a cat",
        )

    async def test_task_read_is_scoped_to_the_creating_api_key(self):
        await self.db.create_async_task(self._task("gen_1", api_key_hash="hash-a"))

        self.assertIsNotNone(await self.db.get_async_task("gen_1", "hash-a"))
        self.assertIsNone(await self.db.get_async_task("gen_1", "hash-b"))

    async def test_duplicate_idempotency_key_is_rejected_per_api_key(self):
        await self.db.create_async_task(self._task("gen_1", "hash-a", idempotency_key="job-1"))

        with self.assertRaises(aiosqlite.IntegrityError):
            await self.db.create_async_task(self._task("gen_2", "hash-a", idempotency_key="job-1"))

        # The same idempotency key under a different API key is a different job.
        await self.db.create_async_task(self._task("gen_3", "hash-b", idempotency_key="job-1"))
        self.assertEqual(
            (await self.db.get_async_task_by_idempotency_key("hash-b", "job-1")).task_id,
            "gen_3",
        )

    async def test_submissions_without_idempotency_key_are_never_deduplicated(self):
        await self.db.create_async_task(self._task("gen_1", "hash-a"))
        await self.db.create_async_task(self._task("gen_2", "hash-a"))

        self.assertIsNotNone(await self.db.get_async_task("gen_2", "hash-a"))

    async def test_fail_unfinished_tasks_only_touches_queued_and_running(self):
        await self.db.create_async_task(self._task("gen_queued"))
        await self.db.create_async_task(self._task("gen_running"))
        await self.db.create_async_task(self._task("gen_done"))
        await self.db.mark_async_task_running("gen_running")
        await self.db.finish_async_task("gen_done", status="succeeded", result_body="{}")

        failed = await self.db.fail_unfinished_async_tasks("restarted")

        self.assertEqual(failed, 2)
        self.assertEqual((await self.db.get_async_task("gen_queued", "hash-a")).status, "failed")
        self.assertEqual((await self.db.get_async_task("gen_running", "hash-a")).status, "failed")
        self.assertEqual((await self.db.get_async_task("gen_done", "hash-a")).status, "succeeded")

    async def test_retention_keeps_recent_results_and_unfinished_jobs(self):
        await self.db.create_async_task(self._task("gen_fresh"))
        await self.db.create_async_task(self._task("gen_stale"))
        await self.db.create_async_task(self._task("gen_running"))
        await self.db.finish_async_task("gen_fresh", status="succeeded", result_body="{}")
        await self.db.finish_async_task("gen_stale", status="succeeded", result_body="{}")
        await self.db.mark_async_task_running("gen_running")

        # Age the stale job past the retention window.
        async with self.db._connect(write=True) as conn:
            await conn.execute(
                "UPDATE async_tasks SET completed_at = datetime('now', '-48 hours') WHERE task_id = 'gen_stale'"
            )
            await conn.commit()

        deleted = await self.db.delete_old_async_tasks(24)

        self.assertEqual(deleted, 1)
        self.assertIsNone(await self.db.get_async_task("gen_stale", "hash-a"))
        self.assertIsNotNone(await self.db.get_async_task("gen_fresh", "hash-a"))
        self.assertIsNotNone(await self.db.get_async_task("gen_running", "hash-a"))

    async def test_results_outlive_the_one_hour_contract(self):
        await self.db.create_async_task(self._task("gen_old"))
        await self.db.finish_async_task("gen_old", status="succeeded", result_body="{}")
        async with self.db._connect(write=True) as conn:
            await conn.execute(
                "UPDATE async_tasks SET completed_at = datetime('now', '-90 minutes') WHERE task_id = 'gen_old'"
            )
            await conn.commit()

        await self.db.delete_old_async_tasks(AsyncTaskManager.RESULT_RETENTION_HOURS)

        self.assertIsNotNone(await self.db.get_async_task("gen_old", "hash-a"))


class AsyncTaskManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(db_path=f"{self._temp_dir.name}/flow.db")
        await self.db.init_db()
        self.manager = AsyncTaskManager(self.db)

    async def asyncTearDown(self):
        await self.manager.shutdown()
        self._temp_dir.cleanup()

    async def _submit(self, run, idempotency_key=None, principal=PRINCIPAL):
        return await self.manager.submit(
            principal=principal,
            model="veo_3_1_t2v",
            prompt="a cat",
            run=run,
            idempotency_key=idempotency_key,
        )

    async def _await_terminal(self, task_id: str, principal: str = PRINCIPAL) -> AsyncTask:
        for _ in range(200):
            task = await self.manager.get(task_id, principal)
            if task.status in ("succeeded", "failed"):
                return task
            await asyncio.sleep(0.01)
        self.fail(f"Task {task_id} never reached a terminal state")

    async def test_successful_run_stores_the_payload(self):
        payload = _openai_payload(VIDEO_OUTPUT)

        async def run():
            return payload

        task, replayed = await self._submit(run)
        self.assertFalse(replayed)
        self.assertEqual(task.status, "queued")
        # The submit response reflects the stored row, not just the in-memory one.
        self.assertIsNotNone(task.created_at)

        finished = await self._await_terminal(task.task_id)
        self.assertEqual(finished.status, "succeeded")
        self.assertEqual(finished.result_body, payload)
        self.assertIsNone(finished.error_message)

    async def test_error_payload_is_recorded_as_failed(self):
        async def run():
            return _error_payload("No available token", 503)

        task, _ = await self._submit(run)

        finished = await self._await_terminal(task.task_id)
        self.assertEqual(finished.status, "failed")
        self.assertEqual(finished.error_message, "No available token")
        # The body is kept so the result endpoint can replay the sync error shape.
        self.assertIn("503", finished.result_body)

    async def test_raised_exception_is_recorded_as_failed(self):
        async def run():
            raise RuntimeError("upstream exploded")

        task, _ = await self._submit(run)

        finished = await self._await_terminal(task.task_id)
        self.assertEqual(finished.status, "failed")
        self.assertIn("upstream exploded", finished.error_message)
        self.assertIsNone(finished.result_body)

    async def test_idempotent_resubmit_reuses_the_task_and_runs_once(self):
        runs = []

        async def run():
            runs.append(1)
            return _openai_payload(VIDEO_OUTPUT)

        first, first_replayed = await self._submit(run, idempotency_key="job-1")
        await self._await_terminal(first.task_id)
        second, second_replayed = await self._submit(run, idempotency_key="job-1")

        self.assertFalse(first_replayed)
        self.assertTrue(second_replayed)
        self.assertEqual(second.task_id, first.task_id)
        self.assertEqual(len(runs), 1)

    async def test_idempotency_is_scoped_per_principal(self):
        async def run():
            return _openai_payload(VIDEO_OUTPUT)

        first, _ = await self._submit(run, idempotency_key="job-1", principal=PRINCIPAL)
        second, replayed = await self._submit(run, idempotency_key="job-1", principal=OTHER_PRINCIPAL)

        self.assertNotEqual(second.task_id, first.task_id)
        self.assertFalse(replayed)

    async def test_task_ids_are_unguessable(self):
        async def run():
            return _openai_payload(VIDEO_OUTPUT)

        task, _ = await self._submit(run)

        self.assertTrue(task.task_id.startswith("gen_"))
        self.assertEqual(len(task.task_id), len("gen_") + 32)

    async def test_other_principal_cannot_read_a_task(self):
        async def run():
            return _openai_payload(VIDEO_OUTPUT)

        task, _ = await self._submit(run)

        self.assertIsNone(await self.manager.get(task.task_id, OTHER_PRINCIPAL))

    async def test_new_rows_are_owned_by_principal_not_key_hash(self):
        async def run():
            return _openai_payload(VIDEO_OUTPUT)

        task, _ = await self._submit(run)

        stored = await self.db.get_async_task(task.task_id, "principal:" + PRINCIPAL)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.api_key_hash, "principal:" + PRINCIPAL)


class AsyncGenerationRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(db_path=f"{self._temp_dir.name}/flow.db")
        await self.db.init_db()
        self.manager = AsyncTaskManager(self.db)
        self._previous_handler = routes.generation_handler
        self._previous_manager = routes.async_task_manager
        routes.set_async_task_manager(self.manager)

    async def asyncTearDown(self):
        await self.manager.shutdown()
        routes.set_generation_handler(self._previous_handler)
        routes.set_async_task_manager(self._previous_manager)
        self._temp_dir.cleanup()

    def _install_handler(self, **kwargs) -> FakeGenerationHandler:
        handler = FakeGenerationHandler(**kwargs)
        routes.set_generation_handler(handler)
        return handler

    async def _submit_openai(self, prompt="a cat", idempotency_key=None, body_key=None):
        request = AsyncGenerationRequest(
            model="veo_3_1_t2v",
            messages=[{"role": "user", "content": prompt}],
            idempotency_key=body_key,
        )
        return await routes.submit_async_chat_completion(
            request=request,
            raw_request=_fake_request(),
            idempotency_key=idempotency_key,
            principal=PRINCIPAL,
        )

    async def _await_terminal(self, task_id: str, principal: str = PRINCIPAL):
        for _ in range(200):
            task = await self.manager.get(task_id, principal)
            if task.status in ("succeeded", "failed"):
                return task
            await asyncio.sleep(0.01)
        self.fail(f"Task {task_id} never reached a terminal state")

    async def test_submit_returns_202_with_a_task_handle(self):
        self._install_handler()

        response = await self._submit_openai()
        body = _response_json(response)

        self.assertEqual(response.status_code, 202)
        self.assertIn(body["status"], ("queued", "running"))
        self.assertTrue(body["task_id"].startswith("gen_"))
        await self._await_terminal(body["task_id"])

    async def test_submit_rejects_an_empty_prompt_like_the_sync_endpoint(self):
        self._install_handler()

        with self.assertRaises(routes.HTTPException) as ctx:
            await self._submit_openai(prompt="")

        self.assertEqual(ctx.exception.status_code, 400)

    async def test_submit_rejects_an_unsupported_model_without_creating_a_job(self):
        handler = self._install_handler()
        request = AsyncGenerationRequest(
            model="not_a_real_model",
            messages=[{"role": "user", "content": "a cat"}],
            idempotency_key="job-typo",
        )

        with self.assertRaises(routes.HTTPException) as ctx:
            await routes.submit_async_chat_completion(
                request=request,
                raw_request=_fake_request(),
                idempotency_key=None,
                principal=PRINCIPAL,
            )

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(len(handler.calls), 0)
        # The idempotency key stays free for a corrected re-submit.
        self.assertIsNone(await self.manager.find_by_idempotency_key(PRINCIPAL, "job-typo"))

    async def test_result_is_425_until_the_job_finishes(self):
        gate = asyncio.Event()
        self._install_handler(gate=gate)

        submit_body = _response_json(await self._submit_openai())
        task_id = submit_body["task_id"]

        pending = await routes.get_async_task_result(task_id=task_id, principal=PRINCIPAL)
        self.assertEqual(pending.status_code, 425)
        self.assertEqual(pending.headers["retry-after"], str(routes.ASYNC_RESULT_RETRY_AFTER_SECONDS))

        gate.set()
        await self._await_terminal(task_id)

        finished = await routes.get_async_task_result(task_id=task_id, principal=PRINCIPAL)
        self.assertEqual(finished.status_code, 200)
        self.assertEqual(
            _response_json(finished)["choices"][0]["message"]["content"],
            VIDEO_OUTPUT,
        )

    async def test_status_reports_the_lifecycle(self):
        gate = asyncio.Event()
        self._install_handler(gate=gate)

        task_id = _response_json(await self._submit_openai())["task_id"]

        status = await routes.get_async_task_status(task_id=task_id, principal=PRINCIPAL)
        self.assertIn(status["status"], ("queued", "running"))
        self.assertNotIn("error", status)

        gate.set()
        await self._await_terminal(task_id)

        status = await routes.get_async_task_status(task_id=task_id, principal=PRINCIPAL)
        self.assertEqual(status["status"], "succeeded")
        self.assertIsNotNone(status["completed_at"])

    async def test_failed_job_replays_the_sync_error_shape_and_status(self):
        self._install_handler(payload=_error_payload("No available token", 503))

        task_id = _response_json(await self._submit_openai())["task_id"]
        await self._await_terminal(task_id)

        status = await routes.get_async_task_status(task_id=task_id, principal=PRINCIPAL)
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error"], "No available token")

        result = await routes.get_async_task_result(task_id=task_id, principal=PRINCIPAL)
        self.assertEqual(result.status_code, 503)
        self.assertEqual(_response_json(result)["error"]["message"], "No available token")

    async def test_crashed_pipeline_surfaces_as_a_500_result(self):
        self._install_handler(raises=RuntimeError("upstream exploded"))

        task_id = _response_json(await self._submit_openai())["task_id"]
        await self._await_terminal(task_id)

        result = await routes.get_async_task_result(task_id=task_id, principal=PRINCIPAL)
        self.assertEqual(result.status_code, 500)
        self.assertIn("upstream exploded", _response_json(result)["error"]["message"])

    async def test_unknown_task_and_other_key_both_return_404(self):
        self._install_handler()
        task_id = _response_json(await self._submit_openai())["task_id"]
        await self._await_terminal(task_id)

        with self.assertRaises(routes.HTTPException) as unknown:
            await routes.get_async_task_status(task_id="gen_does_not_exist", principal=PRINCIPAL)
        self.assertEqual(unknown.exception.status_code, 404)

        with self.assertRaises(routes.HTTPException) as foreign:
            await routes.get_async_task_status(task_id=task_id, principal=OTHER_PRINCIPAL)
        self.assertEqual(foreign.exception.status_code, 404)

    async def test_idempotent_resubmit_returns_the_same_task_without_regenerating(self):
        handler = self._install_handler()

        first = _response_json(await self._submit_openai(idempotency_key="job-1"))
        await self._await_terminal(first["task_id"])
        replay = await self._submit_openai(idempotency_key="job-1")
        replay_body = _response_json(replay)

        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay_body["replayed"])
        self.assertEqual(replay_body["task_id"], first["task_id"])
        self.assertEqual(replay_body["status"], "succeeded")
        self.assertEqual(len(handler.calls), 1)

    async def test_idempotency_key_is_accepted_in_the_body_too(self):
        handler = self._install_handler()

        first = _response_json(await self._submit_openai(body_key="job-2"))
        await self._await_terminal(first["task_id"])
        replay = _response_json(await self._submit_openai(body_key="job-2"))

        self.assertEqual(replay["task_id"], first["task_id"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(len(handler.calls), 1)

    async def test_gemini_submit_returns_the_gemini_result_shape(self):
        handler = self._install_handler()
        request = GeminiGenerateContentRequest(contents=[{"role": "user", "parts": [{"text": "a cat"}]}])

        submit = await routes.submit_async_generate_content(
            model="veo_3_1_t2v",
            request=request,
            raw_request=_fake_request(),
            idempotency_key="gem-1",
            principal=PRINCIPAL,
        )
        submit_body = _response_json(submit)
        self.assertEqual(submit.status_code, 202)
        await self._await_terminal(submit_body["task_id"])

        result = await routes.get_async_task_result(task_id=submit_body["task_id"], principal=PRINCIPAL)
        body = _response_json(result)

        self.assertEqual(result.status_code, 200)
        self.assertEqual(body["candidates"][0]["content"]["parts"][0]["fileData"]["fileUri"],
                         "http://localhost:8000/tmp/clip.mp4")
        # The job records the resolved model (veo_3_1_t2v -> ..._landscape), same
        # as the sync endpoint reports in modelVersion.
        self.assertEqual(body["modelVersion"], handler.calls[0]["model"])
        self.assertTrue(body["modelVersion"].startswith("veo_3_1_t2v"))

    async def test_gemini_failure_uses_the_gemini_error_shape(self):
        self._install_handler(payload=_error_payload("No available token", 503))
        request = GeminiGenerateContentRequest(contents=[{"role": "user", "parts": [{"text": "a cat"}]}])

        submit_body = _response_json(
            await routes.submit_async_generate_content(
                model="veo_3_1_t2v",
                request=request,
                raw_request=_fake_request(),
                idempotency_key=None,
                principal=PRINCIPAL,
            )
        )
        await self._await_terminal(submit_body["task_id"])

        result = await routes.get_async_task_result(task_id=submit_body["task_id"], principal=PRINCIPAL)
        body = _response_json(result)

        self.assertEqual(result.status_code, 503)
        self.assertEqual(body["error"]["status"], "UNAVAILABLE")
        self.assertEqual(body["error"]["message"], "No available token")

    async def test_submit_forwards_pool_and_base_url_to_the_pipeline(self):
        handler = self._install_handler()
        request = AsyncGenerationRequest(
            model="veo_3_1_t2v",
            messages=[{"role": "user", "content": "a cat"}],
        )

        body = _response_json(
            await routes.submit_async_chat_completion(
                request=request,
                raw_request=_fake_request({"host": "flow.example.com", "x-flow-pool": "failed_image"}),
                idempotency_key=None,
                principal=PRINCIPAL,
            )
        )
        await self._await_terminal(body["task_id"])

        self.assertEqual(handler.calls[0]["pool"], "failed_image")
        self.assertEqual(handler.calls[0]["base_url_override"], "http://flow.example.com")
        self.assertFalse(handler.calls[0]["stream"])


class AsyncEndpointAuthTests(unittest.TestCase):
    """The async endpoints must use the same auth dependency as the sync ones."""

    def test_every_async_endpoint_requires_the_shared_api_key_dependency(self):
        endpoints = (
            routes.submit_async_chat_completion,
            routes.submit_async_generate_content,
            routes.get_async_task_status,
            routes.get_async_task_result,
        )

        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint.__name__):
                principal_param = inspect.signature(endpoint).parameters["principal"]
                self.assertIs(principal_param.default.dependency, verify_api_key_flexible)


if __name__ == "__main__":
    unittest.main()
