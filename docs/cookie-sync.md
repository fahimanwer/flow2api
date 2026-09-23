# Cookie sync — "Keep working when I'm away" (worker extension 3.7.0, 2026-09-23)

## The problem it solves

To generate on an account the server needs three things: a valid Google Labs session (`st`),
a reCAPTCHA token, and the account's residential proxy. Since 2026-09-23 the captcha is minted
on the server when the worker is away (`docs/…` server fallback, `src/services/flow_page_captcha.py`)
and the proxy is stored per account. The Labs session was the last thing only the staff
laptop could renew: when Google let it expire, the account failed with `AUTH/ST_EXPIRED` until
that person's Chrome pushed a fresh one.

With cookie sync the extension also shares the Google login cookies of that Chrome profile.
When the Labs session dies, the server signs in to Labs again itself, replaying NextAuth's
"Sign in with Google" with those cookies through the account's own proxy
(`src/services/protocol_login.py`), and stores the new session. The laptop can be closed.

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

## How the server uses it

1. `plugin_update_token`: a valid `session_token` is verified as before (validate-then-promote).
   If the pushed session is dead (Labs answers 401 or "no session"), missing, or its grant is
   stale (`at_stale`) and cookies were sent, the server first checks whether the row already
   holds a newer working session (from an earlier push or the healer) and reuses it; only
   then does it replay the Google login (one at a time per account, inside a 23 s budget).
   A transport failure talking to Google is a 503 (the worker retries), never a login replay.
   Cookies are stored whenever the push proved the account's email, also on `at_stale`; a
   failed cookie login for a dead session answers 400 and stores nothing.
2. `TokenManager._try_protocol_refresh_st`: tried first whenever an access-token refresh fails
   (`_do_refresh_at` recovery attempt 1, before asking the extension). The login goes through
   `tokens.proxy_url` if set, else the account's `redeem_proxy_url` — never the datacenter IP.
3. Background healer (`run_protocol_refresh_once`, every 60 s, gated by the admin
   "protocol refresh" switch and `refresh_interval_minutes`): re-logs in ONLY accounts that are
   disabled for a dead session (`auto_st_expired` / `auto_at_stale`). Active accounts are healed
   on demand; a periodic Google login for 36 healthy accounts would only invite risk flags.
4. If Google refuses the replay (`signin/rejected`, expired cookies), the account behaves as
   before: it waits for the next extension push. Nothing regresses.

## Limits

- Google can end the login itself (sign-out everywhere, risk engine). Then only a human login
  in Chrome helps; the next push shares the new cookies.
- The server's login is one HTTP replay per dead session, through the account's proxy. Google
  may still score it lower than a real browser; the live test on 2026-09-23 is the reference.
- Old extensions (≤ 3.6.4) keep working unchanged; they simply never send cookies.
