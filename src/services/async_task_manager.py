"""Async generation job lifecycle (submit / status / result).

The public generation endpoints await the whole pipeline inline, so a client
that dies mid-call has no handle to reconnect with and can only re-submit,
paying twice. This manager wraps the same pipeline in a resumable job: the
caller gets a task_id immediately, the generation runs in a background task,
and the final payload is persisted so it survives the client's disconnect.

It owns lifecycle only. The generation itself is supplied by the caller as a
coroutine factory, so the pipeline keeps living in one place (api/routes.py).
"""
import asyncio
import hashlib
import json
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional

import aiosqlite

from ..core.database import Database
from ..core.logger import debug_logger
from ..core.models import AsyncTask

# Terminal job states.
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

TERMINAL_STATUSES = (STATUS_SUCCEEDED, STATUS_FAILED)


class AsyncTaskManager:
    """Runs generations in the background and persists their results."""

    # Results stay readable this long after completion. The contract promises at
    # least one hour; a day costs a few KB per job and covers overnight retries.
    RESULT_RETENTION_HOURS = 24
    CLEANUP_INTERVAL_SECONDS = 1800

    RESTART_ERROR_MESSAGE = "Generation was interrupted by a server restart"
    CANCELLED_ERROR_MESSAGE = "Generation was cancelled while the server shut down"

    def __init__(self, db: Database):
        self.db = db
        # asyncio keeps only weak references to bare tasks, so hold them here or
        # a long generation can be garbage-collected mid-flight.
        self._workers: Dict[str, asyncio.Task] = {}
        self._cleanup_task: Optional[asyncio.Task] = None

    @staticmethod
    def hash_api_key(api_key: str) -> str:
        """Scope key for a job. Hashed so the API key never lands in the DB."""
        return hashlib.sha256(api_key.encode()).hexdigest()

    @staticmethod
    def _new_task_id() -> str:
        return f"gen_{uuid.uuid4().hex}"

    async def submit(
        self,
        *,
        api_key: str,
        model: str,
        prompt: str,
        run: Callable[[], Awaitable[str]],
        response_format: str = "openai",
        idempotency_key: Optional[str] = None,
    ) -> tuple[AsyncTask, bool]:
        """Start a generation and return (job, replayed) immediately.

        When idempotency_key matches an existing job for this API key, that job
        is returned with replayed=True and nothing new is started.
        """
        api_key_hash = self.hash_api_key(api_key)

        if idempotency_key:
            existing = await self.db.get_async_task_by_idempotency_key(api_key_hash, idempotency_key)
            if existing:
                debug_logger.log_info(
                    f"[ASYNC] 幂等命中，复用任务: {existing.task_id} (status={existing.status})"
                )
                return existing, True

        task = AsyncTask(
            task_id=self._new_task_id(),
            api_key_hash=api_key_hash,
            idempotency_key=idempotency_key,
            status=STATUS_QUEUED,
            response_format=response_format,
            model=model,
            prompt=prompt,
        )

        try:
            await self.db.create_async_task(task)
        except aiosqlite.IntegrityError:
            # Two submits with the same idempotency key raced; the loser returns
            # the winner's job rather than starting a second generation.
            if idempotency_key:
                existing = await self.db.get_async_task_by_idempotency_key(api_key_hash, idempotency_key)
                if existing:
                    debug_logger.log_info(
                        f"[ASYNC] 幂等竞争，复用已有任务: {existing.task_id}"
                    )
                    return existing, True
            raise

        # Read back so the caller sees the stored row, including the timestamps
        # SQLite fills in.
        stored = await self.db.get_async_task(task.task_id, api_key_hash) or task

        worker = asyncio.create_task(self._run_task(task.task_id, run))
        self._workers[task.task_id] = worker
        worker.add_done_callback(lambda _, tid=task.task_id: self._workers.pop(tid, None))

        debug_logger.log_info(f"[ASYNC] 已提交任务 {task.task_id} (model={model})")
        return stored, False

    async def get(self, task_id: str, api_key: str) -> Optional[AsyncTask]:
        """Read a job, scoped to the API key that created it."""
        return await self.db.get_async_task(task_id, self.hash_api_key(api_key))

    async def find_by_idempotency_key(self, api_key: str, idempotency_key: str) -> Optional[AsyncTask]:
        """Find this key's existing job for an idempotency key, if any."""
        return await self.db.get_async_task_by_idempotency_key(
            self.hash_api_key(api_key), idempotency_key
        )

    async def _run_task(self, task_id: str, run: Callable[[], Awaitable[str]]):
        """Background worker: run the generation and record its outcome."""
        await self.db.mark_async_task_running(task_id)

        try:
            result = await run()
        except asyncio.CancelledError:
            # Never touch the database while cancelled: an aiosqlite connection
            # opened here would be abandoned mid-handshake. shutdown() records
            # the failure instead, and startup sweeps anything a crash missed.
            raise
        except Exception as exc:
            error_message = self._describe_exception(exc)
            debug_logger.log_error(f"[ASYNC] 任务 {task_id} 失败: {error_message}")
            await self._finish_quietly(task_id, STATUS_FAILED, None, error_message)
            return

        status, error_message = self._classify_result(result)
        await self._finish_quietly(task_id, status, result, error_message)
        debug_logger.log_info(f"[ASYNC] 任务 {task_id} 结束: {status}")

    async def _finish_quietly(
        self,
        task_id: str,
        status: str,
        result_body: Optional[str],
        error_message: Optional[str],
    ):
        """Persist a terminal state; never let a DB error escape the worker."""
        try:
            await self.db.finish_async_task(
                task_id,
                status=status,
                result_body=result_body,
                error_message=error_message,
            )
        except Exception as exc:
            debug_logger.log_error(
                f"[ASYNC] 任务 {task_id} 状态写入失败: {type(exc).__name__}: {exc}"
            )

    @staticmethod
    def _describe_exception(exc: Exception) -> str:
        # HTTPException carries the useful text in .detail, not in str(exc).
        detail = getattr(exc, "detail", None)
        if detail:
            return str(detail)
        return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__

    @staticmethod
    def _classify_result(result: str) -> tuple[str, Optional[str]]:
        """Map a handler payload to a job status.

        The pipeline reports failures as a normal JSON payload with an "error"
        object rather than by raising, so the body has to be inspected.
        """
        try:
            payload: Any = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return STATUS_SUCCEEDED, None

        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            return STATUS_FAILED, payload["error"].get("message", "Generation failed")

        return STATUS_SUCCEEDED, None

    async def fail_interrupted_tasks(self) -> int:
        """Fail jobs orphaned by a restart. Call once at startup."""
        failed = await self.db.fail_unfinished_async_tasks(self.RESTART_ERROR_MESSAGE)
        if failed:
            debug_logger.log_warning(f"[ASYNC] 启动时清理 {failed} 个中断的任务")
        return failed

    def running_task_ids(self) -> list:
        """Task ids whose workers are still alive in this process."""
        return list(self._workers)

    async def start_cleanup_task(self) -> bool:
        """Start the retention sweep for finished jobs."""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        return True

    async def stop_cleanup_task(self):
        """Stop the retention sweep."""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

    async def shutdown(self):
        """Cancel in-flight workers, then record them as failed.

        The status write happens here rather than inside the cancelled workers
        so it runs on a coroutine that is not itself being torn down.
        """
        await self.stop_cleanup_task()

        workers = dict(self._workers)
        if not workers:
            return

        for worker in workers.values():
            worker.cancel()
        await asyncio.gather(*workers.values(), return_exceptions=True)
        self._workers.clear()

        try:
            failed = await self.db.fail_unfinished_async_tasks(
                self.CANCELLED_ERROR_MESSAGE, task_ids=list(workers)
            )
            if failed:
                debug_logger.log_warning(f"[ASYNC] 关机时中止 {failed} 个进行中的任务")
        except Exception as exc:
            # Startup sweeps whatever this missed, so a failure here is not fatal.
            debug_logger.log_error(
                f"[ASYNC] 关机时写入任务状态失败: {type(exc).__name__}: {exc}"
            )

    async def _cleanup_loop(self):
        """Background task pruning finished jobs past their retention window."""
        while True:
            try:
                await asyncio.sleep(self.CLEANUP_INTERVAL_SECONDS)
                deleted = await self.db.delete_old_async_tasks(self.RESULT_RETENTION_HOURS)
                if deleted:
                    debug_logger.log_info(f"[ASYNC] 清理了 {deleted} 个过期任务结果")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                debug_logger.log_error(
                    f"[ASYNC] 任务清理失败: {type(exc).__name__}: {exc}"
                )
