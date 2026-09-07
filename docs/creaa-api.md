# Creaa in Flow2API

Creaa runs inside the existing Flow2API backend at `https://flow.ashuthefire.com`, using the existing Flow2API API key. No owner-only allowlist or new staff roles were added. This is a browser-backed provider: the signed-in Chrome profile performs Creaa website requests, while Flow2API persists the queue and task results. The official Creaa API key and MCP OAuth are not used by this adapter.

## Connect the worker

Load/reload the repository's `worker-extension` directory (version 3.4.0), open its popup and expand **Creaa images & videos**. The server and API key populate from the existing Flow2API settings. Enable Creaa, then select **Save & connect Creaa**. The worker opens one separate Creaa creation tab in the same profile. Keep Chrome running and signed into Creaa. The Flow connection continues independently.

The settings allow an explicit server/key override for local testing; ordinary use needs neither. The backend database is `data/creaa.db`, alongside `flow.db`, using the same persistent volume. Run one backend process: the broker's live worker registry and dispatcher are process-local.

Chrome unpacked extensions require a manual reload to run changed service-worker files. Reconnect only reconnects sockets; it does not reload extension source. A page-not-ready or login/challenge message requires looking at the owned Creaa tab. Existing Creaa login/challenge behavior is preserved; the worker does not solve challenges for you.

## API

All requests below require `Authorization: Bearer $FLOW2API_KEY` (the existing backend key). Creaa endpoints are separate from Flow's chat/Gemini endpoints; these asynchronous routes are not drop-in OpenAI Images endpoints.

| Method | Path | Result |
|---|---|---|
| GET | `/v1/creaa/accounts` | Account IDs, online devices, active jobs, parallel limits and queue |
| PUT | `/v1/creaa/accounts/{id}/parallel-limits` | Persist image/video/total dispatch caps with a change note |
| GET | `/v1/creaa/models` | Live website catalog with `creaa/` IDs, parameters and account associations |
| POST | `/v1/creaa/images/generations` | HTTP 202 job |
| POST | `/v1/creaa/videos/generations` | HTTP 202 job |
| GET | `/v1/creaa/jobs?limit=50` | Recent jobs; optional `account_id` and `state` filters |
| GET | `/v1/creaa/jobs/{id}` | State, progress, provider task ID, error or result |
| POST | `/v1/creaa/jobs/{id}/cancel` | Cancel queued work; rejects cancellation after dispatch |
| POST | `/v1/creaa/jobs/{id}/resolve` | Resume a known provider task or acknowledge unresolved work is finished |

Start by listing the connected account and models. Model IDs come from the website catalog, which differs from the official API. For example, the website's GPT model is `openai/gpt-image-2`, exposed here as `creaa/openai/gpt-image-2`; do not substitute the official API's `gpt-image-2` ID.

```bash
curl -sS https://flow.ashuthefire.com/v1/creaa/models \
  -H "Authorization: Bearer $FLOW2API_KEY"

curl -sS https://flow.ashuthefire.com/v1/creaa/images/generations \
  -H "Authorization: Bearer $FLOW2API_KEY" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: cup-image-001' \
  -d '{"model":"creaa/openai/gpt-image-2","prompt":"A red ceramic cup on a white table","aspect_ratio":"1:1","image_size":"1K","quality":"medium","billing_policy":"unlimited_only"}'

curl -sS https://flow.ashuthefire.com/v1/creaa/videos/generations \
  -H "Authorization: Bearer $FLOW2API_KEY" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: cup-video-001' \
  -d '{"model":"creaa/seedance-2.5","prompt":"A slow camera move around a red ceramic cup","duration":6,"aspect_ratio":"16:9","resolution":"1280x720","billing_policy":"unlimited_only"}'
```

Include `account_id` when multiple eligible accounts are connected. Without it, the backend only selects an account when the choice is unambiguous. There is no new staff restriction: callers with the existing key use this surface.

Generation accepts `n: 1`. Image fields: `image_size`, `quality`, `aspect_ratio`. Video fields: `duration`, `resolution`, `aspect_ratio`. Both support `references` containing HTTPS image URLs or `data:image/png|jpeg|webp;base64,...` strings (also normalized `{url:...}` / `{data:...}` objects). Inline references are bounded to avoid huge WebSocket frames; use uploaded HTTPS URLs for large sets. Each model's actual limits are checked again inside Chrome. Video references use the ordinary omni-reference workflow, with the first image as the first reference. Audio/video references, smart video editing, explicit start/end-frame roles and batch output are not exposed in this release.

A repeated Idempotency-Key and identical request returns the same job. Changing the payload with the same key returns 409. A new key means a new generation.

## Billing and results

`unlimited_only` is the default: the worker refreshes website eligibility for the exact selected model/settings and rejects combinations not marked eligible. `allow_credits` requires a positive integer `max_credits`; the local estimate must fit that value. These checks do not create an upstream atomic spending cap. Website billing is authoritative and can change between preflight and submission. There is no fallback to the separately billed official API.

The adapter uses direct generation, not Canvas Agent. The site still enforces its account quotas and challenges. The default is one active Creaa generation total per account. Separate image/video/total caps can be configured using the parallel-limits endpoint; unknown upstream work still holds the entire account. These are local dispatch caps, not claims about Creaa's own concurrency. A queue full or ambiguous account returns an explicit error.

Successful jobs return `result.urls`, containing original provider media URLs, and `result.media_type`. Download outputs while their provider URLs are valid. Flow2API does not yet copy Creaa assets into permanent local storage. A job is not marked successful until at least one usable URL appears. The image/video model list is capability discovery, not a claim that all models were live-tested or included in the subscription.

## Recovery

The backend persists a claim before dispatch and a submit intent before allowing generation. The browser records an attempt marker before calling Creaa and records its provider task ID as soon as returned. After a lost reply, the same attempt is reconciled; it is never blindly submitted again. Submitted tasks resume polling after reconnect. The Chrome ledger stores recovery metadata, not prompt/reference bodies.

`needs_review` holds the account slot because upstream work may still be running. First inspect the Creaa worker tab/history. If its task ID is known, resume tracking:

```json
{"action":"resume","provider_task_id":"THE_EXISTING_CREAA_TASK_ID"}
```

Only after confirming no upstream work remains, release an unresolved job with:

```json
{"action":"fail","confirm_no_upstream_work":true,"note":"Checked Creaa history; no running task remains."}
```

A submit timeout is not proof of failure. Normal reconnect and recover operations never create a replacement image/video. Tracking stops for review after a prolonged failure or timeout; it does not spend again. Do not use the resolve endpoint to release a slot while the original job still runs.

## Implementation and validation

- Backend: `src/api/creaa.py`, `src/services/creaa_bridge.py`, `src/core/creaa_models.py`; lifecycle integration in `src/main.py`.
- Chrome: optional `creaa-worker.js`, with same-origin operations in `creaa-page.js`, imported by the existing worker.
- Source contract observed 2026-09-08: `/api/models/image-edit/config`, `/api/models/image-edit/unlimited-config`, website `generateMediaTask` -> `/api/media/generate`, `fetchMediaTaskStatus` -> `/api/media/tasks/{id}`. Image uploads use the website's `uploadTempImageAsset`. Submission uses the site's own submit-ticket/challenge function unchanged. Frontend contract changes produce explicit failures, not guessed alternative endpoints.
- Python regression tests cover durable recovery, queueing, acknowledgements, task identity, account routing and HTTP/WebSocket behavior. Node tests cover the page adapter and worker transitions with deterministic fixtures; those are not live provider-generation tests.

Run:

```bash
python -m pytest tests/test_creaa_bridge.py tests/test_creaa_integration_regressions.py -q
node --test tests/test_creaa_page.cjs tests/test_creaa_worker.cjs
```

## Controlled parallelism

`PUT /v1/creaa/accounts/{id}/parallel-limits` accepts e.g. `{"images":2,"videos":1,"total":3,"note":"Owner-approved concurrency verification"}`. Changes are recorded in SQLite and apply immediately to new dispatches; they do not cancel existing jobs. Wait for current jobs to finish before changing caps when a clean timing test is required. The endpoint uses the existing API key, with no staff-role changes. Probe bounds are 3 images, 2 videos and 5 total, sufficient to test one beyond the advertised 2-image/1-video allowances. Provider quotas still apply; an accepted request may be queued upstream and is not proof of concurrent processing.
