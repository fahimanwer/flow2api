# Creaa rollout evidence — 2026-09-08 IST

- Built jointly with Fable 5.1 using `claude -p --model claude-fable-5-1`; full backend and extension review records are in this directory.
- PR: https://github.com/fahimanwer/flow2api/pull/14 (merged).
- Production commit: `5e63d48a720f87eb9d617d5718706e9d0a4b9555`.
- Coolify app: `jc4044co4w8w0g8w0g4ks0ws`; deployment `rfusrjcvqwgvswwyxzrhweve`, finished 2026-09-07 19:36:16 UTC (2026-09-08 01:06:16 IST).
- Verified HTTP 200: `/health` (backend running, active Flow accounts), `/v1/creaa/accounts`, `/v1/creaa/models` using the existing Flow2API key.
- Initial accounts/models were 0. After owner enabled the worker: account 64287, 14 models. Live GPT Image 2 test completed (`cj_fc6b6582328f4c5b8761`), PNG verified HTTP 200/image/png, 1,521,068 bytes. Seedance 2.5 test is still running (`cj_84fabed77fe34b6a95cc`). Both requests use unlimited_only; actual ledger deductions have not been independently audited.
- Tests: 22 Creaa Python tests and 19 Node tests passed. Full Python suite: 128 passed, 1 previously documented CAPTCHA fingerprint failure. GitHub image-build CI did not start because its account was locked for billing; Coolify performed the actual production build successfully.
- Extension: 3.4.0, same default server/key as Flow. Owner loaded it during development. Final source package is `Flow2API-Worker.zip` at repository root; its contents were byte-compared with the extension source.
- Browser policy blocked opening Chrome extension management and extension settings. Do not work around those denials with another automation surface. Owner subsequently completed reload/enable. Live checks now run through curl; no extension control workaround was used.
- No new staff-access restriction, throttle change, official Creaa API credit purchase, or automatic CAPTCHA solving was introduced. Existing Flow worker features are unchanged.
