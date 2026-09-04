# Shared Agent Status

Last updated: 2026-09-04 04:40 UTC

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
- **Not yet verified: that Google accepts a token minted on `labs.google/fx` for a migrated account's generation.** Same hostname and key as before, so it should. What settles it: a 200 in `request_logs` for token 36/52/55 after their device reports 3.3.9. If instead they get `PUBLIC_ERROR_UNUSUAL_ACTIVITY…: reCAPTCHA evaluation failed`, roll back by copying `worker-3.3.8.backup.zip` over `worker-latest.zip` and rethink.
- The fleet-wide zero successes 20:00-03:00 UTC were 503 fast-fails (no eligible account: staff offline, others out of daily quota), not reCAPTCHA. Content Factory keeps firing all night; backing off when no browser is online would be a content-factory change (owner has not decided).
- Server-side defences from 2026-09-03 (PRs #1-#6) stand: strict route binding, 90 s route pause after a failed mint with queued requests failing fast, no account strike for mint failures, dead sessions auto-disabled, UNSAFE_GENERATION not counted as an account error.
- Tests: `pytest` runs from `.venv` on the command-center box (see Coordination). 1 pre-existing failure (`test_api_captcha_fingerprint`), unrelated.

## Open

- Staff reload to 3.3.9 (owner to announce). Until then migrated accounts cannot mint; non-migrated ones keep working on any version ≥3.3.4.
- Confirm first 200s from migrated accounts on 3.3.9 (see "Not yet verified" above).
- meta-pages `pipeline.py` Pro-fallback patch (`~/projects/meta-pages-fix/` on the command-center box) still to be applied on the owner's Mac.

## Coordination

- Work happens in `~/projects/flow2api` on the command-center box (or the owner's Mac clone). `~/mac-mirror/` is read-only.
- Merging to `main` redeploys the container and drops extension websockets for ~1 min; they reconnect on their own.
- Before any commit/push/deploy: reread and update this file, prune stale lines. Never deploy from a tree that does not contain `origin/main`.
