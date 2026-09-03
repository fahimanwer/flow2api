# Shared Agent Status

Last updated: 2026-09-04 (Asia/Kolkata)

## Active

- Local working tree is on `main`, rebased onto `fork/main` at `5eab1a2` (PR #7). Production container `jc4044co4w8w0g8w0g4ks0ws` is running image tag `5eab1a27...`. `feat/async-submit-status` remains unmerged; leave it alone.
- Two sessions worked this incident in parallel on 2026-09-03 evening: a cloud session (PR #7, the extension fix) and a local Mac session (PR #6, the server-side throttle). Both are merged.

## Findings

- **Root cause of the extension open/close tab loop: Google moved Flow off `labs.google`.** `https://labs.google/fx/tools/flow` now redirects to `https://flow.google.com`. The worker opened `LABS_URL`, landed on `flow.google.com`, and `isFlowUrl()` (a `startsWith(LABS_URL)` check) said "not on Flow" — so the extension closed the tab, logged "labs tab did not reach Flow URL", and the next mint request opened a fresh one. Affected browsers sat in an open/close loop and could mint nothing. Visible in staff logs from **07:16 UTC 2026-09-03**; hit the owner's own browser that night. CONFIRMED (PR #7 commit `6bf9e54`).
- **CORRECTION (supersedes the first draft of this entry, written 21:25 UTC before PR #7 was visible):** the loop was NOT caused by load funnelling onto one account. That was a real but separate condition — with staff browsers closed overnight, token 55 was the only eligible route and took the Content Factory's whole ~8.4k img/hr load, which made the already-broken tab churn fire every ~3 s instead of occasionally. Funnelling set the *rate*; the domain change set the *failure*. Do not cite the funnel as the root cause.
- Fix (PR #7, `worker-extension/`, ext **3.3.7**): `isFlowUrl` accepts either origin via `FLOW_ORIGINS`; `LABS_URL` is still used to open, so migrated and non-migrated accounts both work. The per-profile proxy PAC also routes `flow.google.com` through the residential IP, so mint and redeem keep a shared egress (otherwise UNUSUAL_ACTIVITY). New `readSessionCookie()` reads the NextAuth cookie from `labs.google` then `flow.google.com`, replacing four hard-coded `labs.google` reads.
- Fix (PR #6, `src/services/browser_captcha_extension.py`): a failed mint pauses that route 90 s and fails already-queued requests and generation-loop retries server-side instead of dispatching them; a successful mint clears it. The "any online browser" fallback now only considers browsers registered *without* a route key, so a bound browser is never borrowed. This is defence-in-depth — it bounds the blast radius of any future mint failure, but it did not and could not fix the domain break.
- Extension distribution state (verified 2026-09-03 21:34 UTC): published `data/ext/worker-latest.zip` = **3.3.7** (uploaded 21:27 via `/api/ext/upload`), repo `worker-extension/manifest.json` = **3.3.7**. Staff devices last reported **3.3.2–3.3.6** and must reload to pick 3.3.7 up — an unpacked extension cannot self-update, it only shows the update banner.
- Not verified: no unit tests were run for either PR. `pytest` is not installed in the local venv, on the Hetzner host, or in the production image.

## Open

- Staff are still on 3.3.2–3.3.6 and each device needs a manual reload/reinstall of 3.3.7 before it can mint again. Until then those accounts stay in the open/close loop whenever they are dispatched to.
- The Content Factory keeps firing image requests all night even when zero browsers are online. The 503s are cheap, but backing off when no route is connected is a content-factory-side change. Owner has not decided.

## Coordination

- 2026-09-04: this WORK.md update is committed locally and **not pushed** — a push to `fork/main` fires the Coolify auto-deploy webhook and rebuilds/restarts the container, dropping live extension websockets. Push when a restart is acceptable.
- Before any future commit, push, or deploy: reread and update this file, prune stale entries, and coordinate with other active sessions through the owner.
- Never deploy from a tree that does not contain `origin/main`.
