# Shared Agent Status

Last updated: 2026-09-15

## Active

- 2026-09-15: Owner asked to remove Creaa completely. Branch `remove-creaa` (from `dd6730f`) deletes the Creaa backend (`src/api/creaa.py`, `src/core/creaa_models.py`, `src/services/creaa_bridge.py`), the Creaa extension modules, all Creaa tests/fixtures and `docs/creaa-*.md`; `src/main.py`, `background.js`, `options.*`, extension README and manifest restored to their pre-Creaa (`main` 3272911) state. Extension manifest bumped **3.3.10 -> 3.4.1** so the owner's local 3.4.0 test build updates; globally published package still **3.3.10**. The build method is preserved in `docs/adding-a-browser-provider.md` (SOP for the next site); the Creaa code itself is recoverable at `dd6730f`. PR #16 merged as `c367c2b`; Coolify auto-deploy verified running image `c367c2b` healthy at 2026-09-15 ~01:45 UTC, `/health` 200, `/v1/creaa/models` 404. Stray `creaa.db` deleted from the data volume by hand (69 KB). Branch `codex/creaa-browser-provider` deleted locally and on fork. Next site under planning: Flow Music (`tmp/flowmusic_provider_plan.md`, Codex critique in progress); finding so far: Flow Music has a Supabase refresh-token session, so it should be a backend token-row provider like Google Flow, not a browser bridge like Creaa.

- Production runs `c367c2b` (PR #16); globally published extension package remains **3.3.10** (repo manifest is 3.4.1, not yet published via the ext-update zip). `feat/async-submit-status` remains unmerged; leave it alone.
- Owner authorized the permanent repository fix and normal Coolify deployment for database thread exhaustion. Branch: `codex/fix-database-thread-leak`. PR #11 merged as `04befb0`; first rollout retained the prior container because Coolify overrides the Docker health command with curl/wget, absent from python:slim. PR #12 added curl to the standard Dockerfile. Coolify production is configured for `/Dockerfile.headed`; follow-up adds curl and the image health check to that file as well. No container patch. PR #13 rollout verified finished on 2026-09-07 11:53 UTC.
- Fix: defer request cancellation until SQLite acquisition/cleanup finishes; shield AnyIO scope cancellation and repeated asyncio task cancellation; allow at most 32 simultaneous connections per Database instance, holding permits until worker threads exit. Writer serialization stays intact.
- Health: `/health` returns HTTP 503 on DB failure; Dockerfile includes a health check. Coolify application health check is enabled for `/health`.
- Validation: 10 new regression tests pass with asyncio and uvloop. Original connection code fails the new cancellation-during-open regression. Full suite: 106 pass, 1 pre-existing failure in `test_api_captcha_fingerprint` (unchanged FlowClient code). Tests cover open/query/close cancellation, repeated cancellation, AnyIO disconnect scopes, 100 competing requests, 200 repeated disconnects, failed opening/configuration, capacity reuse and HTTP health status.
- Upstream `origin/main` contains a README-only chart fix since the production fork; merged before deployment to satisfy the ancestor requirement.

- Linux image verification: 200 isolated disconnects against deployed fix code returned thread count to baseline (1 -> 1).

## September 7 incident

- At ~11:33 UTC: Python had 37,550 threads and reached container process/thread limit 37,556. Database-backed login and generation raised `RuntimeError: can't start new thread`; HTTP health misleadingly returned 200. Last recorded successful image completed 04:50:54 UTC. All 63 accounts remained saved.
- Owner-authorized recovery used **Coolify CLI**, `coolify app restart jc4044co4w8w0g8w0g4ks0ws`, at ~11:35 UTC. Replacement container `...-113516935692` used the unchanged image. Public authenticated tokens/stats endpoints recovered, browser connections returned, and successful generation resumed by 11:36:16 UTC. Threads fell to 6 and memory to ~211 MiB.
- Isolated reproduction against deployed `aiosqlite==0.20.0`: 10 cancelled connection setups leaked 10 worker threads. Its connection-opening exception handler misses `asyncio.CancelledError`; failed context entry never invokes context exit. This is a proven leak mechanism consistent with exhaustion, though stacks for every pre-restart thread were not captured.
- Evidence on Mac: `/tmp/flow2api-incident-20260907/`. The database and extension settings were not modified during recovery. No runtime-container patch was used.

## Coordination

- Work happens in the owner's Mac clone or `~/projects/flow2api` on the command-center box; `~/mac-mirror/` is read-only.
- Merging to fork `main` auto-deploys via Coolify and briefly drops extension websockets; they reconnect automatically. Notify the owner before rollout to coordinate other sessions.
- Before commit/push/deploy: reread/update this file and include it in the change; prune stale assertions. Never deploy a tree missing current `origin/main` or production `fork/main`.
- Permanent fixes must be committed to the repository and deployed normally. Do not edit running containers or use temporary Docker patches.
