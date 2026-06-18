# Flow2API Worker (Chrome extension)

All-in-one internal worker that lets a real, logged-in browser serve a Flow2API
backend. It replaces the two older extensions (Captcha Worker + Token Updater).

It does two things:

1. **reCAPTCHA tokens** — connects to the backend's `/captcha_ws` WebSocket and,
   on request, mints a Google reCAPTCHA Enterprise token from a real browser
   session (which Flow trusts). Uses one **persistent hidden Labs tab** and reuses
   it (fast, low churn); switchable to per-generation mode in Options.
2. **Session refresh** — every N minutes (default 60) it reads the Google Labs
   `session-token` cookie and pushes it to `/api/plugin/update-token`, keeping the
   account login valid. Also runs once immediately on install.

## Install (staff: zip-and-load, no setup)

1. Unzip `Flow2API-Worker.zip`.
2. `chrome://extensions` → enable **Developer mode** → **Load unpacked** → pick the folder.
3. Be **logged into Google Labs / Flow** in that Chrome profile. Done.

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

Persistent tab is recreated if closed; token requests retry once on a fresh tab;
the WebSocket auto-reconnects; a 1-minute keepalive alarm revives the socket and
tab if Chrome suspends the service worker.
