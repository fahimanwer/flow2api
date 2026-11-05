# Decisions

Hard-to-reverse choices that would surprise a future reader. One row each; details live in the linked docs.

| Date | Decision | Why |
|---|---|---|
| 2026-10-23 | Run on Flow web RPCs through an adapter inside `FlowClient._make_request` (REST body → `flow_rpc` → `batchexecute`), not by merging upstream's rewrite. | Google retired the Labs login on 2026-10-21 and every account died. Upstream had moved to the same RPCs, but our fork was ~18k lines apart (Ultra browsers, client policies, characters, watermark); the adapter restored service in hours and kept every layer above the transport. `docs/cookie-sync.md`. |
| 2026-10-23 | Every RPC argument layout is proven against a HAR of the real Flow web app, and every enum value with a real image. | The image shape codes copied from upstream had 4:3 and 3:4 swapped (shipped 2026-10-23, found 2026-11-04, PR #43); upstream still has it. |
| 2026-10-23 | An account's identity comes only from the Flow page's `oPEP7c` field, and "signed out" needs both `oPEP7c` and the xsrf token missing. | Any other email on the page proves nothing; a Google rename of one field must not disable every account at once. |
| 2026-10-23 | The stored tier wins over Flow's credits answer; Flow's number (1 = Pro, 2 = Ultra) is used only for a row with no tier. | A wrongly read tier re-routes paid callers; the numbers were mapped from a handful of accounts only. |
| 2026-11-04 | Only the worker extension mints (plus the server fallback minter); the other captcha modes were deleted. | They started from labs.google or sent solver tasks for a page Flow no longer accepts. 21k lines removed. |
| 2026-11-04 | Normal traffic does not borrow `failed_image` accounts, even when the `auto` pool is empty. | Owner decision: those accounts are kept for staff re-runs. |
| 2026-11-04 | Worker extension 3.8.0 pushes on every Google login rotation (≥ 90 s apart, never dropped) and every 20 min. | A cookie session lasts only until the browser rotates its login; 3.7.x dropped rotations within 10 min of a push and accounts sat dead until the hourly push. |
| 2026-11-05 | Deleted the `FLOW_COOKIE_RPC` switch and the Labs login (`ProtocolLogin`, `TokenManager.cookie_login`); the pushed Google cookies are the only way an account signs in. | Labs auth answers 410 since 2026-10-21, so `FLOW_COOKIE_RPC=0` was an outage, not a rollback; a rollback is `git revert` of the stage 2 PR. Stage 2 step 2; `docs/cookie-sync.md`. |
| 2026-11-05 | Deleted the aisandbox REST transport (curl/urllib, Labs headers, legacy `:uploadUserImage`); `FlowClient._make_request` is only the RPC adapter, and an `at` that is not a `FLOWCK.` handle gets a local 401 with no network call. | aisandbox REST refuses cookie auth since 2026-10-21, so that path could only send a dead credential to Google; the local 401 starts the same refresh Google's 401 did. Image-retry routing (`respect_fingerprint_proxy`) unchanged. Stage 2 step 3. |
