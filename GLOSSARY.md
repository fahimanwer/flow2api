# flow2api

An OpenAI/Gemini-compatible API that makes images and videos with Google Flow, spread over a pool of Google
accounts that staff and server browsers keep signed in.

## Accounts and logins

**Account**:
One Google account in the pool; a row in the `tokens` table. Its tier (Free, Pro, Ultra) decides which
models and callers it serves.
_Avoid_: token (the table name is historical), user

**Worker browser**:
A Chrome running the worker extension for one account: a staff laptop, or a server browser.
_Avoid_: device, plugin

**Server browser**:
A worker browser running in a container on cf-worker-01 (flow-ultra-01, flow-ultra-02), kept up by the server,
not by a person.
_Avoid_: Ultra box

**Push**:
The worker browser sending its account's Google login cookies to the server (`POST /api/plugin/update-token`).
_Avoid_: sync, upload, ST push

**Cookie session**:
The server's own sign-in to flow.google.com for one account, made from the account's pushed cookies and held in
memory. It lasts until the browser rotates its Google login.
_Avoid_: session token, ST, AT

**Session handle**:
The `FLOWCK.…` string in an account's `at` column that names its cookie session.
_Avoid_: access token, AT

**Healer**:
The background loop that re-checks disabled accounts with their stored cookies and turns them back on.
_Avoid_: protocol refresh

## Generation

**Mint**:
Getting a reCAPTCHA token for one generation from a real browser on flow.google.com.
_Avoid_: solve, captcha solve

**Server fallback minter**:
The headless Chromium on the flow2api server that mints when an account's worker browser cannot.
_Avoid_: personal mode, browser mode

**Caller**:
An app that calls flow2api, named by the `X-Flow-Client` header (pinterest-factory, ai-reels, interior-ai,
default).
_Avoid_: client (in code it is `client`), user

**Client policy**:
The rule for which account tiers a caller may use, per media type.

**Pool**:
Which traffic an account serves: `auto` (normal traffic) or `failed_image` (staff re-runs of failed images); set
in the worker extension. Normal traffic never borrows `failed_image` accounts (owner decision 2026-11-04).

**Character**:
A named person or thing, made from 1-2 photos, that stays the same across images (`@Name` in the prompt).
_Avoid_: entity (Google's API word)

## Google's side

**Flow web RPC**:
A call to flow.google.com's own web API (`batchexecute?rpcids=…`), the only Flow API that works since 2026-10-21.
_Avoid_: aisandbox REST, Labs API (both retired for us)
