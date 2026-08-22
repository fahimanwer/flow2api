# Async generation API

The generation endpoints (`/v1/chat/completions`, `:generateContent`) await the whole
generation inline. A Veo video takes minutes, so a client that dies mid-call has no handle to
reconnect with — its only option is to submit again, and pay again.

These three endpoints wrap the *same* pipeline in a resumable job:

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/async/chat/completions` | Submit an OpenAI-shaped request, get a `task_id` back immediately |
| `POST /v1beta/models/{model}:asyncGenerateContent` | Same, for the Gemini-shaped request body |
| `GET /v1/async/tasks/{task_id}` | Job status |
| `GET /v1/async/tasks/{task_id}/result` | The finished payload, in the shape the sync endpoint would have returned |

`POST /models/{model}:asyncGenerateContent` works too, matching the sync route's alias.

**Auth is unchanged**: the same API keys, accepted the same three ways as every other endpoint —
`Authorization: Bearer <key>`, `x-goog-api-key: <key>`, or `?key=<key>`. Each key belongs to a
named **principal** (`[global.api_keys]` in `setting.toml`, e.g. `ai-reels = "sk-…"`; the single
`[global] api_key` is the principal `legacy`). A job is owned by the principal that created it,
so rotating a principal's key keeps its jobs reachable; another principal asking for it gets
`404`, identical to a task id that does not exist. With no key configured at all every request is
refused with `503 authentication not configured`.

**Task ids** are `gen_` + a uuid4 hex (`gen_9f2c…`), so they cannot be guessed or enumerated.

**Results live for 24 hours** after the job finishes, then a background sweep deletes them.

---

## 1. Submit

The request body is exactly the sync endpoint's body, plus an optional idempotency key.
`stream: true` is accepted but ignored — there is no socket to stream into once the call
returns, so the result is always collected in the non-streaming shape.

```bash
curl -X POST "http://localhost:8000/v1/async/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: reel-4711-scene-02" \
  -d '{
    "model": "veo_3_1_t2v",
    "messages": [
      {"role": "user", "content": "a red apple on a wooden table, studio light"}
    ]
  }'
```

```json
{
  "task_id": "gen_9f2c1d7a4b8e4c2fa1d3e5b7c9a0f2e4",
  "status": "queued",
  "model": "veo_3_1_t2v_landscape",
  "created_at": "2026-08-10T09:12:44",
  "started_at": null,
  "completed_at": null,
  "idempotency_key": "reel-4711-scene-02"
}
```

`202 Accepted` means a new generation started. Timestamps are UTC. `model` is the *resolved*
model (aliases and aspect-ratio variants are applied at submit time, exactly as in the sync path).

The Gemini body works the same way:

```bash
curl -X POST "http://localhost:8000/v1beta/models/gemini-3.1-flash-image:asyncGenerateContent" \
  -H "x-goog-api-key: han1234" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: reel-4711-thumb" \
  -d '{
    "contents": [{"role": "user", "parts": [{"text": "a red apple on a wooden table"}]}],
    "generationConfig": {"responseModalities": ["IMAGE"]}
  }'
```

Bad requests fail at submit time, not later: an empty prompt, an unreadable image URL, or an
unsupported model returns the same `400` the sync endpoint returns, and no job is created.

### Idempotency — the point of all this

Send `Idempotency-Key` (header) or `"idempotency_key"` (body field; the header wins). If a job
with that key already exists **for this API key**, the submit starts nothing and returns the
existing job with `200 OK` and `"replayed": true`:

```json
{
  "task_id": "gen_9f2c1d7a4b8e4c2fa1d3e5b7c9a0f2e4",
  "status": "running",
  "model": "veo_3_1_t2v_landscape",
  "replayed": true,
  ...
}
```

So a crashed client can safely re-send the identical request: `202` means it is paying for a
generation, `200 + replayed` means it reconnected to one it already paid for. Two concurrent
submits with the same key are also safe — a unique index decides the winner and the loser gets
the winner's job.

Without an idempotency key nothing is deduplicated: every submit starts a new generation.

## 2. Status

```bash
curl "http://localhost:8000/v1/async/tasks/gen_9f2c1d7a4b8e4c2fa1d3e5b7c9a0f2e4" \
  -H "Authorization: Bearer han1234"
```

```json
{
  "task_id": "gen_9f2c1d7a4b8e4c2fa1d3e5b7c9a0f2e4",
  "status": "running",
  "model": "veo_3_1_t2v_landscape",
  "created_at": "2026-08-10T09:12:44",
  "started_at": "2026-08-10T09:12:44",
  "completed_at": null
}
```

`status` is one of:

| Status | Meaning |
| --- | --- |
| `queued` | Accepted, worker has not started yet |
| `running` | Generation in flight |
| `succeeded` | Finished; fetch the result |
| `failed` | Finished; `error` holds the reason, and the result endpoint replays the full error |

Failed jobs carry an `"error"` field with the message. A job interrupted by a server restart is
reported as `failed` ("Generation was interrupted by a server restart") rather than being left
`running` forever.

`404` means unknown task id — or a task belonging to a different API key.

## 3. Result

```bash
curl -i "http://localhost:8000/v1/async/tasks/gen_9f2c1d7a4b8e4c2fa1d3e5b7c9a0f2e4/result" \
  -H "Authorization: Bearer han1234"
```

Not finished yet → `425 Too Early` with a `Retry-After: 5` header:

```json
{
  "task_id": "gen_9f2c1d7a4b8e4c2fa1d3e5b7c9a0f2e4",
  "status": "running",
  "message": "Generation is not finished yet"
}
```

Finished → the response the sync endpoint would have returned, in the shape of whichever
surface submitted the job. A job submitted to `/v1/async/chat/completions` returns the OpenAI
chat-completion payload:

```json
{
  "id": "chatcmpl-1754817164",
  "object": "chat.completion",
  "model": "veo_3_1_t2v_landscape",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "<video src=\"http://localhost:8000/tmp/abc.mp4\" controls></video>"
      }
    }
  ]
}
```

A job submitted to `:asyncGenerateContent` returns the Gemini `candidates` payload instead,
with the media inlined or referenced exactly as the sync `:generateContent` does.

Failures replay the sync error shape *and* the sync status code — a job that ran out of tokens
returns `503` with the pipeline's own error body, not a wrapper:

```json
{
  "error": {
    "message": "No available token",
    "type": "server_error",
    "code": "generation_failed",
    "status_code": 503
  }
}
```

(For the Gemini surface the same failure comes back as `{"error": {"code": 503, "message": ...,
"status": "UNAVAILABLE"}}`.)

## Polling pattern

```bash
KEY=han1234
BASE=http://localhost:8000

TASK=$(curl -s -X POST "$BASE/v1/async/chat/completions" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -H "Idempotency-Key: reel-4711-scene-02" \
  -d '{"model":"veo_3_1_t2v","messages":[{"role":"user","content":"a red apple"}]}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["task_id"])')

while true; do
  STATUS=$(curl -s "$BASE/v1/async/tasks/$TASK" -H "Authorization: Bearer $KEY" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
  echo "status: $STATUS"
  case "$STATUS" in succeeded|failed) break;; esac
  sleep 5
done

curl -s "$BASE/v1/async/tasks/$TASK/result" -H "Authorization: Bearer $KEY"
```

Re-running that whole script after a crash costs nothing extra: the same `Idempotency-Key`
reconnects to the running job.

## Operational notes

- **Where state lives**: the `async_tasks` table in the existing SQLite database
  (`data/flow.db`), created by `Database.init_db()` like every other table. Nothing new to
  provision. Ownership is stored as `principal:<name>`; the key itself never lands in the
  database. Rows written before named principals existed carry `sha256(raw key)` instead and
  stay readable only through the `legacy` single key until the 24-hour retention sweep removes
  them.
- **Workers are in-process.** They do not survive a restart — that is why startup fails any job
  left `queued`/`running`, so clients get a definite answer instead of polling a dead job. A
  client that sees this can safely re-submit *with a new idempotency key*; reusing the old key
  would just replay the failed job.
- **Retention** is 24 hours after completion (`AsyncTaskManager.RESULT_RETENTION_HOURS`), swept
  every 30 minutes. Unfinished jobs are never swept, however long they run.
- **Concurrency** is unchanged: async jobs go through the same token pool, load balancer, and
  per-token concurrency limits as sync requests, so submitting 50 jobs does not bypass any
  limit — they queue on the same resources.

## Tests

```bash
python -m unittest tests.test_async_generation_api
```

Covers job scoping per API key, idempotent re-submit (including the concurrent-submit race),
the `425`/`404` cases, error replay in both response shapes, retention, and the restart sweep.
