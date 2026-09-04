# Shared Agent Status

Last updated: 2026-09-04 12:30 UTC

## Active

- `main` = PR #9 (extension **3.3.9**, mint on `labs.google/fx`). Production container auto-deploys from `main` (Coolify GitHub App). `feat/async-submit-status` remains unmerged; leave it alone.
- Published staff package (`data/ext/worker-latest.zip` on the data volume) = **3.3.9** (2026-09-04 04:35 UTC). Backups next to it: `worker-3.3.7.backup.zip`, `worker-3.3.8.backup.zip`.
- Every staff device must reload the unpacked extension (or Download from the popup banner) to get 3.3.9. Fleet as of 04:05 UTC: 3.3.2 ×1, 3.3.4 ×6, 3.3.5 ×17, 3.3.6 ×5, 3.3.7 ×3, 3.3.8 ×2.

## Findings (2026-09-03/04 incident, all CONFIRMED on-box unless marked)

- **Google is migrating Flow accounts from `labs.google/fx/tools/flow` to `flow.google.com`, per account, not all at once.** With a migrated account's session, `labs.google/fx/tools/flow` loads (HTTP 200, grecaptcha loads) and then the page's own JS navigates to `https://flow.google.com/` about 1.5 s after load. Non-migrated accounts stay on labs.google and kept minting all day (thousands of successes 2026-09-03 07:00-19:00 UTC on ext 3.3.4-3.3.8). Migrated so far: syedfaisalanwar26 (token 55), arungoswami688 (36), rishitapandey035 (52); probably ankitsharma44198 (41), ankitbabal4991 (77). Verified with headless Chromium in the container using token 55's session cookie.
- **Extension ≤3.3.6 open/close tab loop** = the redirect: `isFlowUrl()` only accepted labs.google, so the redirected tab was closed and reopened per mint request ("labs tab did not reach Flow URL (https://flow.google.com/)"). Fixed in 3.3.7 (PR #7).
- **3.3.7/3.3.8 still could not mint for migrated accounts.** 3.3.8's diagnostic (PR #8) showed the real error: `Failed to set the 'src' property on 'HTMLScriptElement': This document requires 'TrustedScriptURL' assignment. [url=https://flow.google.com/, grecaptcha=absent]`. The new flow.google.com Angular app (`AiSandboxAngularFrontend`) does not load grecaptcha (0 references in its base bundle), and its CSP (`require-trusted-types-for 'script'` + nonce-only `script-src`) blocks the extension's fallback injection. Verified headless: injection with a Trusted Types policy plus the page nonce is still blocked.
- **`https://labs.google/fx` (Labs FX home) does not redirect, loads `grecaptcha.enterprise` itself with the same site key `6LdsFiUs…`, mints a token in ~250 ms, and calls `/fx/api/auth/session` (keeps the NextAuth session cookie the server uses).** Verified anonymous and with token 55's session. `/fx/tools/whisk` and `/fx/tools/image-fx` now also redirect to flow.google.com. This is what 3.3.9 opens.
- **The reCAPTCHA site key did not change** (flow.google.com HTML serves the same key). Server-side API calls still go to labs.google and still work for migrated accounts (token 36 succeeded 3× on 2026-09-04 03:33 UTC).
- **VERIFIED 2026-09-04 04:51 UTC — Google accepts a `labs.google/fx`-minted token for a migrated account.** Token 36 (arungoswami688, a confirmed-migrated account) reported ext 3.3.9 and logged 5× `status_code=200`, last at 04:51:06. No `PUBLIC_ERROR_UNUSUAL_ACTIVITY` on those requests. 3.3.9 is good; **no rollback needed** (the `worker-3.3.8.backup.zip` rollback path stays available but is not required). Fleet-wide at 04:51: 136 successes in 15 min from 5 accounts on 3.3.9, plus 61 from 1 account still on 3.3.5 — the latter being a non-migrated account, consistent with the per-account migration finding above.
- The fleet-wide zero successes 20:00-03:00 UTC were 503 fast-fails (no eligible account: staff offline, others out of daily quota), not reCAPTCHA. Content Factory keeps firing all night; backing off when no browser is online would be a content-factory change (owner has not decided).
- Server-side defences from 2026-09-03 (PRs #1-#6) stand: strict route binding, 90 s route pause after a failed mint with queued requests failing fast, no account strike for mint failures, dead sessions auto-disabled, UNSAFE_GENERATION not counted as an account error.
- Tests: `pytest` runs from `.venv` on the command-center box (see Coordination). 1 pre-existing failure (`test_api_captcha_fingerprint`), unrelated.

- **2026-09-04 12:04-12:20 UTC, owner's browser on 3.3.9: mints succeed (65x 200 in 15 min) but the Labs tab still reloads/reopens and the Chrome window sometimes vanishes.** Root causes CONFIRMED from the pasted extension log + code, fixed in **3.3.10** (PR #10, `worker-extension/background.js`):
  1. Remove-before-create. The mint-retry path, the sweep and `dropOwnedTab` removed the old tab and only then opened a new one. The worker tab is normally the ONLY tab in its window (dedicated staff profile); removing it closes the window, and with no window left `chrome.tabs.create` fails with `No current window` (the exact error in the owner's log 2026-09-03 21:39:40). Now: `createTabSafely` opens the replacement first, in the same window; `removeOwnedTabSafely` never closes the last tab of a window (deregisters it and logs instead); on `No current window` an unfocused window is created so the profile self-heals.
  2. `rollSessionTab` counted a reload that landed on `flow.google.com` as success (`tabOnFlow` true, `tabUsable` false), so the tab silently became unmintable and the next mint replaced it with no log line. Now steered back to `labs.google/fx` in place via `tabs.update`.
  3. No log said WHY a tab was replaced. A swap at 12:04:58 (tab kept at 12:04:39, replaced 19 s later while alive) is UNKNOWN for that reason. Now every replacement logs the old tab's `{url,status,discarded,win}` plus the attempt-1 error; the popup shows only the newest 25 lines (`options.js` `slice(0,25)`), so ask for a paste soon after the event.
  - Also: a Chrome-discarded (memory saver) tab is reloaded in place instead of failing the mint. Sweep log now includes kept/closed URLs.
  - NOT verified on a live browser before merge: owner asked to push before testing. `node --check` passes. Rollback: copy `worker-3.3.9.backup.zip` over `worker-latest.zip` on the data volume.
- **Owner's account is over-subscribed, not failing.** 12:20 UTC: token 55 `inflight=73`, 215 requests parked at `102 solving_image_captcha` vs 65 done in 15 min; every other 3.3.9 account single-digit backlog. Cause: `image_concurrency=-1` (unlimited) + most credits -> balancer stacks it, but one browser mints ~1 captcha / 3 s. Recommended: set token 55 `image_concurrency` ~8. Owner not yet decided.

## Open

- Staff reload to 3.3.9 (owner to announce). Until then migrated accounts cannot mint; non-migrated ones keep working on any version ≥3.3.4.
- ~~Confirm first 200s from migrated accounts on 3.3.9~~ — DONE, see the VERIFIED line above (token 36, 04:51 UTC).
- meta-pages `pipeline.py` Pro-fallback patch (`~/projects/meta-pages-fix/` on the command-center box) still to be applied on the owner's Mac.

## Coordination

- Work happens in `~/projects/flow2api` on the command-center box (or the owner's Mac clone). `~/mac-mirror/` is read-only.
- Merging to `main` redeploys the container and drops extension websockets for ~1 min; they reconnect on their own.
- Before any commit/push/deploy: reread and update this file, prune stale lines. Never deploy from a tree that does not contain `origin/main`.
