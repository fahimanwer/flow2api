# Creaa rollout evidence — 2026-09-08 IST

- Built jointly with Fable 5.1 using `claude -p --model claude-fable-5-1`; full backend and extension review records are in this directory.
- PR: https://github.com/fahimanwer/flow2api/pull/14 (merged).
- Production commit: `5e63d48a720f87eb9d617d5718706e9d0a4b9555`.
- Coolify app: `jc4044co4w8w0g8w0g4ks0ws`; deployment `rfusrjcvqwgvswwyxzrhweve`, finished 2026-09-07 19:36:16 UTC (2026-09-08 01:06:16 IST).
- Verified HTTP 200: `/health` (backend running, active Flow accounts), `/v1/creaa/accounts`, `/v1/creaa/models` using the existing Flow2API key.
- Creaa accounts/models at verification: 0. No image/video generation success or zero-credit billing result has been claimed.
- Tests: 22 Creaa Python tests and 19 Node tests passed. Full Python suite: 128 passed, 1 previously documented CAPTCHA fingerprint failure. GitHub image-build CI did not start because its account was locked for billing; Coolify performed the actual production build successfully.
- Extension: 3.4.0, same default server/key as Flow. Owner loaded it during development. Final source package is `Flow2API-Worker.zip` at repository root; its contents were byte-compared with the extension source.
- Browser policy blocked opening Chrome extension management and extension settings. Do not work around those denials with another automation surface. Final manual step: reload extension, open its popup, enable Creaa, Save & connect. Then run a bounded image/video test using current unlimited eligibility and record provider task IDs and results.
- No new staff-access restriction, throttle change, official Creaa API credit purchase, or automatic CAPTCHA solving was introduced. Existing Flow worker features are unchanged.
