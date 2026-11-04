/* Flow2API Worker — combined captcha + Google-login service worker.
 *
 * Two jobs in one extension:
 *   1) reCAPTCHA: keeps one hidden flow.google.com/about tab and mints a fresh
 *      reCAPTCHA Enterprise token on demand over the /captcha_ws WebSocket.
 *   2) Login sharing (3.8.0, cookie-first): pushes this profile's Google login cookies
 *      to /api/plugin/update-token. Since 2026-10-23 they ARE the account's Flow login on
 *      the server (docs/cookie-sync.md): pushed when Google rotates the login, and at
 *      least every 20 min. Google retired the Labs sign-in on 2026-10-21, so 3.8.0 has
 *      no Labs tab, no Labs cookie and no Labs reload any more.
 *
 * Built for heavy use and a fleet of laptops (one account each): set a unique
 * Route Key per laptop so the backend routes each account's captcha to the right
 * browser. Reliability is the priority — the mint tab is recreated on loss,
 * the socket auto-reconnects, and an alarm revives everything if Chrome suspends
 * the service worker. The worker never closes the last tab of a window.
 *
 * Optional third job (opt-in per profile, see suno.js): share this browser's Suno
 * login with the backend so Flow2API can make songs on that account.
 */

importScripts("suno.js");
importScripts("session_state.js");
importScripts("cookie_sync.js");
importScripts("site_config.js");
// Server-run browsers only: site.js (generated on the box from its site.json by ext-sync.sh) sets
// globalThis.FlowSite. importScripts throws for a missing file, so staff builds simply get {}.
// (A fetch() of the packaged file from the service worker did NOT work on Chromium 153 — 2026-09-24.)
let SITE_CONFIG = {};
try { importScripts("site.js"); SITE_CONFIG = FlowSiteConfig.sanitizeSiteConfig(globalThis.FlowSite); } catch (_) {}

const RECAPTCHA_SITE_KEY = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV";
// 2026-09-23: Google's backend accepts ONLY tokens minted on flow.google.com (a
// labs.google-hosted token is refused: "reCAPTCHA evaluation failed"), and the
// flow.google.com app booby-traps grecaptcha.enterprise.execute (see
// recaptcha_hook.js). So minting happens in an unowned tab on flow.google.com/about —
// a page that does not load reCAPTCHA itself, where we load it with the same site key
// (Trusted Types policy + the page's own script nonce satisfy its CSP). It needs no
// Flow sign-in.
const MINT2_URL = "https://flow.google.com/about";
const MINT2_ORIGIN = "https://flow.google.com/";

const ALARM_SESSION = "flow2api_session_refresh";
const ALARM_KEEPALIVE = "flow2api_keepalive";
const ALARM_PORT_MIGRATION = "flow2api_port_migration"; // 3.7.4: ask again for a pending IP move
const LEGACY_ALARM_RELOAD = "flow2api_session_reload";   // 3.7.x Labs reload; cleared on boot

const PUSH_TIMEOUT_MS     = 25 * 1000;            // hard bound on the session-push POST
// Safety-net push interval. A server session dies when this browser rotates its Google login
// (10–80 min after a push); a cookie rotation pushes at once, this catches anything missed.
const SESSION_PUSH_MAX_MINUTES = 20;

// WebSocket heartbeat: keep the MV3 service worker alive (Chrome 116+ resets the
// idle timer on WS traffic). 15s gives margin under the ~30s idle limit + timer jitter.
const HEARTBEAT_MS = 15000;
const DEAD_SOCKET_MS = 50000;  // > 3 missed pongs; an old server that never pongs is still detected by get_token/register_ack traffic
let lastServerMsgAt = 0;

const DEFAULT_SETTINGS = {
  // Baked-in config so the extension works the moment it is loaded — no setup.
  // Internal tool; these are intentionally hard-coded for zip-and-load distribution.
  serverBase: "https://flow.ashuthefire.com",
  apiKey: "han1234",      // Flow2API API key -> authenticates the captcha WebSocket
  connectionToken: "fahim", // plugin connection token -> authenticates token-update
  routeKey: "",           // empty = shared captcha pool (any browser serves any account)
  clientLabel: "",        // optional friendly name shown in the backend logs
  refreshIntervalMinutes: SESSION_PUSH_MAX_MINUTES,
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
  failedImageMode: false,
  // Opt-in Suno login sharing (suno.js). Same rule as above: MUST be listed here.
  sunoEnabled: false,
  // "Use this account in Flow2API" (cookie_sync.js): share this profile's Google login — the
  // account's Flow login on the server. Default ON; OFF stops the account in Flow2API.
  cookieSyncEnabled: true
};

let ws = null;
let lastMintAt = 0;       // timestamp of the last reCAPTCHA mint (for pacing)
let extPortMigration = null; // {id, port} offered by the server in the last pool fetch (3.7.4)
let mintInFlight = false; // true while handleGetToken mints (in-mem only); an IP move waits for it
let heartbeatInterval = null;
let reconnectTimer = null;
let mintTabId = null;                 // the flow.google.com/about mint tab (NOT owned by a user)
let tokenQueue = Promise.resolve();   // serialize token requests
let connecting = false;

/* ----------------------------- settings ----------------------------- */

async function getSettings() {
  const site = SITE_CONFIG;
  return new Promise((resolve) => {
    chrome.storage.local.get(DEFAULT_SETTINGS, (stored) => {
      const build = (routeKey) => resolve({
        serverBase: (stored.serverBase || DEFAULT_SETTINGS.serverBase).trim().replace(/\/+$/, ""),
        apiKey: (stored.apiKey || "").trim(),
        connectionToken: (stored.connectionToken || "").trim(),
        routeKey,
        clientLabel: (site.clientLabel || stored.clientLabel || "").trim(),
        // At most every 20 min (3.8.0): a stored 60 is the old default that the pre-2.9.0 settings
        // page saved, and nothing in the popup sets this any more. Shorter stored values (>= 5) stay.
        refreshIntervalMinutes: Math.min(SESSION_PUSH_MAX_MINUTES, Math.max(5, parseInt(stored.refreshIntervalMinutes, 10) || SESSION_PUSH_MAX_MINUTES)),
        mintIntervalMs: Math.max(0, parseInt(stored.mintIntervalMs, 10) || 2000),
        // Residential proxy is ALWAYS on for staff builds: the backend redeems the
        // generate call from the same residential IP the extension minted from, so a
        // profile stuck on "direct egress" would break reCAPTCHA alignment. Not toggleable.
        proxyAuto: true,
        proxyUrl: (site.proxyUrl || stored.proxyUrl || "").trim(),
        // site.json only: route EVERY host through the proxy (server-run browser).
        proxyAllHosts: site.proxyAllHosts === true,
        // "Failed-image mode" switch: when ON this account is reserved for staff-driven
        // failed-image regeneration (reported as pool_mode=failed_image, kept out of the
        // automatic article pool).
        failedImageMode: stored.failedImageMode === true,
        sunoEnabled: stored.sunoEnabled === true,
        cookieSyncEnabled: stored.cookieSyncEnabled !== false
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
  if (!extProxyPool) {
    try { const { extProxyPool: saved } = await chrome.storage.local.get(["extProxyPool"]); if (saved) extProxyPool = saved; } catch (_) {}
  }
  try {
    if (!settings.serverBase || !settings.connectionToken) return;
    const base = new URL(settings.serverBase);
    // Send our route_key so the server can assign THIS device a distinct, least-loaded IP.
    const rk = (settings.routeKey || "").trim();
    // 3.7.4: our version (the server moves an Ultra to its own IP only for >= 3.7.4, which pushes
    // right after the switch) and whether a manual proxy is set (such devices are never moved).
    const q = new URLSearchParams();
    if (rk) q.set("route_key", rk);
    q.set("ext_version", extVersion());
    if ((settings.proxyUrl || "").trim()) q.set("manual", "1");
    const url = `${base.protocol}//${base.host}/api/plugin/proxy-pool?${q.toString()}`;
    const resp = await fetch(url, { headers: { "Authorization": `Bearer ${settings.connectionToken}` } });
    if (!resp.ok) return;
    const data = await resp.json();
    if (data && data.pool && Array.isArray(data.pool.ports) && data.pool.ports.length && data.pool.host) {
      extProxyPool = data.pool;
      // 3.7.4: remembered across restarts, so an offline restart keeps the server's ports (the
      // baked-in list does not have the newer ones an Ultra may have moved to).
      try { await chrome.storage.local.set({ extProxyPool: data.pool }); } catch (_) {}
    }
    if (data && Number.isInteger(data.migration_retry_in) && data.migration_retry_in > 0) {
      try { chrome.alarms.create(ALARM_PORT_MIGRATION, { delayInMinutes: Math.max(0.5, data.migration_retry_in / 60) }); } catch (_) {}
    }
    if (data && Number.isInteger(data.assigned_port)) {
      extAssignedPort = data.assigned_port;
    }
    // 3.7.4: a one-time move to this account's own IP, to confirm once applied.
    extPortMigration = (data && typeof data.migration_id === "string" && Number.isInteger(data.assigned_port))
      ? { id: data.migration_id, port: data.assigned_port } : null;
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
  // 3.7.4: never switch IP under a running mint (the server paused new work for this account).
  let migration = settings.proxyUrl ? null : extPortMigration;
  let portBefore = null;
  try { ({ proxyPort: portBefore } = await chrome.storage.local.get(["proxyPort"])); } catch (_) {}
  if (migration) {
    for (let i = 0; i < 180 && mintInFlight; i++) await sleep(500);   // up to 90 s (video mints take ~70 s)
    if (mintInFlight) {
      // Still minting: do not switch now. Stay on the current port and tell the server (back to pending).
      extAssignedPort = null;
      await confirmPortMigration({ ...migration, ok: false });
      migration = null;
    }
  }
  const p = parseProxyUrl(await resolveProxyUrl(settings));
  if (!p) { await clearProxy(); return; }
  proxyCreds = { username: p.username, password: p.password };
  // Persist creds BEFORE the PAC goes live so a proxy 407 ALWAYS has creds to answer with —
  // even if this ephemeral MV3 worker is later torn down (in-memory proxyCreds lost) and then
  // resurrected by a proxied request. onAuthRequired rehydrates from here, so Chrome's native
  // "sign in to the proxy" dialog can never appear after an extension update / worker restart.
  try { await chrome.storage.local.set({ proxyCreds }); } catch (_) {}
  const P = pacProxyToken(p);
  const pac = FlowSiteConfig.buildPacScript(P, settings.proxyAllHosts === true);
  try {
    await chrome.proxy.settings.set({ value: { mode: "pac_script", pacScript: { data: pac } }, scope: "regular" });
    await log("SUCCESS", settings.proxyAllHosts ? "Per-profile proxy applied (ALL hosts, site.json)" : "Per-profile proxy applied (Flow + reCAPTCHA only)", { host: p.host, port: p.port });
    try { await chrome.storage.local.set({ appliedProxyPort: p.port }); } catch (_) {}
    if (migration && p.port === migration.port) await confirmPortMigration({ ...migration, ok: true });
  } catch (e) {
    await log("ERROR", "Failed to apply proxy", { error: e.message });
    if (migration) {
      // Forget the offered port too, so a restart does not apply it without the server knowing.
      extAssignedPort = null;
      try { await chrome.storage.local.set({ proxyPort: portBefore || null }); } catch (_) {}
      await confirmPortMigration({ ...migration, ok: false });
    }
  }
}

// 3.7.4 one-time move to this account's own IP: tell the server we applied (or could not apply) the
// offered port. Only then does the server switch its own side. Kept in storage and retried every
// minute until the server answers, then the session is pushed so it reports the new IP.
async function confirmPortMigration(m) {
  try { await chrome.storage.local.set({ pendingPortAck: m }); } catch (_) {}
  await log(m.ok ? "INFO" : "ERROR", m.ok ? "Moved to this account's own IP — confirming to the server" : "Could not apply the new IP — telling the server", { port: m.port });
  await sendPendingPortAck();
}

async function sendPendingPortAck() {
  let m;
  try { ({ pendingPortAck: m } = await chrome.storage.local.get(["pendingPortAck"])); } catch (_) { return; }
  if (!m) return;
  const settings = await getSettings();
  if (!settings.serverBase || !settings.connectionToken) return;
  // Only report what Chrome is really using now (a restart may have applied something else).
  let applied = null;
  try { ({ appliedProxyPort: applied } = await chrome.storage.local.get(["appliedProxyPort"])); } catch (_) {}
  if (!m.ok && applied === m.port) m = { ...m, ok: true };   // it did end up on the offered port
  if (m.ok) {
    if (applied !== m.port) {
      await chrome.storage.local.remove(["pendingPortAck"]);
      await log("INFO", "IP move not confirmed: Chrome is not on the offered port; asking the server again", { applied, offered: m.port });
      applyProxy(settings).catch(() => {});
      return;
    }
  }
  try {
    const base = new URL(settings.serverBase);
    const resp = await fetch(`${base.protocol}//${base.host}/api/plugin/port-migration-ack`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": `Bearer ${settings.connectionToken}` },
      body: JSON.stringify({ route_key: (settings.routeKey || "").trim(), migration_id: m.id, port: m.port, ok: m.ok }),
    });
    if (!resp.ok) return;   // retried by the keep-alive alarm
    const data = await resp.json().catch(() => ({}));
    await chrome.storage.local.remove(["pendingPortAck"]);
    await log(data.success ? "SUCCESS" : "INFO", "Server answered the IP move", { state: data.state, reason: data.reason });
    if (m.ok && data.success) refreshSession().catch(() => {});
    else if (m.ok && !data.success) applyProxy(settings).catch(() => {});   // rejected: fetch the current offer
  } catch (_) { /* retried by the keep-alive alarm */ }
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

const RED_ICON = "data:image/svg+xml;base64," + btoa(
  '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96"><rect width="96" height="96" rx="18" fill="#f06262"/><text x="48" y="68" font-size="64" text-anchor="middle" fill="#fff" font-family="sans-serif">!</text></svg>'
);

// The toolbar badge is the reliable, always-visible signal — it survives the user
// being away (lid closed) and needs no icon asset. Recomputed from the CURRENT state, by priority:
//   sign-in needed (red "!")  — no Google login here, or the server could not sign in to Flow
//                               with it (push answered 400); ALWAYS outranks an update.
//   grant expired (red "!")   — the server said Google stopped renewing the access token.
//   update_available (blue ↑) — a newer ext version is published; shown when login is ok
//                               so staff notice WITHOUT opening the popup.
async function refreshBadge() {
  try {
    if (!chrome.action) return;
    const red = (title) => {
      chrome.action.setBadgeText({ text: "!" });
      chrome.action.setBadgeBackgroundColor({ color: "#f06262" });
      chrome.action.setTitle({ title });
    };
    const auth = await getAuthState();
    if (auth.state === "login_required") {
      red(auth.kind === "rejected"
        ? "Flow2API Worker — open flow.google.com in this Chrome and check you are signed in"
        : "Flow2API Worker — sign in to Google in this Chrome");
      return;
    }
    // Separate persisted state, cleared only by a push the server confirms (credential_verified).
    if (await isGrantExpired()) {
      red("Flow2API Worker — sign out of Google in this Chrome, sign back in, then Reconnect");
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

async function notifyLogin(kind) {
  // Best-effort desktop notification; the badge is the guaranteed signal.
  try {
    await chrome.notifications.create("flow2api_login_" + Date.now(), {
      type: "basic",
      iconUrl: RED_ICON,
      title: "Flow2API Worker — login needed",
      message: kind === "rejected"
        ? "Flow2API could not sign in to Flow with this Chrome's Google login. Open flow.google.com in this Chrome and check you are signed in."
        : "This Chrome is not signed in to Google. Sign in to Google (open flow.google.com) and Flow2API picks it up by itself.",
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
// "Sign-in needed" (3.8.0). Set when this Chrome has no Google login cookies (kind "signed_out")
// or the server could not sign in to Flow with the pushed cookies (push answered 400, kind
// "rejected"). It gates nothing (minting needs no login); it drives the red badge, the popup line
// and one notification. Cleared by a push the server confirms (2xx with credential_verified).

async function getAuthState() {
  const { authState } = await chrome.storage.local.get(["authState"]);
  return authState || { state: "ok" };
}

async function setLoginRequired(kind, reason) {
  const cur = await getAuthState();
  const transition = cur.state !== "login_required" || cur.kind !== kind;
  await chrome.storage.local.set({
    authState: { state: "login_required", kind, reason: String(reason || "").slice(0, 200), since: transition ? Date.now() : (cur.since || Date.now()) }
  });
  await refreshBadge();
  if (transition) {
    await log("ERROR", kind === "rejected"
      ? "Flow2API could not sign in to Flow with this Chrome's Google login — open flow.google.com and check you are signed in"
      : "This Chrome is not signed in to Google — sign in so Flow2API can use this account", { reason });
    notifyLogin(kind);
  }
}

async function clearLoginRequired() {
  const cur = await getAuthState();
  if (cur.state !== "ok") {
    await chrome.storage.local.set({ authState: { state: "ok" } });
    await refreshBadge();
    await log("SUCCESS", "Flow2API signed in to Flow with this Chrome's Google login");
  }
}

/* ------------------------- grant-expired state ------------------------ */
// Set ONLY when the server answers a session push with action=relogin_required (it
// verified the login inside our cookies and Google refused it).
// Cleared ONLY by a push the server confirms as credential_verified — never by a cookie
// merely being present, which is exactly the trap that kept accounts flapping for weeks.
async function isGrantExpired() {
  try { const { grantExpired } = await chrome.storage.local.get(["grantExpired"]); return !!(grantExpired && grantExpired.state === "grant_expired"); }
  catch (_) { return false; }
}

async function setGrantExpired(message) {
  const was = await isGrantExpired();
  await chrome.storage.local.set({ grantExpired: { state: "grant_expired", since: Date.now(), message: message || "" } });
  await refreshBadge(); // sign-in needed still outranks it
  await log("ERROR", "Google stopped renewing this account's access token — sign out of Google in this Chrome, sign back in, then click Reconnect", { message: (message || "").slice(0, 160) });
  if (!was) notifyGrantExpired();
}

async function clearGrantExpired() {
  if (await isGrantExpired()) {
    await chrome.storage.local.remove("grantExpired");
    await refreshBadge();
    await log("SUCCESS", "Google access token verified — account healthy again");
  }
}

async function notifyGrantExpired() {
  try {
    await chrome.notifications.create("flow2api_grant_" + Date.now(), {
      type: "basic",
      iconUrl: RED_ICON,
      title: "Flow2API Worker — please sign in again",
      message: "Google stopped renewing this account's access token. Sign OUT of Google in this Chrome, sign back IN, then click Reconnect in the extension.",
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

/* ------------------------ window-safe tab handling ------------------- */
//
// 2026-09-04: the worker tab is usually the ONLY tab in its Chrome window (staff run
// it in a dedicated profile). Chrome closes a window when its last tab closes, and a
// profile with no window left makes the next chrome.tabs.create fail with
// "No current window" (seen 2026-09-03 21:39:40). Rules: never close the last tab of
// a window, and open a window when the profile has none.

function shortUrl(u) { u = String(u || ""); return u.length > 90 ? u.slice(0, 90) + "…" : u; }

async function isLastTabInWindow(tab) {
  if (!tab || tab.windowId == null) return false;
  try { const tabs = await chrome.tabs.query({ windowId: tab.windowId }); return tabs.length <= 1; }
  catch (_) { return false; }
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
    await log("WARN", "No Chrome window was open for this profile; opened one for the mint tab", { windowId: win.id, tabId: tab.id });
    return tab;
  }
}

/* ------------------------------- minting ----------------------------- */

// The flow.google.com/about mint tab — since 3.8.0 the only tab the worker opens.
// Reuses the same tab for every mint (reCAPTCHA stays loaded, so a mint is ~1-2 s).
//
// Ownership rules (Codex review 2026-09-23):
// - the id lives in chrome.storage.session, which Chrome clears on browser
//   restart: tab ids are only unique within a session, so a persisted id could
//   name a staff member's unrelated tab after a restart;
// - a tracked tab that is no longer on flow.google.com is never navigated or
//   closed (someone may be using it): it is simply forgotten;
// - at most ONE mint tab is tracked; a tab we replace is closed first (window-
//   safe) so failures cannot leak untracked tabs.
const MINT_TAB_LOAD_MS = 10000;

async function readMintTabId() {
  if (mintTabId != null) return mintTabId;
  try {
    const { mintTabId: saved } = await chrome.storage.session.get(["mintTabId"]);
    if (saved != null) mintTabId = saved;
  } catch (_) {}
  return mintTabId;
}

async function rememberMintTab(id) {
  mintTabId = id;
  try {
    if (id == null) await chrome.storage.session.remove("mintTabId");
    else await chrome.storage.session.set({ mintTabId: id });
  } catch (_) {}
}

function onMintOrigin(tab) { return !!tab && tabUrlOf(tab).startsWith(MINT2_ORIGIN); }

// Serialized: the warm-up on connect and a request arriving at the same moment
// must not both create a tab (two tabs, one tracked). budgetMs bounds every wait
// inside, so a request's overall deadline is honoured through tab loads too.
let mintChain = Promise.resolve();
function ensureMintTab(preferWindowId, deadlineAt) {
  const run = () => _ensureMintTab(preferWindowId, deadlineAt);
  mintChain = mintChain.then(run, run);
  return mintChain;
}

async function _ensureMintTab(preferWindowId, deadlineAt) {
  // Every wait below is cut from the same absolute deadline (queue wait included).
  const budget = () => (deadlineAt == null ? MINT_TAB_LOAD_MS : Math.min(MINT_TAB_LOAD_MS, deadlineAt - Date.now()));
  if (budget() < 1000) throw new Error("no time left to prepare the mint tab");
  const id = await readMintTabId();
  let tab = await getTab(id);
  if (tab && !onMintOrigin(tab)) {
    // Not ours to steer any more (about:blank while loading is ours; anything else is not).
    if (tabUrlOf(tab) !== "about:blank" && tabUrlOf(tab) !== "") {
      await log("WARN", "Mint tab left flow.google.com; forgetting it (not closing it)", { tabId: id, url: shortUrl(tabUrlOf(tab)) });
      await rememberMintTab(null);
      tab = null;
    }
  }
  let waited = false;
  if (tab && tab.discarded) {
    try { await chrome.tabs.reload(id, { bypassCache: false }); await waitForTabComplete(id, budget()); waited = true; tab = await getTab(id); } catch (_) { tab = null; }
  }
  if (tab && onMintOrigin(tab) && tab.status === "complete") return id;
  if (tab && onMintOrigin(tab) && !waited) {
    // Still loading: give it its load budget once (never twice for the same tab).
    await waitForTabComplete(id, budget());
    const again = await getTab(id);
    if (again && onMintOrigin(again)) return id;
  } else if (tab && onMintOrigin(tab)) {
    return id; // reloaded but "complete" never fired: it is ours and on the page; try minting
  }
  // Replace: close what we tracked (window-safe) BEFORE creating, so we never
  // hold two tabs.
  if (tab) {
    await rememberMintTab(null);
    if (!(await isLastTabInWindow(tab))) { try { await chrome.tabs.remove(id); } catch (_) {} }
  }
  const created = await createTabSafely(MINT2_URL, preferWindowId);
  await rememberMintTab(created.id);
  try { await chrome.tabs.update(created.id, { autoDiscardable: false }); } catch (_) {}
  await waitForTabComplete(created.id, budget());
  const settled = await getTab(created.id);
  if (!onMintOrigin(settled)) {
    const url = tabUrlOf(settled);
    if (settled && (url === "about:blank" || url === "")) {
      // Still loading when the budget ran out: keep tracking it (the next ensure
      // reuses it) rather than leaving an untracked tab behind.
      throw new Error("mint tab is still loading flow.google.com");
    }
    // Gone, or someone navigated it elsewhere while it loaded: not ours to close.
    await rememberMintTab(null);
    throw new Error("mint tab did not reach flow.google.com (" + (url || "gone") + ")");
  }
  await sleep(500);
  await log("INFO", "Mint tab opened on flow.google.com", { tabId: created.id });
  return created.id;
}

// Close the mint tab (never if it is the last tab in its window) and forget it,
// so the next ensureMintTab() starts fresh. Used after a failed mint attempt.
function dropMintTab(why) {
  const run = () => _dropMintTab(why);
  mintChain = mintChain.then(run, run);
  return mintChain;
}

async function _dropMintTab(why) {
  const id = await readMintTabId();
  if (id == null) return;
  const tab = await getTab(id);
  if (!tab) { await rememberMintTab(null); return; }
  if (!onMintOrigin(tab)) {
    // Someone navigated it elsewhere: not ours to close. Forget it.
    await log("WARN", "Mint tab left flow.google.com; forgetting it (not closing it)", { tabId: id, url: shortUrl(tabUrlOf(tab)), why });
    await rememberMintTab(null);
    return;
  }
  if (await isLastTabInWindow(tab)) {
    // Closing it would close the window: reload it in place and keep tracking it,
    // so the next ensure reuses this tab instead of opening a second one.
    await log("INFO", "Mint tab is the last tab in its window; reloading it in place instead of closing", { tabId: id, why });
    try { await chrome.tabs.reload(id, { bypassCache: false }); } catch (_) { await rememberMintTab(null); }
    return;
  }
  try { await chrome.tabs.remove(id); } catch (_) {}
  await rememberMintTab(null);
}

// Warm the mint tab (socket connect + keep-alive), so the first real request does not
// pay the page load inside the server's 25 s budget.
async function maybeEnsureMintTab() {
  try { await ensureMintTab(); } catch (_) {}
}

// Mint a reCAPTCHA token in the given tab's MAIN world.
//
// On flow.google.com/about the page does not load reCAPTCHA, so we load
// enterprise.js ourselves: the page's CSP needs a Trusted Types policy for the
// script URL and the page's own nonce on the <script>. If the page HAS loaded
// reCAPTCHA (a project page, or /about in a future build), its public execute is
// the booby-trapped wrapper — recaptcha_hook.js captured the genuine one at
// document_start as window.__f2aRealExecute, and that is what we call.
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
      const ctx = () => ({ url: location.href, hadGrecaptcha: typeof grecaptcha !== "undefined", real: !!window.__f2aRealExecute });
      const done = (v) => { if (!settled) { settled = true; resolve(v); } };
      const ok = (t) => done(Object.assign({ ok: true, token: t }, ctx()));
      const fail = (err) => done(Object.assign({ ok: false, err: String(err || "unknown") }, ctx()));
      try {
        // pageProvided: reCAPTCHA was already on the page (so its public execute may
        // be the booby-trapped wrapper). Decide which function to call INSIDE ready,
        // when the genuine one has had every chance to be captured; never fall back
        // to the public function on a page-provided instance.
        const run = (pageProvided) => {
          grecaptcha.enterprise.ready(() => {
            const real = window.__f2aRealExecute;
            let exec;
            if (real) exec = () => real(siteKey, { action });
            else if (!pageProvided) exec = () => grecaptcha.enterprise.execute(siteKey, { action });
            else { fail("page loaded reCAPTCHA itself and the genuine execute was not captured (trap)"); return; }
            exec()
              .then((t) => (t ? ok(t) : fail("recaptcha returned an empty token")))
              .catch((e) => fail(e && e.message ? e.message : "recaptcha execute failed"));
          });
        };
        const inject = () => {
          // trustedtypes=true makes the loader insert its own inner script through a
          // Trusted Types policy; without it the loader loads but reCAPTCHA never
          // initialises under flow.google.com's CSP (mint hangs to timeout).
          let url = "https://www.google.com/recaptcha/enterprise.js?trustedtypes=true&render=" + siteKey;
          try {
            if (window.trustedTypes && trustedTypes.createPolicy) {
              let pol = window.__f2aTrustedTypes;
              if (!pol) {
                pol = trustedTypes.createPolicy("f2a-recaptcha", { createScriptURL: (u) => u });
                Object.defineProperty(window, "__f2aTrustedTypes", { value: pol, enumerable: false, configurable: true });
              }
              url = pol.createScriptURL(url);
            }
          } catch (e) { fail("trusted types policy refused: " + (e && e.message ? e.message : e)); return; }
          const s = document.createElement("script");
          const nonced = document.querySelector("script[nonce]");
          if (nonced && nonced.nonce) s.nonce = nonced.nonce;
          s.src = url;
          s.onload = () => run(false);
          s.onerror = () => fail("failed to load enterprise.js (blocked by CSP?)");
          (document.head || document.documentElement).appendChild(s);
        };
        if (typeof grecaptcha !== "undefined" && grecaptcha.enterprise && grecaptcha.enterprise.execute) run(!window.__f2aInjected);
        else { try { Object.defineProperty(window, "__f2aInjected", { value: true, enumerable: false, configurable: true }); } catch (_) {} inject(); }
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
  throw new Error(`${r.err} [url=${r.url}, grecaptcha=${r.hadGrecaptcha ? "present" : "absent"}, real=${r.real ? "captured" : "none"}]`);
}

async function handleGetToken(data, settings, responseSocket = ws) {
  // Mark a mint as in progress so a port move (applyProxy) never switches IP under it.
  mintInFlight = true;
  try {
    return await _handleGetToken(data, settings, responseSocket);
  } finally {
    mintInFlight = false;
  }
}

async function _handleGetToken(data, settings, responseSocket = ws) {
  const action = data.action || "IMAGE_GENERATION";
  // The server waits 25 s for an image mint and 75 s for a video mint (flow_client
  // _get_recaptcha_token); everything here — tab loads, retries, the mint itself —
  // must fit inside that, or the server has already given up while we still work.
  const deadline = Date.now() + (action === "VIDEO_GENERATION" ? 70000 : 23000);
  const remaining = () => deadline - Date.now();
  let lastMintError = null; // attempt 1's failure, carried into the replacement log line

  // Try up to twice: a mint that failed in the mint tab is retried in a fresh one.
  for (let attempt = 1; attempt <= 2; attempt++) {
    try {
      // 3.8.0: no Labs tab and no login gate. The token is minted in the flow.google.com/about
      // tab (since 3.6.4, see MINT2_URL), which needs no Flow sign-in.
      if (attempt === 2) {
        if (remaining() < 6000) {
          sendWS({ req_id: data.req_id, status: "error", error: "worker failed: " + String(lastMintError || "") + " (no time left to retry)" }, responseSocket);
          return;
        }
        await dropMintTab("mint attempt 1 failed: " + String(lastMintError || "").slice(0, 160));
      }
      const tabId = await ensureMintTab(undefined, deadline - 3000);

      await paceMint(settings.mintIntervalMs);
      const mintBudget = Math.min(action === "VIDEO_GENERATION" ? 60000 : 20000, remaining() - 500);
      if (mintBudget < 2000) throw new Error("no time left to mint (" + Math.round(remaining()) + " ms remaining)");
      const token = await mintTokenInTab(tabId, action, mintBudget);
      sendWS({ req_id: data.req_id, status: "success", token }, responseSocket);
      return;
    } catch (e) {
      const msg = String(e && e.message || e);
      lastMintError = msg;
      await log("ERROR", `token attempt ${attempt} failed`, { error: msg });
      if (attempt === 2) {
        sendWS({ req_id: data.req_id, status: "error", error: "worker failed: " + msg }, responseSocket);
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
    lastServerMsgAt = Date.now();
    heartbeatInterval = setInterval(() => {
      // A socket through a proxy can stay "open" after the server is gone (redeploy): the proxy
      // swallows our pings. The server answers each ping with a pong (3.7.3+); no pong for
      // DEAD_SOCKET_MS ⇒ close, so onclose reconnects (live: flow-ultra-01 sat on a dead socket for
      // 25 min after a deploy, 2026-09-24).
      if (Date.now() - lastServerMsgAt > DEAD_SOCKET_MS) {
        log("WARN", "No answer from the server for a while — reconnecting the captcha socket");
        try { socket.close(); } catch (_) {}
        return;
      }
      sendWS({ type: "ping" }, socket);
    }, HEARTBEAT_MS);
    // Warm the mint tab so the first real request is fast.
    maybeEnsureMintTab();
  };

  socket.onmessage = (event) => {
    lastServerMsgAt = Date.now();
    let data;
    try { data = JSON.parse(event.data); } catch (_) { return; }
    if (data.type === "pong") return;
    if (data.type === "register_ack") return;
    if (data.type === "get_token") {
      // Reply on the exact socket the request arrived on (falls back to current).
      tokenQueue = tokenQueue.then(() => handleGetToken(data, settings, socket)).catch(() => {});
    }
    if (data.type === "refresh_session") {
      // Direct call (NOT tokenQueue), with or without `reload`: the push is tab-free (cookie
      // read + POST), so it can't conflict with a mint, and queueing behind a long (video)
      // mint would blow the backend's 30 s wait.
      handleRefreshSession(data, socket);
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

// Pushes can overlap (alarm, cookie change, server request); each carries its own cookie_sync_seq.
// Only the answer to the NEWEST push may change the badge and the popup: an older push finishing
// later says nothing about now. In memory is enough — overlapping pushes share one worker life.
let newestAnsweredSeq = 0;
function isNewestAnswer(seq) {
  if (seq < newestAnsweredSeq) return false;
  newestAnsweredSeq = seq;
  return true;
}

// The server's verdict on the newest push, for the popup and support (no cookie values).
async function rememberLastPush(entry) {
  try { await chrome.storage.local.set({ lastPush: { ...entry, at: Date.now() } }); } catch (_) {}
}

// One POST to /api/plugin/update-token. Never throws: a transport failure comes back as status 0.
async function postSessionPush(updateUrl, settings, body) {
  // Bound the push so a hung request can't outlive the server's wait for our ack.
  const pushAbort = new AbortController();
  const pushTimer = setTimeout(() => pushAbort.abort(), PUSH_TIMEOUT_MS);
  try {
    const resp = await fetch(updateUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": `Bearer ${settings.connectionToken}` },
      body: JSON.stringify(body),
      signal: pushAbort.signal
    });
    const text = await resp.text();
    let data = null;
    try { data = JSON.parse(text); } catch (_) {}
    return { status: resp.status, ok: resp.ok, data, text };
  } catch (e) {
    const detail = e && e.name === "AbortError" ? "no answer from Flow2API in 25 s" : "could not reach Flow2API";
    return { status: 0, ok: false, data: { detail }, text: String((e && e.message) || e) };
  } finally {
    clearTimeout(pushTimer);
  }
}

// The session push, cookie-first (3.8.0). This profile's Google login cookies ARE the account's
// Flow login on the server (docs/cookie-sync.md), so there is no Labs tab, Labs cookie or "busy"
// gate any more: build google_cookies and push. token_id = the server asked about that account
// (refresh_session). Every attempt also settles an owed cookie-change push (cookie_sync.js).
async function refreshSession(token_id = null) {
  const builtAt = Date.now();
  let r;
  try {
    r = await pushSession(token_id);
  } catch (e) {
    await log("ERROR", "Session refresh error", { error: e && e.message });
    r = { success: false, error: e && e.message, reason: "network" };
  }
  // A probe (the server asked about ANOTHER account) says nothing about this Chrome's owed push.
  if (!r.probe) await settlePendingCookiePush(builtAt, r.reason !== "network");
  return r;
}

async function pushSession(token_id) {
  const settings = await getSettings();
  if (!settings.serverBase || !settings.connectionToken) {
    await log("INFO", "Session refresh skipped (need Server URL + Connection Token)");
    return { success: false, error: "not configured", reason: "not_configured" };
  }
  const { updateUrl } = deriveUrls(settings);
  const explicit = token_id != null;

  // This profile's Google login. Switch OFF = "Stop using this account in Flow2API": nothing is
  // pushed (the switch's own clear call deleted the server copy).
  const cookieSent = await cookieSyncPushFields();
  if (!cookieSent.enabled) {
    return { success: false, error: "sharing is OFF in this Chrome: Flow2API does not use this account", reason: "sharing_off" };
  }
  if (!cookieSent.fields.google_cookies) {
    if (isNewestAnswer(cookieSent.seq)) {
      await rememberLastPush({ seq: cookieSent.seq, reason: "logged_out", detail: "no Google login cookies in this Chrome" });
      await setLoginRequired("signed_out", "no Google login cookies in this Chrome");
    }
    return { success: false, error: "Sign in to Google in this Chrome", reason: "logged_out" };
  }
  const pushBody = { ...cookieSent.fields, cookie_sync_seq: cookieSent.seq };

  // Slice B: report the residential proxy this profile mints through + this browser's
  // real User-Agent, so the backend can REDEEM the generate call from the SAME IP + UA
  // (fixes reCAPTCHA "unusual activity" caused by residential-mint vs datacenter-redeem).
  let effProxy = "";
  try { effProxy = await resolveProxyUrl(settings); } catch (_) {}
  if (effProxy) pushBody.proxy_url = effProxy;
  try { if (navigator && navigator.userAgent) pushBody.user_agent = navigator.userAgent; } catch (_) {}
  // Bind this account to THIS device so its captcha minting routes back here (same
  // residential IP as the redeem). Uses the stable per-profile route key.
  if (settings.routeKey) pushBody.route_key = settings.routeKey;
  // Two-pool routing: report this profile's pool from the "Failed-image mode" switch.
  pushBody.pool_mode = settings.failedImageMode ? "failed_image" : "auto";
  // Version visibility: report this build's version so the admin can see who's outdated.
  pushBody.ext_version = extVersion();
  // 3.5.2: the project open on flow.google.com (if any). Flow no longer lets the server create a
  // first project through Labs for a fresh account, so a new account registers with this one.
  try {
    const tabs = await chrome.tabs.query({ url: ["*://flow.google.com/project/*", "*://labs.google/fx/tools/flow/project/*"] });
    for (const t of tabs || []) {
      const pid = FlowSessionState.flowProjectIdFromUrl(t.url || "");
      if (pid) { pushBody.project_id = pid; pushBody.project_name = String(t.title || "").slice(0, 80); break; }
    }
  } catch (_) {}
  // Which row: the one the server asked about, else the one this profile registered as (the
  // server still checks the login's email against that row).
  const boundId = explicit ? null : await getBoundTokenId();
  if (explicit) pushBody.token_id = token_id;
  else if (boundId != null) pushBody.token_id = boundId;

  await noteCookiePushAttempt();
  let resp = await postSessionPush(updateUrl, settings, pushBody);
  let answer = describePushAnswer(resp.status, resp.data);
  if (shouldRetryUnbound(resp.status, answer, boundId != null)) {
    // The remembered row is gone (404) or is another account now (409: this profile switched
    // Google accounts). 3.7.x kept sending it and was refused forever.
    await log("WARN", "This Chrome's remembered account no longer matches; forgetting it and pushing by email", { tokenId: boundId, status: resp.status, detail: answer.detail });
    await forgetBoundTokenId();
    delete pushBody.token_id;
    resp = await postSessionPush(updateUrl, settings, pushBody);
    answer = describePushAnswer(resp.status, resp.data);
  }
  // A 404/409 on a push the server asked for is the answer to the server's question ("is this the
  // browser of account N?": the shared-pool fan-out asks every browser), not news about this Chrome.
  const aboutThisChrome = !(explicit && (resp.status === 404 || resp.status === 409));
  // A push built before a later ON/OFF says nothing about now (e.g. a 400 landing after OFF must not
  // bring back the red sign-in badge).
  const newest = aboutThisChrome && cookieSent.gen === cookieSyncGen && isNewestAnswer(cookieSent.seq);

  if (!resp.ok) {
    await log("ERROR", "Session push failed", { status: resp.status, body: String(resp.text || "").slice(0, 200) });
    if (newest) {
      await rememberLastPush({ seq: cookieSent.seq, http: resp.status, reason: answer.reason, detail: answer.detail });
      await noteCookieSyncFailure(cookieSent, answer);
      // 400 = the server could not sign in to Flow with these cookies: red badge + popup line.
      if (answer.rejected) await setLoginRequired("rejected", answer.detail);
    }
    return { success: false, error: resp.status ? `server ${resp.status}` : answer.detail, message: answer.detail || null, reason: answer.reason, probe: !aboutThisChrome };
  }
  const result = resp.data || {};
  if (result.token_id != null) await rememberBoundTokenId(result.token_id);
  if (newest) {
    await rememberLastPush({ seq: cookieSent.seq, http: resp.status, reason: answer.reason, verified: answer.verified, detail: answer.detail });
    await noteCookieSyncResult(cookieSent, result.cookie_sync);
    // The server VERIFIES the login (a Flow sign-in with the pushed cookies). A 2xx alone is not
    // "healthy": only credential_verified clears the sign-in states.
    if (answer.reason === "relogin_required") await setGrantExpired(result.message);
    else if (answer.verified) { await clearLoginRequired(); await clearGrantExpired(); }
  }
  if (answer.reason === "relogin_required") {
    return { success: false, message: result.message, action: result.action, reason: "relogin_required" };
  }
  await log("SUCCESS", "Google login pushed to Flow2API", { action: result.action, verified: answer.verified, message: result.message });
  // 3.7.4: the server queued this account's one-time move to its own IP — fetch the port now.
  if (result.proxy_migration_pending && !settings.proxyUrl) applyProxy(settings).catch(() => {});
  return { success: true, message: result.message, action: result.action, reason: "refreshed" };
}

// Server "push now": refresh_session over the captcha socket (the admin UI or the healer asked
// THIS browser). The same cookie push as every other; the reload flag (3.7.x: reload the Labs tab
// first) has nothing left to reload. Replies with the push's real status — never lies "refreshed".
async function handleRefreshSession(data, responseSocket = ws) {
  let status, msg = null, err = null;
  try {
    const r = await refreshSession(data.token_id == null ? null : data.token_id);
    // reason carries refreshed / relogin_required / account_mismatch / logged_out / network /
    // not_configured / sharing_off verbatim.
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
  await chrome.alarms.clear(LEGACY_ALARM_RELOAD);   // 3.7.x reloaded the Labs tab every 3 h; nothing to reload now
  chrome.alarms.create(ALARM_SESSION, { periodInMinutes: settings.refreshIntervalMinutes, delayInMinutes: 0.1 });
  chrome.alarms.create(ALARM_KEEPALIVE, { periodInMinutes: 1 }); // revive SW/socket/mint tab
}

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === ALARM_SESSION) {
    // Safety-net push (every 20 min); a Google cookie rotation pushes sooner (cookie_sync.js).
    await refreshSession();
    sunoSync("alarm").catch(() => {});   // no-op unless the Suno switch is ON
    checkForUpdate();   // piggyback the session cycle to refresh the update banner
  } else if (alarm.name === ALARM_PORT_MIGRATION) {
    applyProxy(await getSettings()).catch(() => {});
  } else if (alarm.name === ALARM_KEEPALIVE) {
    retryPendingCookieClear().catch(() => {});   // no-op unless an OFF is still unacknowledged
    runPendingCookiePush().catch(() => {});      // backstop: no-op unless a cookie change is owed and due
    sendPendingPortAck().catch(() => {});        // no-op unless an IP-move confirmation is unanswered
    connectWS();
    // Only warm the mint tab when we actually have an OPEN socket — never open
    // tabs while disconnected.
    if (ws && ws.readyState === WebSocket.OPEN) {
      maybeEnsureMintTab();
    }
  }
});

/* ------------------------------ lifecycle ---------------------------- */

chrome.tabs.onRemoved.addListener((tabId) => {
  if (tabId === mintTabId) rememberMintTab(null).catch(() => {});
});

// 3.8.0 boot janitor. Up to 3.7.5 the worker kept its own hidden labs.google tab (ids in storage).
// Release those once: close each one still on labs.google (never the last tab of a window — that
// would close the window), and forget the rest (after a Chrome restart a stored id can name an
// unrelated tab). Also drop the Labs-era state: the tab lease and a 3.7.x "Labs login required"
// breaker (it has no `kind`; it was about the retired Labs cookie). Runs BEFORE connectWS.
async function releaseLabsTabsOnBoot() {
  const { ownedTabIds = [], authState } = await chrome.storage.local.get(["ownedTabIds", "authState"]);
  let closed = 0, leftOpen = 0;
  for (const id of ownedTabIds) {
    const tab = await getTab(id);
    if (!tab || !tabUrlOf(tab).startsWith("https://labs.google/")) continue;
    if (await isLastTabInWindow(tab)) { leftOpen++; continue; }
    try { await chrome.tabs.remove(id); closed++; } catch (_) {}
  }
  await chrome.storage.local.remove(["ownedTabIds", "persistentTabId", "creationLease"]);
  if (authState && authState.state === "login_required" && !authState.kind) await chrome.storage.local.remove("authState");
  if (closed || leftOpen) await log("INFO", "Released the old Google Labs tab (3.8.0 does not use Labs)", { closed, leftOpen });
  await refreshBadge();
}

chrome.runtime.onInstalled.addListener(async () => {
  await log("INFO", "Flow2API Worker installed");
  await applyProxy(await getSettings());   // proxy live before any network use
  await setupAlarms();
  await releaseLabsTabsOnBoot();
  connectWS();
  // Kick off an immediate session push so the backend is valid right away.
  refreshSession().catch(() => {});
  scheduleSunoSync("install", 3000);
});

chrome.runtime.onStartup.addListener(async () => {
  await applyProxy(await getSettings());   // proxy live before WS/Google
  await setupAlarms();
  await releaseLabsTabsOnBoot();
  connectWS();
  // Push the session on boot as well (was install + hourly only): a device that signed in and
  // then restarted Chrome otherwise stayed unregistered for up to an hour (2026-09-22).
  refreshSession().catch(() => {});
  scheduleSunoSync("startup", 3000);
});

// Is a Google account signed in in this Chrome at all? (accounts.google.com session cookies;
// any of these means "yes".)
async function googleSignedIn() {
  try {
    for (const name of ["SID", "__Secure-1PSID", "__Secure-3PSID", "SAPISID"]) {
      const c = await chrome.cookies.get({ url: "https://accounts.google.com/", name });
      if (c && c.value) return true;
    }
  } catch (_) {}
  return false;
}

// Ultra server browsers (3.7.5): the host agent calls this through CDP Runtime.evaluate on this service worker
// (debug port on the container loopback only) to point a FRESH profile at Flow2API before the first sign-in:
// server, connection token and a fixed route key the server already expects. Then the proxy, alarms and the
// captcha socket are re-applied, like the options page's settingsChanged. Returns the route key now in effect,
// never the token. Not reachable from web pages or other extensions.
globalThis.flowBootstrap = async function flowBootstrap(raw) {
  const cfg = FlowSiteConfig.sanitizeBootstrapConfig(raw);
  if (!cfg) return { ok: false, error: "invalid bootstrap config" };
  await chrome.storage.local.set(cfg);
  const settings = await getSettings();
  await applyProxy(settings);
  await setupAlarms();
  closeSocket(); connectWS();
  await log("INFO", "Server browser bootstrap applied", { serverBase: settings.serverBase, routeKey: settings.routeKey });
  return { ok: true, routeKey: settings.routeKey, serverBase: settings.serverBase, version: extVersion() };
};

chrome.runtime.onMessage.addListener((req, _sender, sendResponse) => {
  if (req.action === "testCaptchaConnection") {
    closeSocket(); connectWS();
    sendResponse({ ok: true });
    return true;
  }
  if (req.action === "openFlowSignIn") {
    // Popup button: open flow.google.com so staff can see (and fix) which Google account is signed in.
    chrome.tabs.create({ url: FlowSessionState.FLOW_SIGNIN_URL, active: true }, () => sendResponse({ ok: true }));
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
  // Suno login sharing (suno.js). Deliberately NOT routed through settingsChanged:
  // that handler re-applies the proxy and bounces the Flow socket, which Suno must
  // never disturb.
  if (req.action === "sunoSetEnabled") {
    sunoSetEnabled(req.enabled === true).then((r) => sendResponse(r)).catch((e) => sendResponse({ ok: false, error: e.message }));
    return true;
  }
  if (req.action === "sunoSyncNow") {
    sunoSync("manual", { force: true }).then((r) => sendResponse(r));
    return true;
  }
  if (req.action === "getSunoState") {
    getSunoState().then((r) => sendResponse(r)).catch(() => sendResponse({ enabled: false, state: null }));
    return true;
  }
  // "Keep working when I'm away" (cookie_sync.js). Same rule as Suno: never through
  // settingsChanged (that re-applies the proxy and bounces the Flow socket).
  if (req.action === "cookieSyncSetEnabled") {
    cookieSyncSetEnabled(req.enabled !== false).then((r) => sendResponse(r)).catch((e) => sendResponse({ ok: false, error: e.message }));
    return true;
  }
  if (req.action === "getCookieSyncState") {
    getCookieSyncState().then((r) => sendResponse(r)).catch(() => sendResponse({ enabled: true, state: null }));
    return true;
  }
  if (req.action === "getConnState") {
    (async () => {
      const auth = await getAuthState();
      const { cookieSyncEnabled } = await cookieSyncStore();
      const { lastPush } = await chrome.storage.local.get(["lastPush"]);
      sendResponse({
        connected: !!(ws && ws.readyState === WebSocket.OPEN),
        grantExpired: await isGrantExpired(),
        loginRequired: auth.state === "login_required",
        loginKind: auth.kind || null,
        googleSignedIn: await googleSignedIn(),
        sharingOff: cookieSyncEnabled === false,
        lastPush: lastPush || null,   // the server's verdict on the newest push (status, detail, when)
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
  await releaseLabsTabsOnBoot();
  connectWS();
  checkForUpdate();   // fire-and-forget: refresh the update banner state on boot
  scheduleSunoSync("boot", 3000);   // no-op unless the Suno switch is ON
})();
