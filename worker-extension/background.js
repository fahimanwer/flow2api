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
const LABS_URL = "https://labs.google/fx/tools/flow";
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
  proxyUrl: ""
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
  const updateUrl = `${base.protocol}//${base.host}/api/plugin/update-token`;
  return { wsUrl, updateUrl };
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

// Get-or-create this profile's port (one of the 5 static IPs), persisted so the
// profile keeps the SAME IP across SW restarts (a flapping IP triggers Google's
// "verify it's you"). Random pick; with <=5 profiles this usually gives distinct
// IPs — for guaranteed-distinct, set a specific :port via the proxy override.
async function getProxyPort() {
  let { proxyPort } = await chrome.storage.local.get(["proxyPort"]);
  const ports = PROXY_BASE.ports;
  if (!proxyPort || !ports.includes(proxyPort)) {
    proxyPort = ports[Math.floor(Math.random() * ports.length)];
    await chrome.storage.local.set({ proxyPort });
  }
  return proxyPort;
}

// Effective proxy URL: manual proxyUrl overrides; else proxyAuto builds the
// Oxylabs URL from PROXY_BASE + this profile's port; else "" (direct).
async function resolveProxyUrl(settings) {
  if (settings.proxyUrl) return settings.proxyUrl;
  if (!settings.proxyAuto) return "";
  const b = PROXY_BASE;
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
  const p = parseProxyUrl(await resolveProxyUrl(settings));
  if (!p) { await clearProxy(); return; }
  proxyCreds = { username: p.username, password: p.password };
  const P = pacProxyToken(p);
  const pac = [
    "function FindProxyForURL(url, host) {",
    "  var P = '" + P + "';",
    "  if (dnsDomainIs(host, 'labs.google')) return P;",                 // the Flow site + its session
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
  try { await chrome.proxy.settings.clear({ scope: "regular" }); } catch (_) {}
  await log("INFO", "Per-profile proxy cleared (direct egress)");
}

// MV3 proxy-auth: supply Oxylabs creds ASYNC, ONLY for proxy (407) challenges —
// never for origin (Google) 401s, so the Google session login is untouched.
// requestId de-dup avoids an infinite 407 loop when creds are wrong.
chrome.webRequest.onAuthRequired.addListener(
  (details, asyncCallback) => {
    if (!details.isProxy || !proxyCreds) { asyncCallback({}); return; }
    if (answeredAuthReqs.has(details.requestId)) {     // already tried -> creds bad
      answeredAuthReqs.delete(details.requestId);
      asyncCallback({ cancel: true });
      return;
    }
    answeredAuthReqs.add(details.requestId);
    asyncCallback({ authCredentials: { username: proxyCreds.username, password: proxyCreds.password } });
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

// The toolbar badge is the reliable, always-visible "login required" signal —
// it survives the user being away (lid closed) and needs no icon asset.
function setBadge(state) {
  try {
    if (!chrome.action) return;
    if (state === "login_required") {
      chrome.action.setBadgeText({ text: "!" });
      chrome.action.setBadgeBackgroundColor({ color: "#f06262" });
      chrome.action.setTitle({ title: "Flow2API Worker — Google Labs login required" });
    } else {
      chrome.action.setBadgeText({ text: "" });
      chrome.action.setTitle({ title: "Flow2API Worker" });
    }
  } catch (_) {}
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
function isFlowUrl(u) { return !!u && u.startsWith(LABS_URL); }

// A tab counts as "on Flow" if EITHER its committed url OR its pending (loading)
// url is the Flow URL — checked independently so an about:blank-then-Flow tab
// (which reports url:"about:blank", pendingUrl:Flow) is not missed.
function tabOnFlow(tab) {
  return !!tab && (isFlowUrl(tab.url || "") || isFlowUrl(tab.pendingUrl || ""));
}

// A tab is "usable" (ready to mint) only when navigation has COMMITTED to Flow.
function tabUsable(tab) { return !!tab && isFlowUrl(tab.url || ""); }

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

async function hasSessionCookie() {
  try {
    let c = await chrome.cookies.get({ url: "https://labs.google", name: SESSION_COOKIE });
    if (!c) {
      const all = await chrome.cookies.getAll({ domain: "labs.google" });
      c = all.find((x) => x.name === SESSION_COOKIE) || null;
    }
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
  for (const x of live) {
    if (keep && x.id === keep.id) { survivors.push(x.id); continue; }
    try { await chrome.tabs.remove(x.id); closed++; } catch (_) {}
  }
  await mutateOwned((cur) => cur.filter((id) => survivors.includes(id)));

  if (keep) {
    persistentTabId = keep.id;
    await chrome.storage.local.set({ persistentTabId: keep.id });
  } else {
    persistentTabId = null;
    await chrome.storage.local.remove("persistentTabId");
  }
  if (closed > 0) await log("INFO", "Swept extra owned Labs tabs", { kept: keep ? keep.id : null, closed });
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

/* --------------------------- tab creation ---------------------------- */

// Open ONE hidden Labs tab, wait for it to settle, and verify it actually reached
// Flow (not a login/consent redirect). Records ownership + a durable creation
// lease BEFORE the long load so a service-worker crash mid-load can't orphan it
// or let a respawned worker create a second tab. Throws on login redirect (and
// trips the circuit breaker). Used for BOTH persistent and ephemeral modes; it
// does NOT itself assign persistentTabId (callers own that policy).
async function openLabsTab() {
  // Absolute create ceiling: enforced at the single creation chokepoint, so NO
  // caller (persistent, ephemeral, warm, retry) can push owned tabs past the cap.
  const liveOwned = await queryOwnedLiveTabs();
  if (liveOwned.length >= MAX_OWNED_TABS) await sweepOwnedTabs();

  await chrome.storage.local.set({ creationLease: { state: "creating", expiresAt: Date.now() + LEASE_MS } });
  let tab;
  try {
    tab = await chrome.tabs.create({ url: LABS_URL, active: false });
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
      // and don't let it be recreated forever.
      try { await chrome.tabs.remove(tab.id); } catch (_) {}
      await removeOwned(tab.id);
      if (isLoginTab(settled)) await setLoginRequired("Labs redirected to " + tabUrlOf(settled));
      throw new Error("labs tab did not reach Flow URL (" + (tabUrlOf(settled) || "gone") + ")");
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
    persistentTabId = usable;
    await chrome.storage.local.set({ persistentTabId: usable });
    await sweepOwnedTabs(usable);
    return persistentTabId;
  }

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

  // 5) Create exactly one.
  const id = await openLabsTab();
  await clearLoginRequired();
  persistentTabId = id;
  await chrome.storage.local.set({ persistentTabId: id });
  await sweepOwnedTabs(id); // close anything that snuck in during the load window
  await log("INFO", "Persistent Labs tab opened", { tabId: persistentTabId });
  return persistentTabId;
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
async function mintTokenInTab(tabId, action, timeoutMs) {
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    func: (siteKey, action, timeoutMs) => new Promise((resolve, reject) => {
      let settled = false;
      const finish = (fn, v) => { if (!settled) { settled = true; fn(v); } };
      try {
        const run = () => {
          grecaptcha.enterprise.ready(() => {
            grecaptcha.enterprise.execute(siteKey, { action })
              .then((t) => finish(resolve, t))
              .catch((e) => finish(reject, e && e.message ? e.message : "recaptcha execute failed"));
          });
        };
        if (typeof grecaptcha !== "undefined" && grecaptcha.enterprise) {
          run();
        } else {
          const s = document.createElement("script");
          s.src = "https://www.google.com/recaptcha/enterprise.js?render=" + siteKey;
          s.onload = run;
          s.onerror = () => finish(reject, "failed to load enterprise.js");
          document.head.appendChild(s);
        }
        setTimeout(() => finish(reject, "timeout minting recaptcha token"), timeoutMs);
      } catch (e) {
        finish(reject, e.message);
      }
    }),
    args: [RECAPTCHA_SITE_KEY, action, timeoutMs]
  });
  if (results && results[0] && results[0].result) return results[0].result;
  throw new Error("empty token result");
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
  const timeoutMs = action === "VIDEO_GENERATION" ? 30000 : 20000;

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
          // drop that owned tab so a fresh one is created (no sticky bad tab).
          const cur = await findUsableLabsTab();
          if (cur != null) {
            try { await chrome.tabs.remove(cur); } catch (_) {}
            await removeOwned(cur);
            persistentTabId = null;
            await chrome.storage.local.remove("persistentTabId");
          }
        }
        tabId = await ensurePersistentTab();
      }

      await paceMint(settings.mintIntervalMs);
      const token = await mintTokenInTab(tabId, action, timeoutMs);
      sendWS({ req_id: data.req_id, status: "success", token }, responseSocket);
      return;
    } catch (e) {
      const msg = String(e && e.message || e);
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
    sendWS({ type: "register", route_key: settings.routeKey, client_label: settings.clientLabel, pool_mode: settings.failedImageMode ? "failed_image" : "auto" }, socket);
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
      // Direct call (NOT tokenQueue): refreshSession's common path is tab-free
      // (cookie read + POST), so it can't conflict with an in-flight mint, and the
      // only tab-reloading path (empty-cookie fallback) is already mint-busy-guarded.
      // Queueing behind a long (video) mint would blow the backend's 30s wait.
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

// Drop an owned tab (best-effort remove + deregister + clear persistent pointer).
async function dropOwnedTab(tabId) {
  try { await chrome.tabs.remove(tabId); } catch (_) {}
  await removeOwned(tabId);
  if (tabId === persistentTabId) {
    persistentTabId = null;
    await chrome.storage.local.remove("persistentTabId");
  }
}

// Time-to-expiry (ms) of the session-token cookie. null = no cookie; 0 = present
// but session-scoped (no expirationDate) -> treat as roll-eligible on cadence so
// the proactive feature never silently no-ops. Mirrors hasSessionCookie's read.
async function sessionCookieTimeToExpiry() {
  let c = await chrome.cookies.get({ url: "https://labs.google", name: SESSION_COOKIE });
  if (!c) {
    const all = await chrome.cookies.getAll({ domain: "labs.google" });
    c = all.find((x) => x.name === SESSION_COOKIE) || null;
  }
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
    try {
      await chrome.tabs.reload(tabId, { bypassCache: false });
    } catch (_) {
      await dropOwnedTab(tabId); // tab vanished mid-reload -> fall through to fresh open
      tabId = null;
    }
    if (tabId != null) {
      await waitForTabComplete(tabId);
      const settled = await getTab(tabId);
      if (!tabOnFlow(settled)) {
        if (isLoginTab(settled)) await setLoginRequired("reload bounced to " + tabUrlOf(settled));
        else await log("WARN", "Reload settled off-Flow (non-login)", { url: tabUrlOf(settled) });
        await dropOwnedTab(tabId);
        return null;
      }
      await sleep(1200); // grecaptcha settle, mirror openLabsTab
      return tabId;
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

async function refreshSession(token_id = null) {
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
    // Read the session-token cookie directly (no extra tab needed).
    let cookie = await chrome.cookies.get({ url: "https://labs.google", name: SESSION_COOKIE });
    if (!cookie) {
      const all = await chrome.cookies.getAll({ domain: "labs.google" });
      cookie = all.find((c) => c.name === SESSION_COOKIE) || null;
    }
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
        cookie = await chrome.cookies.get({ url: "https://labs.google", name: SESSION_COOKIE });
        if (!cookie) {
          const all2 = await chrome.cookies.getAll({ domain: "labs.google" });
          cookie = all2.find((c) => c.name === SESSION_COOKIE) || null;
        }
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

    const resp = await fetch(updateUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": `Bearer ${settings.connectionToken}` },
      body: JSON.stringify(pushBody)
    });
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
    await clearLoginRequired(); // a valid cookie pushed -> session is healthy
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
    const r = await refreshSession(data.token_id);
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

chrome.runtime.onMessage.addListener((req, sender, sendResponse) => {
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
    sendResponse({ connected: !!(ws && ws.readyState === WebSocket.OPEN) });
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
})();
