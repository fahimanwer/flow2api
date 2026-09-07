# Shared Agent Status

Last updated: 2026-09-07

## Active

- Production currently runs `5247d6c` (PR #10), extension package **3.3.10**. `feat/async-submit-status` remains unmerged; leave it alone.
- Owner authorized the permanent repository fix and normal Coolify deployment for database thread exhaustion. Branch: `codex/fix-database-thread-leak`. PR #11 merged as `04befb0`; first rollout retained the prior container because Coolify overrides the Docker health command with curl/wget, absent from python:slim. Follow-up adds curl to the repository Dockerfile; no container patch. Deployment verification pending.
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
