# Creaa private browser API — reviewed plan

Date: 2026-09-08. Scope: planning only, for Fahim and Ankit. No production changes, generation, credential export, or deployment in this planning turn.


## Final recommendation after Fable critique

Fable review completed using `claude -p --model fable` on 2026-09-08. Full unedited response: [creaa-fable-critique.md](creaa-fable-critique.md). No tools were enabled for the reviewer; it critiqued the evidence and draft supplied here, not an independently inspected browser.

Verdict: technically plausible; ready for a bounded feasibility test, not a production build. The first milestone is one direct image and one short video with stable task identity, known billing, and recoverable output. The API described below is the target architecture, deferred until that milestone succeeds. Website automation is not a way to override Creaa's terms or acquire unavailable model entitlements.

### Small first milestone

1. Resolve provider permission for this private browser automation and which seats/accounts may be used. The current terms restrict automation. Do not infer an exception from official MCP availability or a two-person audience. No support message has been sent in this task.
2. Verify the browser account and purchase receipt belong together. The displayed 20,000-credit allowance does not establish Pro tier or match API identity by itself.
3. Scope a manual feasibility test to one direct image (Agent OFF) and one shortest eligible video, with a numeric maximum potential credit spend agreed before execution. Do not assume a visible unlimited label guarantees zero cost. This planning request authorizes neither additional credit spend nor a paid-plan purchase.
4. Record exact chosen settings, cost and balance before/after, task ID, task details/history recovery, result type and download behavior. Capture only the test requests in sanitized evidence; no full-account HAR containing cookies, headers, reference media, prompt history or signed URLs.
5. Reload during one already-submitted job to see whether its same task can be recovered. This needs no extra generation. If durable task correlation cannot be established, stop unattended API work; a manual assisted workflow may still be useful.
6. After permission and successful evidence, build one local extension experiment: preflight -> one submission -> observed task identity -> recover same task after reload -> original output. Only then build queue and two-person integration.

The first pass does not test longest videos, deliberately exceed caps, generate with Agent ON or submit policy-rejected prompts. Those cost/risk probes are optional later tests with separately bounded scope; they are not required to prove the first image/video workflow. An asset-expiry test can be scheduled later, but no monitoring or automation was created here.

### Accepted review changes

- Treat task correlation and billing as prerequisites, not details to discover after building the queue.
- Persist submit intent at the executor boundary and wait for backend acknowledgement before the tab performs the action. Verify the current lease, account, job and cancelled status immediately before execution. A crash or lost acknowledgement still produces uncertainty: intent logging is NOT atomic with the website click.
- Use one active generation per account initially and at most two queued jobs per caller. A long video can delay the other founder's images. Do not raise concurrency until both reliable correlation and actual shared/separate upstream caps are known.
- Detect manual jobs and occupied provider slots; count manual work toward upstream capacity. Never claim that a new history item belongs to us simply because our own queue was serialized. Use an observed provider ID tied to the actual submit action; otherwise mark ambiguous and require review.
- Handle tab discard, background throttling, profile sleep and user account changes in addition to MV3 worker shutdown. Recovery reads provider state, not JavaScript memory. Do not repeatedly steal focus or force the user's workspace foreground; an owned visible tab can be used during the first test.
- Define cancellation precisely: guaranteed cancellation only before dispatch; afterward it is best effort until provider acceptance is known. Before-click checks narrow the race but do not guarantee cancellation atomically. Never report a potentially running upstream generation as cancelled.
- Defer hosted deployment, optional event streams, advanced media transport and broad model coverage. Keep basic job ownership, credential separation and idempotency for the two-person pilot; local development is not a reason to remove those correctness controls.

### Corrections to Fable's proposed test plan

Its proposed one-hour/one-day expiry checks cannot fit its own two-to-three-hour estimate. Its eight-test matrix also exceeds the smallest proof needed. One serialized automated job does not make timestamp-based history matching safe in the presence of manual jobs. A missing task ID blocks unattended reliability, but does not prove a manually supervised helper is impossible. Accepted changes above preserve these distinctions.

## Deferred target design and original evidence

## Decision and evidence

A browser-backed private API is plausible, not yet proven. The API would orchestrate the ordinary website workflow through a Chrome extension using the signed-in browser, rather than convert a website subscription into official API credits. If Creaa explicitly refuses browser automation, this is not an authorized substitute; keep the adapter usable for supported official access instead. Their payment terms restrict scripts/automated membership use and unauthorized membership sharing: https://creaa.ai/payment-terms.html . This is an operational restriction to clarify, not a technical permission granted by a two-person audience.

Observed live in Chrome, not from marketing:
- Workspace shows 8,060 / 20,000 website credits. Account identity and purchased tier have not been independently verified against the receipt.
- Image workspace: GPT Image 2, 1K, Medium, one image; Unlimited enabled, tooltip says 0 credits and up to 2 in-progress tasks. Agent is ALSO enabled, conflicting with pricing FAQ saying Canvas Agent spends credits. Do not treat the toggle as proof of free execution.
- Video workspace: Seedance 2.5, 720p, 6 seconds, Omni reference, Agent disabled; Unlimited enabled with one in-progress task. No video submitted; eligibility, duration ceiling and billing remain unverified.
- Image picker: GPT Image 2; Nano Banana 2, Lite, Pro; Seedream-5.0-Pro.
- Video picker: Seedance 2.5, 2.0, 2.0 Fast, 2 Mini; MiniMax H3; Omni flash; Veo 3.1; Wan 3.0; HappyHorse 1.1. Presence does not imply entitlement or a working model.
- Official API, API-key MCP, and browser-OAuth CLI/MCP all returned API plan free / 0 API credits. Nano Banana 2 1K estimate was 10 API credits; test requests rejected for insufficient API credits. These results establish current API behavior, not a universal policy for every plan/account.
- No website network request contracts, asset upload routes, task identifiers, cancellation APIs, or download expiry behavior have been captured. Do not invent endpoints from the official API catalog.

## Existing Flow2API integration points

Inspected worker-extension/manifest.json, README.md, background.js; src/services/browser_captcha_extension.py; src/api/routes.py; generation_handler.py; WORK.md.
- Existing extension handles get_token and refresh_session, persistent tabs, reconnect, device routing. It is not an existing generic full-generation browser bridge.
- Existing backend has /captcha_ws, route-bound browser connections, and OpenAI chat/Gemini generation routes. GenerationHandler is Flow-specific.
- Reuse concepts for routing, heartbeat, logs and request normalization; do not copy Google captcha/proxy/cookie-export behavior or shared-pool routing into Creaa.
- Prototype as a separate local service and separate Creaa extension. Do not modify the live Flow extension or its fleet. Integrate a provider adapter only after proof, avoiding an early broad provider refactor. Leave feat/async-submit-status alone.

## Architecture

Fahim/Ankit client -> private authenticated API -> durable job DB and scheduler -> authenticated outbound extension WebSocket -> owned Creaa generation tab -> website task -> result downloader -> authenticated artifact storage -> client.

Provider operations: read_account, list_capabilities, upload_reference, preflight, submit_image, submit_video, get_job, fetch_result. These are internal typed commands, not arbitrary fetch URLs or JavaScript.

The extension owns one separate generation tab, shows active account/connection/queue/pause, and keeps website authentication inside Chrome. Start with ordinary DOM-driven controls. After feasibility work, consider a narrow same-origin request adapter only for observed supported website actions; stop on login, CAPTCHA, denied entitlement or unsupported settings. No authentication, paywall, quota, challenge, or billing bypass and no automatic fallback to a paid path.

Users, devices, and provider accounts are separate records. Two users may have separate permitted accounts, or a permitted shared/team seat arrangement; do not assume one personal membership may be shared. Each job is bound to caller + provider account + device at admission and checked again before submit. Device reconnect never changes account or borrows another browser. Default one active generation total per provider account initially; only raise toward verified provider caps after determining whether image/video quotas share capacity. Browser closure means offline, not resubmit elsewhere.

Pair each extension with a revocable device credential and pin account identity. Authorize every model/job/artifact endpoint; use per-user tokens, not the current broadly shared Flow key. Initial deployment localhost; two-person hosted use requires authenticated TLS access and explicit allowlist. Narrow host permissions to Creaa plus the private bridge and only necessary observed asset origins. No proxy/cookies permission by default. Page-to-extension messages validate tab, origin, typed payload and job identity. Never allow a page to issue arbitrary bridge commands.

## API contract proposal (ours, not claimed Creaa endpoints)

GET /v1/models returns provider-prefixed model ID, media kind, verified parameter combinations, billing mode (unlimited/credits/unknown), current eligibility and last verification time.
POST /v1/images/generations and POST /v1/videos/generations accept prompt, account_id, model, ratio, quality/resolution, references; videos additionally duration and supported reference mode. Return HTTP 202 + job_id and status_url. Explicitly document this asynchronous behavior; it is not drop-in OpenAI Images compatibility. Existing consumers get a later compatibility wrapper with bounded waiting.
GET /v1/jobs/{id}; GET /v1/jobs/{id}/artifacts; optional authenticated event stream. POST /v1/jobs/{id}/cancel cancels queued work; submitted work reports cancellation unsupported unless verified upstream cancellation exists.
Default billing_policy=unlimited_only. Paid use requires caller-specified maximum credits. Unknown cost/entitlement, expired benefit or changed model settings reject or pause before submit. Re-read exact selected options, direct mode (Agent off), unlimited state and displayed cost immediately before submitting; check resulting billing after completion. If preflight/submit is not atomic, document residual billing risk; cannot promise guaranteed zero spend from UI alone.

## Job correctness and media handling

Persist job and immutable normalized request hash BEFORE dispatch. Idempotency key is scoped to caller/account/provider; same key/different payload is a conflict. Durable states: queued, leased, preparing, submitting, submitted, running, retrieving, succeeded, failed, needs_login, needs_review, cancelled.
Persist local attempt marker before clicking Generate; capture provider task ID immediately afterward. A timeout after submit is UNKNOWN, never proof of failure. Reconcile through provider history/task IDs; if exact match unavailable, stop at needs_review instead of double-generation. Never promise exactly-once creation without upstream idempotency. Fence stale device leases; late results are accepted only for their recorded job/attempt/account. A server lease cannot undo an already issued browser submission.

Keep durable IDs locally and server-side; extension globals are disposable. MV3 workers can terminate, so reconnect resumes status checks and artifact retrieval without re-submission (https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/lifecycle). Queue capacity, deadlines and polling backoff are bounded. Cancellation/disconnect must release local resources without leaking DB threads; inherit the recent Flow cancellation lessons, not unresolved branches.

References are caller-owned uploads: bounded size/type, provider-specific limits, checksum and upload asset IDs. Avoid arbitrary external URL fetching in MVP. Video support includes text-to-video first, then image-to-video and multi-reference only after their real UI workflows are mapped. Do not assume advertised editing modes work for every model.

Videos cannot travel as giant base64 WebSocket messages. Use bounded streaming/chunked transfer with backpressure and checksums to private artifact storage, retain original outputs without lossy conversion, and verify MIME/bytes before completion. If an asset is only a browser blob/authenticated URL, retrieve within the authorized browser context. Treat provider URLs as expiring; use recoverable retrieving state and preserve task IDs so downloads can retry without generation. Proposed initial retention seven days, configurable, user-scoped reads/deletion; never expose bearer-bearing download URLs in logs.

## Deferred implementation sequence and acceptance gates

0. Confirm account identity/tier and browser automation/seat terms. This plan can be completed without support replying; production rollout depends on clarified permitted use.
1. Use the smaller manual feasibility milestone above first, with a separately agreed numeric potential-spend cap. No generation was performed in this planning turn. Only after permitted use and successful manual evidence evaluate normal DOM control versus a narrow website request adapter. Stop unattended work if billing or reliable task correlation cannot be established.
2. Separate prototype: localhost queue/SQLite job store + isolated MV3 extension, pinned single account, one image model and Seedance 2.5 only if eligible. Use controlled fixtures until bounded live testing is explicitly scoped.
3. Reliability: disconnect before dispatch / during submit / after acceptance; worker suspend; backend restart; login expiry; duplicate requests; account switch; credits-only model under unlimited_only; paid-limit violation; repeated completion; failed/expired downloads; queued cancellation; image/video sharing one cap. No blind resubmission in uncertain cases.
4. Add image reference/edit workflow and video references, then additional catalog entries individually after validation. Expose visible-but-unverified models as unavailable, not silently aliasing them.
5. Two-person pilot: distinct credentials, account-level global scheduling, pause controls, authenticated artifacts, job visibility isolation, no production Flow changes. Only then consider provider integration in Flow2API.

Success: a client submits a job, the expected allowed account performs exactly the intended visible workflow, a verified task is tracked across reconnect, and the client receives the original media with known billing. A UI toggle alone and a green connected badge do not count.

Effort is conditional: budget 0.5–1 engineering day for feasibility, 2–4 for a minimal image + video prototype if contracts cooperate, and several more for reliable references, recovery and a two-person pilot. These are planning estimates, not a delivery promise. Do not buy another tier based on this prototype plan.
