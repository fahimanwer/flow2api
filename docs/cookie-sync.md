# Cookie sync — the account's Google login on the server (worker extension 3.7.0+, cookie sessions since 2026-10-23)

> **Since 2026-10-23 these cookies ARE the Flow login.** Google retired the Labs session API
> (`labs.google/fx/api/auth/session` → "Authentication endpoint has been deprecated and disabled",
> `/api/auth/csrf` → 410, from ~2026-10-21 19:35 UTC). The server now logs in to flow.google.com
> with the pushed `google_cookies` and calls Flow's web RPCs; see "How the server uses it" below.
> An account whose worker never pushes cookies (switch OFF, extension < 3.7.0) cannot work at all.

## What it does

To generate on an account the server needs three things: a Google login for Flow, a reCAPTCHA
token, and the account's residential proxy. The captcha is minted by the worker browser (or by
the server fallback, `src/services/flow_page_captcha.py`, when the worker is away) and the proxy
is stored per account. The Google login comes from cookie sync: the extension shares the Google
login cookies of its Chrome profile, and the server logs in to Flow with them through the
account's own proxy.

What it does NOT do any more (corrected 2026-10-23): keep an account working for long with the
laptop closed. Until 2026-10-21 the server replayed the Labs "Sign in with Google" and got a
long-lived Labs session; that login is retired. A server session now lasts only until the
browser rotates its Google login (measured: 10–30 min after a push, see Limits), and the next
push carries the rotated cookies. With the browser gone, the account stops within the hour.

## What is shared, and where it goes

- Exactly the cookies Chrome would send to `https://accounts.google.com/` for that profile
  (`chrome.cookies.getAll({url})`), serialised as a JSON list of `{name, value}`. Nothing else
  from the browser.
- Sent inside the normal session push (`POST /api/plugin/update-token`, plugin connection
  token auth) as `google_cookies`, together with `protocol_mode: "protocol"`.
- Stored in the `tokens` row (`google_cookies`, `protocol_mode`, `login_account`,
  `google_cookies_updated_at`) in `flow.db`, in plain text like `st`/`at`. The admin API never
  returns the value (`GET /api/tokens` reports `google_cookies_set` and the timestamp only), and
  no log line ever contains it (`[COOKIE_SYNC]` lines carry counts).
- Security trade-off, accepted by the owner: whoever can read `flow.db` holds the Google login
  of every account that has the switch ON. That is the same class of secret as the Labs
  session already stored there, which grants the same Flow access.

## The switch (extension popup)

"Keep working when I'm away" is ON by default in 3.7.0. Turning it OFF calls
`POST /api/plugin/cookie-sync {action: clear}` right away (no Google round-trip, so it works
even when the Labs login is broken) and the server deletes its copy (`protocol_mode` back to
`session`); the switch shows "Deletion pending" and retries every minute until the server
confirms. OFF does not revoke the Labs session the server already holds; that one simply
expires on its own and is not renewed. Turning it ON pushes the current login immediately.
The status line shows what the last push proved: "Server copy updated N min ago (K cookies)"
means the server stored the cookies, not that a future renewal is guaranteed (Google can
still refuse the replay; then the account waits for the laptop as before).

Every cookie write carries a client sequence (`cookie_sync_seq`); the server applies a write
only if its sequence is newer than the stored one, in a single conditional statement, so a
slow ON push can never undo a later OFF. The clear call is addressed by the account's token id
and authenticated with the shared plugin connection token: any worker holding that token can
clear any account, which is the same trust level the session push already has.

When the switch is ON the push also repeats whenever Google rotates a login cookie
(`SID`, `HSID`, `SSID`, `LSID`, `__Secure-1PSID/3PSID`, `__Secure-1PSIDTS/3PSIDTS`), debounced
30 s and at most once per 10 min, on top of the hourly session push.

## How the server uses it (cookie sessions, 2026-10-23)

Code: `src/services/flow_cookie_session.py` (login + transport), `src/services/flow_rpc.py` and
`flow_rpc_video.py` (REST → RPC translation), hooks in `FlowClient.st_to_at` / `_make_request`.

1. **Login.** The `{name, value}` list is split: `LSID`, `ACCOUNT_CHOOSER`, `OTZ`, `__Host-*` on
   `accounts.google.com`, the rest on `.google.com`. `GET flow.google.com/projects` through the
   account's proxy (`proxy_url`, else `redeem_proxy_url`; never direct) bounces through
   `accounts.google.com/ServiceLogin` and `flow.google.com/accounts/SetOSID`, which gives the
   server its own `OSID` / `__Secure-OSID`. The account is read from the page's `oPEP7c` field
   only, and must equal the row's email.
2. **The account's `at` is a handle** (`FLOWCK.<email hash>.<random>`) onto that in-memory session.
   Every session has its own handle, so a session built while checking a push serves nothing
   until the guarded DB write (Ultra ownership, account match) stores it. After a restart the
   handle in the DB is rebuilt from the stored cookies.
3. **Calls.** `_make_request` turns each aisandbox REST call into the Flow web RPC: credits
   `nzlxg`, image `ogiZ0b`, upload `maseQ` (own `UPLOAD_IMAGE` captcha), 2K/4K `SPrCad`, project
   `jHPbke`, videos `YhhmEf`/`MZZa6b`/`nprQif`/`eb1hJf`/`fZytfe`/`p0UkFb`, status `jwpduf` + `as29s`.
   The image request was compared slot by slot with a HAR of the real Flow web app (2026-10-23).
4. **Push** (`plugin_update_token`): cookie-only pushes are validated by a Flow login with the
   PUSHED cookies; no Labs login is ever replayed. The row keeps its `st` value as a key.
5. **Logged out** = Google shows a sign-in page, answers 401, or Flow shows its signed-out app
   page. Server errors, a 403 or a changed page are retry-later errors and never disable an account.
6. **Tier** is never guessed: the stored tier wins; a new row takes Flow's 2nd credits number
   (1 = Pro, 2 = Ultra, matching every account seen on 2026-10-23).
7. `FLOW_COOKIE_RPC=0` restores the retired Labs path (useful only if Google brings it back).

Not on the new API yet (clear 501, never an account strike): Flow Characters, stitching an
extended video.

## Limits

- Google can end the login itself (sign-out everywhere, risk engine). Then only a human login
  in Chrome helps; the next push shares the new cookies.
- Device-bound logins (DBSC): 26 of 36 stored cookie sets on 2026-10-23 carry `__Secure-1PSIDRTS` /
  `__Secure-STRP`. Copied cookies of such a login go stale (Flow shows its signed-out page; seen on
  account 51 about an hour after its last push). The extension re-pushes when Google rotates the
  login cookie (at most every 10 min) and hourly. Measured 2026-10-23 (`tmp/dbsc_lifetime_1023.py`, frozen
  cookie snapshots, through each account's proxy): two DBSC accounts' sessions AND fresh logins still worked
  28 min after the snapshot (cookies up to ~41 min old); a non-DBSC account's snapshot died 19 min in,
  exactly when its browser rotated its login cookies and pushed new ones (old OSID session 401, new login →
  sign-in page), while production moved to the new cookies on that push. So what ends a server session is
  the BROWSER rotating its login, not a fixed lifetime; the server follows only through the next push.
  UNVERIFIED: whether the extension's 10-min gap between cookie-triggered pushes (`COOKIE_SYNC_MIN_GAP_MS`)
  explains account 51 dying with no newer push.
- Old extensions (≤ 3.6.4) never send cookies, so since 2026-10-23 their accounts cannot work.
