/* Flow2API Worker — "Keep working when I'm away" (Google cookie sync), 3.7.0.
 *
 * Loaded by background.js via importScripts(), so it shares that file's globals
 * (getSettings, log, refreshSession). Per Chrome profile, default ON: the session push
 * also carries the Google login cookies of THIS profile (exactly the cookies Chrome
 * would send to accounts.google.com), so the Flow2API server can renew the Google
 * Labs session itself while the laptop is closed. Turning the switch OFF sends an
 * explicit empty value and the server deletes its copy at once.
 *
 * Cookie VALUES never appear in the extension log: only counts and names of the
 * long-lived login cookies that were present.
 */

const GOOGLE_ACCOUNTS_URL = "https://accounts.google.com/";
// Any of these present = a login the server's replay can use. Same list as the backend
// (protocol_login.GOOGLE_COOKIE_NAMES); a jar with only __Secure-*PSID is refused there.
const GOOGLE_LOGIN_COOKIE_NAMES = ["SID", "HSID", "SSID", "APISID", "SAPISID"];
// A change to one of these means the login state changed or Google rotated the session:
// re-push (debounced, rate-limited) so the server's copy stays usable.
const GOOGLE_COOKIE_TRIGGER_NAMES = new Set([
  "SID", "HSID", "SSID", "LSID",
  "__Secure-1PSID", "__Secure-3PSID",
  "__Secure-1PSIDTS", "__Secure-3PSIDTS",
]);
const COOKIE_SYNC_MAX_COOKIES = 300;
const COOKIE_SYNC_MAX_BYTES = 256 * 1024;
const COOKIE_SYNC_DEBOUNCE_MS = 30 * 1000;      // Google rewrites several cookies in a burst
const COOKIE_SYNC_MIN_GAP_MS = 10 * 60 * 1000;  // at most one cookie-triggered push per 10 min

// --- pure helpers (unit-tested from Node) -------------------------------------

// Exact host or subdomain of google.com (cookie domains may carry a leading dot).
function googleHostMatches(host) {
  const h = String(host || "").toLowerCase().replace(/^\./, "");
  return h === "google.com" || h.endsWith(".google.com");
}

// Serialise the cookies Chrome would send to accounts.google.com as the JSON list the
// backend's protocol login understands ([{name, value}], sorted by name, first occurrence
// of a name wins). Returns null when no Google login cookie is present (signed out) or
// the export would exceed the caps (a pathological jar is not worth storing).
function serializeGoogleCookies(cookies) {
  const jar = new Map();
  for (const c of (cookies || [])) {
    if (!c || !c.name || !c.value) continue;
    if (c.domain && !googleHostMatches(c.domain)) continue;
    if (!jar.has(c.name)) jar.set(c.name, String(c.value));
  }
  if (!GOOGLE_LOGIN_COOKIE_NAMES.some((n) => jar.has(n))) return null;
  const list = Array.from(jar.entries())
    .sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0))
    .map(([name, value]) => ({ name, value }));
  if (list.length > COOKIE_SYNC_MAX_COOKIES) return null;
  const json = JSON.stringify(list);
  if (json.length > COOKIE_SYNC_MAX_BYTES) return null;
  return { json, count: list.length, loginNames: GOOGLE_LOGIN_COOKIE_NAMES.filter((n) => jar.has(n)) };
}

// Does this cookies.onChanged event warrant a re-push?
function isGoogleLoginCookieChange(changeInfo) {
  const c = changeInfo && changeInfo.cookie;
  if (!c || !c.name) return false;
  if (!GOOGLE_COOKIE_TRIGGER_NAMES.has(c.name)) return false;
  return googleHostMatches(c.domain);
}

// Rate limiter for cookie-triggered pushes: true when a push may go out now.
function cookieSyncPushAllowed(lastPushAt, now, minGapMs = COOKIE_SYNC_MIN_GAP_MS) {
  if (!lastPushAt) return true; // never pushed
  return (now - lastPushAt) >= minGapMs;
}

// The popup's status line, from what the last push proved (never claims more).
function describeCookieSyncState(enabled, state, now = Date.now()) {
  if (!enabled) {
    if (state && state.status === "clearing") return { cls: "warn", text: "Off. Deletion pending — Flow2API has not confirmed yet (retrying every minute)." };
    return { cls: "", text: "Off — Flow2API keeps this account working only while this laptop is open." };
  }
  const st = state || {};
  if (st.status === "stored") {
    const ago = st.at ? Math.max(0, Math.round((now - st.at) / 60000)) : null;
    const when = ago == null ? "" : (ago < 1 ? " just now" : ` ${ago} min ago`);
    return { cls: "ok", text: `✅ Server copy updated${when} (${st.count || 0} cookies). Flow2API can renew this account's session while you are away.` };
  }
  if (st.status === "signed_out") return { cls: "warn", text: "Not signed in to Google in this Chrome. Sign in, then the next push shares the login on its own." };
  if (st.status === "cleared") return { cls: "", text: "Server copy deleted." };
  if (st.status === "clearing") return { cls: "warn", text: "Deletion pending — Flow2API has not confirmed yet (retrying every minute)." };
  if (st.status === "error") return { cls: "err", text: st.message || "The last push failed. It retries every hour." };
  return { cls: "", text: "Waiting for the next session push…" };
}

// --- controller (service worker only) ----------------------------------------

const COOKIE_SYNC_STORE_DEFAULTS = {
  cookieSyncEnabled: true, cookieSyncState: null, cookieSyncLastPushAt: 0,
  cookieSyncPendingClear: null,   // {seq} while an OFF has not been acknowledged by the server
  boundTokenId: null,             // the token row this profile registered as (from the server's reply)
};

function cookieSyncStore() {
  return new Promise((resolve) => chrome.storage.local.get(COOKIE_SYNC_STORE_DEFAULTS, resolve));
}

async function setCookieSyncState(state) {
  await chrome.storage.local.set({ cookieSyncState: state });
}

async function getCookieSyncState() {
  const st = await cookieSyncStore();
  return {
    enabled: st.cookieSyncEnabled !== false, state: st.cookieSyncState, lastPushAt: st.cookieSyncLastPushAt || 0,
    pendingClear: !!st.cookieSyncPendingClear,
  };
}

async function getBoundTokenId() {
  const st = await cookieSyncStore();
  return st.boundTokenId == null ? null : st.boundTokenId;
}

async function rememberBoundTokenId(id) {
  const n = parseInt(id, 10);
  if (!Number.isFinite(n)) return;
  const st = await cookieSyncStore();
  if (st.boundTokenId !== n) await chrome.storage.local.set({ boundTokenId: n });
}

// Can a session push go out WITHOUT a Labs session cookie? Only when away mode is ON
// and a Google login is present for the server to derive the Labs session from.
async function cookieSyncCanCarryPush() {
  const st = await cookieSyncStore();
  if (st.cookieSyncEnabled === false) return false;
  return !!(await exportGoogleCookies());
}

// Read + serialise this profile's Google login cookies. Null = signed out / unusable.
async function exportGoogleCookies() {
  let cookies = [];
  try {
    cookies = await chrome.cookies.getAll({ url: GOOGLE_ACCOUNTS_URL });
  } catch (e) {
    await log("WARN", "Could not read Google cookies", { error: e && e.message });
    return null;
  }
  return serializeGoogleCookies(cookies);
}

// Fields refreshSession() adds to every session push (a server without cookie sync
// stored them blindly, so the backend MUST be deployed before this build).
//   ON  + signed in : google_cookies=<json>, protocol_mode="protocol", cookie_sync_seq
//   ON  + signed out: nothing (the server keeps whatever it has; the popup says why)
//   OFF             : google_cookies="", protocol_mode="session"  (explicit clear)
// `seq` is Date.now() at build time: the server ignores a write older than its last
// one, so a slow ON push can never undo a later OFF.
// Every ON/OFF bumps the generation; a reply to a push built under an older generation
// is ignored (it could report "stored" after a later OFF, or "cleared" for an old OFF).
let cookieSyncGen = 0;
// Sequences are strictly increasing within this worker even when two pushes are built in
// the same millisecond (the server treats an equal sequence as the same write).
let cookieSyncLastSeq = 0;
function nextCookieSyncSeq() {
  cookieSyncLastSeq = Math.max(cookieSyncLastSeq + 1, Date.now());
  return cookieSyncLastSeq;
}

async function cookieSyncPushFields() {
  const st = await cookieSyncStore();
  const seq = nextCookieSyncSeq();
  const gen = cookieSyncGen;
  if (st.cookieSyncEnabled === false) {
    return { fields: { google_cookies: "", protocol_mode: "session" }, seq, gen, count: 0, enabled: false };
  }
  const exported = await exportGoogleCookies();
  if (!exported) {
    await setCookieSyncState({ status: "signed_out", at: Date.now() });
    return { fields: {}, seq, gen, count: 0, enabled: true };
  }
  return { fields: { google_cookies: exported.json, protocol_mode: "protocol" }, seq, gen, count: exported.count, enabled: true, loginNames: exported.loginNames };
}

// Called by refreshSession() with the server's answer (its `cookie_sync` object).
async function noteCookieSyncResult(sent, result) {
  if (!sent) return;
  if (sent.gen !== cookieSyncGen) return; // built before a later ON/OFF: says nothing about now
  if (!sent.enabled) {
    if (result && result.cleared) {
      await chrome.storage.local.set({ cookieSyncPendingClear: null, cookieSyncState: { status: "cleared", at: Date.now() } });
    }
    // `stale` = the server holds a NEWER write; it does not prove deletion, so a pending
    // clear stays pending (the keepalive retry re-sends it with a fresh sequence).
    return;
  }
  if (!sent.count) return; // nothing was sent (signed out); state already says so
  if (result && result.stored) {
    await chrome.storage.local.set({
      cookieSyncState: { status: "stored", at: Date.now(), count: result.cookies || sent.count, derived: !!result.derived_from_cookies },
      cookieSyncLastPushAt: Date.now(),
    });
    await log("INFO", "Google login shared with Flow2API", { cookies: sent.count, login: (sent.loginNames || []).join(","), derived: !!result.derived_from_cookies });
  } else if (result && result.stale) {
    // An OFF landed after this push was built: the server kept the newer state.
  } else {
    await setCookieSyncState({ status: "error", at: Date.now(), message: "Flow2API did not store the login (server too old? it needs the cookie-sync build)" });
  }
}

// OFF must delete the server copy even when the Labs login is broken or Google is down:
// a dedicated clear call that needs no Google round-trip, retried every minute until
// the server acknowledges it.
async function clearCookiesOnServer(seq) {
  const gen = cookieSyncGen;
  const settings = await getSettings();
  const tokenId = await getBoundTokenId();
  if (!settings.serverBase || !settings.connectionToken || tokenId == null) return { ok: false, reason: "unbound" };
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), PUSH_TIMEOUT_MS);
  try {
    const resp = await fetch(`${settings.serverBase}/api/plugin/cookie-sync`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": `Bearer ${settings.connectionToken}` },
      body: JSON.stringify({ action: "clear", token_id: tokenId, cookie_sync_seq: seq, ext_version: extVersion() }),
      signal: abort.signal,
    });
    if (!resp.ok) return { ok: false, reason: `server ${resp.status}` };
    const result = await resp.json();
    if (gen !== cookieSyncGen) return { ok: false, reason: "superseded" }; // ON happened meanwhile
    if (result && result.cookie_sync && result.cookie_sync.cleared) {
      await chrome.storage.local.set({ cookieSyncPendingClear: null, cookieSyncState: { status: "cleared", at: Date.now() } });
      await log("INFO", "Flow2API deleted its copy of the Google login");
      return { ok: true };
    }
    if (result && result.cookie_sync && result.cookie_sync.stale) {
      // A newer write exists on the server: re-send the clear with a fresh sequence next time.
      await chrome.storage.local.set({ cookieSyncPendingClear: { seq: nextCookieSyncSeq() } });
      return { ok: false, reason: "stale" };
    }
    return { ok: false, reason: "not_acknowledged" };
  } catch (e) {
    return { ok: false, reason: e && e.name === "AbortError" ? "timeout" : "network" };
  } finally {
    clearTimeout(timer);
  }
}

async function retryPendingCookieClear() {
  const st = await cookieSyncStore();
  if (!st.cookieSyncPendingClear || st.cookieSyncEnabled !== false) return;
  await clearCookiesOnServer(st.cookieSyncPendingClear.seq);
}

async function cookieSyncSetEnabled(enabled) {
  cookieSyncGen++;
  if (enabled !== false) {
    await chrome.storage.local.set({ cookieSyncEnabled: true, cookieSyncPendingClear: null, cookieSyncState: { status: "syncing", at: Date.now() } });
    await log("INFO", "Keep working when I'm away: ON — sharing the Google login now");
    return refreshSession();
  }
  const seq = nextCookieSyncSeq();
  await chrome.storage.local.set({ cookieSyncEnabled: false, cookieSyncPendingClear: { seq }, cookieSyncState: { status: "clearing", at: Date.now() } });
  await log("INFO", "Keep working when I'm away: OFF — asking Flow2API to delete its copy of the Google login");
  const r = await clearCookiesOnServer(seq);
  if (!r.ok) await setCookieSyncState({ status: "clearing", at: Date.now(), message: r.reason });
  return r;
}

// Google rotated or replaced a login cookie: re-push, debounced and rate-limited.
let cookieSyncTimer = null;
function scheduleCookieSyncPush(reason) {
  if (cookieSyncTimer) clearTimeout(cookieSyncTimer);
  cookieSyncTimer = setTimeout(async () => {
    cookieSyncTimer = null;
    try {
      const st = await cookieSyncStore();
      if (st.cookieSyncEnabled === false) return;
      if (!cookieSyncPushAllowed(st.cookieSyncLastPushAt, Date.now())) return; // hourly push covers it
      await log("INFO", "Google login cookie changed — sharing the new login", { reason });
      await refreshSession();
    } catch (_) {}
  }, COOKIE_SYNC_DEBOUNCE_MS);
}

if (typeof chrome !== "undefined" && chrome.cookies && chrome.cookies.onChanged) {
  chrome.cookies.onChanged.addListener((changeInfo) => {
    if (!isGoogleLoginCookieChange(changeInfo)) return;
    scheduleCookieSyncPush(changeInfo.cause || "cookie_changed");
  });
}

// Node test hook (the service worker has no `module`).
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    googleHostMatches, serializeGoogleCookies, isGoogleLoginCookieChange, cookieSyncPushAllowed,
    describeCookieSyncState, nextCookieSyncSeq, GOOGLE_COOKIE_TRIGGER_NAMES, COOKIE_SYNC_MAX_COOKIES, COOKIE_SYNC_MAX_BYTES,
  };
}
