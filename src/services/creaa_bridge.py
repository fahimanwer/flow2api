"""Durable broker between the Creaa REST surface and browser worker extensions.

Responsibilities
----------------
* keep every job in SQLite so a restart never loses or blindly re-runs work;
* hold exactly one worker WebSocket per device_id and remember which account it serves;
* enforce persisted per-media and total account caps (default one total);
* persist ``claimed`` BEFORE an ``execute`` message leaves the server, and persist
  ``submitting`` BEFORE acknowledging it - the worker must not click "generate"
  until it has that ack, so an interruption can always be classified;
* on reconnect re-send the SAME job/attempt as ``resume`` only when a stable
  provider task id exists; never re-execute a possibly-submitted job;
* validate every worker event against connection, account, device and attempt,
  and refuse terminal regressions.

Concurrency model (single app process, documented on purpose)
-----------------------------------------------------------
All state mutation happens on the event loop inside ``self._state_lock``. Database
work is short stdlib ``sqlite3`` calls executed inline (a few milliseconds each)
guarded by a ``threading.Lock`` - no per-request threads, nothing to leak.
WebSocket sends always happen OUTSIDE the state lock and outside any transaction;
if a send fails after a state was persisted, the state is repaired under the lock
(claimed -> queued, submitting -> needs_review because ack delivery is uncertain).

Wire protocol: see ``docs`` / the router in ``api.creaa``. This module never
knows Creaa website URLs, cookies or upstream routes.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from ..core.creaa_models import (
    ACTIVE_STATES,
    CANCELLED,
    CLAIMED,
    EVENT_STATES,
    FAILED,
    NEEDS_LOGIN,
    NEEDS_REVIEW,
    QUEUED,
    RESOLVABLE_STATES,
    RESUMABLE_STATES,
    RUNNING,
    STATES_REQUIRING_PROVIDER_ID,
    SUBMITTED,
    SUBMITTING,
    SUCCEEDED,
    TERMINAL_STATES,
    CreaaValidationError,
    canonical_request_hash,
    catalog_supports,
    event_transition_allowed,
    iso_utc,
    is_terminal,
    new_attempt_id,
    new_job_id,
    normalize_catalog,
    normalize_generation_request,
    public_request,
    validate_opaque_id,
    wire_request,
    MAX_IDEMPOTENCY_KEY_CHARS,
)
from ..core.logger import debug_logger

MAX_RESULT_JSON_CHARS = 256_000
MAX_ERROR_MESSAGE_CHARS = 2000
MAX_PROVIDER_TASK_ID_CHARS = 256
MAX_PROGRESS = 100.0


class CreaaConflictError(CreaaValidationError):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(code, message, status=409, **extra)


@dataclass
class WorkerConnection:
    websocket: Any
    device_id: Optional[str] = None
    account_id: Optional[str] = None
    account_label: str = ""
    models: List[Dict[str, Any]] = field(default_factory=list)
    capabilities: Dict[str, Any] = field(default_factory=dict)
    connected_at: float = field(default_factory=time.time)
    registered_at: Optional[float] = None
    last_seen: float = field(default_factory=time.time)

    @property
    def registered(self) -> bool:
        return self.device_id is not None and self.account_id is not None


@dataclass
class AccountRecord:
    account_id: str
    account_label: str = ""
    last_device_id: Optional[str] = None
    models: List[Dict[str, Any]] = field(default_factory=list)
    capabilities: Dict[str, Any] = field(default_factory=dict)
    login_required: bool = False
    first_seen_at: Optional[float] = None
    last_seen_at: Optional[float] = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS creaa_jobs (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE,
    account_id TEXT NOT NULL,
    device_id TEXT,
    media_type TEXT NOT NULL,
    model TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt_id TEXT,
    provider_task_id TEXT,
    request_json TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    billing_policy TEXT NOT NULL,
    max_credits INTEGER,
    progress REAL,
    result_json TEXT,
    error_code TEXT,
    error_message TEXT,
    resolution_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    claimed_at REAL,
    submitted_at REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_creaa_jobs_account_state ON creaa_jobs(account_id, state);
CREATE INDEX IF NOT EXISTS idx_creaa_jobs_created ON creaa_jobs(created_at);
CREATE TABLE IF NOT EXISTS creaa_accounts (
    account_id TEXT PRIMARY KEY,
    account_label TEXT,
    last_device_id TEXT,
    models_json TEXT,
    capabilities_json TEXT,
    login_required INTEGER NOT NULL DEFAULT 0,
    first_seen_at REAL,
    last_seen_at REAL
);
CREATE TABLE IF NOT EXISTS creaa_parallel_limits (
    account_id TEXT PRIMARY KEY,
    images INTEGER NOT NULL,
    videos INTEGER NOT NULL,
    total INTEGER NOT NULL,
    note TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""

_JOB_COLUMNS = (
    "id", "idempotency_key", "account_id", "device_id", "media_type", "model", "state",
    "attempt_id", "provider_task_id", "request_json", "request_hash", "billing_policy",
    "max_credits", "progress", "result_json", "error_code", "error_message", "resolution_json",
    "created_at", "updated_at", "claimed_at", "submitted_at", "finished_at",
)


class CreaaBridge:
    """Durable job broker + worker registry. One instance per process."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        max_queued_per_account: int = 100,
        claimed_timeout: float = 120.0,
        submitting_timeout: float = 180.0,
        running_timeout: float = 3600.0,
        housekeeping_interval: float = 15.0,
    ):
        self.db_path = Path(db_path)
        self.max_queued_per_account = max(1, int(max_queued_per_account))
        self.claimed_timeout = float(claimed_timeout)
        self.submitting_timeout = float(submitting_timeout)
        self.running_timeout = float(running_timeout)
        self.housekeeping_interval = float(housekeeping_interval)

        self._conn: Optional[sqlite3.Connection] = None
        self._db_lock = threading.Lock()
        self._state_lock: Optional[asyncio.Lock] = None
        self._dispatch_lock: Optional[asyncio.Lock] = None
        self._workers: Dict[str, WorkerConnection] = {}       # device_id -> connection
        self._sockets: Dict[int, WorkerConnection] = {}       # id(websocket) -> connection
        self._accounts: Dict[str, AccountRecord] = {}         # every account ever registered
        self._tasks: Set[asyncio.Task] = set()
        self._housekeeping_task: Optional[asyncio.Task] = None
        self._started = False
        self._closing = False

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._started:
            return
        self._state_lock = asyncio.Lock()
        self._dispatch_lock = asyncio.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        with conn:
            conn.executescript(_SCHEMA)
        self._conn = conn
        self._load_accounts()
        recovered = self._recover_after_restart()
        self._started = True
        self._closing = False
        if recovered:
            debug_logger.log_info(f"[Creaa] restart recovery: {recovered}")
        if self.housekeeping_interval > 0:
            self._housekeeping_task = asyncio.create_task(self._housekeeping_loop())

    async def close(self) -> None:
        self._closing = True
        tasks = list(self._tasks)
        if self._housekeeping_task is not None:
            tasks.append(self._housekeeping_task)
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        self._housekeeping_task = None
        for conn in list(self._sockets.values()):
            try:
                await conn.websocket.close(code=1001)
            except Exception:
                pass
        self._sockets.clear()
        self._workers.clear()
        if self._conn is not None:
            with self._db_lock:
                try:
                    self._conn.close()
                finally:
                    self._conn = None
        self._started = False

    def _ensure_started(self) -> None:
        if not self._started or self._conn is None or self._state_lock is None:
            raise RuntimeError("CreaaBridge is not started")

    # ------------------------------------------------------------------ database

    @contextmanager
    def _db(self):
        """Short, inline SQLite access. Commits on success, rolls back on error."""
        conn = self._conn
        if conn is None:
            raise RuntimeError("CreaaBridge database is closed")
        with self._db_lock:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def _fetch_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._db() as conn:
            row = conn.execute("SELECT * FROM creaa_jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def _fetch_job_by_key(self, key: str) -> Optional[Dict[str, Any]]:
        with self._db() as conn:
            row = conn.execute("SELECT * FROM creaa_jobs WHERE idempotency_key = ?", (key,)).fetchone()
        return dict(row) if row else None

    def _update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields.setdefault("updated_at", time.time())
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self._db() as conn:
            conn.execute(f"UPDATE creaa_jobs SET {assignments} WHERE id = ?", (*fields.values(), job_id))

    def _count_jobs(self, account_id: str, states: Iterable[str]) -> int:
        states = tuple(states)
        marks = ",".join("?" for _ in states)
        with self._db() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM creaa_jobs WHERE account_id = ? AND state IN ({marks})",
                (account_id, *states),
            ).fetchone()
        return int(row["n"])

    def _jobs_in_states(self, states: Iterable[str], account_id: Optional[str] = None,
                        device_id: Optional[str] = None) -> List[Dict[str, Any]]:
        states = tuple(states)
        marks = ",".join("?" for _ in states)
        sql = f"SELECT * FROM creaa_jobs WHERE state IN ({marks})"
        params: List[Any] = list(states)
        if account_id is not None:
            sql += " AND account_id = ?"
            params.append(account_id)
        if device_id is not None:
            sql += " AND device_id = ?"
            params.append(device_id)
        sql += " ORDER BY created_at ASC"
        with self._db() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def _load_accounts(self) -> None:
        with self._db() as conn:
            rows = conn.execute("SELECT * FROM creaa_accounts").fetchall()
        self._accounts = {}
        for row in rows:
            self._accounts[row["account_id"]] = AccountRecord(
                account_id=row["account_id"],
                account_label=row["account_label"] or "",
                last_device_id=row["last_device_id"],
                models=json.loads(row["models_json"] or "[]"),
                capabilities=json.loads(row["capabilities_json"] or "{}"),
                login_required=bool(row["login_required"]),
                first_seen_at=row["first_seen_at"],
                last_seen_at=row["last_seen_at"],
            )

    def _save_account(self, record: AccountRecord) -> None:
        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO creaa_accounts (account_id, account_label, last_device_id, models_json,
                    capabilities_json, login_required, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    account_label = excluded.account_label,
                    last_device_id = excluded.last_device_id,
                    models_json = excluded.models_json,
                    capabilities_json = excluded.capabilities_json,
                    login_required = excluded.login_required,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    record.account_id, record.account_label, record.last_device_id,
                    json.dumps(record.models), json.dumps(record.capabilities),
                    1 if record.login_required else 0, record.first_seen_at, record.last_seen_at,
                ),
            )
        self._accounts[record.account_id] = record

    def _recover_after_restart(self) -> Dict[str, int]:
        """Classify in-flight rows left by the previous process.

        * claimed: the worker never received a submitting ack (we ack only after
          persisting ``submitting``), so it cannot have clicked. Safe to queue again.
        * submitting without provider id: the ack may or may not have reached the
          worker. Uncertain -> needs_review, slot stays held until an operator resolves.
        * submitted/running: provider id known; tracking resumes when a worker registers.
        """
        now = time.time()
        counts = {"requeued": 0, "needs_review": 0}
        with self._db() as conn:
            counts["requeued"] = conn.execute(
                "UPDATE creaa_jobs SET state = ?, device_id = NULL, attempt_id = NULL, claimed_at = NULL, updated_at = ? "
                "WHERE state = ?",
                (QUEUED, now, CLAIMED),
            ).rowcount
            counts["needs_review"] = conn.execute(
                "UPDATE creaa_jobs SET state = ?, error_code = ?, error_message = ?, updated_at = ? "
                "WHERE state = ? AND (provider_task_id IS NULL OR provider_task_id = '')",
                (
                    NEEDS_REVIEW, "interrupted_before_provider_id",
                    "backend restarted after submit intent; upstream state unknown", now, SUBMITTING,
                ),
            ).rowcount
        return {k: v for k, v in counts.items() if v}

    # ------------------------------------------------------------------ views

    @staticmethod
    def _public_job(row: Dict[str, Any]) -> Dict[str, Any]:
        request = json.loads(row["request_json"])
        error = None
        if row.get("error_code") or row.get("error_message"):
            error = {"code": row.get("error_code"), "message": row.get("error_message")}
        return {
            "id": row["id"],
            "object": "creaa.job",
            "state": row["state"],
            "media_type": row["media_type"],
            "model": row["model"],
            "account_id": row["account_id"],
            "device_id": row.get("device_id"),
            "attempt_id": row.get("attempt_id"),
            "provider_task_id": row.get("provider_task_id"),
            "billing_policy": row["billing_policy"],
            "max_credits": row.get("max_credits"),
            "progress": row.get("progress"),
            "result": json.loads(row["result_json"]) if row.get("result_json") else None,
            "error": error,
            "resolution": json.loads(row["resolution_json"]) if row.get("resolution_json") else None,
            "request": public_request(request),
            "idempotency_key": row.get("idempotency_key"),
            "created_at": iso_utc(row["created_at"]),
            "updated_at": iso_utc(row["updated_at"]),
            "claimed_at": iso_utc(row.get("claimed_at")),
            "submitted_at": iso_utc(row.get("submitted_at")),
            "finished_at": iso_utc(row.get("finished_at")),
        }

    def _online_devices_for(self, account_id: str) -> List[WorkerConnection]:
        return [c for c in self._workers.values() if c.account_id == account_id]

    def _pick_worker(self, account_id: str) -> Optional[WorkerConnection]:
        candidates = self._online_devices_for(account_id)
        if not candidates:
            return None
        return max(candidates, key=lambda c: c.registered_at or 0.0)

    def list_models(self) -> List[Dict[str, Any]]:
        """Live catalog: only models advertised by currently connected workers."""
        merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for conn in self._workers.values():
            for entry in conn.models:
                key = (entry["id"], entry["media_type"])
                item = merged.get(key)
                if item is None:
                    item = dict(entry)
                    item["id"] = f"creaa/{entry['id']}"
                    item["provider_model_id"] = entry["id"]
                    item["accounts"] = []
                    merged[key] = item
                if conn.account_id not in item["accounts"]:
                    item["accounts"].append(conn.account_id)
        return sorted(merged.values(), key=lambda m: (m["media_type"], m["id"]))

    def parallel_limits(self, account_id: str) -> Dict[str, int]:
        with self._db() as conn:
            row = conn.execute("SELECT images, videos, total FROM creaa_parallel_limits WHERE account_id = ?", (account_id,)).fetchone()
        return dict(row) if row else {"images": 1, "videos": 1, "total": 1}

    async def set_parallel_limits(self, account_id: str, images: int, videos: int, total: int, note: str) -> Dict[str, Any]:
        self._ensure_started()
        if any(isinstance(v, bool) or not isinstance(v, int) for v in (images, videos, total)):
            raise CreaaValidationError("invalid_parallel_limits", "parallel limits must be integers")
        if not (1 <= images <= 3 and 1 <= videos <= 2 and 1 <= total <= 5):
            raise CreaaValidationError("invalid_parallel_limits", "images: 1..3, videos: 1..2, total: 1..5")
        if not isinstance(note, str) or not note.strip() or len(note) > 500:
            raise CreaaValidationError("note_required", "Record a reason for this parallelism change (1..500 characters)")
        async with self._state_lock:
            if account_id not in self._accounts:
                raise CreaaValidationError("account_not_found", "Register the account first", status=404)
            old = self.parallel_limits(account_id)
            with self._db() as conn:
                conn.execute("INSERT INTO creaa_parallel_limits VALUES (?, ?, ?, ?, ?, ?) "
                             "ON CONFLICT(account_id) DO UPDATE SET images=excluded.images, videos=excluded.videos, "
                             "total=excluded.total, note=excluded.note, updated_at=excluded.updated_at",
                             (account_id, images, videos, total, note.strip(), time.time()))
        new = {"images": images, "videos": videos, "total": total}
        debug_logger.log_info(f"[Creaa] parallel limits account={account_id} {old} -> {new}; {note.strip()}")
        self._schedule_dispatch()
        return {"account_id": account_id, "previous": old, "limits": new}

    def list_accounts(self) -> List[Dict[str, Any]]:
        out = []
        for record in self._accounts.values():
            devices = self._online_devices_for(record.account_id)
            active = self._jobs_in_states(ACTIVE_STATES, account_id=record.account_id)
            queued = self._count_jobs(record.account_id, (QUEUED,))
            out.append({
                "account_id": record.account_id,
                "account_label": record.account_label,
                "online": bool(devices),
                "login_required": record.login_required,
                "devices": [
                    {"device_id": d.device_id, "registered_at": iso_utc(d.registered_at), "last_seen": iso_utc(d.last_seen)}
                    for d in devices
                ],
                "models": (devices[-1].models if devices else record.models),
                "capabilities": (devices[-1].capabilities if devices else record.capabilities),
                "active_job": ({"id": active[0]["id"], "state": active[0]["state"]} if active else None),
                "active_jobs": [{"id": j["id"], "state": j["state"], "media_type": j["media_type"]} for j in active],
                "parallel_limits": self.parallel_limits(record.account_id),
                "queued_jobs": queued,
                "last_seen_at": iso_utc(record.last_seen_at),
            })
        return sorted(out, key=lambda a: a["account_id"])

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        self._ensure_started()
        row = self._fetch_job(job_id)
        return self._public_job(row) if row else None

    def list_jobs(self, limit: int = 50, account_id: Optional[str] = None,
                  state: Optional[str] = None) -> List[Dict[str, Any]]:
        self._ensure_started()
        limit = max(1, min(int(limit), 200))
        sql = "SELECT * FROM creaa_jobs"
        clauses, params = [], []
        if account_id:
            clauses.append("account_id = ?")
            params.append(account_id)
        if state:
            clauses.append("state = ?")
            params.append(state)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._db() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._public_job(dict(row)) for row in rows]

    # ------------------------------------------------------------------ REST-facing operations

    async def submit(self, media_type: str, body: Any,
                     idempotency_key: Optional[str] = None) -> Tuple[Dict[str, Any], bool]:
        """Validate, choose an account, persist a queued job. Returns (job, created)."""
        self._ensure_started()
        normalized = normalize_generation_request(media_type, body)
        if idempotency_key is not None:
            idempotency_key = idempotency_key.strip()
            if not idempotency_key or len(idempotency_key) > MAX_IDEMPOTENCY_KEY_CHARS:
                raise CreaaValidationError("invalid_idempotency_key", f"Idempotency-Key must be 1..{MAX_IDEMPOTENCY_KEY_CHARS} characters")
        request_hash = canonical_request_hash(media_type, normalized)

        async with self._state_lock:
            if idempotency_key:
                existing = self._fetch_job_by_key(idempotency_key)
                if existing is not None:
                    if existing["request_hash"] != request_hash:
                        raise CreaaConflictError(
                            "idempotency_payload_mismatch",
                            "Idempotency-Key was already used with a different payload",
                            job_id=existing["id"],
                        )
                    return self._public_job(existing), False

            account_id = self._choose_account(normalized["model"], media_type, normalized.get("account_id"))
            if self._count_jobs(account_id, (QUEUED,)) >= self.max_queued_per_account:
                raise CreaaValidationError(
                    "queue_full", f"account {account_id} already has {self.max_queued_per_account} queued jobs",
                    status=429, account_id=account_id,
                )
            now = time.time()
            job_id = new_job_id()
            with self._db() as conn:
                conn.execute(
                    """
                    INSERT INTO creaa_jobs (id, idempotency_key, account_id, device_id, media_type, model, state,
                        attempt_id, provider_task_id, request_json, request_hash, billing_policy, max_credits,
                        progress, result_json, error_code, error_message, resolution_json,
                        created_at, updated_at, claimed_at, submitted_at, finished_at)
                    VALUES (?, ?, ?, NULL, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, ?, NULL, NULL, NULL)
                    """,
                    (
                        job_id, idempotency_key, account_id, media_type, normalized["model"], QUEUED,
                        json.dumps(normalized), request_hash, normalized["billing_policy"], normalized["max_credits"],
                        now, now,
                    ),
                )
            row = self._fetch_job(job_id)
        debug_logger.log_info(f"[Creaa] job {job_id} queued media={media_type} model={normalized['model']} account={account_id}")
        self._schedule_dispatch()
        return self._public_job(row), True

    def _choose_account(self, model: str, media_type: str, requested: Optional[str]) -> str:
        if requested:
            record = self._accounts.get(requested)
            if record is None:
                raise CreaaValidationError("unknown_account", f"account {requested} has never registered", account_id=requested)
            catalog = self._pick_worker(requested).models if self._online_devices_for(requested) else record.models
            if not catalog_supports(catalog, model, media_type):
                raise CreaaValidationError(
                    "model_not_available_for_account",
                    f"account {requested} does not advertise {media_type} model {model}",
                    account_id=requested, model=model,
                )
            return requested
        online_accounts = sorted({c.account_id for c in self._workers.values() if c.account_id})
        if not online_accounts:
            raise CreaaValidationError("no_worker_online", "no Creaa worker is connected; pass account_id to queue for a known account", status=503)
        eligible = [
            account_id for account_id in online_accounts
            if catalog_supports(self._pick_worker(account_id).models, model, media_type)
        ]
        if len(eligible) == 1:
            return eligible[0]
        if not eligible:
            raise CreaaValidationError("model_not_available", f"no online worker advertises {media_type} model {model}", model=model)
        raise CreaaConflictError(
            "account_required", "several online accounts can serve this model; pass account_id explicitly",
            eligible_accounts=eligible,
        )

    async def cancel(self, job_id: str) -> Dict[str, Any]:
        """Queued jobs only. Anything already handed to a worker cannot be cancelled here."""
        self._ensure_started()
        async with self._state_lock:
            row = self._fetch_job(job_id)
            if row is None:
                raise CreaaValidationError("job_not_found", "job not found", status=404)
            if row["state"] == CANCELLED:
                return self._public_job(row)
            if row["state"] != QUEUED:
                code = "already_terminal" if is_terminal(row["state"]) else "cancel_unsupported_state"
                raise CreaaConflictError(
                    code,
                    f"job is {row['state']}; only queued jobs can be cancelled (provider cancellation is not available)",
                    state=row["state"],
                )
            now = time.time()
            self._update_job(job_id, state=CANCELLED, finished_at=now, error_code="cancelled", error_message="cancelled while queued")
            row = self._fetch_job(job_id)
        debug_logger.log_info(f"[Creaa] job {job_id} cancelled while queued")
        return self._public_job(row)

    async def resolve(self, job_id: str, action: str, provider_task_id: Optional[str] = None,
                      confirm_no_upstream_work: bool = False, note: Optional[str] = None) -> Dict[str, Any]:
        """Explicit operator acknowledgement for needs_review / needs_login jobs."""
        self._ensure_started()
        note = (note or "")[:500] or None
        resume_target: Optional[Tuple[Dict[str, Any], WorkerConnection]] = None
        async with self._state_lock:
            row = self._fetch_job(job_id)
            if row is None:
                raise CreaaValidationError("job_not_found", "job not found", status=404)
            if row["state"] not in RESOLVABLE_STATES:
                raise CreaaConflictError(
                    "not_resolvable", f"job is {row['state']}; only needs_review/needs_login jobs can be resolved",
                    state=row["state"],
                )
            now = time.time()
            resolution = {"action": action, "by": "operator", "at": iso_utc(now), "note": note, "from_state": row["state"]}
            if action == "resume":
                if not provider_task_id or not isinstance(provider_task_id, str):
                    raise CreaaValidationError("provider_task_id_required", "resume requires provider_task_id")
                provider_task_id = provider_task_id.strip()[:MAX_PROVIDER_TASK_ID_CHARS]
                if row.get("provider_task_id") and row["provider_task_id"] != provider_task_id:
                    raise CreaaConflictError(
                        "provider_task_id_mismatch",
                        "job already has a different provider_task_id",
                        provider_task_id=row["provider_task_id"],
                    )
                attempt_id = row.get("attempt_id") or new_attempt_id()
                self._update_job(
                    job_id, state=SUBMITTED, provider_task_id=provider_task_id, attempt_id=attempt_id,
                    submitted_at=row.get("submitted_at") or now, error_code=None, error_message=None,
                    resolution_json=json.dumps(resolution),
                )
                row = self._fetch_job(job_id)
                worker = self._pick_worker(row["account_id"])
                if worker is not None:
                    if row.get("device_id") != worker.device_id:
                        self._update_job(job_id, device_id=worker.device_id)
                        row = self._fetch_job(job_id)
                    resume_target = (row, worker)
            elif action == "fail":
                if confirm_no_upstream_work is not True:
                    raise CreaaValidationError(
                        "confirmation_required",
                        "marking failed requires confirm_no_upstream_work=true (operator verified nothing is running upstream)",
                    )
                self._update_job(
                    job_id, state=FAILED, finished_at=now, error_code="resolved_no_upstream_work",
                    error_message="operator confirmed no upstream work remains",
                    resolution_json=json.dumps(resolution),
                )
                row = self._fetch_job(job_id)
            else:
                raise CreaaValidationError("invalid_action", "action must be resume or fail")
        debug_logger.log_info(f"[Creaa] job {job_id} resolved action={action} -> {row['state']}")
        if resume_target is not None:
            await self._send_job_message(resume_target[1], "resume", resume_target[0])
        self._schedule_dispatch()
        return self._public_job(row)

    # ------------------------------------------------------------------ websocket side

    async def connect(self, websocket: Any) -> None:
        self._ensure_started()
        await websocket.accept()
        self._sockets[id(websocket)] = WorkerConnection(websocket=websocket)

    async def disconnect(self, websocket: Any) -> None:
        conn = self._sockets.pop(id(websocket), None)
        if conn is None:
            return
        if conn.device_id and self._workers.get(conn.device_id) is conn:
            del self._workers[conn.device_id]
            debug_logger.log_info(f"[Creaa] worker offline device={conn.device_id} account={conn.account_id}")
            if self._started and not self._closing:
                await self._on_worker_offline(conn)

    async def _on_worker_offline(self, conn: WorkerConnection) -> None:
        """Classify jobs bound to a device whose socket just closed."""
        async with self._state_lock:
            now = time.time()
            for job in self._jobs_in_states((CLAIMED,), device_id=conn.device_id):
                # No submitting ack was ever produced for this attempt, so no click happened.
                self._update_job(job["id"], state=QUEUED, device_id=None, attempt_id=None, claimed_at=None)
            for job in self._jobs_in_states((SUBMITTING,), device_id=conn.device_id):
                self._update_job(
                    job["id"], state=NEEDS_REVIEW, error_code="worker_disconnected_before_provider_id",
                    error_message="worker disconnected after submit intent; upstream state unknown", updated_at=now,
                )
        self._schedule_dispatch()

    async def handle_message(self, websocket: Any, data: str) -> None:
        conn = self._sockets.get(id(websocket))
        if conn is None:
            return
        conn.last_seen = time.time()
        try:
            payload = json.loads(data)
        except (TypeError, ValueError):
            await self._send(conn, {"type": "error", "code": "invalid_json", "message": "message is not valid JSON"})
            return
        if not isinstance(payload, dict):
            await self._send(conn, {"type": "error", "code": "invalid_message", "message": "message must be a JSON object"})
            return
        message_type = payload.get("type")
        try:
            if message_type == "register":
                await self._handle_register(conn, payload)
            elif message_type == "ping":
                await self._send(conn, {"type": "pong", "ts": iso_utc(time.time())})
            elif message_type == "job_event":
                await self._handle_job_event(conn, payload)
            else:
                await self._send(conn, {"type": "error", "code": "unknown_message_type",
                                        "message": f"unknown message type {str(message_type)[:50]!r}"})
        except CreaaValidationError as exc:
            await self._send(conn, {"type": "error", **exc.as_detail()})
        except Exception as exc:  # never let one bad message kill the socket loop
            debug_logger.log_error(f"[Creaa] error handling {message_type!r}: {type(exc).__name__}: {exc}")
            await self._send(conn, {"type": "error", "code": "internal_error", "message": "internal error handling message"})

    async def _handle_register(self, conn: WorkerConnection, payload: Dict[str, Any]) -> None:
        device_id = validate_opaque_id(payload.get("device_id"), "device_id")
        account_id = payload.get("account_id")
        if not isinstance(account_id, str) or not account_id.strip():
            # No shared/anonymous fallback: a worker without a stable account identity is refused.
            await self._send(conn, {"type": "error", "code": "account_id_required",
                                    "message": "register requires a stable account_id"})
            try:
                await conn.websocket.close(code=1008)
            except Exception:
                pass
            return
        account_id = validate_opaque_id(account_id, "account_id")
        models = normalize_catalog(payload.get("models"))
        capabilities = payload.get("capabilities") if isinstance(payload.get("capabilities"), dict) else {}
        try:
            json.dumps(capabilities)
        except (TypeError, ValueError):
            capabilities = {}
        label = str(payload.get("account_label") or "")[:200]

        now = time.time()
        resume_rows: List[Dict[str, Any]] = []
        pending_attempts: List[Dict[str, Any]] = []
        replaced: Optional[WorkerConnection] = None
        async with self._state_lock:
            previous = self._workers.get(device_id)
            if previous is not None and previous is not conn:
                replaced = previous
                self._sockets.pop(id(previous.websocket), None)
            if conn.device_id and conn.device_id != device_id and self._workers.get(conn.device_id) is conn:
                del self._workers[conn.device_id]
            conn.device_id = device_id
            conn.account_id = account_id
            conn.account_label = label
            conn.models = models
            conn.capabilities = capabilities
            conn.registered_at = now
            conn.last_seen = now
            self._workers[device_id] = conn

            record = self._accounts.get(account_id) or AccountRecord(account_id=account_id, first_seen_at=now)
            record.account_label = label or record.account_label
            record.last_device_id = device_id
            record.models = models
            record.capabilities = capabilities
            record.login_required = capabilities.get("logged_in") is False
            record.last_seen_at = now
            self._save_account(record)

            # Resume tracking for this account's jobs that have a stable provider id.
            for job in self._jobs_in_states(RESUMABLE_STATES, account_id=account_id):
                if not job.get("provider_task_id"):
                    pending_attempts.append({"job_id": job["id"], "attempt_id": job["attempt_id"], "state": job["state"]})
                    continue
                if job.get("device_id") != device_id:
                    self._update_job(job["id"], device_id=device_id)
                    job["device_id"] = device_id
                resume_rows.append(job)
            # Uncertain attempts are advertised so the worker can report what it knows;
            # they are never re-executed.
            for job in self._jobs_in_states((SUBMITTING, NEEDS_REVIEW), account_id=account_id):
                if job.get("device_id") != device_id:
                    self._update_job(job["id"], device_id=device_id)
                pending_attempts.append({"job_id": job["id"], "attempt_id": job["attempt_id"], "state": job["state"]})

        if replaced is not None:
            try:
                await replaced.websocket.close(code=1000)
            except Exception:
                pass
        debug_logger.log_info(
            f"[Creaa] worker registered device={device_id} account={account_id} models={len(models)} "
            f"resume={len(resume_rows)} pending={len(pending_attempts)}"
        )
        await self._send(conn, {
            "type": "register_ack",
            "device_id": device_id,
            "account_id": account_id,
            "resume_jobs": [job["id"] for job in resume_rows],
            "pending_attempts": pending_attempts,
        })
        for job in resume_rows:
            await self._send_job_message(conn, "resume", job)
        self._schedule_dispatch()

    async def _handle_job_event(self, conn: WorkerConnection, payload: Dict[str, Any]) -> None:
        job_id = payload.get("job_id")
        attempt_id = payload.get("attempt_id")
        event_state = payload.get("state")

        def reject(code: str, **extra: Any) -> Dict[str, Any]:
            return {"type": "event_ack", "job_id": job_id, "attempt_id": attempt_id, "state": event_state,
                    "accepted": False, "error": code, **extra}

        if not conn.registered:
            await self._send(conn, reject("not_registered"))
            return
        if not isinstance(job_id, str) or not isinstance(attempt_id, str) or event_state not in EVENT_STATES:
            await self._send(conn, reject("invalid_event"))
            return

        became_submitting = False
        result_state: Optional[str] = None
        async with self._state_lock:
            job = self._fetch_job(job_id)
            if job is None:
                reply = reject("unknown_job")
            elif job["account_id"] != conn.account_id:
                reply = reject("account_mismatch")
            elif job.get("device_id") != conn.device_id:
                reply = reject("device_mismatch")
            elif job.get("attempt_id") != attempt_id:
                reply = reject("attempt_mismatch", current_attempt_id=job.get("attempt_id"))
            elif job["state"] in TERMINAL_STATES:
                reply = reject("terminal_state", job_state=job["state"])
            elif not event_transition_allowed(job["state"], event_state):
                reply = reject("invalid_transition", job_state=job["state"])
            else:
                reply, became_submitting, result_state = self._apply_event(job, conn, payload)
        await self._send(conn, reply, repair=(job_id, attempt_id) if became_submitting else None)
        if result_state in TERMINAL_STATES or result_state in (NEEDS_REVIEW, NEEDS_LOGIN, QUEUED):
            self._schedule_dispatch()

    def _apply_event(self, job: Dict[str, Any], conn: WorkerConnection,
                     payload: Dict[str, Any]) -> Tuple[Dict[str, Any], bool, Optional[str]]:
        """Apply an already-authorised event. Runs under the state lock."""
        job_id, attempt_id, event_state = job["id"], job["attempt_id"], payload["state"]
        now = time.time()
        ack = {"type": "event_ack", "job_id": job_id, "attempt_id": attempt_id, "state": event_state, "accepted": True}

        provider_task_id = payload.get("provider_task_id")
        if provider_task_id is not None:
            if not isinstance(provider_task_id, str) or not provider_task_id.strip():
                return {**ack, "accepted": False, "error": "invalid_provider_task_id"}, False, None
            provider_task_id = provider_task_id.strip()[:MAX_PROVIDER_TASK_ID_CHARS]
            if job.get("provider_task_id") and job["provider_task_id"] != provider_task_id:
                return {**ack, "accepted": False, "error": "provider_task_id_mismatch",
                        "provider_task_id": job["provider_task_id"]}, False, None
        effective_provider_id = provider_task_id or job.get("provider_task_id")
        if event_state in STATES_REQUIRING_PROVIDER_ID and not effective_provider_id:
            return {**ack, "accepted": False, "error": "provider_task_id_required"}, False, None

        fields: Dict[str, Any] = {"updated_at": now}
        if provider_task_id and not job.get("provider_task_id"):
            fields["provider_task_id"] = provider_task_id
            fields["submitted_at"] = now

        progress = payload.get("progress")
        if isinstance(progress, (int, float)) and not isinstance(progress, bool):
            fields["progress"] = max(0.0, min(float(progress), MAX_PROGRESS))

        error = payload.get("error")
        error_code, error_message = None, None
        if isinstance(error, dict):
            error_code = str(error.get("code") or "")[:100] or None
            error_message = str(error.get("message") or "")[:MAX_ERROR_MESSAGE_CHARS] or None
        elif isinstance(error, str):
            error_message = error[:MAX_ERROR_MESSAGE_CHARS]

        new_state = event_state
        became_submitting = False
        if event_state == SUBMITTING:
            became_submitting = True
        elif event_state == SUCCEEDED:
            result = payload.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("urls"), list) or not result["urls"]:
                return {**ack, "accepted": False, "error": "result_urls_required"}, False, None
            if not all(isinstance(u, str) and (u.startswith("https://") or u.startswith("http://") or u.startswith("blob:")) for u in result["urls"]):
                return {**ack, "accepted": False, "error": "invalid_result_url"}, False, None
            result_json = json.dumps(result)
            if len(result_json) > MAX_RESULT_JSON_CHARS:
                return {**ack, "accepted": False, "error": "result_too_large"}, False, None
            fields.update(result_json=result_json, finished_at=now, progress=MAX_PROGRESS, error_code=None, error_message=None)
        elif event_state == FAILED:
            fields.update(finished_at=now, error_code=error_code or "worker_reported_failure",
                          error_message=error_message or "worker reported failure")
        elif event_state == NEEDS_REVIEW:
            fields.update(error_code=error_code or "worker_lost_track",
                          error_message=error_message or "worker could not determine upstream state")
        elif event_state == NEEDS_LOGIN:
            record = self._accounts.get(conn.account_id)
            if record is not None and not record.login_required:
                record.login_required = True
                self._save_account(record)
            if job["state"] == CLAIMED:
                # Nothing was submitted (no submitting ack exists). Return the job to the
                # queue; dispatch to this account pauses until a logged-in register arrives.
                new_state = QUEUED
                fields.update(device_id=None, attempt_id=None, claimed_at=None)
            else:
                fields.update(error_code=error_code or "needs_login",
                              error_message=error_message or "worker session requires login")

        fields["state"] = new_state
        self._update_job(job_id, **fields)
        ack["job_state"] = new_state
        debug_logger.log_info(f"[Creaa] job {job_id} {job['state']} -> {new_state} (event={event_state}, device={conn.device_id})")
        return ack, became_submitting, new_state

    # ------------------------------------------------------------------ dispatch

    def _schedule_dispatch(self) -> None:
        if not self._started or self._closing:
            return
        task = asyncio.create_task(self._dispatch())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def dispatch_now(self) -> int:
        """Run one dispatch pass inline (used by tests and housekeeping)."""
        return await self._dispatch()

    async def _dispatch(self) -> int:
        if not self._started or self._closing:
            return 0
        async with self._dispatch_lock:
            claimed: List[Tuple[Dict[str, Any], WorkerConnection]] = []
            async with self._state_lock:
                now = time.time()
                for account_id in sorted({c.account_id for c in self._workers.values() if c.account_id}):
                    record = self._accounts.get(account_id)
                    if record is not None and record.login_required:
                        continue
                    active = self._jobs_in_states(ACTIVE_STATES, account_id=account_id)
                    if any(j["state"] in (NEEDS_REVIEW, NEEDS_LOGIN) for j in active):
                        continue  # unknown upstream work holds the whole account, regardless of configured caps
                    limits = self.parallel_limits(account_id)
                    counts = {kind: sum(j["media_type"] == kind for j in active) for kind in ("image", "video")}
                    used = len(active)
                    worker = self._pick_worker(account_id)
                    if worker is None:
                        continue
                    for job in self._jobs_in_states((QUEUED,), account_id=account_id):
                        if used >= limits["total"]:
                            break
                        kind = job["media_type"]
                        if counts[kind] >= limits["images" if kind == "image" else "videos"]:
                            continue  # a saturated video lane must not block otherwise available images
                        attempt_id = new_attempt_id()
                        self._update_job(job["id"], state=CLAIMED, device_id=worker.device_id,
                                         attempt_id=attempt_id, claimed_at=now, updated_at=now,
                                         error_code=None, error_message=None)
                        claimed.append((self._fetch_job(job["id"]), worker))
                        used += 1
                        counts[kind] += 1
            sent = 0
            for job, worker in claimed:
                ok = await self._send_job_message(worker, "execute", job)
                if ok:
                    sent += 1
                    continue
                async with self._state_lock:
                    current = self._fetch_job(job["id"])
                    if current and current["state"] == CLAIMED and current["attempt_id"] == job["attempt_id"]:
                        # Delivery failed, so the worker never saw this attempt.
                        self._update_job(job["id"], state=QUEUED, device_id=None, attempt_id=None, claimed_at=None)
            return sent

    async def _send_job_message(self, worker: WorkerConnection, message_type: str, job: Dict[str, Any]) -> bool:
        request = json.loads(job["request_json"])
        message = {
            "type": message_type,
            "job": {
                "id": job["id"],
                "attempt_id": job["attempt_id"],
                "account_id": job["account_id"],
                "device_id": job["device_id"],
                "media_type": job["media_type"],
                "request": wire_request(request),
                "provider_task_id": job.get("provider_task_id"),
            },
        }
        return await self._send(worker, message)

    async def _send(self, conn: WorkerConnection, message: Dict[str, Any],
                    repair: Optional[Tuple[str, str]] = None) -> bool:
        """Send outside any lock. ``repair`` names a (job, attempt) whose freshly persisted
        ``submitting`` state must remain held: an exception does not prove the ack was undelivered."""
        try:
            await conn.websocket.send_text(json.dumps(message))
            return True
        except Exception as exc:
            debug_logger.log_warning(f"[Creaa] send failed device={conn.device_id}: {type(exc).__name__}: {exc}")
            if repair is not None:
                job_id, attempt_id = repair
                async with self._state_lock:
                    current = self._fetch_job(job_id)
                    if current and current["state"] == SUBMITTING and current["attempt_id"] == attempt_id:
                        self._update_job(job_id, state=NEEDS_REVIEW,
                                         error_code="submit_ack_delivery_uncertain",
                                         error_message="submit acknowledgement delivery is uncertain; reconcile before another submission")
                self._schedule_dispatch()
            return False

    # ------------------------------------------------------------------ housekeeping

    async def run_housekeeping(self, now: Optional[float] = None) -> Dict[str, int]:
        """Time-based safety nets. Never re-executes anything."""
        self._ensure_started()
        now = time.time() if now is None else now
        counts = {"claimed_failed": 0, "submitting_review": 0, "running_review": 0}
        async with self._state_lock:
            for job in self._jobs_in_states((CLAIMED,)):
                if now - (job.get("claimed_at") or job["updated_at"]) > self.claimed_timeout:
                    self._update_job(job["id"], state=FAILED, finished_at=now, error_code="worker_unresponsive",
                                     error_message="worker never acknowledged the execute message")
                    counts["claimed_failed"] += 1
            for job in self._jobs_in_states((SUBMITTING,)):
                if now - job["updated_at"] > self.submitting_timeout:
                    self._update_job(job["id"], state=NEEDS_REVIEW, updated_at=now, error_code="submit_ack_timeout",
                                     error_message="no submitted/failed report after submit intent; upstream state unknown")
                    counts["submitting_review"] += 1
            for job in self._jobs_in_states((SUBMITTED, RUNNING)):
                if now - job["updated_at"] > self.running_timeout:
                    self._update_job(job["id"], state=NEEDS_REVIEW, updated_at=now, error_code="progress_timeout",
                                     error_message="no progress report for too long; upstream state unknown")
                    counts["running_review"] += 1
        if any(counts.values()):
            debug_logger.log_warning(f"[Creaa] housekeeping: {counts}")
            self._schedule_dispatch()
        return counts

    async def _housekeeping_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.housekeeping_interval)
                await self.run_housekeeping()
                await self._dispatch()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                debug_logger.log_error(f"[Creaa] housekeeping error: {type(exc).__name__}: {exc}")
