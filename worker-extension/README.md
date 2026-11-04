# Flow2API Worker (Chrome extension)

All-in-one internal worker that lets a real, logged-in browser serve a Flow2API
backend. It replaces the two older extensions (Captcha Worker + Token Updater).

It does two things:

1. **reCAPTCHA tokens** — connects to the backend's `/captcha_ws` WebSocket and,
   on request, mints a Google reCAPTCHA Enterprise token from a real browser
   session (which Flow trusts). Uses one hidden **flow.google.com/about** tab and reuses
   it (fast, low churn). Minting needs no Flow sign-in.
2. **Login sharing (cookie sync)** — pushes the profile's Google login cookies (what Chrome holds
   for `accounts.google.com` and `flow.google.com`, each with its domain/path/flags) to
   `/api/plugin/update-token`. Since 2026-10-23 (Google retired the Labs login) these cookies ARE
   the account's Flow login on the server (`docs/cookie-sync.md`). 3.8.0 (2026-11-04) is
   cookie-first: no Labs tab, Labs cookie or Labs reload any more. When Google rotates the login
   (`SID`/`HSID`/`SSID`/`LSID`, `__Secure-1PSID`/`3PSID`, `…PSIDTS`, `…PSIDRTS`, `__Secure-STRP`)
   the push goes out ~10 s after the burst, never sooner than 90 s after the previous push (delayed
   with `chrome.alarms`, never dropped); a safety-net push runs every 20 min. The popup shows the
   server's verdict: a refused login (HTTP 400) turns the icon red with "Open flow.google.com in
   this Chrome and check you are signed in".

3. **Suno login sharing (optional, off by default)** — `suno.js`. Flip
   **Share my Suno login** ON in the popup when this Chrome profile is signed in
   to suno.com. The worker reads the Suno login cookie your own browsing created and
   POSTs it to `/api/plugin/suno-cookie` (plugin connection token), then re-sends it
   whenever the cookie changes (debounced) and at least every 6 h. It opens no Suno
   tabs and does not touch the Flow socket or the proxy. OFF stops syncing; the
   account already saved on the backend stays until an admin removes it.

## Install (staff: zip-and-load, no setup)

1. Unzip `Flow2API-Worker.zip`.
2. `chrome://extensions` → enable **Developer mode** → **Load unpacked** → pick the folder.
3. Be **signed in to Google** in that Chrome profile and open https://flow.google.com/ once (it must show
   your account; the popup's **Open flow.google.com** button does that). Keep **"Use this account in
   Flow2API" ON**: since 2026-10-23 an account cannot work without it. This Chrome must stay running; the
   account pauses about 20–60 min after Chrome stops. Turning the switch OFF (it asks first) means "stop
   using this account in Flow2API": the server deletes its copy of the login.

Config (server URL, API key, connection token) is baked into the defaults for
zip-and-load distribution — this is an internal tool, so the credentials are
intentionally hard-coded. Edit defaults in `background.js` / `options.js` if the
server or keys change.

## Fleet / multi-account

Each laptop logs into its own Flow account; the session push auto-creates/updates
that account's token (matched by email). With an empty **Route Key** all browsers
form a shared captcha pool (any browser serves any account — reCAPTCHA tokens are
site-level, not account-bound). Set a unique Route Key per laptop + the matching
`extension_route_key` on its token only if you want strict per-account routing.

## Reliability

The mint tab is recreated if closed; token requests retry once on a fresh tab;
the WebSocket auto-reconnects; a 1-minute keepalive alarm revives the socket and
mint tab if Chrome suspends the service worker. The worker never closes the last tab
of a window. On first boot, 3.8.0 closes the hidden Labs tab older versions kept.

Tests for the pure helpers: `node --test tests/ext/` from the repo root.
