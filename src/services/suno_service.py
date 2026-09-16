"""Suno provider: durable job store, dispatcher and poller.

Design notes that are easy to get wrong, all of them deliberate:

* A job that is blocked by the pre-flight captcha check has sent nothing
  upstream, so it releases its dispatch slot. A job whose generate call gave an
  ambiguous answer keeps its slot, because the work may exist and be billed.
* One generation returns an A/B pair. The job settles only once *every* clip is
  terminal, so a finished job never abandons a still-rendering sibling and one
  failed clip never fails a job whose other clip worked.
* Session refresh runs as a shared task that owns the per-account lock through
  both the refresh and the durable save, so a cancelled caller cannot leave a
  half-rotated Clerk cookie behind.
* Account selection, capacity check and the job claim share one write
  transaction; every network call happens outside it.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import aiosqlite

from ..core.logger import debug_logger
from ..core import suno_models as sm
from ..core.suno_models import (
    SunoConflictError,
    SunoValidationError,
)
from .suno_client import (
    SunoAPIError,
    SunoAuthError,
    SunoClient,
    SunoRateLimited,
    SunoSession,
    parse_cookie_string,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS suno_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    upstream_user_id TEXT UNIQUE,
    display_name TEXT,
    cookies TEXT NOT NULL,
    clerk_sid TEXT,
    status TEXT NOT NULL DEFAULT 'ready',
    operator_disabled INTEGER NOT NULL DEFAULT 0,
    cooldown_until REAL,
    captcha_paused_until REAL,
    plan TEXT,
    credits INTEGER,
    credits_checked_at REAL,
    provider_cap INTEGER,
    operator_limit INTEGER NOT NULL DEFAULT 2,
    last_refresh_at REAL,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS suno_jobs (
    id TEXT PRIMARY KEY,
    account_id INTEGER,
    state TEXT NOT NULL,
    attempt_id TEXT,
    transaction_uuid TEXT,
    idempotency_key TEXT,
    request_hash TEXT NOT NULL,
    request_json TEXT NOT NULL,
    clip_ids TEXT,
    clips_json TEXT,
    error TEXT,
    retrieval_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    submitted_at REAL,
    completed_at REAL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_suno_jobs_idempotency
    ON suno_jobs(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_suno_jobs_state_account ON suno_jobs(state, account_id);
CREATE INDEX IF NOT EXISTS idx_suno_jobs_created_at ON suno_jobs(created_at DESC);
"""


def _now() -> float:
    return time.time()


def _request_hash(request: Dict[str, Any]) -> str:
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _loads(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class SunoService:
    """Owns Suno accounts and jobs. One instance per process."""

    #: Global admission bound. Jobs are assigned an account at dispatch, not at
    #: admission, so the queue cannot be bounded per account.
    MAX_ADMITTED_JOBS = 50
    #: Concurrent audio proxy streams across all callers.
    MAX_AUDIO_STREAMS = 4
    DISPATCH_INTERVAL = 2.0
    POLL_INTERVAL = 6.0
    #: How long an account stops taking new work after a captcha block.
    CAPTCHA_PAUSE_SECONDS = 300.0
    RATE_LIMIT_COOLDOWN_SECONDS = 120.0
    #: Bounded attempts to turn completed clips into a resolvable download.
    MAX_FINALIZE_ATTEMPTS = 5

    def __init__(self, db, proxy_manager=None, client: Optional[SunoClient] = None,
                 captcha_provider=None):
        self.db = db
        self.client = client or SunoClient(proxy_manager)
        #: Optional hook that can mint a generation captcha token. None in v1:
        #: a challenged job is blocked rather than paid for or faked.
        self.captcha_provider = captcha_provider

        self._started = False
        self._sessions: Dict[int, SunoSession] = {}
        self._account_locks: Dict[int, asyncio.Lock] = {}
        self._refresh_tasks: Dict[int, asyncio.Task] = {}
        self._running_jobs: Dict[str, asyncio.Task] = {}
        self._finalize_attempts: Dict[str, int] = {}
        self._audio_slots = asyncio.Semaphore(self.MAX_AUDIO_STREAMS)
        self._dispatch_task: Optional[asyncio.Task] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if self._started:
            return
        async with self.db.connect(write=True) as conn:
            await conn.executescript(_SCHEMA)
            await conn.commit()
        recovered = await self._recover_after_restart()
        self._started = True
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
        self._poll_task = asyncio.create_task(self._poll_loop())
        debug_logger.log_info(
            f"[SUNO] service started (requeued={recovered['requeued']}, "
            f"resumed={recovered['resumed']})"
        )

    async def close(self) -> None:
        self._started = False
        for task in [self._dispatch_task, self._poll_task, *self._refresh_tasks.values(),
                     *self._running_jobs.values()]:
            if task and not task.done():
                task.cancel()
        pending = [t for t in [self._dispatch_task, self._poll_task,
                               *self._refresh_tasks.values(), *self._running_jobs.values()] if t]
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._refresh_tasks.clear()
        self._running_jobs.clear()

    async def _recover_after_restart(self) -> Dict[str, int]:
        """A restart loses in-flight tasks, not upstream work.

        ``submitting`` is the dangerous one: the generate call may or may not
        have reached Suno, so those jobs go to ``needs_review`` rather than back
        in the queue. ``submitted``/``finalizing`` simply resume polling.
        """
        now = _now()
        async with self.db.connect(write=True) as conn:
            cursor = await conn.execute(
                "UPDATE suno_jobs SET state = ?, error = ?, updated_at = ? WHERE state = ?",
                (sm.NEEDS_REVIEW,
                 "Backend restarted while submitting; upstream state unknown.",
                 now, sm.SUBMITTING),
            )
            requeued = cursor.rowcount or 0
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM suno_jobs WHERE state IN (?, ?)",
                (sm.SUBMITTED, sm.FINALIZING),
            )
            row = await cursor.fetchone()
            resumed = int(row[0]) if row else 0
            await conn.commit()
        return {"requeued": requeued, "resumed": resumed}

    def _ensure_started(self) -> None:
        if not self._started:
            raise SunoValidationError("service_unavailable", "The Suno provider is not running.")

    # ----------------------------------------------------------- account I/O

    def _lock_for(self, account_id: int) -> asyncio.Lock:
        lock = self._account_locks.get(account_id)
        if lock is None:
            lock = asyncio.Lock()
            self._account_locks[account_id] = lock
        return lock

    async def _fetch_account(self, account_id: int) -> Optional[Dict[str, Any]]:
        async with self.db.connect() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM suno_accounts WHERE id = ?", (account_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def _update_account(self, account_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        async with self.db.connect(write=True) as conn:
            await conn.execute(
                f"UPDATE suno_accounts SET {assignments} WHERE id = ?",
                (*fields.values(), account_id),
            )
            await conn.commit()

    @staticmethod
    def _public_account(row: Dict[str, Any], active_jobs: int = 0) -> Dict[str, Any]:
        """Projection for API responses. Never includes cookies, sid or JWT."""
        return {
            "id": row["id"],
            "upstream_user_id": row.get("upstream_user_id"),
            "display_name": row.get("display_name"),
            "status": row.get("status"),
            "operator_disabled": bool(row.get("operator_disabled")),
            "cooldown_until": row.get("cooldown_until"),
            "captcha_paused_until": row.get("captcha_paused_until"),
            "plan": row.get("plan"),
            "credits": row.get("credits"),
            "credits_checked_at": row.get("credits_checked_at"),
            "provider_cap": row.get("provider_cap"),
            "operator_limit": row.get("operator_limit"),
            "effective_limit": SunoService._effective_limit(row),
            "active_jobs": active_jobs,
            "last_refresh_at": row.get("last_refresh_at"),
            "last_error": row.get("last_error"),
        }

    @staticmethod
    def _effective_limit(row: Dict[str, Any]) -> int:
        """Concurrency for one account.

        An unverified provider cap means one at a time. Suno's plans advertise
        more, but an advertised number is not a measured one, and over-issuing
        costs credits.
        """
        operator_limit = int(row.get("operator_limit") or 1)
        provider_cap = row.get("provider_cap")
        if provider_cap is None:
            return 1
        return max(1, min(operator_limit, int(provider_cap)))

    # -------------------------------------------------------- session refresh

    async def _refresh_worker(self, account_id: int) -> SunoSession:
        """Refresh one account's Clerk session and persist the result.

        The lock is taken *inside* the task so it covers the refresh and the
        durable save together. Callers await this through ``asyncio.shield``, so
        a cancelled caller never interrupts a rotation mid-flight.
        """
        async with self._lock_for(account_id):
            row = await self._fetch_account(account_id)  # re-read after acquiring
            if not row:
                raise SunoValidationError("unknown_account", f"Suno account {account_id} is gone.")

            cached = self._sessions.get(account_id)
            if cached and cached.jwt and (_now() - cached.jwt_obtained_at) <= self.client.JWT_TTL_SECONDS:
                return cached  # someone refreshed while we queued

            session = SunoSession(
                parse_cookie_string(row["cookies"] or ""), sid=row.get("clerk_sid")
            )
            try:
                await self.client.refresh_session(session)
            except SunoAuthError as exc:
                await self._update_account(
                    account_id, status=sm.ACCOUNT_NEEDS_LOGIN, last_error=str(exc)[:500]
                )
                self._sessions.pop(account_id, None)
                raise
            except SunoAPIError as exc:
                await self._update_account(
                    account_id, status=sm.ACCOUNT_COOLDOWN,
                    cooldown_until=_now() + self.RATE_LIMIT_COOLDOWN_SECONDS,
                    last_error=str(exc)[:500],
                )
                raise

            # Persist the rotated cookie jar and sid BEFORE anything else uses
            # them: Clerk invalidates the previous __client on rotation.
            await self._update_account(
                account_id,
                cookies=session.cookie_string(),
                clerk_sid=session.sid,
                last_refresh_at=_now(),
                last_error=None,
                status=sm.ACCOUNT_READY if row.get("status") == sm.ACCOUNT_NEEDS_LOGIN
                else row.get("status"),
            )
            self._sessions[account_id] = session
            return session

    async def _session_for(self, account_id: int) -> SunoSession:
        cached = self._sessions.get(account_id)
        if cached and cached.jwt and (_now() - cached.jwt_obtained_at) <= self.client.JWT_TTL_SECONDS:
            return cached

        task = self._refresh_tasks.get(account_id)
        if task is None or task.done():
            task = asyncio.create_task(self._refresh_worker(account_id))
            self._refresh_tasks[account_id] = task
        return await asyncio.shield(task)

    # ------------------------------------------------------ account commands

    async def import_account(self, cookie_string: str, display_name: str = "",
                             account_id: Optional[int] = None) -> Dict[str, Any]:
        """Add a Suno account, or replace an existing account's credentials.

        Replacement is serialized against refresh on the same lock and refuses a
        cookie belonging to a different upstream user: a different account
        cannot inherit this one's jobs or its audio.
        """
        self._ensure_started()
        jar = parse_cookie_string(cookie_string or "")
        if not jar.get("__client"):
            raise SunoValidationError(
                "invalid_cookie",
                "That cookie string has no '__client' entry. Copy the whole Cookie header "
                "from a signed-in suno.com request.",
            )

        session = SunoSession(jar)
        try:
            await self.client.refresh_session(session)
            info = await self.client.session_info(session)
        except SunoAuthError as exc:
            raise SunoValidationError("cookie_rejected", f"Suno rejected that cookie: {exc}")

        upstream_user_id = str(
            info.get("user_id") or info.get("id") or (info.get("user") or {}).get("id") or ""
        ).strip() or None

        now = _now()
        if account_id is not None:
            async with self._lock_for(account_id):
                existing = await self._fetch_account(account_id)
                if not existing:
                    raise SunoValidationError("unknown_account", f"No Suno account {account_id}.")
                if (existing.get("upstream_user_id") and upstream_user_id
                        and existing["upstream_user_id"] != upstream_user_id):
                    raise SunoConflictError(
                        "different_user",
                        "That cookie belongs to a different Suno user. Import it as a new "
                        "account instead of replacing this one.",
                    )
                await self._update_account(
                    account_id,
                    cookies=session.cookie_string(),
                    clerk_sid=session.sid,
                    upstream_user_id=upstream_user_id or existing.get("upstream_user_id"),
                    status=sm.ACCOUNT_READY,
                    last_error=None,
                    last_refresh_at=now,
                )
                self._sessions[account_id] = session
                row = await self._fetch_account(account_id)
            return self._public_account(row or {})

        async with self.db.connect(write=True) as conn:
            conn.row_factory = aiosqlite.Row
            if upstream_user_id:
                cursor = await conn.execute(
                    "SELECT id FROM suno_accounts WHERE upstream_user_id = ?", (upstream_user_id,)
                )
                if await cursor.fetchone():
                    raise SunoConflictError(
                        "duplicate_account",
                        "That Suno user is already connected. Replace its credentials instead.",
                    )
            cursor = await conn.execute(
                """INSERT INTO suno_accounts
                   (upstream_user_id, display_name, cookies, clerk_sid, status,
                    last_refresh_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (upstream_user_id, display_name or None, session.cookie_string(), session.sid,
                 sm.ACCOUNT_READY, now, now, now),
            )
            new_id = cursor.lastrowid
            await conn.commit()

        self._sessions[int(new_id)] = session
        await self.refresh_account_billing(int(new_id))
        row = await self._fetch_account(int(new_id))
        self._wake.set()
        return self._public_account(row or {})

    async def refresh_account_billing(self, account_id: int) -> Dict[str, Any]:
        """Read plan and credits. Best effort: never fails the caller."""
        try:
            session = await self._session_for(account_id)
            info = await self.client.billing_info(session)
        except Exception as exc:
            debug_logger.op_warning(f"[SUNO] billing read failed for account {account_id}: {exc}")
            return {}
        credits = info.get("total_credits_left")
        if credits is None:
            credits = info.get("credits_left")
        plan = info.get("subscription_type") or info.get("plan")
        await self._update_account(
            account_id,
            credits=int(credits) if isinstance(credits, (int, float)) else None,
            plan=str(plan) if plan else None,
            credits_checked_at=_now(),
        )
        return info

    async def list_accounts(self) -> List[Dict[str, Any]]:
        async with self.db.connect() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM suno_accounts ORDER BY id")
            rows = [dict(r) for r in await cursor.fetchall()]
            cursor = await conn.execute(
                f"""SELECT account_id, COUNT(*) AS n FROM suno_jobs
                    WHERE state IN ({','.join('?' * len(sm.ACTIVE_STATES))})
                    GROUP BY account_id""",
                tuple(sm.ACTIVE_STATES),
            )
            counts = {r["account_id"]: r["n"] for r in await cursor.fetchall()}
        return [self._public_account(row, counts.get(row["id"], 0)) for row in rows]

    async def set_account_limit(self, account_id: int, operator_limit: Optional[int] = None,
                                provider_cap: Optional[int] = None) -> Dict[str, Any]:
        fields: Dict[str, Any] = {}
        if operator_limit is not None:
            if not 1 <= operator_limit <= 16:
                raise SunoValidationError("invalid_limit", "operator_limit must be 1-16.")
            fields["operator_limit"] = operator_limit
        if provider_cap is not None:
            if not 1 <= provider_cap <= 16:
                raise SunoValidationError("invalid_limit", "provider_cap must be 1-16.")
            fields["provider_cap"] = provider_cap
        if not fields:
            raise SunoValidationError("invalid_limit", "Nothing to change.")
        await self._update_account(account_id, **fields)
        row = await self._fetch_account(account_id)
        if not row:
            raise SunoValidationError("unknown_account", f"No Suno account {account_id}.")
        self._wake.set()
        return self._public_account(row)

    async def set_account_enabled(self, account_id: int, enabled: bool) -> Dict[str, Any]:
        """Operator switch. Kept separate from health so a successful refresh
        never silently re-enables an account the owner turned off."""
        row = await self._fetch_account(account_id)
        if not row:
            raise SunoValidationError("unknown_account", f"No Suno account {account_id}.")
        await self._update_account(
            account_id,
            operator_disabled=0 if enabled else 1,
            status=sm.ACCOUNT_READY if (enabled and row.get("status") == sm.ACCOUNT_DISABLED)
            else (sm.ACCOUNT_DISABLED if not enabled else row.get("status")),
        )
        row = await self._fetch_account(account_id)
        if enabled:
            self._wake.set()
        return self._public_account(row or {})

    async def delete_account(self, account_id: int, retire_audio: bool = False) -> Dict[str, Any]:
        """Remove an account.

        Audio is served by re-asking Suno for a download URL with this account's
        credentials, so deleting it silently breaks every finished job that
        points at it. Ordinary deletion is refused while such jobs exist;
        ``retire_audio`` is the explicit "yes, drop the audio" path and leaves
        the job rows in place returning 410 for audio.
        """
        row = await self._fetch_account(account_id)
        if not row:
            raise SunoValidationError("unknown_account", f"No Suno account {account_id}.")

        async with self.db.connect() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                f"""SELECT COUNT(*) AS n FROM suno_jobs
                    WHERE account_id = ? AND state IN ({','.join('?' * len(sm.ACTIVE_STATES))})""",
                (account_id, *sm.ACTIVE_STATES),
            )
            in_flight = int((await cursor.fetchone())["n"])
            cursor = await conn.execute(
                "SELECT COUNT(*) AS n FROM suno_jobs WHERE account_id = ? AND state = ?",
                (account_id, sm.SUCCEEDED),
            )
            finished = int((await cursor.fetchone())["n"])

        if in_flight:
            raise SunoConflictError(
                "jobs_in_flight",
                f"{in_flight} job(s) on this account are still unresolved. Resolve them first.",
                in_flight=in_flight,
            )
        if finished and not retire_audio:
            raise SunoConflictError(
                "audio_depends_on_account",
                f"{finished} finished job(s) still serve audio through this account. "
                "Re-send with retire_audio=true to delete it and retire that audio.",
                finished=finished,
            )

        async with self.db.connect(write=True) as conn:
            await conn.execute(
                "UPDATE suno_jobs SET account_id = NULL, retrieval_error = ?, updated_at = ? "
                "WHERE account_id = ?",
                ("Account deleted; audio retired.", _now(), account_id),
            )
            await conn.execute("DELETE FROM suno_accounts WHERE id = ?", (account_id,))
            await conn.commit()

        self._sessions.pop(account_id, None)
        self._account_locks.pop(account_id, None)
        task = self._refresh_tasks.pop(account_id, None)
        if task and not task.done():
            task.cancel()
        return {"deleted": account_id, "audio_retired": bool(finished)}

    # ------------------------------------------------------------- job entry

    async def submit(self, body: Any) -> Dict[str, Any]:
        """Admit a generation request. Returns the job row (HTTP 202 shape)."""
        self._ensure_started()
        request = sm.normalize_generation_request(body)
        request_hash = _request_hash(request)
        idempotency_key = request.pop("idempotency_key", None)
        requested_account = request.pop("account_id", None)

        job_id = f"sj_{uuid.uuid4().hex[:20]}"
        now = _now()

        # Idempotency lookup and admission share one write transaction, so two
        # concurrent identical requests cannot both be admitted.
        async with self.db.connect(write=True) as conn:
            conn.row_factory = aiosqlite.Row
            if idempotency_key:
                cursor = await conn.execute(
                    "SELECT * FROM suno_jobs WHERE idempotency_key = ?", (idempotency_key,)
                )
                existing = await cursor.fetchone()
                if existing:
                    existing = dict(existing)
                    if existing["request_hash"] != request_hash:
                        raise SunoConflictError(
                            "idempotency_conflict",
                            "That Idempotency-Key was used with a different request body.",
                            job_id=existing["id"],
                        )
                    return self._public_job(existing)

            cursor = await conn.execute(
                f"""SELECT COUNT(*) AS n FROM suno_jobs
                    WHERE state IN ({','.join('?' * len(sm.ADMITTED_STATES))})""",
                tuple(sm.ADMITTED_STATES),
            )
            admitted = int((await cursor.fetchone())["n"])
            if admitted >= self.MAX_ADMITTED_JOBS:
                raise SunoValidationError(
                    "queue_full",
                    f"The Suno queue is full ({admitted} jobs). Try again shortly.",
                )

            if requested_account is not None:
                cursor = await conn.execute(
                    "SELECT id FROM suno_accounts WHERE id = ?", (requested_account,)
                )
                if not await cursor.fetchone():
                    raise SunoValidationError(
                        "unknown_account", f"No Suno account {requested_account}."
                    )
                request["pinned_account_id"] = requested_account

            await conn.execute(
                """INSERT INTO suno_jobs
                   (id, account_id, state, idempotency_key, request_hash, request_json,
                    created_at, updated_at)
                   VALUES (?, NULL, ?, ?, ?, ?, ?, ?)""",
                (job_id, sm.QUEUED, idempotency_key, request_hash,
                 json.dumps(request), now, now),
            )
            await conn.commit()

        self._wake.set()
        job = await self._fetch_job(job_id)
        return self._public_job(job or {})

    async def _fetch_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        async with self.db.connect() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM suno_jobs WHERE id = ?", (job_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        row = await self._fetch_job(job_id)
        return self._public_job(row) if row else None

    async def list_jobs(self, limit: int = 50, offset: int = 0,
                        state: Optional[str] = None) -> Dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        clauses, params = "", []
        if state:
            if state not in sm.ALL_STATES:
                raise SunoValidationError("invalid_state", f"Unknown state '{state}'.")
            clauses = " WHERE state = ?"
            params.append(state)
        async with self.db.connect() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                f"SELECT * FROM suno_jobs{clauses} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )
            rows = [dict(r) for r in await cursor.fetchall()]
            cursor = await conn.execute(f"SELECT COUNT(*) AS n FROM suno_jobs{clauses}", tuple(params))
            total = int((await cursor.fetchone())["n"])
        return {
            "jobs": [self._public_job(r) for r in rows],
            "total": total, "limit": limit, "offset": offset,
        }

    def _public_job(self, row: Dict[str, Any]) -> Dict[str, Any]:
        if not row:
            return {}
        request = _loads(row.get("request_json"), {})
        clips = _loads(row.get("clips_json"), {})
        clip_ids = _loads(row.get("clip_ids"), [])
        playable = [cid for cid, c in clips.items() if sm.clip_is_playable(c)]
        return {
            "job_id": row["id"],
            "state": row["state"],
            "account_id": row.get("account_id"),
            "model": request.get("model"),
            "custom": request.get("custom"),
            "clip_ids": clip_ids,
            "clips": [sm.summarize_clip(clips.get(cid) or {"id": cid}) for cid in clip_ids],
            # Early playback is a weaker signal than completion; keep it separate
            # so a caller never reads "one clip is streaming" as "job finished".
            "playable_clip_ids": playable,
            "error": row.get("error"),
            "retrieval_error": row.get("retrieval_error"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "submitted_at": row.get("submitted_at"),
            "completed_at": row.get("completed_at"),
        }

    async def cancel(self, job_id: str) -> Dict[str, Any]:
        """Cancel is only honest before dispatch, or while captcha-blocked.

        Once the generate call has gone out we cannot un-spend the credits, so
        cancellation there is refused rather than reported as success.
        """
        now = _now()
        async with self.db.connect(write=True) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM suno_jobs WHERE id = ?", (job_id,))
            row = await cursor.fetchone()
            if not row:
                raise SunoValidationError("unknown_job", f"No Suno job {job_id}.")
            row = dict(row)
            if row["state"] not in (sm.QUEUED, sm.BLOCKED_CAPTCHA):
                raise SunoConflictError(
                    "not_cancellable",
                    f"A job in state '{row['state']}' cannot be cancelled; it may already "
                    "exist upstream.",
                    state=row["state"],
                )
            cursor = await conn.execute(
                "UPDATE suno_jobs SET state = ?, updated_at = ?, completed_at = ? "
                "WHERE id = ? AND state = ?",
                (sm.CANCELLED, now, now, job_id, row["state"]),
            )
            if not cursor.rowcount:
                raise SunoConflictError("not_cancellable", "The job changed state; re-read it.")
            await conn.commit()
        return await self.get_job(job_id) or {}

    async def retry_blocked(self, job_id: str) -> Dict[str, Any]:
        """Put a captcha-blocked job back in the queue."""
        now = _now()
        async with self.db.connect(write=True) as conn:
            cursor = await conn.execute(
                "UPDATE suno_jobs SET state = ?, error = NULL, updated_at = ? "
                "WHERE id = ? AND state = ?",
                (sm.QUEUED, now, job_id, sm.BLOCKED_CAPTCHA),
            )
            if not cursor.rowcount:
                raise SunoConflictError(
                    "not_blocked", "Only a captcha-blocked job can be retried this way."
                )
            await conn.commit()
        self._wake.set()
        return await self.get_job(job_id) or {}

    async def resolve(self, job_id: str, action: str, clip_ids: Optional[List[str]] = None,
                      confirm_no_upstream_work: bool = False, note: str = "") -> Dict[str, Any]:
        """Operator resolution for ``needs_review``.

        ``resume`` adopts clip ids the operator found in Suno's own library;
        ``fail`` releases the slot but only with an explicit confirmation that
        no upstream work is running, because that is the claim being made.
        """
        row = await self._fetch_job(job_id)
        if not row:
            raise SunoValidationError("unknown_job", f"No Suno job {job_id}.")
        if row["state"] not in sm.RESOLVABLE_STATES:
            raise SunoConflictError(
                "not_resolvable", f"A job in state '{row['state']}' needs no resolution.",
                state=row["state"],
            )

        now = _now()
        if action == "resume":
            ids = [sm.validate_clip_id(c) for c in (clip_ids or [])]
            if not ids:
                raise SunoValidationError("missing_clip_ids", "'resume' needs at least one clip id.")
            async with self.db.connect(write=True) as conn:
                cursor = await conn.execute(
                    "UPDATE suno_jobs SET state = ?, clip_ids = ?, error = NULL, "
                    "submitted_at = COALESCE(submitted_at, ?), updated_at = ? "
                    "WHERE id = ? AND state = ?",
                    (sm.SUBMITTED, json.dumps(ids), now, now, job_id, row["state"]),
                )
                if not cursor.rowcount:
                    raise SunoConflictError("state_changed", "The job changed state; re-read it.")
                await conn.commit()
            self._wake.set()
        elif action == "fail":
            if not confirm_no_upstream_work:
                raise SunoValidationError(
                    "confirmation_required",
                    "Set confirm_no_upstream_work=true once you have checked Suno's library. "
                    "Failing a job that is still running would free the slot and double-spend.",
                )
            async with self.db.connect(write=True) as conn:
                cursor = await conn.execute(
                    "UPDATE suno_jobs SET state = ?, error = ?, updated_at = ?, completed_at = ? "
                    "WHERE id = ? AND state = ?",
                    (sm.FAILED, note or "Resolved by operator: no upstream work.",
                     now, now, job_id, row["state"]),
                )
                if not cursor.rowcount:
                    raise SunoConflictError("state_changed", "The job changed state; re-read it.")
                await conn.commit()
        else:
            raise SunoValidationError("invalid_action", "action must be 'resume' or 'fail'.")
        return await self.get_job(job_id) or {}

    # ------------------------------------------------------------- dispatch

    async def _dispatch_loop(self) -> None:
        while self._started:
            try:
                await self._dispatch_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - loop must survive
                debug_logger.op_warning(f"[SUNO] dispatch loop error: {exc}")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.DISPATCH_INTERVAL)
            self._wake.clear()

    async def _claim_next_job(self) -> Optional[Tuple[str, int]]:
        """Pick an account with spare capacity and claim one queued job.

        Account choice, capacity check and the claim happen in a single write
        transaction so two dispatch passes cannot both take the last slot. No
        network call happens in here.
        """
        now = _now()
        async with self.db.connect(write=True) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM suno_accounts WHERE operator_disabled = 0 AND status = ?",
                (sm.ACCOUNT_READY,),
            )
            accounts = [dict(r) for r in await cursor.fetchall()]
            if not accounts:
                return None

            cursor = await conn.execute(
                f"""SELECT account_id, COUNT(*) AS n FROM suno_jobs
                    WHERE state IN ({','.join('?' * len(sm.ACTIVE_STATES))})
                    GROUP BY account_id""",
                tuple(sm.ACTIVE_STATES),
            )
            used = {r["account_id"]: r["n"] for r in await cursor.fetchall()}

            available: List[Tuple[int, int]] = []
            for account in accounts:
                if account.get("cooldown_until") and account["cooldown_until"] > now:
                    continue
                if account.get("captcha_paused_until") and account["captcha_paused_until"] > now:
                    continue
                free = self._effective_limit(account) - int(used.get(account["id"], 0))
                if free > 0:
                    available.append((free, account["id"]))
            if not available:
                return None
            available.sort(reverse=True)  # most free capacity first

            cursor = await conn.execute(
                "SELECT * FROM suno_jobs WHERE state = ? ORDER BY created_at LIMIT 20",
                (sm.QUEUED,),
            )
            for job in [dict(r) for r in await cursor.fetchall()]:
                request = _loads(job.get("request_json"), {})
                pinned = request.get("pinned_account_id")
                chosen = None
                for _free, account_id in available:
                    if pinned is None or pinned == account_id:
                        chosen = account_id
                        break
                if chosen is None:
                    continue

                attempt_id = uuid.uuid4().hex
                transaction_uuid = sm.new_transaction_uuid()
                # Persisted BEFORE the generate call goes out, so a lost reply
                # still leaves a record of what was attempted and on which
                # account.
                cursor = await conn.execute(
                    "UPDATE suno_jobs SET state = ?, account_id = ?, attempt_id = ?, "
                    "transaction_uuid = ?, updated_at = ? WHERE id = ? AND state = ?",
                    (sm.SUBMITTING, chosen, attempt_id, transaction_uuid, now, job["id"], sm.QUEUED),
                )
                if cursor.rowcount:
                    await conn.commit()
                    return job["id"], chosen
            await conn.commit()
        return None

    async def _dispatch_once(self) -> int:
        dispatched = 0
        while len(self._running_jobs) < self.MAX_ADMITTED_JOBS:
            claim = await self._claim_next_job()
            if not claim:
                break
            job_id, account_id = claim
            task = asyncio.create_task(self._run_job(job_id, account_id))
            self._running_jobs[job_id] = task
            task.add_done_callback(lambda _t, jid=job_id: self._running_jobs.pop(jid, None))
            dispatched += 1
        return dispatched

    async def _run_job(self, job_id: str, account_id: int) -> None:
        """Captcha gate, then the one generate call. Network only; no locks."""
        job = await self._fetch_job(job_id)
        if not job or job["state"] != sm.SUBMITTING:
            return
        request = _loads(job.get("request_json"), {})
        attempt_id = job.get("attempt_id")

        try:
            session = await self._session_for(account_id)
        except SunoAPIError as exc:
            # Could not even authenticate: nothing was sent, so requeue.
            await self._requeue(job_id, attempt_id, f"Session unavailable: {exc}")
            return

        try:
            gate = await self.client.captcha_check(session, "generation")
        except SunoAPIError as exc:
            await self._requeue(job_id, attempt_id, f"Captcha check failed: {exc}")
            return

        captcha_token: Optional[str] = None
        captcha_version = gate.get("captcha_version")
        if gate.get("required"):
            captcha_token = await self._mint_captcha(session, captcha_version)
            if not captcha_token:
                # Nothing was submitted. Release the slot and stand the account
                # down briefly instead of hammering a gate we cannot pass.
                await self._block_for_captcha(job_id, attempt_id, account_id, captcha_version)
                return

        payload = sm.build_generate_payload(
            request, transaction_uuid=job.get("transaction_uuid") or sm.new_transaction_uuid(),
            captcha_token=captcha_token, captcha_version=captcha_version,
        )

        try:
            clips = await self.client.generate(session, payload)
        except SunoRateLimited as exc:
            # A rate limiter rejects before doing work, so no generation exists.
            await self._update_account(
                account_id, cooldown_until=_now() + self.RATE_LIMIT_COOLDOWN_SECONDS,
                last_error=str(exc)[:500],
            )
            await self._requeue(job_id, attempt_id, "Suno rate limited this account.")
            return
        except SunoAuthError as exc:
            await self._update_account(
                account_id, status=sm.ACCOUNT_NEEDS_LOGIN, last_error=str(exc)[:500]
            )
            self._sessions.pop(account_id, None)
            await self._requeue(job_id, attempt_id, "Suno session expired before submission.")
            return
        except SunoAPIError as exc:
            if 400 <= exc.status_code < 500:
                # An explicit refusal: Suno saw the request and declined it, so
                # no clip was created and no credit was spent.
                await self._settle_failed(job_id, attempt_id, f"Suno rejected the request: {exc}")
            else:
                # Timeout, transport error or 5xx: the request may have landed.
                # Never retry this; a duplicate would be billed.
                await self._needs_review(
                    job_id, attempt_id,
                    f"Generation outcome unknown ({exc}). Check Suno's library before resolving.",
                )
            return
        except Exception as exc:  # pragma: no cover - defensive
            await self._needs_review(job_id, attempt_id, f"Generation outcome unknown ({exc}).")
            return

        clip_ids = [c.get("id") for c in clips if isinstance(c, dict) and c.get("id")]
        if not clip_ids:
            await self._needs_review(job_id, attempt_id, "Suno returned clips without ids.")
            return

        clips_by_id = {c["id"]: c for c in clips if isinstance(c, dict) and c.get("id")}
        now = _now()
        async with self.db.connect(write=True) as conn:
            await conn.execute(
                "UPDATE suno_jobs SET state = ?, clip_ids = ?, clips_json = ?, "
                "submitted_at = ?, updated_at = ? WHERE id = ? AND state = ? AND attempt_id = ?",
                (sm.SUBMITTED, json.dumps(clip_ids), json.dumps(clips_by_id), now, now,
                 job_id, sm.SUBMITTING, attempt_id),
            )
            await conn.commit()
        self._wake.set()

    async def _mint_captcha(self, session: SunoSession, version: Optional[int]) -> Optional[str]:
        """Ask the configured provider for a generation captcha token.

        v1 ships without a provider. Suno's widget is "invisible", which means
        it usually resolves without user interaction, not that it never presents
        a challenge, so this is a real gate and not a formality.
        """
        if not self.captcha_provider:
            return None
        try:
            return await self.captcha_provider.mint(version=version, site_key=sm.HCAPTCHA_SITE_KEY)
        except Exception as exc:
            debug_logger.op_warning(f"[SUNO] captcha provider failed: {exc}")
            return None

    # -------------------------------------------------------- state helpers

    async def _requeue(self, job_id: str, attempt_id: Optional[str], reason: str) -> None:
        """Return an un-submitted job to the queue and drop its reservation."""
        now = _now()
        async with self.db.connect(write=True) as conn:
            await conn.execute(
                "UPDATE suno_jobs SET state = ?, account_id = NULL, attempt_id = NULL, "
                "error = ?, updated_at = ? WHERE id = ? AND state = ? AND attempt_id IS ?",
                (sm.QUEUED, reason[:500], now, job_id, sm.SUBMITTING, attempt_id),
            )
            await conn.commit()

    async def _block_for_captcha(self, job_id: str, attempt_id: Optional[str],
                                 account_id: int, version: Optional[int]) -> None:
        now = _now()
        label = "hCaptcha" if version != sm.CAPTCHA_VERSION_TURNSTILE else "Cloudflare Turnstile"
        async with self.db.connect(write=True) as conn:
            await conn.execute(
                "UPDATE suno_jobs SET state = ?, account_id = NULL, attempt_id = NULL, "
                "error = ?, updated_at = ? WHERE id = ? AND state = ? AND attempt_id IS ?",
                (sm.BLOCKED_CAPTCHA,
                 f"Suno demanded a {label} token for this generation and no captcha provider "
                 "is configured. Nothing was submitted and nothing was charged.",
                 now, job_id, sm.SUBMITTING, attempt_id),
            )
            await conn.commit()
        await self._update_account(
            account_id, captcha_paused_until=now + self.CAPTCHA_PAUSE_SECONDS
        )

    async def _needs_review(self, job_id: str, attempt_id: Optional[str], reason: str) -> None:
        now = _now()
        async with self.db.connect(write=True) as conn:
            await conn.execute(
                "UPDATE suno_jobs SET state = ?, error = ?, updated_at = ? "
                "WHERE id = ? AND attempt_id IS ? AND state IN (?, ?, ?)",
                (sm.NEEDS_REVIEW, reason[:500], now, job_id, attempt_id,
                 sm.SUBMITTING, sm.SUBMITTED, sm.FINALIZING),
            )
            await conn.commit()

    async def _settle_failed(self, job_id: str, attempt_id: Optional[str], reason: str) -> None:
        now = _now()
        async with self.db.connect(write=True) as conn:
            await conn.execute(
                "UPDATE suno_jobs SET state = ?, error = ?, updated_at = ?, completed_at = ? "
                "WHERE id = ? AND attempt_id IS ? AND state = ?",
                (sm.FAILED, reason[:500], now, now, job_id, attempt_id, sm.SUBMITTING),
            )
            await conn.commit()

    # ----------------------------------------------------------------- poll

    async def _poll_loop(self) -> None:
        while self._started:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover
                debug_logger.op_warning(f"[SUNO] poll loop error: {exc}")
            await asyncio.sleep(self.POLL_INTERVAL)

    async def _poll_once(self) -> int:
        async with self.db.connect() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM suno_jobs WHERE state IN (?, ?) ORDER BY updated_at LIMIT 40",
                (sm.SUBMITTED, sm.FINALIZING),
            )
            jobs = [dict(r) for r in await cursor.fetchall()]

        handled = 0
        for job in jobs:
            account_id = job.get("account_id")
            if account_id is None:
                continue
            try:
                await self._poll_job(job, account_id)
                handled += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A poll failure is a failure to observe, never a job failure.
                debug_logger.op_warning(f"[SUNO] poll failed for {job['id']}: {exc}")
        return handled

    async def _poll_job(self, job: Dict[str, Any], account_id: int) -> None:
        clip_ids = _loads(job.get("clip_ids"), [])
        if not clip_ids:
            return
        session = await self._session_for(account_id)
        clips = await self.client.feed(session, clip_ids)
        known = _loads(job.get("clips_json"), {})
        for clip in clips:
            if isinstance(clip, dict) and clip.get("id"):
                known[clip["id"]] = clip

        verdict = sm.evaluate_clips(known, clip_ids)
        now = _now()

        if not verdict["all_terminal"]:
            async with self.db.connect(write=True) as conn:
                await conn.execute(
                    "UPDATE suno_jobs SET clips_json = ?, updated_at = ? WHERE id = ? AND state = ?",
                    (json.dumps(known), now, job["id"], job["state"]),
                )
                await conn.commit()
            return

        if verdict["settle"] == sm.FAILED:
            reasons = []
            for cid in verdict["failed_ids"]:
                message = sm.summarize_clip(known.get(cid, {})).get("error_message")
                if message:
                    reasons.append(str(message))
            async with self.db.connect(write=True) as conn:
                await conn.execute(
                    "UPDATE suno_jobs SET state = ?, clips_json = ?, error = ?, "
                    "updated_at = ?, completed_at = ? WHERE id = ? AND state IN (?, ?)",
                    (sm.FAILED, json.dumps(known),
                     "; ".join(reasons)[:500] or "Every clip failed upstream.",
                     now, now, job["id"], sm.SUBMITTED, sm.FINALIZING),
                )
                await conn.commit()
            return

        # At least one clip completed. Move to finalizing and prove the audio is
        # actually retrievable before claiming success.
        async with self.db.connect(write=True) as conn:
            await conn.execute(
                "UPDATE suno_jobs SET state = ?, clips_json = ?, updated_at = ? "
                "WHERE id = ? AND state IN (?, ?)",
                (sm.FINALIZING, json.dumps(known), now, job["id"], sm.SUBMITTED, sm.FINALIZING),
            )
            await conn.commit()
        await self._finalize(job["id"], account_id, verdict["complete_ids"])

    async def _finalize(self, job_id: str, account_id: int, complete_ids: List[str]) -> None:
        """Confirm a completed clip can actually be downloaded, then succeed.

        Suno's ``audio_url`` is ``/api/forbidden`` and its ``media_urls`` entries
        are encrypted, so "status: complete" alone is not evidence of anything
        playable. The download endpoint is the evidence.
        """
        attempts = self._finalize_attempts.get(job_id, 0) + 1
        self._finalize_attempts[job_id] = attempts
        resolved: Optional[str] = None
        last_error = ""

        try:
            session = await self._session_for(account_id)
            for clip_id in complete_ids:
                try:
                    info = await self.client.download_url(session, clip_id, "mp3")
                except SunoAPIError as exc:
                    last_error = str(exc)
                    continue
                if isinstance(info, dict) and info.get("status") == "processing":
                    last_error = "Suno is still packaging the download."
                    continue
                url = _extract_download_url(info)
                if url:
                    resolved = clip_id
                    break
                last_error = "Suno returned no download URL."
        except SunoAPIError as exc:
            last_error = str(exc)

        now = _now()
        if resolved:
            self._finalize_attempts.pop(job_id, None)
            async with self.db.connect(write=True) as conn:
                await conn.execute(
                    "UPDATE suno_jobs SET state = ?, retrieval_error = NULL, updated_at = ?, "
                    "completed_at = ? WHERE id = ? AND state = ?",
                    (sm.SUCCEEDED, now, now, job_id, sm.FINALIZING),
                )
                await conn.commit()
            return

        if attempts >= self.MAX_FINALIZE_ATTEMPTS:
            self._finalize_attempts.pop(job_id, None)
            # The songs exist and were paid for, so this is not a generation
            # failure; it needs a human, and it must never trigger a re-run.
            await self._needs_review(
                job_id, (await self._fetch_job(job_id) or {}).get("attempt_id"),
                f"Clips completed but no download resolved after {attempts} attempts: {last_error}",
            )
            return

        async with self.db.connect(write=True) as conn:
            await conn.execute(
                "UPDATE suno_jobs SET retrieval_error = ?, updated_at = ? WHERE id = ?",
                (last_error[:500], now, job_id),
            )
            await conn.commit()

    # ---------------------------------------------------------------- audio

    async def stream_audio(self, job_id: str, clip_id: str,
                           fmt: str = "mp3") -> AsyncIterator[bytes]:
        """Proxy one clip's audio, bound to the job that produced it."""
        clip_id = sm.validate_clip_id(clip_id)
        fmt = sm.validate_audio_format(fmt)

        job = await self._fetch_job(job_id)
        if not job:
            raise SunoValidationError("unknown_job", f"No Suno job {job_id}.")
        if clip_id not in _loads(job.get("clip_ids"), []):
            raise SunoValidationError(
                "clip_not_in_job", "That clip does not belong to this job.",
            )
        account_id = job.get("account_id")
        if account_id is None:
            raise SunoConflictError(
                "audio_retired",
                "The Suno account behind this job was deleted, so its audio is retired.",
                http_status=410,
            )

        async with self._audio_slots:
            session = await self._session_for(int(account_id))
            info = await self.client.download_url(session, clip_id, fmt)
            if isinstance(info, dict) and info.get("status") == "processing":
                raise SunoConflictError(
                    "audio_processing", "Suno is still packaging this download; retry shortly.",
                )
            url = _extract_download_url(info)
            if not url:
                raise SunoConflictError(
                    "audio_unavailable", "Suno returned no download URL for this clip.",
                )
            async for chunk in self.client.stream_download(url):
                yield chunk


def _extract_download_url(info: Any) -> Optional[str]:
    """Pull the download URL out of ``/api/download/clip/{id}``'s reply.

    The key name is the one part of this response we have not observed live, so
    accept the handful of plausible spellings rather than guessing exactly one.
    """
    if isinstance(info, str):
        return info or None
    if not isinstance(info, dict):
        return None
    for key in ("url", "download_url", "audio_url", "signed_url", "presigned_url"):
        value = info.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    data = info.get("data")
    if isinstance(data, dict):
        return _extract_download_url(data)
    return None
