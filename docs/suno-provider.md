# Suno music provider in Flow2API

Built 2026-09-16. Backend-only provider that drives a Suno web account from
Flow2API, in the same token-row shape as Google Flow. Reviewed by Codex
(gpt-6-astra, high effort) before implementation; all seven P1 findings are
folded into the design below.

**Status: implemented and unit-tested, not yet proven against a live Suno
account.** Nothing here has generated a real song. Section 6 is the gate.

---

## 1. Why this does not use `gcui-art/suno-api`

The owner pointed at https://github.com/gcui-art/suno-api. That project is
stale and its generation path is broken, so this is a native Python
implementation instead. Evidence, checked 2026-09-16:

| Problem | Source |
|---|---|
| `POST /api/generate/v2/` returns 422 `token_validation_failed` for all users since Suno's create-page redesign | issues #263, #269, open PR #286 |
| Every download path it uses is dead (`audio_url` is `/api/forbidden`, `cdn1.suno.ai` 403s, raw `media_urls` bytes are encrypted) | issue #289 |
| Suno v5.5 dropped the `.custom-textarea` selector its browser automation drives | issue #277 |
| Unmaintained: last push 2026-03-06, 108 open issues, "still maintaining?" unanswered | repo metadata, issue #270 |
| Needs a paid 2Captcha account plus a headless Playwright browser, and gets its hCaptcha token by driving the real UI and aborting the victim request | its README and `src/lib/SunoApi.ts` |

Its findings were still useful, and are credited below. No code was copied, so
its LGPL-3.0 does not attach to this repository.

## 2. The contract, re-derived from Suno's live frontend

Everything below is **observed frontend behaviour**, read on 2026-09-16 from
Suno's own web bundles (99 chunks linked from `https://suno.com/`, build
`_next/static/immutable/chunks/*`, fetched without logging in). Observing what
the client does is not the same as knowing what the server requires, so section
6 still has to confirm the request the UI actually sends.

Base: `https://studio-api.prod.suno.com`. Auth: `https://auth.suno.com` (Clerk).

**Generation** (`GEN_ENDPOINT` in the bundle):
`POST /api/generate/v2-web/`. The old `/api/generate/v2/` does not appear in the
current client at all. Payload fields seen being assembled: `token`,
`token_provider`, `transaction_uuid`, `mv`, `prompt`, `duration`, `metadata`,
`project_id`, `generation_type`, `make_instrumental`, and for custom mode
`title`, `tags`, `negative_tags`.

**Captcha gate**: `POST /api/c/check` with `{ctype: "generation"}` returns
`{required, captcha_version}`. Constants: `HCAPTCHA_GENERATION_CAPTCHA_VERSION = 1`,
`TURNSTILE_GENERATION_CAPTCHA_VERSION = 2`, `FORCE_ENABLE_CAPTCHA = false`.
Version 1 is an invisible hCaptcha, sitekey `d65453de-3f1a-4aac-9366-a0f06e52b2ce`,
served through Suno's first-party proxies (`hcaptcha-endpoint-prod.suno.com`).
When `required` is false the client sends `token: null` and generation proceeds.

**Polling**: `POST /api/feed/v3` with `{limit, filters}`. The client keeps
polling a clip while its status is neither `complete` nor `error`.
`GET /api/clip/{id}` fetches one.

**Download**: `GET /api/download/clip/{clip_id}?format=mp3|m4a`, retried while it
answers `status: "processing"`. This is what Suno's own download button uses,
and it is the piece the upstream project is missing. Its player treats
`audio_url == /api/forbidden` as *normal* and skips encrypted `media_urls`
entries, so issue #289's "downloads are broken" is really "those routes were
retired". Whether a **free** account has download rights is unknown; there is a
`free_download_rights_popup` experiment and a `/api/mango/rights` endpoint.

**Models**: the `mv` field takes a codename. Mapping read from the bundle:

| Public id | `mv` | | Public id | `mv` |
|---|---|---|---|---|
| v3 | `chirp-v3-0` | | v5 | `chirp-crow` |
| v3.5 | `chirp-v3-5` | | v5.5 | `chirp-fenix` |
| v4 | `chirp-v4` | | v6-mini | `chirp-goose` |
| v4.5 | `chirp-auk` | | v6 | `chirp-hawk` |
| v4.5+ | `chirp-bluejay` | | | |

Input limits: lyrics 1250 chars (3000 on v4.5+/v5, 5000 on v5.5+), description
2000, style 1000, negative style 1000, title 80.

## 3. Architecture

```
caller -> /v1/suno/... -> SQLite job store + dispatcher -> SunoClient (curl_cffi)
       -> studio-api.prod.suno.com -> poll feed/v3 -> download/clip
       -> authenticated audio proxy -> caller
```

Files: `src/core/suno_models.py` (pure logic), `src/services/suno_client.py`
(transport), `src/services/suno_service.py` (job store, dispatcher, poller),
`src/api/suno.py` (routes). Tables `suno_accounts` and `suno_jobs` live in the
existing `flow.db` through the existing `Database` instance, so they inherit its
writer lock, connection semaphore and cancellation-safe cleanup. The worker
extension is untouched.

### Job states

```
queued -> submitting -> submitted -> finalizing -> succeeded
   |          |  \-> failed (verified rejection, nothing created)
   |          \-> needs_review (ambiguous submit; HOLDS the account slot)
   \-> blocked_captcha (pre-submit gate; releases the slot) -> queued | cancelled
```

The two rules worth stating plainly, because both cost money when wrong:

* **`blocked_captcha` does not hold a slot.** That state is reached by the
  pre-flight check, before anything is sent, so no upstream work exists. Holding
  capacity there would let two challenged jobs strand an account permanently.
* **`needs_review` does hold a slot, and is never retried automatically.** A
  timeout or a 5xx after the generate call means the song may exist and be
  billed. Only an operator resolves it, and failing it requires an explicit
  `confirm_no_upstream_work`.

A generation returns an A/B pair. The job settles only when **every** clip is
terminal (`complete` or `error`), so a finished job never abandons a
still-rendering sibling and one failed clip never fails a job whose other clip
worked. `streaming` is playable but not terminal, and is surfaced separately as
`playable_clip_ids`. Success additionally requires that a download actually
resolves: `status: complete` alone is not evidence of playable audio. If clips
complete but no download resolves after bounded retries the job goes to
`needs_review`, never back through generation.

### Concurrency

`min(operator_limit, provider_cap)`, and **1 when `provider_cap` is unknown**.
Suno advertises 2 concurrent generations on the free plan and more on paid ones,
but an advertised number is not a measured one, so the cap must be set
explicitly once observed. Occupied slots count `submitting`, `submitted`,
`finalizing` and `needs_review`. The admission queue is globally bounded at 50.

### Credentials

Two ways in. **(a) Worker extension (preferred, 2026-09-17):** a staff member
signed in to suno.com in the Chrome profile that runs the Flow2API Worker flips
**Share my Suno login** ON in the popup (extension 3.5.0+). The extension exports
the Cookie header to `POST /api/plugin/suno-cookie` (plugin connection token, the
same credential that pushes the Google Flow cookie), re-sends it whenever the
`__client` cookie changes (debounced 5 s) and at least every 6 h. The backend
upserts by the Suno user the cookie proves: create, or replace the credential on
the existing row for that user. A repeat of the same browser cookie on a `ready`
account whose last Clerk exchange is under 24 h old returns `unchanged` without
contacting Clerk (fingerprint of the *supplied* cookie in `source_client_hash`;
the stored jar is the rotated one), re-reading billing if it is over 6 h old.
An unhealthy or stale account, or a manual "Sync Suno now" (`force`), is always
re-validated. Identical concurrent uploads serialize per fingerprint so Clerk
sees one exchange; imports run as service-owned tasks that shutdown drains. Missing Suno identity is refused
before any write; identity-less rows (old admin imports) are never matched.
Admin-owned fields (display name, limits, disabled) are never touched on update.
**(b) Admin paste:** the whole `Cookie:` header from a signed-in suno.com request
via `POST /api/suno/accounts` (admin session). Both paths share one code path
that exchanges the cookie with Clerk exactly **once** and saves that resulting
session; the `__client` entry is the one that matters. The backend exchanges it
through Clerk for a short-lived JWT before each call. Clerk **rotates** the cookie on
every exchange, so the jar is persisted back to the account row immediately and
before anything else uses it. Refresh runs as a shared task that owns the
per-account lock across both the refresh and the durable save, so a cancelled
caller cannot leave a half-rotated cookie behind; admin credential replacement
takes the same lock and refuses a cookie belonging to a different Suno user.

Cookies, Clerk session ids and JWTs never appear in an API response, and the
Suno client logs method, path and status only, never bodies.

## 4. API

Generation and reads use the existing Flow2API key. Anything that touches
credentials or resolves a stuck job needs an **admin session**.

| Method | Path | Auth |
|---|---|---|
| GET | `/v1/suno/models` | api key |
| POST | `/v1/suno/music/generations` | api key, returns 202 + `job_id` |
| GET | `/v1/suno/jobs`, `/v1/suno/jobs/{id}` | api key |
| POST | `/v1/suno/jobs/{id}/cancel` | api key, only before dispatch |
| POST | `/v1/suno/jobs/{id}/retry` | api key, only from `blocked_captcha` |
| GET | `/v1/suno/jobs/{id}/audio/{clip_id}?format=mp3\|m4a` | api key |
| GET | `/v1/suno/accounts` | api key, projected |
| POST | `/api/suno/accounts` | **admin**, import or replace a cookie |
| PUT | `/api/suno/accounts/{id}/limits` | **admin** |
| POST | `/api/suno/accounts/{id}/enable\|disable\|refresh-billing` | **admin** |
| DELETE | `/api/suno/accounts/{id}?retire_audio=` | **admin** |
| POST | `/api/suno/jobs/{id}/resolve` | **admin** |

```bash
# description mode
curl -sS https://flow.ashuthefire.com/v1/suno/music/generations \
  -H "Authorization: Bearer $FLOW2API_KEY" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: song-001' \
  -d '{"model":"suno/v5","prompt":"warm lo-fi hip hop for late night studying"}'

# custom mode: your own lyrics and style
curl -sS https://flow.ashuthefire.com/v1/suno/music/generations \
  -H "Authorization: Bearer $FLOW2API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"custom":true,"model":"suno/v5","title":"Blue Hour",
       "tags":"indie folk, acoustic guitar, soft vocals",
       "prompt":"[Verse]\nThe blue hour comes\n[Chorus]\nAnd I am still here"}'

curl -sS https://flow.ashuthefire.com/v1/suno/jobs/$JOB \
  -H "Authorization: Bearer $FLOW2API_KEY"

curl -sS -o song.mp3 \
  "https://flow.ashuthefire.com/v1/suno/jobs/$JOB/audio/$CLIP?format=mp3" \
  -H "Authorization: Bearer $FLOW2API_KEY"
```

Audio is proxied, not stored. Each request re-asks Suno for a download location
using the owning account's credentials, follows only Suno/CDN hosts, forwards
no credentials to the CDN, and streams with bounded size and concurrency.
Deleting an account is refused while finished jobs still serve audio from it;
`?retire_audio=true` is the explicit "drop that audio" path, after which those
clips return 410.

Connecting an account (admin session required):

```bash
curl -sS https://flow.ashuthefire.com/api/suno/accounts \
  -H "Authorization: Bearer $ADMIN_SESSION_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"cookie":"<the whole Cookie header from suno.com>","display_name":"main"}'
```

Or, with the worker extension 3.5.0+, skip the paste: sign in to suno.com in the
worker's Chrome profile and switch **Share my Suno login** ON in the popup; it
pushes to `/api/plugin/suno-cookie` and keeps the cookie current on its own.
Turning the switch OFF stops syncing but does not delete the account.

**Do not sign out** of suno.com in that browser: signing out revokes the session.
With the admin paste path, also stop browsing suno.com in that profile, because
browsing rotates the cookie away from the backend. With the extension path the
rotation is re-pushed automatically, but whether the *backend's* own refresh
rotates the cookie in a way that signs the browser out is UNKNOWN until tested
(gate 9 below).

## 5. What is deliberately not built

* **No captcha solver.** `SunoService.captcha_provider` is a hook and v1 ships
  with none. When Suno demands a token the job is blocked, nothing is submitted
  and nothing is charged. 2Captcha was rejected: it is a recurring per-solve
  cost and it is exactly what the dead upstream project does. The better option
  is the worker extension already running on the owner's residential IP, but
  that is real work, not a small extra, and it is unproven for Suno.
* No admin web UI section; account management is the curl calls above.
* No lyrics generation, stems, extend/replace/cover, personas or uploads.

## 6. Gate before this can be called working

Needs a Suno account. In order:

1. Import the cookie; confirm `/v1/suno/accounts` shows plan and credits.
2. How often is `POST /api/c/check` `required: false` on a warm session from the
   Hetzner egress? **This decides whether v1 is usable at all.** Invisible
   hCaptcha usually resolves without interaction, which is not the same as never
   challenging.
3. Capture the real `/api/generate/v2-web/` request the UI sends and diff it
   against `build_generate_payload`; fix any missing mandatory field.
4. Confirm the `POST /api/feed/v3` filter key for specific clip ids.
5. Confirm `GET /api/download/clip/{id}?format=mp3` returns a URL, that the
   bytes are a playable MP3, and whether a free plan has download rights.
6. Check whether `transaction_uuid` comes back on the clip. If it does,
   `needs_review` can be reconciled automatically; if not it stays operator-only.
7. Measure credits spent per generation, and set `provider_cap` from an observed
   N+1 rejection rather than the advertised number.

Record the answers in `docs/suno-spike.md` and set the caps. Until then the
provider will start, accept jobs and block or fail honestly rather than pretend.

8. **Browser/backend session coexistence (release gate for the 3.5.0 zip).** With
   the extension switch ON: (a) let the backend refresh several times while the
   browser keeps using suno.com; (b) reload suno.com after a backend refresh; (c) use
   the backend after browsing rotated the cookie (extension must have re-pushed,
   `action=updated`); (d) restart the backend and confirm the account is still
   `ready`. If (a) or (b) signs the browser out, the extension must own the session
   (step 2 design) before the zip is distributed.
9. Extension popup end to end on one staff laptop: switch ON -> "Login shared";
   sign out of suno.com -> "Not signed in"; sign in again -> re-synced without
   touching the popup; switch OFF mid-sync -> nothing recorded locally.

## 7. Legal posture

Suno has no official public API; as of July 2026 it was "exploring a developer
API" with a curated group. This drives the owner's own account through the same
web endpoints the browser uses, which is the same posture as Flow2API's existing
Google Flow usage. That is the owner's call and it should be recorded in
`WORK.md` before this is used in production.

## 8. Tests

```bash
python -m pytest tests/test_suno_models.py tests/test_suno_client.py \
                 tests/test_suno_service.py tests/test_suno_api.py -q
```

116 tests: request validation and model mapping; every state transition
including blocked-job release and recovery, ambiguous submit, late responses
after an operator resolution, and restart recovery; dispatch capacity and
idempotency races; refresh single-flight, waiter cancellation and credential
replacement; mixed A/B outcomes; download host validation and redirect safety;
account deletion and audio retirement; and the api-key versus admin split.
None of them talk to Suno.
