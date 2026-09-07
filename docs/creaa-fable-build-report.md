> Historical Fable handoff report. Codex subsequently fixed catalog validation and acknowledgement-delivery ambiguity; current implementation and tests supersede those original claims.

# Creaa browser-bridge broker — Fable build report

Date: 2026-09-08. Branch `codex/creaa-browser-provider`, repo `~/my-projects/flow2api`.
Author: Fable 5.1 (implementation only; no commit, push or deploy). Codex owns main.py, extension, docs.

## Files created (only these)

| File | Purpose |
|---|---|
| `src/core/creaa_models.py` | Pure helpers: job states, allowed worker transitions, request validation, idempotency hash, catalog helpers. No I/O. |
| `src/services/creaa_bridge.py` | `CreaaBridge`: SQLite-backed job store, worker registry, dispatcher, event validation, recovery, housekeeping. |
| `src/api/creaa.py` | REST routes under `/v1/creaa/*` (existing API key via `verify_api_key_flexible`) and the `/creaa_ws` worker WebSocket (same key rules as `/captcha_ws`). |
| `tests/test_creaa_bridge.py` | 19 tests: validation, durability across restart, serialisation, disconnect, wrong-worker rejection, event order/immutability, cancel, resolve, needs_login, ack-delivery failure, account selection, queue bound, housekeeping, duplicate device, REST+WS end to end. |

No other repo files touched. Codex's `src/main.py` diff already wires
`CreaaBridge(db_path=Path(db.db_path).with_name("creaa.db"))`, `creaa.set_service(...)`,
`app.include_router(creaa.router)` and `start()/close()` in lifespan; that matches the public interface.

## Test results

Runner: the project venv has no pytest/httpx, so I used Homebrew python3.12 with the venv site-packages plus
pytest/pytest-asyncio/httpx installed into `/tmp/creaa-pydeps` (scratch runner `/tmp/creaa-run-pytest.py`).
Nothing was installed into the repo venv.

- `tests/test_creaa_bridge.py`: **19 passed**, no warnings.
- Full suite: **125 passed, 1 failed** — the pre-existing `test_api_captcha_fingerprint` failure already noted in WORK.md. Unrelated to this work.

## Design as built

**Job states**: `queued → claimed → submitting → submitted → running → succeeded|failed`, plus `cancelled`,
`needs_review`, `needs_login`. Terminal = succeeded/failed/cancelled (immutable). Slot-holding states =
claimed/submitting/submitted/running/needs_review/needs_login → exactly one per account.

**Durable before wire**: dispatcher persists `claimed` (attempt_id, device_id) before sending `execute`.
`submitting` is persisted before the `event_ack` is sent; the worker must wait for that ack before clicking.
If the ack send raises, the job is returned to `queued` (worker provably never released).

**Restart recovery** (`start()`): `claimed` → `queued` (no submitting ack could have been produced, so no click);
`submitting` without provider id → `needs_review` (holds slot, no execute); `submitted`/`running` keep state and
get a `resume` (same job id + attempt id + provider_task_id) when a worker for that account registers.

**Disconnect**: `claimed` → `queued`; `submitting` → `needs_review`; provider-id states untouched until resume.

**Event validation**: rejects unregistered socket, unknown job, account mismatch, device mismatch, attempt mismatch,
terminal state, illegal transition (e.g. submitted before submitting, running→submitting), `submitted`/`running`/
`succeeded` without a provider id, provider id change, succeeded without result urls, oversize result.
Rejections reply `event_ack` with `accepted:false` and `error` code; accepted acks add `job_state`.

**needs_login**: while `claimed` → back to `queued` and the account is paused until a register with
`capabilities.logged_in` not false. Any later state → `needs_login` holds the slot; resumable on register only if a
provider id exists, otherwise needs explicit resolve.

**Resolve** (`POST /v1/creaa/jobs/{id}/resolve`, strict schema, extra fields rejected):
`{"action":"resume","provider_task_id":"..."}` → `submitted` and `resume` sent if a worker is online;
`{"action":"fail","confirm_no_upstream_work":true}` → `failed`. Only from needs_review/needs_login, else 409.

**Cancel**: queued only; 409 `cancel_unsupported_state` / `already_terminal` otherwise. No provider cancellation claimed.

**Idempotency-Key**: same key + same canonical payload → same job (202 + `Idempotent-Replayed: true`);
different payload → 409 `idempotency_payload_mismatch`.

**Account selection**: explicit `account_id` must be a previously registered account whose catalog advertises the
model (queueing while offline allowed). Without it: exactly one eligible online account is chosen; zero → 400/503;
several → 409 `account_required` with `eligible_accounts`. No cross-account fallback ever.

**Validation limits**: prompt ≤ 4000 chars, n=1 only, ≤ 4 references (http(s) URL ≤ 2048 chars or
`data:image/*;base64` ≤ 8,000,000 chars; never fetched), aspect_ratio/image_size/quality/resolution enums,
duration 1–60 s, `allow_credits` requires `max_credits` 1–100000, default `unlimited_only`, `creaa/` prefix
stripped once, unknown fields rejected, image-only vs video-only fields enforced. Queue bound 100 queued per account (429).

**Housekeeping** (every 15 s by default, disabled with `housekeeping_interval=0`): stale `claimed` (120 s) →
failed `worker_unresponsive`; stale `submitting` (180 s) → needs_review; no progress on submitted/running for
3600 s → needs_review. Never re-executes.

**Storage/concurrency**: stdlib `sqlite3`, WAL, single connection, `check_same_thread=False`, short inline
operations under a `threading.Lock`; all state changes under one `asyncio.Lock`; WebSocket sends outside locks.
Single-process only (documented in the module docstring). No aiosqlite, no per-request threads.

**Logs/payloads**: logs carry job ids, states, device/account ids only — never prompts, references, or keys.
Job JSON echoes the request with references reduced to `{type, length}`.

## Wire protocol as implemented (matches Codex's `creaa-worker.js` usage)

- `register` → `register_ack {device_id, account_id, resume_jobs:[ids], pending_attempts:[{job_id, attempt_id, state}]}`
  then zero or more `resume {job:{...}}`. Missing `account_id` → `error account_id_required` + close 1008.
  Same `device_id` again → old socket closed (1000), new one takes over.
- `ping` → `pong`.
- `execute|resume {job:{id, attempt_id, account_id, device_id, media_type, request:{model, prompt, aspect_ratio,
  image_size, quality, duration, resolution, references, billing_policy, max_credits}, provider_task_id}}`.
- `job_event {job_id, attempt_id, state, provider_task_id?, progress?, result?:{urls:[...]}, error?}` →
  `event_ack {job_id, attempt_id, state, accepted, job_state?|error?}`.
- Error frames: `{type:'error', code, message, ...}`.

## Not done / open points for Codex or the owner

- No artifact download, no media bytes handling (result stores original URLs only), by scope.
- `/v1/creaa/models` lists only models advertised by currently connected workers; offline accounts show in
  `/v1/creaa/accounts` with their last catalog.
- `pending_attempts` in `register_ack` also lists `submitting`/`needs_review` jobs so the worker can report the
  truth for that same attempt (a late `submitted` with provider id, or `failed`) — this is accepted and moves the
  job forward without operator action. Worker must never re-click for those.
- WORK.md line for this work is Codex's to add (I did not touch it).
- Scratch files outside the repo: `/tmp/creaa-pydeps/`, `/tmp/creaa-run-pytest.py`. Safe to delete.
