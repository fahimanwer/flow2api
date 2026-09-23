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

"Keep working when I'm away" is ON by default in 3.7.0. Turning it OFF sends
`google_cookies: ""` right away and the server deletes its copy (`protocol_mode` back to
`session`). Turning it ON pushes the current login immediately. The status line shows what the
last push proved: "Server copy updated N min ago (K cookies)", "Not signed in to Google", or
the server's error.

When the switch is ON the push also repeats whenever Google rotates a login cookie
(`SID`, `HSID`, `SSID`, `LSID`, `__Secure-1PSID/3PSID`, `__Secure-1PSIDTS/3PSIDTS`), debounced
30 s and at most once per 10 min, on top of the hourly session push.

## How the server uses it

1. `plugin_update_token`: a valid `session_token` is verified as before (validate-then-promote).
   If the pushed session is dead (`st_expired`) or missing and cookies were sent, the server
   derives a fresh session from the cookies first and verifies that one. Cookies are stored
   only after a verified credential; a failed cookie login answers 400 and stores nothing.
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
