/* Flow2API Worker — combined captcha + token-refresh service worker.
 *
 * Two jobs in one extension:
 *   1) reCAPTCHA: holds a persistent (hidden) Google Labs tab and mints a fresh
 *      reCAPTCHA Enterprise token on demand over the /captcha_ws WebSocket.
 *   2) Session refresh: on a timer, extracts the Google Labs session-token cookie
 *      and POSTs it to /api/plugin/update-token so the backend's login stays valid.
 *
 * Built for heavy use and a fleet of laptops (one account each): set a unique
 * Route Key per laptop so the backend routes each account's captcha to the right
 * browser. Reliability is the priority — persistent tab is recreated on loss,
 * the socket auto-reconnects, and an alarm revives everything if Chrome suspends
 * the service worker.
 *
 * Anti-tab-storm guarantees (post sleep/wake): the extension only ever closes
 * tabs IT created (tracked in storage), never the user's own Labs tabs; tab
 * creation is bounded by a storage-backed lease + a hard ceiling so a wake storm
 * can never pile up tabs; and if Google Labs needs a fresh login (session expired
 * during sleep -> redirect to accounts.google.com) it STOPS opening tabs, backs
 * off, and raises a "login required" badge instead of churning Chrome to a crash.
 */

const RECAPTCHA_SITE_KEY = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV";
// Where the worker tab is opened to mint. 2026-09-04: Google now bounces migrated
// accounts from labs.google/fx/tools/flow to flow.google.com (client-side, ~1.5 s
// after load). The new flow.google.com Angular app does NOT load grecaptcha at
// all, and its CSP (Trusted Types + nonce) blocks injecting it, so a tab there can
// never mint ("Failed to set the 'src' property ... TrustedScriptURL"). The Labs
// FX home page does not redirect, loads grecaptcha.enterprise itself with the
// SAME site key, and keeps the same NextAuth session alive (/fx/api/auth/session)
// for migrated and non-migrated accounts alike — so that is where we mint.
const MINT_URL = "https://labs.google/fx";
// A tab is "on Flow" (not bounced to login/consent) on either origin, so a
// redirect to flow.google.com is never mistaken for a sign-out ...
const FLOW_ORIGINS = ["https://labs.google/fx", "https://flow.google.com"];
// ... but only a labs.google tab can actually mint (see above).
const MINT_ORIGINS = ["https://labs.google/fx"];
const COOKIE_DOMAINS = ["labs.google", "flow.google.com"];
const SESSION_COOKIE = "__Secure-next-auth.session-token";

const ALARM_SESSION = "flow2api_session_refresh";
const ALARM_KEEPALIVE = "flow2api_keepalive";
const ALARM_RELOAD = "flow2api_session_reload";

// Proactive session-cookie roll: reload the persistent Labs tab when the
// session-token cookie is within this window of expiry, so NextAuth re-issues
// (rolls) a fresh cookie long before it can drift to expiry.
const RELOAD_THRESHOLD_MS = 24 * 60 * 60 * 1000;  // reload when < 24h to expiry
const RELOAD_MIN_GAP_MS   = 2 * 60 * 60 * 1000;   // never reload more than ~once / 2h
const RELOAD_ACTIVE_MS    = 45 * 1000;            // skip reload if a mint started in last 45s (>= VIDEO 30s)
const COOKIE_SETTLE_MS    = 1500;                 // let NextAuth write the rolled cookie
const PUSH_TIMEOUT_MS     = 25 * 1000;            // hard bound on the session-push POST

// WebSocket heartbeat: keep the MV3 service worker alive (Chrome 116+ resets the
// idle timer on WS traffic). 15s gives margin under the ~30s idle limit + timer jitter.
const HEARTBEAT_MS = 15000;

// Tab-creation safety rails.
const LEASE_MS = 25000;            // creation lease lifetime (> worst-case tab load ~16s)
const MAX_OWNED_TABS = 2;          // hard ceiling fuse: never keep more owned Labs tabs than this
const LOGIN_HOSTS = ["accounts.google.com", "consent.google.com", "signin.google.com"];
const AUTH_BACKOFF_MS = [60000, 300000, 900000, 1800000]; // 1m, 5m, 15m, 30m (capped)

const DEFAULT_SETTINGS = {
  // Baked-in config so the extension works the moment it is loaded — no setup.
  // Internal tool; these are intentionally hard-coded for zip-and-load distribution.
  serverBase: "https://flow.ashuthefire.com",
  apiKey: "han1234",      // Flow2API API key -> authenticates the captcha WebSocket
  connectionToken: "fahim", // plugin connection token -> authenticates token-update
  routeKey: "",           // empty = shared captcha pool (any browser serves any account)
  clientLabel: "",        // optional friendly name shown in the backend logs
  refreshIntervalMinutes: 60,
  tabMode: "persistent",  // "persistent" (reuse one hidden tab) | "ephemeral" (open/close per token)
  mintIntervalMs: 2000,   // min spacing between reCAPTCHA mints; paces one browser under Google's rate limit
  // Per-profile egress proxy. With proxyAuto ON (default), EACH Chrome profile
  // auto-builds its OWN sticky Oxylabs session (a random per-profile sessid that
  // persists in this profile's storage), so every profile mints reCAPTCHA and
  // holds its Google session from a DIFFERENT IP with zero setup. proxyUrl, if
  // set, overrides the auto URL (form: http://USER:PASS@HOST:PORT). proxyAuto
  // false + empty proxyUrl = direct (no proxy).
  proxyAuto: true,
  proxyUrl: "",
  // "Failed-image mode" switch. MUST be listed here: getSettings() reads via
  // chrome.storage.local.get(DEFAULT_SETTINGS), and that form returns ONLY the keys
  // present in this object — a key absent here reads back undefined no matter what the
  // options popup saved, so the register/session-push would always report pool=auto and
  // the reserve-my-account toggle silently never took effect. Default OFF (auto pool).
  failedImageMode: false
};

let ws = null;
let lastMintAt = 0;       // timestamp of the last reCAPTCHA mint (for pacing)
let mintInFlight = false; // true while handleGetToken holds the persistent tab (in-mem only)
let lastReloadAt = 0;     // timestamp of last proactive reload (in-mem rate cap)
let heartbeatInterval = null;
let reconnectTimer = null;
let persistentTabId = null;           // in-memory cache of the live persistent tab id
let tokenQueue = Promise.resolve();   // serialize token requests
let ensureChain = Promise.resolve();  // serialize persistent-tab ensures within this SW life
let ownedQueue = Promise.resolve();   // serialize owned-tab-id storage mutations
let connecting = false;

/* ----------------------------- settings ----------------------------- */

function getSettings() {
  return new Promise((resolve) => {
    chrome.storage.local.get(DEFAULT_SETTINGS, (stored) => {
      const build = (routeKey) => resolve({
        serverBase: (stored.serverBase || DEFAULT_SETTINGS.serverBase).trim().replace(/\/+$/, ""),
        apiKey: (stored.apiKey || "").trim(),
        connectionToken: (stored.connectionToken || "").trim(),
        routeKey,
        clientLabel: (stored.clientLabel || "").trim(),
        refreshIntervalMinutes: Math.max(5, parseInt(stored.refreshIntervalMinutes, 10) || 60),
        tabMode: stored.tabMode === "ephemeral" ? "ephemeral" : "persistent",
        mintIntervalMs: Math.max(0, parseInt(stored.mintIntervalMs, 10) || 2000),
        // Residential proxy is ALWAYS on for staff builds: the backend redeems the
        // generate call from the same residential IP the extension minted from, so a
        // profile stuck on "direct egress" would break reCAPTCHA alignment. Not toggleable.
        proxyAuto: true,
        proxyUrl: (stored.proxyUrl || "").trim(),
        // "Failed-image mode" switch: when ON this account is reserved for staff-driven
        // failed-image regeneration (reported as pool_mode=failed_image, kept out of the
        // automatic article pool).
        failedImageMode: stored.failedImageMode === true
      });
      const explicit = (stored.routeKey || "").trim();
      if (explicit) return build(explicit);
      // No explicit route key → use a STABLE auto per-profile key. The backend binds
      // THIS account's token to it, so captcha minting for the account routes back to
      // THIS device — mint and redeem then share the same residential IP (reCAPTCHA
      // consistency). Persisted so it never changes for this profile.
      chrome.storage.local.get(["autoRouteKey"], ({ autoRouteKey }) => {
        if (autoRouteKey) return build(autoRouteKey);
        autoRouteKey = "auto-" + ((self.crypto && crypto.randomUUID)
          ? crypto.randomUUID()
          : (Date.now() + "-" + Math.random().toString(36).slice(2)));
        chrome.storage.local.set({ autoRouteKey }, () => build(autoRouteKey));
      });
    });
  });
}

function deriveUrls(settings) {
  const base = new URL(settings.serverBase);
  const wsScheme = base.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${wsScheme}//${base.host}/captcha_ws`;
  const origin = `${base.protocol}//${base.host}`;
  const updateUrl = `${origin}/api/plugin/update-token`;
  const versionUrl = `${origin}/api/plugin/ext-version`;
  const downloadUrl = `${origin}/download/worker-latest.zip?token=${encodeURIComponent(settings.connectionToken || "")}`;
  return { wsUrl, updateUrl, versionUrl, downloadUrl };
}

// This build's own version, from the manifest — reported to the backend and
// compared against the latest published package for the update banner.
function extVersion() {
  try { return chrome.runtime.getManifest().version || ""; } catch (_) { return ""; }
}

// Numeric dotted-version compare: >0 if a>b, <0 if a<b, 0 if equal. "3.3.10" > "3.3.9".
function cmpVersion(a, b) {
  const pa = String(a || "").split("."), pb = String(b || "").split(".");
  const n = Math.max(pa.length, pb.length);
  for (let i = 0; i < n; i++) {
    const x = parseInt(pa[i] || "0", 10) || 0, y = parseInt(pb[i] || "0", 10) || 0;
    if (x !== y) return x - y;
  }
  return 0;
}

// Ask the backend for the latest published version and cache an updateInfo blob
// the popup reads. DIRECT egress (the PAC only proxies Google), so this never
// burns residential bandwidth. Silent-soft on any error — never blocks the worker.
async function checkForUpdate(settings) {
  settings = settings || (await getSettings());
  if (!settings.serverBase || !settings.connectionToken) return null;
  try {
    const { versionUrl, downloadUrl } = deriveUrls(settings);
    const resp = await fetch(versionUrl, {
      headers: { "Authorization": `Bearer ${settings.connectionToken}` },
    });
    if (!resp.ok) return null;
    const data = await resp.json();
    const latest = (data && data.version) ? String(data.version) : "";
    const current = extVersion();
    const info = {
      current,
      latest,
      updateAvailable: !!latest && cmpVersion(latest, current) > 0,
      downloadUrl,
      checkedAt: Date.now(),
    };
    await chrome.storage.local.set({ updateInfo: info });
    if (info.updateAvailable) {
      await log("INFO", `Extension update available: v${latest} (you have v${current})`);
      // Notify ONCE per new version (not on every hourly re-check) so it grabs attention
      // without becoming noise; the toolbar badge is the persistent reminder after that.
      const { notifiedUpdateVersion } = await chrome.storage.local.get(["notifiedUpdateVersion"]);
      if (notifiedUpdateVersion !== latest) {
        await chrome.storage.local.set({ notifiedUpdateVersion: latest });
        notifyUpdate(latest);
      }
    }
    await refreshBadge();   // raise (or clear, once updated) the blue ↑ on the toolbar icon
    return info;
  } catch (e) {
    return null;
  }
}

/* ------------------------------- proxy ------------------------------- */
// Per-profile residential egress so each Chrome profile mints reCAPTCHA and
// holds its Google session from a DIFFERENT IP, spreading Google's per-IP
// reCAPTCHA flag across profiles without more machines.
//
// chrome.proxy applies to the WHOLE profile. We deliberately BYPASS the
// Flow2API backend (the /captcha_ws WebSocket + /api/plugin/update-token) so
// only Google traffic egresses through the metered residential proxy — the
// long-lived heartbeat socket would otherwise burn proxy bandwidth and risk
// the sticky tunnel dropping. Empty proxyUrl => proxy cleared (direct egress).

let proxyCreds = null;               // { username, password } or null (for onAuthRequired)
const answeredAuthReqs = new Set();  // requestIds already answered (407 loop guard)

// Baked-in Oxylabs static-ISP base (zip-and-load distribution, like apiKey).
// This account is a STATIC ISP plan: disp.oxylabs.io ports 8001-8005 are 5
// DISTINCT fixed residential IPs (sessid/sesstime suffixes don't work here).
// proxyAuto gives each profile its own random port -> its own static IP, so
// profiles mint reCAPTCHA from different IPs. Up to 5 distinct IPs available.
const PROXY_BASE = {
  user: "user-fahim_ZpTwH",
  pass: "6Fk+WKveSpned",
  host: "disp.oxylabs.io",
  ports: [8001, 8002, 8003, 8004, 8005]
};

// #2 server-managed proxy pool (fetched from /api/plugin/proxy-pool). When the admin adds
// IPs in the UI, new profiles pick them up here WITHOUT redistributing the extension.
// Falls back to the baked-in PROXY_BASE if the server hasn't set a pool / is unreachable.
let extProxyPool = null;
// Server-assigned port for THIS device (coordinated least-loaded balancing across all
// devices, so each account gets its own IP while ports are free, then spreads evenly).
let extAssignedPort = null;

function activeProxyBase() {
  if (extProxyPool && extProxyPool.host && Array.isArray(extProxyPool.ports) && extProxyPool.ports.length) {
    return {
      host: extProxyPool.host,
      user: extProxyPool.user || PROXY_BASE.user,
      pass: extProxyPool.pass || PROXY_BASE.pass,
      ports: extProxyPool.ports
    };
  }
  return PROXY_BASE;
}

async function fetchProxyPool(settings) {
  try {
    if (!settings.serverBase || !settings.connectionToken) return;
    const base = new URL(settings.serverBase);
    // Send our route_key so the server can assign THIS device a distinct, least-loaded IP.
    const rk = (settings.routeKey || "").trim();
    const url = `${base.protocol}//${base.host}/api/plugin/proxy-pool`
      + (rk ? `?route_key=${encodeURIComponent(rk)}` : "");
    const resp = await fetch(url, { headers: { "Authorization": `Bearer ${settings.connectionToken}` } });
    if (!resp.ok) return;
    const data = await resp.json();
    if (data && data.pool && Array.isArray(data.pool.ports) && data.pool.ports.length && data.pool.host) {
      extProxyPool = data.pool;
    }
    if (data && Number.isInteger(data.assigned_port)) {
      extAssignedPort = data.assigned_port;
    }
  } catch (_) { /* keep the baked-in fallback */ }
}

// Get-or-create this profile's port (one static IP from the ACTIVE pool), persisted so the
// profile keeps the SAME IP across SW restarts (a flapping IP triggers Google's
// "verify it's you"). Random pick; for guaranteed-distinct, set a specific :port override.
async function getProxyPort() {
  const ports = activeProxyBase().ports;
  // Prefer the SERVER-ASSIGNED port (coordinated least-loaded balancing across every
  // device). Adopting it overrides any stale random pick, so adding IPs and reconnecting
  // auto-resolves collisions. Persist it for stability across service-worker restarts.
  if (extAssignedPort && ports.includes(extAssignedPort)) {
    const cur = await chrome.storage.local.get(["proxyPort"]);
    if (cur.proxyPort !== extAssignedPort) await chrome.storage.local.set({ proxyPort: extAssignedPort });
    return extAssignedPort;
  }
  // Fallback (old server / offline / no route_key): keep a persisted port, else random.
  let { proxyPort } = await chrome.storage.local.get(["proxyPort"]);
  if (!proxyPort || !ports.includes(proxyPort)) {
    proxyPort = ports[Math.floor(Math.random() * ports.length)];
    await chrome.storage.local.set({ proxyPort });
  }
  return proxyPort;
}

// Effective proxy URL: manual proxyUrl overrides; else proxyAuto builds the Oxylabs URL
// from the ACTIVE proxy base (server pool if set, else baked-in) + this profile's port.
async function resolveProxyUrl(settings) {
  if (settings.proxyUrl) return settings.proxyUrl;
  if (!settings.proxyAuto) return "";
  const b = activeProxyBase();
  const port = await getProxyPort();
  return `http://${b.user}:${b.pass}@${b.host}:${port}`;
}

// Parse "http://user:pass@host:port". We split the credentials MANUALLY instead
// of via URL.username/password so proxy-special chars (e.g. '+' in the Oxylabs
// password) survive verbatim. Returns null on empty/invalid (=> direct).
function parseProxyUrl(raw) {
  const s = (raw || "").trim();
  if (!s) return null;
  const m = s.match(/^(https?|socks5|socks4):\/\/(?:([^:@/]+)(?::([^@/]*))?@)?([^:/?#]+):(\d+)\/?$/i);
  if (!m) return null;
  return {
    scheme: m[1].toLowerCase(),            // "http" | "https" | "socks5" | "socks4"
    username: m[2] ? decodeURIComponent(m[2]) : "",
    password: m[3] != null ? m[3] : "",    // RAW (not decoded) — keeps literal '+'
    host: m[4],
    port: parseInt(m[5], 10)
  };
}

// PAC proxy token for a parsed proxy ("PROXY host:port" / "SOCKS5 host:port").
function pacProxyToken(p) {
  const kind = p.scheme === "socks5" ? "SOCKS5" : p.scheme === "socks4" ? "SOCKS" : "PROXY";
  return `${kind} ${p.host}:${p.port}`;
}

// Apply (or, if resolved empty/invalid, clear) the per-profile proxy. Idempotent.
// Uses a PAC script so ONLY the Flow site + reCAPTCHA-mint URLs go through the
// proxy — the user's normal browsing (Google search, everything else) and the
// Flow2API backend go DIRECT. This stops the proxy from hijacking the whole
// profile while still making reCAPTCHA mint from the per-profile residential IP.
async function applyProxy(settings) {
  await fetchProxyPool(settings);   // #2: pick up any server-managed pool (added IPs)
  const p = parseProxyUrl(await resolveProxyUrl(settings));
  if (!p) { await clearProxy(); return; }
  proxyCreds = { username: p.username, password: p.password };
  // Persist creds BEFORE the PAC goes live so a proxy 407 ALWAYS has creds to answer with —
  // even if this ephemeral MV3 worker is later torn down (in-memory proxyCreds lost) and then
  // resurrected by a proxied request. onAuthRequired rehydrates from here, so Chrome's native
  // "sign in to the proxy" dialog can never appear after an extension update / worker restart.
  try { await chrome.storage.local.set({ proxyCreds }); } catch (_) {}
  const P = pacProxyToken(p);
  const pac = [
    "function FindProxyForURL(url, host) {",
    "  var P = '" + P + "';",
    "  if (dnsDomainIs(host, 'labs.google')) return P;",                 // the Flow site + its session
    "  if (dnsDomainIs(host, 'flow.google.com')) return P;",             // Flow's new home (2026-09)
    "  if (shExpMatch(url, '*://www.google.com/recaptcha/*')) return P;", // reCAPTCHA mint
    "  if (shExpMatch(url, '*://www.gstatic.com/recaptcha/*')) return P;",// reCAPTCHA assets
    "  if (dnsDomainIs(host, 'recaptcha.net')) return P;",               // reCAPTCHA fallback domain
    "  return 'DIRECT';",                                                // everything else untouched
    "}"
  ].join("\n");
  try {
    await chrome.proxy.settings.set({ value: { mode: "pac_script", pacScript: { data: pac } }, scope: "regular" });
    await log("SUCCESS", "Per-profile proxy applied (Flow + reCAPTCHA only)", { host: p.host, port: p.port });
  } catch (e) {
    await log("ERROR", "Failed to apply proxy", { error: e.message });
  }
}

async function clearProxy() {
  proxyCreds = null;
  answeredAuthReqs.clear();
  try { await chrome.storage.local.remove("proxyCreds"); } catch (_) {}   // no persisted creds while direct
  try { await chrome.proxy.settings.clear({ scope: "regular" }); } catch (_) {}
  await log("INFO", "Per-profile proxy cleared (direct egress)");
}

// MV3 proxy-auth: supply Oxylabs creds ASYNC, ONLY for proxy (407) challenges —
// never for origin (Google) 401s, so the Google session login is untouched.
// requestId de-dup avoids an infinite 407 loop when creds are wrong.
chrome.webRequest.onAuthRequired.addListener(
  async (details, asyncCallback) => {
    if (!details.isProxy) { asyncCallback({}); return; }
    // The MV3 worker is ephemeral: it can be torn down (extension update, idle) while the PAC
    // proxy setting persists at profile scope. When a proxied request RESURRECTS the worker,
    // in-memory proxyCreds starts null and applyProxy's network fetch hasn't repopulated it
    // yet — which used to fall through to asyncCallback({}) and pop Chrome's native proxy
    // sign-in dialog. Rehydrate from storage (persisted by applyProxy) so we ALWAYS answer the
    // 407 ourselves. Empty storage => proxy is genuinely off => let Chrome decide (no hijack).
    let creds = proxyCreds;
    if (!creds || !creds.username) {
      try {
        const s = await chrome.storage.local.get(["proxyCreds"]);
        if (s.proxyCreds && s.proxyCreds.username) { creds = s.proxyCreds; proxyCreds = creds; }
      } catch (_) {}
    }
    if (!creds || !creds.username) { asyncCallback({}); return; }
    if (answeredAuthReqs.has(details.requestId)) {     // already tried -> creds bad, stop the loop
      answeredAuthReqs.delete(details.requestId);
      asyncCallback({ cancel: true });
      return;
    }
    answeredAuthReqs.add(details.requestId);
    asyncCallback({ authCredentials: { username: creds.username, password: creds.password } });
  },
  { urls: ["<all_urls>"] },
  ["asyncBlocking"]        // permitted in MV3 because of "webRequestAuthProvider"
);
const _clearAuthReq = (d) => answeredAuthReqs.delete(d.requestId);
chrome.webRequest.onCompleted.addListener(_clearAuthReq, { urls: ["<all_urls>"] });
chrome.webRequest.onErrorOccurred.addListener(_clearAuthReq, { urls: ["<all_urls>"] });

/* ------------------------------- logging ----------------------------- */

async function log(level, message, details) {
  const entry = { ts: new Date().toISOString(), level, message, details: details || null };
  console.log(`[Flow2API ${level}] ${message}`, details || "");
  const { logs = [] } = await chrome.storage.local.get(["logs"]);
  logs.unshift(entry);
  if (logs.length > 80) logs.splice(80);
  await chrome.storage.local.set({ logs });
}

/* ------------------------------ user signal -------------------------- */

// The toolbar badge is the reliable, always-visible signal — it survives the user
// being away (lid closed) and needs no icon asset. Two states share it, by priority:
//   login_required (red "!")  — urgent, must re-sign-in; ALWAYS outranks an update.
//   update_available (blue ↑) — a newer ext version is published; shown when login is ok
//                               so staff notice WITHOUT opening the popup.
async function setBadge(state) {
  try {
    if (!chrome.action) return;
    if (state === "login_required") {
      chrome.action.setBadgeText({ text: "!" });
      chrome.action.setBadgeBackgroundColor({ color: "#f06262" });
      chrome.action.setTitle({ title: "Flow2API Worker — Google Labs login required" });
      return;
    }
    // Grant expired: the cookie is still there (so login_required never fires) but Google
    // stopped renewing this account's access token. Only a human sign-out/sign-in fixes it.
    // Separate persisted state — NOT login_required, which is cleared by mere cookie presence.
    if (await isGrantExpired()) {
      chrome.action.setBadgeText({ text: "!" });
      chrome.action.setBadgeBackgroundColor({ color: "#f06262" });
      chrome.action.setTitle({ title: "Flow2API Worker — sign out of Google Labs and sign back in, then Reconnect" });
      return;
    }
    // Login is fine — surface an available update on the icon (blue ↑) so it can't be missed.
    const { updateInfo } = await chrome.storage.local.get(["updateInfo"]);
    if (updateInfo && updateInfo.updateAvailable) {
      chrome.action.setBadgeText({ text: "↑" });
      chrome.action.setBadgeBackgroundColor({ color: "#3b6cf6" });
      chrome.action.setTitle({ title: `Flow2API Worker — update available (v${updateInfo.latest || ""}). Open me to update.` });
      return;
    }
    chrome.action.setBadgeText({ text: "" });
    chrome.action.setTitle({ title: "Flow2API Worker" });
  } catch (_) {}
}

// Recompute the badge from the CURRENT auth + update state (login always wins). Call after
// either changes so the icon stays truthful without every caller knowing both signals.
async function refreshBadge() {
  setBadge((await getAuthState()).state);
}

async function notifyLogin() {
  // Best-effort desktop notification; the badge is the guaranteed signal.
  try {
    await chrome.notifications.create("flow2api_login_" + Date.now(), {
      type: "basic",
      iconUrl: "data:image/svg+xml;base64," + btoa(
        '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96"><rect width="96" height="96" rx="18" fill="#f06262"/><text x="48" y="68" font-size="64" text-anchor="middle" fill="#fff" font-family="sans-serif">!</text></svg>'
      ),
      title: "Flow2API Worker — login needed",
      message: "Your Google Labs session expired. Open Google Labs and sign in again to resume reCAPTCHA minting.",
      priority: 2
    });
  } catch (_) {}
}

// One desktop notification when a NEW version is first detected (badge is the persistent
// signal; this is the attention-grab). Clicking it starts the download — see onClicked below.
async function notifyUpdate(latest) {
  try {
    await chrome.notifications.create("flow2api_update_" + latest, {
      type: "basic",
      iconUrl: "data:image/svg+xml;base64," + btoa(
        '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96"><rect width="96" height="96" rx="18" fill="#3b6cf6"/><text x="48" y="70" font-size="60" text-anchor="middle" fill="#fff" font-family="sans-serif">↑</text></svg>'
      ),
      title: "Flow2API Worker — update available",
      message: `Version ${latest} is ready. Click here to download it, then reload the extension (chrome://extensions ↻).`,
      priority: 2
    });
  } catch (_) {}
}

// Click the update notification -> open the download URL so the new zip starts downloading.
chrome.notifications.onClicked.addListener((id) => {
  if (!id || !id.startsWith("flow2api_update_")) return;
  (async () => {
    try {
      const { downloadUrl } = deriveUrls(await getSettings());
      await chrome.tabs.create({ url: downloadUrl });
    } catch (_) {}
  })();
});

/* ------------------------------ auth state --------------------------- */

async function getAuthState() {
  const { authState } = await chrome.storage.local.get(["authState"]);
  return authState || { state: "ok", failCount: 0, nextRetryAt: 0 };
}

// Enter the "login required" circuit-breaker: stop opening tabs, back off
// (growing interval), and raise the badge so the user knows to re-login.
async function setLoginRequired(reason) {
  const cur = await getAuthState();
  const transition = cur.state !== "login_required";
  const failCount = (cur.state === "login_required" ? cur.failCount : 0) + 1;
  const backoff = AUTH_BACKOFF_MS[Math.min(failCount - 1, AUTH_BACKOFF_MS.length - 1)];
  await chrome.storage.local.set({
    authState: { state: "login_required", failCount, nextRetryAt: Date.now() + backoff, reason: reason || "" }
  });
  setBadge("login_required");
  await log("ERROR", "Google Labs login required — pausing tab creation", { reason, backoffMs: backoff });
  if (transition) notifyLogin(reason);
}

async function clearLoginRequired() {
  const cur = await getAuthState();
  if (cur.state !== "ok") {
    await chrome.storage.local.set({ authState: { state: "ok", failCount: 0, nextRetryAt: 0 } });
    setBadge("ok");
    await log("SUCCESS", "Google Labs session restored");
  }
}

/* ------------------------- grant-expired state ------------------------ */
// Set ONLY when the server answers a session push with action=relogin_required (it
// verified the access token inside our cookie against Google's API and it is dead).
// Cleared ONLY by a push the server confirms as credential_verified — never by the cookie
// merely being present, which is exactly the trap that kept accounts flapping for weeks.
async function isGrantExpired() {
  try { const { grantExpired } = await chrome.storage.local.get(["grantExpired"]); return !!(grantExpired && grantExpired.state === "grant_expired"); }
  catch (_) { return false; }
}

async function setGrantExpired(message) {
  const was = await isGrantExpired();
  await chrome.storage.local.set({ grantExpired: { state: "grant_expired", since: Date.now(), message: message || "" } });
  setBadge("ok"); // falls through to the grant-expired branch (login_required still outranks it)
  await log("ERROR", "Google stopped renewing this account's access token — sign out of Google Labs, sign back in, then click Reconnect", { message: (message || "").slice(0, 160) });
  if (!was) notifyGrantExpired();
}

async function clearGrantExpired() {
  if (await isGrantExpired()) {
    await chrome.storage.local.remove("grantExpired");
    setBadge("ok");
    await log("SUCCESS", "Google access token verified — account healthy again");
  }
}

async function notifyGrantExpired() {
  try {
    await chrome.notifications.create("flow2api_grant_" + Date.now(), {
      type: "basic",
      iconUrl: "data:image/svg+xml;base64," + btoa(
        '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96"><rect width="96" height="96" rx="18" fill="#f06262"/><text x="48" y="68" font-size="64" text-anchor="middle" fill="#fff" font-family="sans-serif">!</text></svg>'
      ),
      title: "Flow2API Worker — please sign in again",
      message: "Google stopped renewing this account's access token. Sign OUT of Google Labs, sign back IN, then click Reconnect in the extension.",
      priority: 2
    });
  } catch (_) {}
}

/* --------------------------- tab utilities --------------------------- */

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

// Space out reCAPTCHA mints so a single browser stays under Google's rate limit
// (avoids PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC under burst load).
async function paceMint(intervalMs) {
  if (!intervalMs) return;
  const wait = lastMintAt + intervalMs - Date.now();
  if (wait > 0) await sleep(wait);
  lastMintAt = Date.now();
}

function getTab(tabId) {
  return new Promise((resolve) => {
    if (tabId == null) { resolve(null); return; }
    chrome.tabs.get(tabId, (tab) => resolve(chrome.runtime.lastError ? null : (tab || null)));
  });
}

function tabUrlOf(tab) { return (tab && (tab.url || tab.pendingUrl)) || ""; }
function isFlowUrl(u) { return !!u && FLOW_ORIGINS.some((o) => u.startsWith(o)); }
function isMintUrl(u) { return !!u && MINT_ORIGINS.some((o) => u.startsWith(o)); }

// A tab counts as "on Flow" if EITHER its committed url OR its pending (loading)
// url is the Flow URL — checked independently so an about:blank-then-Flow tab
// (which reports url:"about:blank", pendingUrl:Flow) is not missed.
function tabOnFlow(tab) {
  return !!tab && (isFlowUrl(tab.url || "") || isFlowUrl(tab.pendingUrl || ""));
}

// A tab is "usable" (ready to mint) only when navigation has COMMITTED to a
// labs.google page — a flow.google.com tab is on Flow but cannot mint, so it is
// never reused; the caller drops it and opens MINT_URL instead.
function tabUsable(tab) { return !!tab && isMintUrl(tab.url || ""); }

function isLoginTab(tab) {
  const u = tabUrlOf(tab);
  try { const h = new URL(u).hostname; return LOGIN_HOSTS.some((x) => h === x || h.endsWith("." + x)); }
  catch (_) { return false; }
}

function waitForTabComplete(tabId, timeoutMs = 15000) {
  return new Promise((resolve) => {
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      chrome.tabs.onUpdated.removeListener(onUpdated);
      clearTimeout(timer);
      resolve();
    };
    const onUpdated = (id, info) => { if (id === tabId && info.status === "complete") finish(); };
    const timer = setTimeout(finish, timeoutMs);
    chrome.tabs.onUpdated.addListener(onUpdated);
    chrome.tabs.get(tabId, (tab) => {
      if (chrome.runtime.lastError) { finish(); return; }
      if (tab && tab.status === "complete") finish();
    });
  });
}

// The NextAuth session cookie, from whichever Flow domain holds it (labs.google
// first, then flow.google.com). Returns the cookie object or null.
async function readSessionCookie() {
  for (const domain of COOKIE_DOMAINS) {
    let c = await chrome.cookies.get({ url: "https://" + domain, name: SESSION_COOKIE });
    if (!c) {
      const all = await chrome.cookies.getAll({ domain });
      c = all.find((x) => x.name === SESSION_COOKIE) || null;
    }
    if (c && c.value) return c;
  }
  return null;
}

async function hasSessionCookie() {
  try {
    const c = await readSessionCookie();
    return !!(c && c.value);
  } catch (_) {
    return true; // don't block on cookie API errors
  }
}

/* --------------------------- owned-tab registry ---------------------- */
// We only ever CLOSE tabs we opened ourselves (tracked here), never the user's
// own Labs tabs. All mutations are serialized to avoid lost-update races with
// the onRemoved listener.

function mutateOwned(fn) {
  ownedQueue = ownedQueue.then(async () => {
    const { ownedTabIds = [] } = await chrome.storage.local.get(["ownedTabIds"]);
    const next = fn(ownedTabIds.slice());
    await chrome.storage.local.set({ ownedTabIds: next });
    return next;
  }).catch(() => []);
  return ownedQueue;
}
async function getOwned() {
  const { ownedTabIds = [] } = await chrome.storage.local.get(["ownedTabIds"]);
  return ownedTabIds;
}
function addOwned(id) { return mutateOwned((a) => (a.includes(id) ? a : a.concat(id))); }
function removeOwned(id) { return mutateOwned((a) => a.filter((x) => x !== id)); }

// Live owned tabs that are on Flow (committed or loading).
async function queryOwnedFlowTabs() {
  const owned = await getOwned();
  const out = [];
  for (const id of owned) {
    const tab = await getTab(id);
    if (tab && tabOnFlow(tab)) out.push({ id, tab });
  }
  return out;
}

// Every live owned tab (regardless of URL — counts login/loading tabs too), used
// by the absolute create ceiling so nothing slips outside the fuse.
async function queryOwnedLiveTabs() {
  const owned = await getOwned();
  const out = [];
  for (const id of owned) {
    const tab = await getTab(id);
    if (tab) out.push({ id, tab });
  }
  return out;
}

// Close every owned tab and clear all persistent state (used by ephemeral-mode boot).
async function closeAllOwnedTabs() {
  for (const id of await getOwned()) { try { await chrome.tabs.remove(id); } catch (_) {} }
  await mutateOwned(() => []);
  persistentTabId = null;
  await chrome.storage.local.remove("persistentTabId");
}

// Close every OWNED Labs tab except one to keep. Prunes dead/closed ids and never
// keeps a tab sitting on a login page. Returns the kept id (or null). This is the
// hard global cap: even if every other guard failed, at most one owned tab survives.
async function sweepOwnedTabs(keepId = null) {
  const owned = await getOwned();
  const live = [];
  for (const id of owned) {
    const tab = await getTab(id);
    if (tab) live.push({ id, tab });
  }
  if (live.length === 0) { await mutateOwned(() => []); return null; } // prune dead ids; keep persistentTabId untouched

  // Choose which to keep: explicit keepId, else a committed-Flow tab, else first.
  let keep = keepId != null ? live.find((x) => x.id === keepId) : null;
  if (!keep) keep = live.find((x) => tabUsable(x.tab)) || null;
  if (!keep) keep = live[0];
  // Never keep a login-redirected tab around.
  if (keep && isLoginTab(keep.tab)) keep = null;

  let closed = 0;
  const survivors = [];
  const closedUrls = [];
  for (const x of live) {
    if (keep && x.id === keep.id) { survivors.push(x.id); continue; }
    // Window-safe: a tab that is the last in its window is left open and merely
    // deregistered (removeOwnedTabSafely logs it) — closing it closes the window.
    if (await removeOwnedTabSafely(x.id, "extra owned tab")) { closed++; closedUrls.push(shortUrl(x.tab.url)); }
  }
  await mutateOwned((cur) => cur.filter((id) => survivors.includes(id)));

  if (keep) {
    persistentTabId = keep.id;
    await chrome.storage.local.set({ persistentTabId: keep.id });
  } else {
    persistentTabId = null;
    await chrome.storage.local.remove("persistentTabId");
  }
  if (closed > 0) await log("INFO", "Swept extra owned Labs tabs", { kept: keep ? keep.id : null, keptUrl: keep ? shortUrl(keep.tab.url) : null, closed, closedUrls });
  return keep ? keep.id : null;
}

// Find a live, COMMITTED-to-Flow tab we can mint in right now (or null). Only
// considers tabs WE own (the durable owned-list) — never adopts a tab by URL, so
// a user's own Labs tab can never be claimed and later closed by the sweep.
async function findUsableLabsTab() {
  for (const id of await getOwned()) {
    const tab = await getTab(id);
    if (tabUsable(tab)) return id;
  }
  return null;
}

/* ------------------------ window-safe tab handling ------------------- */
//
// 2026-09-04: the worker tab is usually the ONLY tab in its Chrome window (staff run
// it in a dedicated profile). Chrome closes a window when its last tab closes, and a
// profile with no window left makes the next chrome.tabs.create fail with
// "No current window" (seen 2026-09-03 21:39:40). Every path below that used to
// remove-then-recreate did exactly that: the window vanished, the replacement could
// not be opened, and the account went dark until a human clicked the profile.
// Rules now: (1) create the replacement FIRST, in the same window, then remove the
// old tab; (2) never close the last tab of a window, deregister it instead;
// (3) a tab that drifted off labs.google/fx is navigated back, not killed.

function shortUrl(u) { u = String(u || ""); return u.length > 90 ? u.slice(0, 90) + "…" : u; }

// Compact picture of every owned tab, for the log line that explains a replacement.
async function snapshotOwnedTabs() {
  const out = [];
  for (const id of await getOwned()) {
    const t = await getTab(id);
    out.push(t
      ? { id, url: shortUrl(t.url), pending: t.pendingUrl ? shortUrl(t.pendingUrl) : undefined, status: t.status, discarded: !!t.discarded, win: t.windowId }
      : { id, gone: true });
  }
  return out;
}

async function isLastTabInWindow(tab) {
  if (!tab || tab.windowId == null) return false;
  try { const tabs = await chrome.tabs.query({ windowId: tab.windowId }); return tabs.length <= 1; }
  catch (_) { return false; }
}

// Remove an owned tab without ever closing its window. Returns true if the tab was
// actually closed; false if it was left open (last in window) or already gone. In
// both false cases it is deregistered so the extension stops managing it.
async function removeOwnedTabSafely(tabId, why) {
  const tab = await getTab(tabId);
  if (tab && await isLastTabInWindow(tab)) {
    await log("INFO", "Left tab open: it is the last tab in its window (closing it would close the window)", { tabId, url: shortUrl(tab.url), why: why || null });
    await removeOwned(tabId);
    return false;
  }
  let closed = false;
  try { await chrome.tabs.remove(tabId); closed = true; } catch (_) {}
  await removeOwned(tabId);
  return closed;
}

// chrome.tabs.create that survives the two ways it fails in a worker profile: the
// preferred window is gone (retry without it) and the profile has NO window at all
// ("No current window") -> open one, unfocused, so the account self-heals instead
// of waiting for a human to click the profile.
async function createTabSafely(url, preferWindowId) {
  const noWindow = (e) => /no current window/i.test(String(e && e.message || e));
  if (preferWindowId != null) {
    try { return await chrome.tabs.create({ url, active: false, windowId: preferWindowId }); }
    catch (e) { if (noWindow(e)) { /* fall through to windows.create */ } else { /* window gone: retry below */ } }
  }
  try {
    return await chrome.tabs.create({ url, active: false });
  } catch (e) {
    if (!noWindow(e)) throw e;
    const win = await chrome.windows.create({ url, focused: false, type: "normal" });
    const tab = win && win.tabs && win.tabs[0];
    if (!tab) throw new Error("no Chrome window was open and creating one failed");
    await log("WARN", "No Chrome window was open for this profile; opened one for the Labs tab", { windowId: win.id, tabId: tab.id });
    return tab;
  }
}

/* --------------------------- tab creation ---------------------------- */

// Open ONE hidden Labs tab, wait for it to settle, and verify it actually reached
// Flow (not a login/consent redirect). Records ownership + a durable creation
// lease BEFORE the long load so a service-worker crash mid-load can't orphan it
// or let a respawned worker create a second tab. Throws on login redirect (and
// trips the circuit breaker). Used for BOTH persistent and ephemeral modes; it
// does NOT itself assign persistentTabId (callers own that policy).
// opts.windowId: open the tab in that window (the one the tab it replaces lives in),
// so a later removal of the old tab can never close the window the user is watching.
async function openLabsTab(opts = {}) {
  // Absolute create ceiling: enforced at the single creation chokepoint, so NO
  // caller (persistent, ephemeral, warm, retry) can push owned tabs past the cap.
  const liveOwned = await queryOwnedLiveTabs();
  if (liveOwned.length >= MAX_OWNED_TABS) await sweepOwnedTabs();

  await chrome.storage.local.set({ creationLease: { state: "creating", expiresAt: Date.now() + LEASE_MS } });
  let tab;
  try {
    tab = await createTabSafely(MINT_URL, opts.windowId);
  } catch (e) {
    await chrome.storage.local.remove("creationLease");
    throw e;
  }
  await addOwned(tab.id);
  await chrome.storage.local.set({ creationLease: { state: "creating", tabId: tab.id, expiresAt: Date.now() + LEASE_MS } });

  try {
    await waitForTabComplete(tab.id);
    const settled = await getTab(tab.id);
    if (!tabOnFlow(settled)) {
      // Redirected away from Flow (login/consent) or vanished — don't keep it,
      // and don't let it be recreated forever. (Left open, unowned, if it is the
      // last tab in its window: a login page the user can actually sign in on.)
      await removeOwnedTabSafely(tab.id, "did not reach Flow");
      if (isLoginTab(settled)) await setLoginRequired("Labs redirected to " + tabUrlOf(settled));
      throw new Error("labs tab did not reach Flow URL (" + (tabUrlOf(settled) || "gone") + ")");
    }
    if (!tabUsable(settled)) {
      // On Flow, but on a page with no reCAPTCHA (flow.google.com). Don't keep it:
      // every mint there fails, and the retry would just reuse it.
      await removeOwnedTabSafely(tab.id, "landed on a page without reCAPTCHA");
      throw new Error("labs tab was redirected to " + tabUrlOf(settled) + " where reCAPTCHA cannot be minted");
    }
    await sleep(1200); // let grecaptcha settle
    return tab.id;
  } finally {
    await chrome.storage.local.remove("creationLease");
  }
}

// Ensure exactly ONE persistent Labs tab exists; returns its id. Serialized via
// ensureChain (one at a time within this SW life) and guarded across SW respawns
// by the storage lease + owned-tab ceiling, so concurrent callers (warm-up,
// keepalive, session refresh, token backlog) can never spawn a tab storm.
function ensurePersistentTab() {
  ensureChain = ensureChain.then(_ensurePersistentTab, _ensurePersistentTab);
  return ensureChain;
}

async function _ensurePersistentTab() {
  // 0) Circuit breaker: while login is required and backoff is active, refuse —
  //    but first recover immediately if the user has logged back in (cookie back),
  //    so a token request right after re-login isn't needlessly rejected.
  const auth = await getAuthState();
  if (auth.state === "login_required" && Date.now() < (auth.nextRetryAt || 0)) {
    if (await hasSessionCookie()) { await clearLoginRequired(); }
    else throw new Error("login_required");
  }

  // 1) Already have a live, usable tab -> adopt + collapse any extras to one.
  const usable = await findUsableLabsTab();
  if (usable != null) {
    // Chrome's memory saver can discard a background tab: url is kept (so it still
    // looks usable) but the page is gone and a mint in it fails. Reload it in place
    // rather than letting the retry throw the tab away.
    const t = await getTab(usable);
    if (t && t.discarded) {
      await log("INFO", "Labs tab was discarded by Chrome (memory saver); reloading it in place", { tabId: usable });
      try { await chrome.tabs.reload(usable, { bypassCache: false }); await waitForTabComplete(usable); await sleep(1200); } catch (_) {}
    }
    persistentTabId = usable;
    await chrome.storage.local.set({ persistentTabId: usable });
    await sweepOwnedTabs(usable);
    return persistentTabId;
  }

  // Nothing usable. Record WHY before creating, so a replacement is never a mystery
  // in the log again (2026-09-04: a tab was swapped 19 s after being kept, and
  // nothing said what the old tab's URL was).
  const before = await snapshotOwnedTabs();
  const liveBefore = before.filter((x) => !x.gone);
  const preferWindowId = liveBefore.length ? liveBefore[0].win : undefined;

  // 2) Honor an in-flight creation lease (possibly from a prior SW life): wait a
  //    beat and re-check instead of starting a second creation.
  const { creationLease } = await chrome.storage.local.get(["creationLease"]);
  if (creationLease && creationLease.expiresAt > Date.now()) {
    await sleep(1500);
    const again = await findUsableLabsTab();
    if (again != null) { persistentTabId = again; await sweepOwnedTabs(again); return persistentTabId; }
  }

  // 3) Hard ceiling fuse: never exceed MAX_OWNED_TABS owned Labs tabs.
  const ownedFlow = await queryOwnedFlowTabs();
  if (ownedFlow.length >= MAX_OWNED_TABS) {
    const kept = await sweepOwnedTabs();
    if (kept != null) {
      const t = await getTab(kept);
      if (tabUsable(t)) { persistentTabId = kept; return persistentTabId; }
    }
  }

  // 4) Auth gate: don't open a tab that will just bounce to login.
  if (!(await hasSessionCookie())) {
    await setLoginRequired("session cookie missing");
    throw new Error("login_required");
  }

  // 5) Create exactly one — in the same window as whatever we had, so the sweep
  //    that follows removes a tab from a window that still has ours in it.
  const id = await openLabsTab({ windowId: preferWindowId });
  await clearLoginRequired();
  persistentTabId = id;
  await chrome.storage.local.set({ persistentTabId: id });
  await sweepOwnedTabs(id); // close anything that snuck in during the load window
  await log("INFO", "Persistent Labs tab opened", {
    tabId: persistentTabId,
    why: liveBefore.length ? "owned tab(s) were not on labs.google/fx" : (before.length ? "owned tab(s) were gone" : "no owned tab"),
    previous: before,
  });
  return persistentTabId;
}

// Swap the persistent tab for a fresh one because a mint failed IN it. Order matters:
// the new tab is created first, in the SAME window, and only then is the old one
// removed — so the window survives even when the old tab was its last tab (the
// 2026-09-03 "No current window" failure). Serialized on ensureChain like ensure.
function replacePersistentTab(why) {
  const run = () => _replacePersistentTab(why);
  ensureChain = ensureChain.then(run, run);
  return ensureChain;
}

async function _replacePersistentTab(why) {
  const cur = await findUsableLabsTab();
  if (cur == null) return _ensurePersistentTab(); // nothing to replace: normal path (logs its own reason)
  const curTab = await getTab(cur);
  const fresh = await openLabsTab({ windowId: curTab ? curTab.windowId : undefined });
  await clearLoginRequired();
  persistentTabId = fresh;
  await chrome.storage.local.set({ persistentTabId: fresh });
  await removeOwnedTabSafely(cur, why);
  await sweepOwnedTabs(fresh);
  await log("WARN", "Replaced the Labs tab", {
    why,
    old: curTab ? { id: cur, url: shortUrl(curTab.url), status: curTab.status, discarded: !!curTab.discarded, win: curTab.windowId } : { id: cur, gone: true },
    new: fresh,
  });
  return fresh;
}

// Warm/keep the persistent tab, respecting the login circuit breaker. Recovers
// promptly: if login was required but the user has since logged back in (cookie
// present again), clear the breaker and proceed.
async function maybeEnsurePersistentTab() {
  const settings = await getSettings();
  if (settings.tabMode !== "persistent") return;
  const auth = await getAuthState();
  if (auth.state === "login_required") {
    if (await hasSessionCookie()) {
      await clearLoginRequired();
    } else if (Date.now() < (auth.nextRetryAt || 0)) {
      return; // still backing off
    }
  }
  ensurePersistentTab().catch(() => {});
}

/* ------------------------------- minting ----------------------------- */

// Run grecaptcha.enterprise.execute in the given tab's MAIN world.
//
// The injected function NEVER rejects. A rejected promise does not survive the
// executeScript boundary — Chrome hands back `result: undefined` and the reason is
// gone — so every failure used to surface as the useless "empty token result" and
// Google's actual verdict was lost (2026-09-03: hours spent guessing why minting
// broke after the flow.google.com move). Resolve a verdict object instead, then
// re-throw it here with the page context that explains it.
async function mintTokenInTab(tabId, action, timeoutMs) {
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    func: (siteKey, action, timeoutMs) => new Promise((resolve) => {
      let settled = false;
      const ctx = () => ({ url: location.href, hadGrecaptcha: typeof grecaptcha !== "undefined" });
      const done = (v) => { if (!settled) { settled = true; resolve(v); } };
      const ok = (t) => done(Object.assign({ ok: true, token: t }, ctx()));
      const fail = (err) => done(Object.assign({ ok: false, err: String(err || "unknown") }, ctx()));
      try {
        const run = () => {
          grecaptcha.enterprise.ready(() => {
            grecaptcha.enterprise.execute(siteKey, { action })
              .then((t) => (t ? ok(t) : fail("recaptcha returned an empty token")))
              .catch((e) => fail(e && e.message ? e.message : "recaptcha execute failed"));
          });
        };
        const inject = () => {
          const s = document.createElement("script");
          s.src = "https://www.google.com/recaptcha/enterprise.js?render=" + siteKey;
          s.onload = run;
          s.onerror = () => fail("failed to load enterprise.js");
          document.head.appendChild(s);
        };
        // The page loads grecaptcha itself (labs.google/fx does so within ~250 ms of
        // load). Give it up to 8 s before falling back to injecting the script — on a
        // Trusted-Types page (flow.google.com) injection is blocked and fails loudly.
        const started = Date.now();
        const waitForPage = () => {
          if (settled) return;
          if (typeof grecaptcha !== "undefined" && grecaptcha.enterprise) { run(); return; }
          if (Date.now() - started > 8000) { inject(); return; }
          setTimeout(waitForPage, 200);
        };
        waitForPage();
        setTimeout(() => fail("timeout minting recaptcha token"), timeoutMs);
      } catch (e) {
        fail(e && e.message ? e.message : e);
      }
    }),
    args: [RECAPTCHA_SITE_KEY, action, timeoutMs]
  });
  const r = results && results[0] ? results[0].result : null;
  if (r && r.ok && r.token) return r.token;
  // No verdict at all means the injection itself failed (tab navigated away, no
  // host permission) — say that rather than blaming reCAPTCHA.
  if (!r) throw new Error("mint injection returned nothing (tab gone or not injectable)");
  throw new Error(`${r.err} [url=${r.url}, grecaptcha=${r.hadGrecaptcha ? "present" : "absent"}]`);
}

async function handleGetToken(data, settings, responseSocket = ws) {
  // Mark the persistent tab as in-use so proactive reload / session-refresh
  // fallback never reload it out from under an in-flight mint.
  mintInFlight = true;
  try {
    return await _handleGetToken(data, settings, responseSocket);
  } finally {
    mintInFlight = false;
  }
}

async function _handleGetToken(data, settings, responseSocket = ws) {
  const action = data.action || "IMAGE_GENERATION";
  // Video mints take longer (server waits 75 s for VIDEO_GENERATION).
  const timeoutMs = action === "VIDEO_GENERATION" ? 60000 : 20000;
  let lastMintError = null; // attempt 1's failure, carried into the replacement log line

  // Try up to twice: a stale persistent tab is recreated on the second attempt.
  for (let attempt = 1; attempt <= 2; attempt++) {
    let ephemeralTabId = null;
    try {
      let tabId;
      if (settings.tabMode === "ephemeral") {
        ephemeralTabId = await openLabsTab();
        tabId = ephemeralTabId;
      } else {
        if (attempt === 2) {
          // First mint failed. If the current tab isn't a usable Flow tab, the
          // breaker/auth gate will handle it; if it IS Flow but still failing,
          // swap it for a fresh one (no sticky bad tab). Create-then-remove, same
          // window — never the old remove-then-create that closed the window.
          tabId = await replacePersistentTab("mint attempt 1 failed: " + String(lastMintError || "").slice(0, 160));
        } else {
          tabId = await ensurePersistentTab();
        }
      }

      await paceMint(settings.mintIntervalMs);
      const token = await mintTokenInTab(tabId, action, timeoutMs);
      sendWS({ req_id: data.req_id, status: "success", token }, responseSocket);
      return;
    } catch (e) {
      const msg = String(e && e.message || e);
      lastMintError = msg;
      await log("ERROR", `token attempt ${attempt} failed`, { error: msg });
      if (msg === "login_required") {
        // No point retrying — surface immediately so the backend can route elsewhere.
        sendWS({ req_id: data.req_id, status: "error", error: "worker login required (re-login to Google Labs)" }, responseSocket);
        return;
      }
      if (attempt === 2) {
        sendWS({ req_id: data.req_id, status: "error", error: "worker failed: " + msg }, responseSocket);
      }
    } finally {
      if (ephemeralTabId != null) {
        try { await chrome.tabs.remove(ephemeralTabId); } catch (_) {}
        await removeOwned(ephemeralTabId);
      }
    }
  }
}

/* ------------------------------ WebSocket ---------------------------- */

// Send on the preferred socket (the one a request arrived on) if it's open,
// else fall back to the current global socket. Replying on a reconnected socket
// is now safe because the server matches responses by req_id, not by connection.
function sendWS(obj, preferredSocket = ws) {
  const payload = JSON.stringify(obj);
  const sockets = [];
  if (preferredSocket) sockets.push(preferredSocket);
  if (ws && ws !== preferredSocket) sockets.push(ws);
  for (const socket of sockets) {
    if (socket && socket.readyState === WebSocket.OPEN) {
      try { socket.send(payload); return true; } catch (_) {}
    }
  }
  return false;
}

async function connectWS() {
  if (connecting) return;
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  connecting = true;

  const settings = await getSettings();
  if (!settings.serverBase || !settings.apiKey) {
    connecting = false;
    await log("INFO", "Not configured yet (need Server URL + API Key)");
    return;
  }

  let wsUrl;
  try {
    wsUrl = deriveUrls(settings).wsUrl;
  } catch (e) {
    connecting = false;
    await log("ERROR", "Invalid Server URL", { error: e.message });
    return;
  }

  const url = new URL(wsUrl);
  url.searchParams.set("key", settings.apiKey);
  if (settings.routeKey) url.searchParams.set("route_key", settings.routeKey);
  if (settings.clientLabel) url.searchParams.set("client_label", settings.clientLabel);

  let socket;
  try {
    socket = new WebSocket(url.toString());
    ws = socket;
  } catch (e) {
    connecting = false;
    scheduleReconnect();
    return;
  }

  // Bind handlers to THIS socket (not the global `ws`) so a superseded
  // connection from a reconnect can't clobber the live one's state.
  socket.onopen = () => {
    if (ws !== socket) { try { socket.close(); } catch (_) {} return; }
    connecting = false;
    log("SUCCESS", "Captcha WebSocket connected", { routeKey: settings.routeKey || "(empty)" });
    sendWS({ type: "register", route_key: settings.routeKey, client_label: settings.clientLabel, pool_mode: settings.failedImageMode ? "failed_image" : "auto", ext_version: extVersion() }, socket);
    if (heartbeatInterval) clearInterval(heartbeatInterval);
    heartbeatInterval = setInterval(() => sendWS({ type: "ping" }, socket), HEARTBEAT_MS);
    // Warm the persistent tab so the first real request is fast (login-aware).
    maybeEnsurePersistentTab();
  };

  socket.onmessage = (event) => {
    let data;
    try { data = JSON.parse(event.data); } catch (_) { return; }
    if (data.type === "register_ack") return;
    if (data.type === "get_token") {
      // Reply on the exact socket the request arrived on (falls back to current).
      tokenQueue = tokenQueue.then(() => handleGetToken(data, settings, socket)).catch(() => {});
    }
    if (data.type === "refresh_session") {
      if (data.reload) {
        // Forced reload navigates the Labs tab → must be serialized behind mints
        // (tokenQueue) exactly like the periodic roll; the server waits longer (45s) for it.
        tokenQueue = tokenQueue.then(() => handleRefreshSession(data, socket)).catch(() => {});
      } else {
        // Direct call (NOT tokenQueue): refreshSession's common path is tab-free
        // (cookie read + POST), so it can't conflict with an in-flight mint, and the
        // only tab-reloading path (empty-cookie fallback) is already mint-busy-guarded.
        // Queueing behind a long (video) mint would blow the backend's 30s wait.
        handleRefreshSession(data, socket);
      }
    }
  };

  socket.onclose = () => {
    if (ws !== socket) return; // a superseded socket closed — ignore
    connecting = false;
    if (heartbeatInterval) clearInterval(heartbeatInterval);
    ws = null;
    scheduleReconnect();
  };

  socket.onerror = () => { try { socket.close(); } catch (_) {} };
}

function scheduleReconnect(delayMs = 2000) {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connectWS, delayMs);
}

function closeSocket() {
  if (heartbeatInterval) clearInterval(heartbeatInterval);
  if (reconnectTimer) clearTimeout(reconnectTimer);
  if (ws) { try { ws.close(); } catch (_) {} ws = null; }
}

/* --------------------------- session refresh ------------------------- */

// Drop an owned tab (window-safe remove + deregister + clear persistent pointer).
async function dropOwnedTab(tabId, why) {
  await removeOwnedTabSafely(tabId, why);
  if (tabId === persistentTabId) {
    persistentTabId = null;
    await chrome.storage.local.remove("persistentTabId");
  }
}

// Bring an owned tab that drifted off labs.google/fx back there, in place. Returns
// true if it is mintable afterwards. Used instead of killing the tab, because
// killing it is what closed the user's window.
async function steerTabBackToMintPage(tabId) {
  try {
    await chrome.tabs.update(tabId, { url: MINT_URL });
    await waitForTabComplete(tabId);
  } catch (_) { return false; }
  const t = await getTab(tabId);
  if (!tabUsable(t)) return false;
  await sleep(1200); // grecaptcha settle
  return true;
}

// Time-to-expiry (ms) of the session-token cookie. null = no cookie; 0 = present
// but session-scoped (no expirationDate) -> treat as roll-eligible on cadence so
// the proactive feature never silently no-ops. Mirrors hasSessionCookie's read.
async function sessionCookieTimeToExpiry() {
  const c = await readSessionCookie();
  if (!c || !c.value) return null;
  if (!c.expirationDate) return 0; // session cookie, no expiry -> eligible to roll
  return c.expirationDate * 1000 - Date.now();
}

// Force NextAuth to re-issue (roll) the session-token cookie by navigating a Labs
// tab: reload an existing usable owned tab if we have one (no MAX_OWNED_TABS
// pressure), else open one fresh via openLabsTab() (ceiling/lease/login-redirect
// safe). NEVER throws. Returns the live tab id on Flow, or null if we ended up off
// Flow / logged out (the breaker is armed inside on a real login bounce). Does NOT
// read or push the cookie — the caller re-reads after COOKIE_SETTLE_MS.
async function rollSessionTab() {
  let tabId = await findUsableLabsTab();
  if (tabId != null) {
    const beforeTab = await getTab(tabId);
    try {
      await chrome.tabs.reload(tabId, { bypassCache: false });
    } catch (_) {
      await dropOwnedTab(tabId, "vanished mid-reload"); // -> fall through to fresh open
      tabId = null;
    }
    if (tabId != null) {
      await waitForTabComplete(tabId);
      const settled = await getTab(tabId);
      if (isLoginTab(settled)) {
        // Real sign-out. Arm the breaker; leave the login page where the user can
        // see it (dropOwnedTab keeps a last-in-window tab open, unowned).
        await setLoginRequired("reload bounced to " + tabUrlOf(settled));
        await dropOwnedTab(tabId, "login bounce");
        return null;
      }
      if (!tabUsable(settled)) {
        // Off labs.google/fx (or on flow.google.com, which has no reCAPTCHA).
        // 3.3.9 returned this tab as "fine" in the flow.google.com case, so the
        // next mint quietly replaced it. Steer it back in place instead.
        await log("WARN", "Reload landed off the mint page; steering the tab back to labs.google/fx", { url: tabUrlOf(settled) });
        if (await steerTabBackToMintPage(tabId)) return tabId;
        await log("WARN", "Could not steer the tab back; opening a fresh one", { url: tabUrlOf(await getTab(tabId)) });
        await dropOwnedTab(tabId, "stuck off the mint page");
        tabId = null;
      } else {
        await sleep(1200); // grecaptcha settle, mirror openLabsTab
        return tabId;
      }
    }
    // Fresh open goes in the same window the old tab was in.
    try {
      const id = await openLabsTab({ windowId: beforeTab ? beforeTab.windowId : undefined });
      persistentTabId = id;
      await chrome.storage.local.set({ persistentTabId: id });
      await sweepOwnedTabs(id);
      return id;
    } catch (e) {
      await log("WARN", "rollSessionTab fresh open failed", { error: e && e.message });
      return null;
    }
  }
  // No usable tab to reload -> open one fresh. openLabsTab enforces the ceiling,
  // records the lease, and arms login_required + throws on a login redirect.
  try {
    const id = await openLabsTab();
    persistentTabId = id;
    await chrome.storage.local.set({ persistentTabId: id });
    await sweepOwnedTabs(id); // collapse to exactly one
    return id;
  } catch (e) {
    await log("WARN", "rollSessionTab fresh open failed", { error: e && e.message });
    return null; // breaker already armed by openLabsTab if this was a login bounce
  }
}

// Expiry-aware proactive roll, run on ALARM_RELOAD via tokenQueue so it never
// races a mint. Login-aware + rate-capped + yields to active traffic.
async function maybeReloadForRoll() {
  const settings = await getSettings();
  if (settings.tabMode !== "persistent") return;          // ephemeral reopens every mint anyway
  if (mintInFlight) return;                                // a mint is holding the tab right now
  if (Date.now() - lastMintAt < RELOAD_ACTIVE_MS) return; // busy serving -> defer

  // Circuit-breaker gate, mirroring maybeEnsurePersistentTab.
  const auth = await getAuthState();
  if (auth.state === "login_required") {
    if (await hasSessionCookie()) await clearLoginRequired();
    else if (Date.now() < (auth.nextRetryAt || 0)) return;
  }

  if (Date.now() - lastReloadAt < RELOAD_MIN_GAP_MS) return; // hard rate cap

  const ttl = await sessionCookieTimeToExpiry();
  if (ttl === null) return;               // no cookie -> let refreshSession's fallback handle it
  if (ttl > RELOAD_THRESHOLD_MS) return;  // plenty of life left -> common no-op case
  if (ttl === 0) await log("INFO", "Session cookie has no expiry; rolling on cadence");

  lastReloadAt = Date.now();
  const tabId = await rollSessionTab();   // never throws; arms breaker on login bounce
  if (tabId == null) return;              // bounced to login / off-Flow -> handled inside

  await refreshSession();                 // push the freshly rolled cookie
  await log("INFO", "Proactive reload rolled session cookie", { tabId, prevTtlMs: ttl });
}

async function refreshSession(token_id = null, opts = {}) {
  const settings = await getSettings();
  if (!settings.serverBase || !settings.connectionToken) {
    await log("INFO", "Session refresh skipped (need Server URL + Connection Token)");
    return { success: false, error: "not configured", reason: "not_configured" };
  }
  const { updateUrl } = deriveUrls(settings);

  try {
    // Make sure a Labs tab is loaded so the session cookie is fresh/active
    // (login-aware: won't spin up tabs while a re-login is required).
    if (settings.tabMode === "persistent") {
      await maybeEnsurePersistentTab();
    }
    // Forced reload (server found our access token dead): navigate Labs FIRST so
    // NextAuth gets a chance to renew the grant, then push. Never yank the tab from
    // under a mint — the caller (handleRefreshSession) already serializes via tokenQueue,
    // this is the belt to that suspender.
    if (opts.reload && settings.tabMode === "persistent") {
      // Already serialized behind mints via tokenQueue, so only a truly in-flight mint
      // vetoes; the "mint finished seconds ago" veto would make every queued reload
      // return busy (the preceding mint just set lastMintAt).
      if (mintInFlight) {
        return { success: false, error: "busy minting, retry next cycle", reason: "busy" };
      }
      const reloadedTab = await rollSessionTab(); // never throws; arms breaker on login bounce
      if (reloadedTab == null) {
        const auth = await getAuthState();
        if (auth.state === "login_required") return { success: false, error: "login required", reason: "logged_out" };
      } else {
        await sleep(COOKIE_SETTLE_MS);
      }
      await log("INFO", "Forced Labs reload before session push (server reported a dead access token)");
    }
    // Read the session-token cookie directly (no extra tab needed).
    let cookie = await readSessionCookie();
    if (!cookie || !cookie.value) {
      // FALLBACK: the cookie read came back empty even though Google may still be
      // logged in. In persistent mode, force a navigation (reload existing tab, or
      // open a fresh one) so NextAuth re-issues the cookie, then re-read. Only end
      // at login_required if we genuinely bounced to a login page (armed inside
      // rollSessionTab/openLabsTab) — never on a transient empty read.
      if (settings.tabMode === "persistent") {
        // B1: never reload the tab out from under an in-flight / very recent mint.
        if (mintInFlight || (Date.now() - lastMintAt < RELOAD_ACTIVE_MS)) {
          return { success: false, error: "busy minting, retry next cycle", reason: "busy" };
        }
        const tabId = await rollSessionTab(); // never throws; arms breaker on login bounce
        if (tabId == null) {
          const auth = await getAuthState();
          return {
            success: false,
            error: auth.state === "login_required"
              ? "login required (Google Labs logged out)"
              : "reload fallback failed (will retry)",
            reason: auth.state === "login_required" ? "logged_out" : "network",
          };
        }
        await sleep(COOKIE_SETTLE_MS); // let NextAuth write the rolled cookie
        cookie = await readSessionCookie();
        if (!cookie || !cookie.value) {
          // On Flow but still no cookie: unhealthy, not necessarily logged out.
          // Fail soft — next ALARM_SESSION retries; do NOT setLoginRequired here.
          await log("WARN", "Cookie still missing after reload (will retry next cycle)");
          return { success: false, error: "session-token missing after reload (will retry)", reason: "network" };
        }
        // recovered -> fall through to push below
      } else {
        // Ephemeral mode: unchanged behavior.
        await setLoginRequired("session-token cookie not found");
        return { success: false, error: "session-token not found (log into Google Labs)", reason: "logged_out" };
      }
    }

    // Slice B: report the residential proxy this profile mints through + this browser's
    // real User-Agent, so the backend can REDEEM the generate call from the SAME IP + UA
    // (fixes reCAPTCHA "unusual activity" caused by residential-mint vs datacenter-redeem).
    let effProxy = "";
    try { effProxy = await resolveProxyUrl(settings); } catch (_) {}
    const pushBody = { session_token: cookie.value };
    if (token_id != null) pushBody.token_id = token_id;
    if (effProxy) pushBody.proxy_url = effProxy;
    try { if (navigator && navigator.userAgent) pushBody.user_agent = navigator.userAgent; } catch (_) {}
    // Bind this account to THIS device so its captcha minting routes back here (same
    // residential IP as the redeem). Uses the stable per-profile route key.
    if (settings.routeKey) pushBody.route_key = settings.routeKey;
    // Two-pool routing: report this profile's pool from the "Failed-image mode" switch.
    pushBody.pool_mode = settings.failedImageMode ? "failed_image" : "auto";
    // Version visibility: report this build's version so the admin can see who's outdated.
    pushBody.ext_version = extVersion();

    // Bound the push so a hung request can't outlive the server's wait for our ack.
    const pushAbort = new AbortController();
    const pushTimer = setTimeout(() => pushAbort.abort(), PUSH_TIMEOUT_MS);
    let resp;
    try {
      resp = await fetch(updateUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": `Bearer ${settings.connectionToken}` },
        body: JSON.stringify(pushBody),
        signal: pushAbort.signal
      });
    } finally {
      clearTimeout(pushTimer);
    }
    if (!resp.ok) {
      const txt = await resp.text();
      await log("ERROR", "Session push failed", { status: resp.status, body: txt.slice(0, 200) });
      return {
        success: false,
        error: `server ${resp.status}`,
        reason: resp.status === 409 ? "account_mismatch" : resp.status === 400 ? "logged_out" : "network",
      };
    }
    const result = await resp.json();
    // The server VERIFIES the access token inside our cookie against Google's API. A 2xx
    // no longer means "healthy": honor the action before touching any state.
    if (result && result.action === "relogin_required") {
      await setGrantExpired(result.message);
      return { success: false, message: result.message, action: result.action, reason: "relogin_required" };
    }
    await clearLoginRequired(); // a valid cookie pushed -> session is healthy
    if (!result || result.credential_verified !== false) await clearGrantExpired();
    await log("SUCCESS", "Session token pushed to Flow2API", { action: result.action, message: result.message });
    return { success: true, message: result.message, action: result.action, reason: "refreshed" };
  } catch (e) {
    await log("ERROR", "Session refresh error", { error: e.message });
    return { success: false, error: e.message, reason: "network" };
  }
}

// Backend (admin UI) asked THIS specific browser to refresh its session NOW.
// Called directly (not via tokenQueue) — see the onmessage refresh_session branch.
// Replies with an honest status the backend maps to a UI toast; never lies "refreshed".
async function handleRefreshSession(data, responseSocket = ws) {
  let status, msg = null, err = null;
  try {
    const r = await refreshSession(data.token_id, { reload: !!data.reload });
    // reason carries relogin_required / refreshed / busy / logged_out / network verbatim.
    status = r.reason || (r.success ? "refreshed" : "network");
    msg = r.message || null;
    err = r.error || null;
  } catch (e) {
    status = "network";
    err = e && e.message;
  }
  sendWS({ type: "session_refresh_result", req_id: data.req_id, status, message: msg, error: err }, responseSocket);
}

/* ------------------------------- alarms ------------------------------ */

async function setupAlarms() {
  const settings = await getSettings();
  await chrome.alarms.clear(ALARM_SESSION);
  await chrome.alarms.clear(ALARM_KEEPALIVE);
  await chrome.alarms.clear(ALARM_RELOAD);
  chrome.alarms.create(ALARM_SESSION, { periodInMinutes: settings.refreshIntervalMinutes, delayInMinutes: 0.1 });
  chrome.alarms.create(ALARM_KEEPALIVE, { periodInMinutes: 1 }); // revive SW/socket/tab
  chrome.alarms.create(ALARM_RELOAD, { periodInMinutes: 180, delayInMinutes: 5 }); // ~3h proactive cookie roll
}

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === ALARM_SESSION) {
    await refreshSession();
    checkForUpdate();   // piggyback the hourly session cycle to refresh the update banner
  } else if (alarm.name === ALARM_KEEPALIVE) {
    connectWS();
    // Only ensure a tab when we actually have an OPEN socket — never spin up
    // tabs while disconnected. The call itself is login-aware and serialized.
    if (ws && ws.readyState === WebSocket.OPEN) {
      maybeEnsurePersistentTab();
    }
  } else if (alarm.name === ALARM_RELOAD) {
    // Proactive cookie roll. Enqueue on tokenQueue so it serializes FIFO with
    // mints — it waits for any in-flight mint and blocks the next mint only while
    // the tab reloads. Never on ensureChain. Only when the socket is OPEN, so we
    // never spin tabs while disconnected (mirrors the keepalive guard).
    if (ws && ws.readyState === WebSocket.OPEN) {
      tokenQueue = tokenQueue.then(() => maybeReloadForRoll()).catch(() => {});
    }
  }
});

/* ------------------------------ lifecycle ---------------------------- */

chrome.tabs.onRemoved.addListener((tabId) => {
  removeOwned(tabId).catch(() => {});
  if (tabId === persistentTabId) {
    persistentTabId = null;
    chrome.storage.local.remove("persistentTabId");
  }
});

// On SW boot/wake: drop a stale (expired) creation lease, restore the badge, and
// collapse our owned tabs. Persistent mode keeps exactly one; ephemeral mode keeps
// none. A still-valid lease is preserved so a genuinely in-flight create isn't
// duplicated. Runs BEFORE connectWS so the socket's warm-up can't race the janitor.
async function reconcileTabsOnBoot() {
  const settings = await getSettings();
  const { creationLease } = await chrome.storage.local.get(["creationLease"]);
  if (!creationLease || (creationLease.expiresAt || 0) <= Date.now()) {
    await chrome.storage.local.remove("creationLease");
  }
  setBadge((await getAuthState()).state);
  if (settings.tabMode === "ephemeral") {
    await closeAllOwnedTabs();   // ephemeral mode never keeps a persistent tab
  } else {
    await sweepOwnedTabs();      // collapse owned tabs to exactly one
  }
}

chrome.runtime.onInstalled.addListener(async () => {
  await log("INFO", "Flow2API Worker installed");
  await applyProxy(await getSettings());   // proxy live before any network use
  await setupAlarms();
  await reconcileTabsOnBoot();
  connectWS();
  // Kick off an immediate session push so the backend is valid right away.
  refreshSession().catch(() => {});
});

chrome.runtime.onStartup.addListener(async () => {
  await applyProxy(await getSettings());   // proxy live before WS/Google
  await setupAlarms();
  await reconcileTabsOnBoot();
  connectWS();
});

chrome.runtime.onMessage.addListener((req, _sender, sendResponse) => {
  if (req.action === "testCaptchaConnection") {
    closeSocket(); connectWS();
    sendResponse({ ok: true });
    return true;
  }
  if (req.action === "refreshSessionNow") {
    refreshSession().then((r) => sendResponse(r));
    return true;
  }
  if (req.action === "settingsChanged") {
    (async () => {
      await applyProxy(await getSettings());   // proxy may have changed -> re-apply/clear
      await setupAlarms();
      closeSocket(); connectWS();
    })();
    sendResponse({ ok: true });
    return true;
  }
  if (req.action === "disableProxy") {
    // One-click panic disable: turn off auto, clear any override, drop the proxy now.
    (async () => {
      await chrome.storage.local.set({ proxyAuto: false, proxyUrl: "" });
      await clearProxy();
    })();
    sendResponse({ ok: true });
    return true;
  }
  if (req.action === "getConnState") {
    (async () => {
      sendResponse({
        connected: !!(ws && ws.readyState === WebSocket.OPEN),
        grantExpired: await isGrantExpired(),
        loginRequired: (await getAuthState()).state === "login_required",
      });
    })();
    return true;
  }
  if (req.action === "getUpdateInfo") {
    // Return a FRESH result (not the stale cache) so the banner is correct on the popup's
    // first render — fixes the notice getting stranded in the log right after a publish.
    // Falls back to cache if the check fails/offline.
    (async () => {
      const fresh = await checkForUpdate().catch(() => null);
      const info = fresh
        || (await chrome.storage.local.get(["updateInfo"])).updateInfo
        || { current: extVersion(), updateAvailable: false };
      sendResponse({ updateInfo: info });
    })();
    return true;
  }
  if (req.action === "getLogs") {
    chrome.storage.local.get(["logs"]).then(({ logs = [] }) => sendResponse({ logs }));
    return true;
  }
  if (req.action === "clearLogs") {
    chrome.storage.local.set({ logs: [] }).then(() => sendResponse({ ok: true }));
    return true;
  }
});

// Boot (covers the service-worker waking up). Sequenced: proxy, then janitor, then warm-up.
(async () => {
  await applyProxy(await getSettings());   // proxy live before WS/Google
  await setupAlarms();
  await reconcileTabsOnBoot();
  connectWS();
  checkForUpdate();   // fire-and-forget: refresh the update banner state on boot
})();
