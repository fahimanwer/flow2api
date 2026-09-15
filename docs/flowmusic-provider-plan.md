# Plan: add Google Flow Music (flowmusic.app) as a provider in Flow2API

Date: 2026-09-15. Author: Claude (Fable 5.1). **Revision 3, design locked pending the spike.**
Codex (gpt-6-astra, high effort) reviewed rounds 1 and 2; both verdicts ship-with-changes,
every P1 accepted and folded in. Critique transcripts: `tmp/codex_flowmusic_provider_plan_0915-0711.md`
and `..._0915-0718.md` (local, not committed). No code written yet.

Status of the gates: **the live spike (section 3) has not been run.** It needs a signed-in
browser on a dedicated Google account, which this session did not have (Claude Chrome
extension not connected). Implementation is blocked on it.

## 0. Context

- Flow2API today: FastAPI backend at `flow.ashuthefire.com` proxying Google Flow (Veo/Imagen)
  through per-account session tokens (`tokens` table, `src/core/database.py:729`), a Chrome
  worker extension for reCAPTCHA minting, WARP egress proxy. Shared API key for generation
  (`src/core/auth.py:44`); admin session auth for account management (`src/api/admin.py:701`).
- Creaa provider (browser-driven WebSocket bridge) removed 2026-09-15 (PR #16). Method kept
  in `docs/adding-a-browser-provider.md`, which now starts with a "which kind of provider"
  decision table. Flow Music is the token-row kind (1.2, 1.4), not the browser-bridge kind.
- Owner goal: add https://www.flowmusic.app so callers can generate songs through Flow2API,
  same backend, same API key, staff-access policy unchanged.

## 1. Evidence

### 1.1 Product (REPORTED, public pages, fetched 2026-09-15)
- Google Flow Music = ex-ProducerAI (Google Labs, Feb 2026; rebrand Apr 2026), ex-Riffusion.
  Models: Lyria 3.5 (flagship since Jul 2026) and Lyria 3 Pro. Full songs ~2-3 min, mp3/wav/
  m4a + stems, free downloads. Each generation returns an A/B pair (up to 2 clips).
  Generation ~40-150 s (one third-party measurement: 139 s).
- Plans (pricing page): Free = daily top-up credits, 2 concurrent; Starter $6 = 3,000
  credits/mo (~600 songs), 8 concurrent; Plus $18 = 10,000, 12; Member $48 = 30,000, 16.
  Google AI Plus members get Starter benefits (support.google.com/googleone/answer/16882689).
  Google AI Pro/Ultra: UNKNOWN; settle by reading the tier after login on the owner's account.
- Cost per song: third-party claim ~5 credits per song for direct generation vs far more for
  Producer-agent turns. **Unverified**; the usage sample is day-aggregated. Measure the full
  debit for one request (both clips, any lyrics step) in the spike.

### 1.2 Auth (REPORTED from useapi.net setup doc + owner-pasted authorize URL)
- Supabase Auth at `sb.flowmusic.app`, Google OAuth PKCE, scope
  `subscriptions.thirdparty.googleone.eligibility`, `access_type=offline`, `prompt=consent`.
  The Google One checkbox at first login unlocks AI-plan benefits.
- Session = Supabase JWT `access_token` (~1 h) + `refresh_token`. useapi.net runs Flow Music
  server-side from the refresh token alone (register, auto-refresh, submit, poll, download),
  up to 50 accounts, `maxJobs` 1-16 per account.
- Refresh tokens rotate. Supabase documents a reuse interval and parent-token recovery, so
  the other holder is not always invalidated instantly; Flow Music's exact behaviour is
  UNKNOWN and is a spike item. Signing out revokes. useapi's rule: after handing the token to
  the server, do not sign out and do not keep using that browser profile on the site.
- useapi's setup requires one manual generation plus one image and one audio upload in the
  browser first, to dismiss first-use consent dialogs. Which of those matter for text-only
  v1 is UNKNOWN; spike item.

### 1.3 Official alternative (CONFIRMED, ai.google.dev pricing page)
- Gemini API (Interactions API, paid tier only): **Lyria 3.5 full song $0.08**; the separate
  **Lyria 3 Clip Preview** model is $0.04 per 30 s clip. Synchronous, official, no session
  juggling.
- Flow Music session path: ~$0.01/song on paid plans or free daily credits, plus stems and
  edits. Same ToS posture as Flow2API's existing Google Flow use (general Google ToS; no
  Flow Music-specific automation clause found). This is the owner's call; it is **not yet
  recorded** in WORK.md and must be before implementation starts.

### 1.4 Native API contract (CONFIRMED from the public Next.js bundle, no login, 2026-09-15)
Read by Claude from `https://www.flowmusic.app/_next/static/chunks/pages/_app-c35355ef6000fcf4.js`
(build `agNS0p8X3s8OHg7wc1gGi`). Codex could not fetch the bundle and did not independently
certify this section. These are **client-side observations**: they show what the inspected
frontend does, not what the server guarantees.

- Base URL `https://wb.flowmusic.app` (env variants `wb-*.flowmusic.app`; the client refuses
  any origin not ending in `.flowmusic.app`). Requests go through `ky` with a `beforeRequest`
  hook that sets only `Authorization: Bearer <supabase access_token>` (plus optional
  `x-country-override`/`x-region-override` from a debug localStorage key). Timeout 60 s.
  **No challenge header (Turnstile/reCAPTCHA/App Check/signature) was found in the inspected
  client path.**
- Supabase project ref `ednjccqcmbxeaxbidinr`; the anon key is embedded in the bundle
  (public). Auth uses `@supabase/ssr`, i.e. cookie storage, not localStorage.
- Account: `GET /users/me` (fields include `has_g1_entitlement`, `subscription_plan`),
  `GET /billing/credits` (frontend polls every 5 min), `GET /subscription`, `GET /models`
  (`{models:[{public_name, is_default, ...}]}`; model names such as `lyria-3.5` are not
  hard-coded, they come from this call). **No concurrency-cap field was identified yet**;
  the spike must find the source of the cap (see 3.3).
- Generation is the Producer conversation flow. No standalone song-generation endpoint was
  found in the inspected client (only `POST /generate/image` for images):
  - `POST /conversation/create` -> `{conversation_id}`
  - `POST /conversation` body `{conversation_id, parts, client_context, model_name, mode}`
    -> `{job_id}`. Here `mode` is the **chat mode** (captured default `"standard"`), and
    `model_name` is the selected model's `public_name`. `client_context` =
    `{current_song_id, song_queue, project_id, selected_model, lyrics_id_map,
    ghostwriter_version ("standard" default), enable_lyria_agent (client flag, default off)}`.
  - `parts` = `[{part_kind:"user-prompt", content:<text or [text, uploads...]>}]`, and when
    the compose panel was edited, extra `tool-call` parts carrying a **compose recipe**
    `{mode:"create", title, imageId, lyrics, soundPrompt, instrumental, advanced, seed, bpm,
    length}`. The recipe `mode` values are `create | extend | replace | cover | advanced`
    (`inputClipId` for the edit modes, `recipeId` for advanced). This recipe mode is a
    different field from the conversation chat mode above. Server op types seen in
    transcripts: `audio__create_song` (`sound_prompt`, `seed`) and `audio__modify_song`.
  - `GET /messages/{job_id}/stream?last_id=0` -> SSE with events `begin, conversation_id,
    part, suggestion, generated-title, error, complete, final`. Part kinds: `user-prompt,
    text, thinking, tool-call, tool-return, retry-prompt`. Results are identified inside
    `tool-return` parts by `{operation_id, clip_id}` and `{operation_id_b, clip_id_b,
    a_b_test_id}` for the A/B pair. Whether SSE `complete` implies playable audio is
    UNKNOWN (3.2).
  - The frontend can rediscover a running job: `GET /conversations/{id}/running-job` ->
    `{job_id}`, then reopen the stream. Cancel: `POST /conversations/{id}/stop`.
    `GET /conversations/{id}/usage` exists. `GET /audio-create-song-status/{id}` exists
    under "operations" (likely the audio operation status; spike item).
  - HTTP 429 on submit shows "Producer is busy helping others" (site-level rate limit).
    Credit failure strings in the UI: "You're out of credits", "Not enough credits".
- Clips and audio: `POST /clips` (list), `GET /clips/auth-user`, `PATCH /clips/{id}`,
  `GET /download/audio/{clip_id}` (with query params), `POST /batch-download-clip`,
  `GET /stems/clip/{clip_id}`.
- Not found: any official CLI or developer path. `/auth/cli/callback` and transfer-token
  endpoints exist but write cookies for `producer.ai` (legacy internal).

## 2. Design decision (locked)

**Backend-only, token-row provider (like Google Flow), not a browser bridge (like Creaa).**

Why: the site has a server-holdable session credential; the inspected client sends no
anti-bot token; jobs have durable ids with rediscovery and stop endpoints. The browser was
the only reason for Creaa's WebSocket/lease/tab machinery. That is out. What stays from
the SOP: uncertain submissions and durable recovery, because a lost HTTP response after an
accepted submit is the same problem wherever the request runs.

Kept: separate tables inside the existing `flow.db` through the **existing `Database`
instance** (`src/core/database.py:51`: shared connection cap, writer lock, cancellation-safe
cleanup), not a second file. No extension change in v1 (manifest stays 3.4.1). Official
Gemini API documented as the fallback if sessions prove unstable.

Approved by Codex round 2 with conditions (both folded in below):
(a) audio via an authenticated proxy keyed by persisted clip id; (b) automatic
`needs_review` recovery via `running-job`, only under the rules in 4.2.

## 3. Step 1: spike (hard gate; owner runs it)

Dedicated Google account with Flow Music access (18+, supported region, Google One box
ticked if it has an AI plan). ~1-2 h. Secrets go into the backend later, never into docs.

### 3.1 Browser capture
1. Sign out, DevTools Network with Preserve log, sign in. Note field names of the
   `sb.flowmusic.app/auth/v1/token?grant_type=pkce` response and the auth cookie names.
2. Complete first-use consent: generate one text-only song in the UI. Note any consent call
   that precedes the first `POST /conversation`, and whether image/audio uploads are needed.
3. Capture the full submit exchange: `POST /conversation/create`, `POST /conversation`
   (`parts`, compose recipe, `client_context`, `model_name`, chat `mode`), every header and
   cookie, and all SSE events through `complete`/`final`. Then `GET /conversations/{id}/usage`
   and `GET /billing/credits` before and after. Record the debit for the whole request.
4. Reload mid-generation: confirm `running-job` returns the same `job_id` and the stream
   resumes with `last_id`.
5. Submit N+1 where N is the account's concurrency cap. Capture the rejection. Also note
   whether one Producer request occupies one slot or its two audio operations occupy two.

### 3.2 Terminal semantics (Codex round 2)
6. Distinguish three moments: conversation `complete`, audio operation completion
   (`/audio-create-song-status/{id}` or equivalent), and clip download readiness
   (`/download/audio/{clip_id}` returns bytes or a URL, and when). Note a case where the
   conversation completes with text only and no clip.
7. Open `/download/audio/{clip_id}`: bytes or signed URL, expiry hints; re-open after 1 h.
8. Recovery after the fact: submit, deliberately discard the response, wait for the song to
   finish, then check whether `running-job` (expected empty), conversation history, or a
   clip listing lets you find that song's clips. This decides whether recovery after an
   outage is automatic or operator-only.

### 3.3 Server-side proof and caps
9. Hand off the session (stop using the browser profile; do not sign out). From the Hetzner
   box via WARP and direct: refresh the token, `GET /users/me`, `POST /conversation/create`,
   `POST /conversation`, consume the stream, `GET /download/audio/{clip}`. Wait past
   access-token expiry and repeat with the newly saved refresh token. Then touch the
   browser tab once and see whether the server's refresh token still works (rotation
   reuse-interval behaviour).
10. Find the concurrency cap source: a field on `/users/me`, `/subscription`, `/billing/*`,
    or none. If none, record the tier->cap mapping from the pricing page and treat unknown
    tiers as cap 1.
11. Write `docs/flowmusic-spike.md`: endpoints, bodies with field names, timings, caps,
    debit per song, URL behaviour, recovery findings. No secrets.

Exit criteria: server-side refresh + submit + stream + download succeed from the intended
egress with no challenge; `running-job` identifies the submission; terminal semantics and
clip readiness understood; debit measured; cap source known.

## 4. Step 2: implementation

### 4.1 Data (in the existing `Database`)
- `flowmusic_accounts`: id, upstream_user_id UNIQUE, email, refresh_token, access_token,
  access_expires_at, status (`ready|needs_login|cooldown|disabled`), operator_disabled
  (bool, independent of health), cooldown_until, subscription_plan, has_g1_entitlement,
  provider_cap, operator_limit, credits_snapshot, credits_checked_at, last_refresh_at,
  last_error, created_at, updated_at.
- `flowmusic_jobs`: id, account_id, state, attempt_id, conversation_id, provider_job_id,
  operation_ids_json, clip_ids_json, idempotency_key UNIQUE, request_hash, request_json,
  result_json, error, created_at, updated_at, submitted_at, completed_at. Indexes on
  (state, account_id) and (account_id, state). Job list paginated.
- Credentials never appear in list/get responses (explicit projection).

### 4.2 State machine
```
queued -> submitting -> submitted -> finalizing -> succeeded
   |          |  \-> failed (verified rejection, no upstream work)
   |          \-> needs_review -> submitted (positive reconciliation)
   |                            \-> failed (operator confirms no upstream work)
   |                 submitted -> failed (verified terminal upstream failure)
   \-> cancelled (conditional UPDATE while still queued)
```
- Each job gets a **fresh conversation** used for exactly one attempt. The
  `conversation_id` is committed together with `submitting`, account and `attempt_id`
  **before** `POST /conversation` is sent.
- Lost response / timeout / crash after submit -> `needs_review`, slot held. Automatic
  reconciliation: query `running-job` for the persisted conversation; a positive `job_id`
  moves the same job+attempt to `submitted` (conditional update, so a late response cannot
  override an operator resolution). **An empty or failed lookup keeps `needs_review`**; it
  never releases the slot or resubmits. If 3.2 step 8 shows finished songs are discoverable
  by conversation, add that as a second automatic path; otherwise operator-only.
- Once `provider_job_id` is known, reconnect to that job's stream directly.
- `finalizing` = upstream reported completion; operation/clip ids persisted first; then
  verification that at least one clip is downloadable. **Success = at least one verified
  playable clip.** A completed conversation with no clip is `failed` with the transcript
  error. A missing B clip becomes a partial success only once its operation is terminal.
- Account status is separate: `needs_login`/`cooldown`/`disabled` stop dispatch; jobs in
  `submitted`/`finalizing`/`needs_review` remain and resume after re-auth. Changing an
  account's status never settles a job or releases its slot.
- Poll/stream timeout = observation failed, not generation failed.
- A download failure after `succeeded` is a retrieval error; it never turns the job into a
  retryable generation failure and the clip ids are preserved.

### 4.3 Dispatch
- One transaction: pick account (status ready, not operator-disabled, cooldown expired,
  credits not known-exhausted, free slots), reserve slot, `queued -> submitting`.
  Occupied = submitting + submitted + finalizing + needs_review. Cap =
  `min(operator_limit, provider_cap)`; unknown provider cap -> 1.
- Idempotency: DB unique key; same key + different `request_hash` -> 409; same -> existing
  job. Admission is atomic and checked after the idempotency lookup; the queue bound is
  **global** (jobs get an account only at dispatch), default 50 queued.
- Credit exhaustion (rejection shape from the spike, or snapshot <= 0) sets `cooldown` with
  a persisted `cooldown_until` and a recheck on expiry. Health recovery never clears
  `operator_disabled`.

### 4.4 Session refresh
- Per-account lock held by the shared refresh task through **refresh and durable save**
  (the task is shielded as a whole; waiters may be cancelled, the task is not).
- After acquiring, re-read the row: if a newer access token exists, use it and skip.
- Save refresh_token + access_token + expiry in one write **before** any tier/credits call.
- Admin credential replacement takes the same lock and must verify the same
  `upstream_user_id`; a different upstream user is rejected (it cannot inherit jobs).
- Classify: `invalid_grant`/`refresh_token_not_found` -> `needs_login`; transport/5xx/429 ->
  `cooldown` with backoff; malformed -> log + `cooldown`. Never disable on a bare 400/401.
- One process owns an account's credential. Coolify replaces containers; verify during the
  first deploy whether the old container is stopped before the new one starts (Coolify
  rolling update). If overlap exists, gate refresh on a DB-level lease row (`refresh_lease`
  with owner id and expiry) rather than only `asyncio.Lock`.

### 4.5 Client (`src/services/flowmusic_client.py`)
- httpx, proxy from `ProxyManager` like `FlowClient`; endpoints exactly as in 1.4 + spike
  doc; typed errors on unexpected shapes; SSE consumer with `last_id` resume; bounded
  reconnects.
- Auth calls log only method/path/status. Bodies with tokens never reach the request/response
  body logging in `src/core/logger.py:172` and `:233`.

### 4.6 API (`src/api/flowmusic.py`)
- Shared API key: `GET /v1/flowmusic/models`, `POST /v1/flowmusic/music/generations` ->
  202 `{job_id}`, `GET /v1/flowmusic/jobs` (paged), `GET /v1/flowmusic/jobs/{id}`,
  `POST .../cancel`, `GET /v1/flowmusic/jobs/{id}/audio/{clip_id}?format=mp3|wav`,
  `GET /v1/flowmusic/accounts` (projected, no tokens).
- Admin session auth (`src/api/admin.py:701` dependency): `POST /api/flowmusic/accounts`
  (import refresh token), `PUT .../{id}/credentials` (replace; same upstream user only),
  `PUT .../{id}/limits`, `POST .../{id}/enable|disable`, `DELETE` (see 4.7),
  `POST /api/flowmusic/jobs/{id}/resolve` (`resume` with job id, or `fail` with
  `confirm_no_upstream_work`).
- Admin UI: one section on the existing admin page.

### 4.7 Audio delivery
v1 promises **authenticated streaming, not retained files.** The audio endpoint checks
that `clip_id` belongs to the job, uses the credentials of the job's recorded account,
reacquires a download location by clip id via `/download/audio/{clip_id}`, follows a
signed URL server-side **without forwarding the account bearer to another origin**, streams
with bounded time and concurrency, validates content-type, and closes upstream on caller
disconnect. Availability depends on upstream retention and usable account credentials;
Flow2API does not promise it beyond that. No files in the public `/tmp` mount
(`src/main.py:338`), no `file_cache.py` changes.

Account lifetime is therefore part of the result contract: ordinary `DELETE` is refused
while any job (succeeded included) still references the account; a separate
`DELETE ...?retire_audio=true` retires those audio links (jobs keep clip ids, audio returns
410). Disabled accounts still refresh for retrieval of existing jobs. Retained storage is
a v2 option.

### 4.8 Request surface (v1)
`model` (`flowmusic/<public_name>`), `prompt` (<=10k), `instrumental` (bool), `lyrics`
(optional, <=10k); sent as a user prompt plus a `create` compose recipe exactly as the
frontend does. `n` fixed by upstream (A/B). Deferred: references, stems, extend/replace/
cover, ghostwriter selection, projects, agent mode.

### 4.9 Wiring
`src/main.py`: construct service with the existing `db`, `start()`/`close()` in lifespan,
include router. Extension untouched.

## 5. Tests
Unit: validation; every transition in 4.2 including lost response, crash before id
persist, empty `running-job`, late response after operator resolution; conditional cancel;
dispatch vs cap change/disable/cooldown race; global admission bound; idempotency concurrent
+ mismatched body; refresh single-flight, stale-token 401, waiter cancel, replacement during
refresh, wrong upstream user on replacement; restart with jobs in every non-terminal state;
re-auth resumes; completion with zero clips; partial A/B; download failure after success;
admin endpoints reject the shared API key; tokens absent from responses and logs.
HTTP tests for every route. Client tests from sanitized spike fixtures.
Live (owner, bounded): one song end-to-end via `flow.ashuthefire.com`, credits before/after
recorded in WORK.md; then N+1 probe.

## 6. Risks
1. Token rotation / one-owner rule; onboarding is a manual paste. Re-auth path in 4.4.
2. Producer-conversation cost vs the claimed 5-credit path: measure; if every request costs
   agent-tier credits, recompute the economics against $0.08/song official.
3. Site-level 429 rate limits independent of plan caps.
4. Regional/consent gates at submit from a datacenter egress: spike 3.3 decides.
5. Recovery after an outage may be operator-only (spike 3.2 step 8).
6. Effort: spike 0.5-1 day; backend + tests 2-3 days; admin UI 0.5 day.
