# SOP: adding a browser-backed website provider to Flow2API

Written 2026-09-15 from the Creaa provider (built 2026-09-08, removed 2026-09-15 at the
owner's request). The Creaa code is gone from `main`; it is fully recoverable at commit
`dd6730f` (branch `codex/creaa-browser-provider`, deleted after merge). Read
`git show dd6730f:src/services/creaa_bridge.py` etc. when a concrete reference is useful.
This page keeps the *method*, so the next site (Flow Music, or anything else) can be added
without rediscovering the same lessons.

"Browser-backed provider" means: the owner's signed-in Chrome profile performs the site's
ordinary website requests, driven by the Flow2API worker extension, while the Flow2API
backend owns the durable queue, job state and results. No official API key, no cookie
export, no login/paywall/quota/challenge bypass.

---

## Which kind of provider is it? (decide this first)

Added 2026-09-15 after the Flow Music investigation. Before following the rest of this
page, classify the site by reading its public JavaScript bundle (no login needed: fetch the
landing page, list the `<script src>` chunks, grep them for the API base URL, the auth
library, and any anti-bot names such as turnstile/recaptcha/appcheck):

| Signal in the bundle | Provider kind | Pattern to follow |
|---|---|---|
| A server-holdable session credential (Supabase/Firebase/OAuth refresh token, long-lived cookie) **and** requests carry only a bearer token, no captcha or signature header | **Token-row provider** (like Google Flow, like Flow Music) | Backend calls the site's API directly; accounts are rows with refresh logic; no extension work. Keep the job state machine from section 2 (uncertain submissions still exist). |
| Submit requires a reCAPTCHA/Turnstile token, a browser-only "submit ticket", or the session cannot be exported | **Browser-bridge provider** (like Creaa) | Everything below. |

A token-row provider is roughly a quarter of the work of a browser bridge. Flow Music is
the reference for the token-row kind: `wb.flowmusic.app` API, Supabase auth at
`sb.flowmusic.app`, bearer-only requests (see the Flow Music plan/spike docs).

## 0. Go / no-go gates (answer these before writing code)

1. **Terms.** Does the site's ToS/payment terms forbid scripts or automated use? Creaa's
   did restrict it; we treated a two-person private use as acceptable, but this is the
   owner's decision, recorded in WORK.md, not something to infer.
2. **Auth surface.** How does the site log in (Google OAuth, Supabase, cookies, JWT in
   localStorage)? Can the browser stay signed in for days unattended? What does the
   logged-out page look like, so the worker can detect `needs_login`?
3. **Durable task identity.** After you press Generate, does the site return a task ID that
   survives a page reload and can be polled? **If not, stop.** Without a task ID every
   timeout becomes `needs_review` and an unattended queue cannot be made correct.
4. **Billing semantics.** What does one generation cost? Is there an "unlimited" mode, and
   does it really mean zero spend? Write the balance down before and after one manual run.
5. **Concurrency caps.** How many in-progress tasks does the site allow per account, and do
   media kinds share the cap? Creaa: 2 images + 1 video, learned only by probing.
6. **Result asset.** Is the output a signed CDN URL, a blob, or an authenticated URL? Does
   it expire? This decides the whole retrieval design.

Answer 3, 4, 5, 6 with a **manual spike** in DevTools (network tab, preserve log) before
building anything. Budget 2 to 3 hours. Write the findings into `docs/<site>-spike.md`.

## 1. Manual spike checklist (DevTools, no code)

Run each by hand, note the request/response, the balance before/after, and timing:

- One generation with the cheapest settings. Capture the submit request, the task ID
  format, and the poll endpoint.
- Reload the page mid-generation. Does the task reappear with the same ID and result?
- Submit N+1 tasks where N is the advertised cap. What does the rejection look like?
- Two media kinds at once, if the site has more than one. Shared cap or not?
- A prompt the site rejects for policy. Is it billed? What does the failure look like?
- Log out and back in. What does the logged-out state look like in the DOM and in requests?
- Open the result URL an hour later. Does it still work?

Prefer calling the site's **own frontend functions** (the app object it exposes on
`window`) over raw DOM clicking or reinventing its fetch calls. Creaa exposed
`window.imageEditChat` with `generateMediaTask`, `fetchMediaTaskStatus`,
`uploadTempImageAsset`, `refreshUnlimitedConfig`; using those kept its submit-ticket and
challenge logic intact. If the site changes its frontend, fail loudly with a clear error;
never guess alternative endpoints.

## 2. Architecture (what Creaa ended up as, reuse as-is)

```
caller -> Flow2API HTTP (/v1/<site>/...) -> SQLite job store + dispatcher
       -> outbound WebSocket from extension (/<site>_ws) -> owned site tab
       -> site's own JS -> provider task -> poll -> result URLs -> job.result
```

### Backend (Python, FastAPI)
- `src/core/<site>_models.py`: state constants, transition table, request validation,
  model ID prefix (`<site>/...`), bounds (prompt length, reference count/size).
- `src/services/<site>_bridge.py`: one class owning the SQLite DB (`data/<site>.db` next
  to `flow.db`), the live worker registry, account records, dispatch loop, housekeeping.
- `src/api/<site>.py`: router + `set_service()` + the WebSocket endpoint.
- `src/main.py`: construct the bridge, call `start()` inside lifespan, `close()` in
  `finally`, `include_router`.

Endpoints that proved necessary:

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/<site>/accounts` | connected accounts, online devices, active jobs, caps |
| PUT | `/v1/<site>/accounts/{id}/parallel-limits` | per-account dispatch caps + note |
| GET | `/v1/<site>/models` | live catalog read from the site, prefixed IDs |
| POST | `/v1/<site>/<kind>/generations` | HTTP 202 + job_id (async, not OpenAI drop-in) |
| GET | `/v1/<site>/jobs`, `/jobs/{id}` | state, progress, provider task ID, result |
| POST | `/v1/<site>/jobs/{id}/cancel` | only before dispatch |
| POST | `/v1/<site>/jobs/{id}/resolve` | operator: resume a known task ID, or fail with `confirm_no_upstream_work` |

### Job state machine (keep all of these)

```
queued -> claimed -> submitting -> submitted -> running -> succeeded
                                            \-> failed
queued -> cancelled (only before dispatch)
any in-flight -> needs_review   (uncertain: may or may not exist upstream)
any in-flight -> needs_login    (session lost mid-job)
```

- `claimed` is persisted **before** the execute message leaves the server.
- `submitting` means the worker announced intent; the backend **ack gates the click**.
  The tab never presses Generate until the ack arrives. A lost ack means no click.
- `submitted` requires a provider task ID. A submit timeout is **unknown**, never failure.
- `needs_review` holds the account slot because upstream may still be running. Only an
  operator releases it through `/resolve`. Reconnect never resubmits.

### WebSocket protocol (extension -> backend, JSON)
- `register {device_id, account_id, models, caps}` -> `register_ack`; the backend then
  sends `resume` for every job in `submitted/running/needs_login` for that account.
- `job_event {job_id, attempt_id, state, provider_task_id?, progress?, result?, error?}`
  -> ack with `ok` or `{ok:false, job_state}`. On a terminal or mismatched `job_state`,
  the worker must clear its pending event and stop retrying.
- Backend -> worker: `execute {job}` and `resume {job}`.
- `ping` every 15 s. Key the worker's active map by **job + attempt**, not job alone.

### Extension (MV3, inside `worker-extension/`)
- `<site>-worker.js` imported from `background.js` with `importScripts`. Own tab, own
  socket, own `chrome.alarms` reconnect (1 min). Disabled by default; a popup section in
  `options.html`/`options.js` with enable, server, key, status, "open tab".
- `<site>-page.js` is a single function run with `chrome.scripting.executeScript`
  `world: 'MAIN'` that switches on an operation name (`status`, `catalog`, `preflight`,
  `submit`, `poll`) and calls the site's own app object. All site knowledge lives here.
- Persist per-job recovery metadata in `chrome.storage.local` (`<site>Job:<id>`), never
  prompt bodies or base64 references (storage would exhaust).
- Hold a mutex around prepare+submit so two jobs cannot interleave page settings.
- Handle tab discard (`tab.discarded` -> reload), worker shutdown, login redirect, and
  the user changing accounts in the tab. Recovery reads site state, not JS memory.
- Do not add `proxy`/`cookies` behaviour for the new site; keep host permissions narrow.

## 3. Rollout steps (what worked)

1. Fixture-driven tests first: Python for the bridge (recovery after restart, ack
   handling, attempt fencing, account routing, HTTP+WS), Node `--test` for the page
   adapter and worker transitions with deterministic fixtures. Creaa had 22 + 19.
2. Bump `worker-extension/manifest.json` version. The backend serves the published zip;
   the owner's unpacked build needs a manual reload (Reconnect does not reload source).
3. Deploy backend via the normal PR -> `fork/main` -> Coolify path. Verify `/health`,
   `/v1/<site>/accounts`, `/v1/<site>/models` return 200 with the existing key.
4. Owner enables the worker in the popup. Confirm the account registers with a model list.
5. One live image, then one live video (if any), with balances recorded before/after.
6. Only then probe concurrency: raise caps one above the advertised limit, observe the
   rejection text, set caps to the verified value, record in WORK.md.

Time budget from Creaa: 0.5 to 1 day spike, 2 to 4 days minimal prototype, several more
for references, recovery and multi-person use.

## 4. Mistakes we made once and must not repeat

- Designing the queue before confirming task identity and billing. Do the spike first.
- Writing the attempt marker in the service worker. It must be persisted and acked by the
  backend from the same context that performs the click.
- Keying active work by job ID only. A requeued attempt was silently dropped.
- Not clearing a pending event after a hard backend rejection; it retried forever.
- `restart()` nulling the socket before `close()`, so ack waiters hung 15 s and then sent
  a false `needs_review`.
- Counting `ok:false` but not thrown page errors toward the retry limit.
- Forgetting that Chrome extension management pages are blocked by browser policy on the
  owner's machine; the owner reloads the extension by hand. Do not work around it.

## 5. Removal (if a provider is dropped)

Delete `src/api/<site>.py`, `src/core/<site>_models.py`, `src/services/<site>_bridge.py`,
its tests and fixtures, the two extension files, the popup section, the `importScripts`
line, and the docs. Restore `src/main.py`. Bump the manifest version past any local test
build so the update nag fires. The stray `data/<site>.db` on the Coolify volume is
harmless and can be deleted by hand.
