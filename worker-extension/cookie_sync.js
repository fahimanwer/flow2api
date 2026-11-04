/* Flow2API Worker — Google login sharing (cookie sync), 3.7.0; cookie-first since 3.8.0.
 *
 * Loaded by background.js via importScripts(), so it shares that file's globals
 * (getSettings, log, refreshSession). Per Chrome profile, default ON: every session push
 * carries the Google login cookies of THIS profile (what Chrome sends to accounts.google.com
 * and flow.google.com). Since 2026-10-23 these cookies ARE the account's Flow login on the
 * server (docs/cookie-sync.md): with the switch OFF the account stops working in Flow2API.
 * Turning the switch OFF asks the server to delete its copy at once.
 *
 * Cookie VALUES never appear in the extension log: only counts and names of the
 * long-lived login cookies that were present.
 */

// Chrome's cookies for both hosts make up the export (deduped by name + domain + path).
const GOOGLE_COOKIE_URLS = ["https://accounts.google.com/", "https://flow.google.com/"];
// Any of these present = a login the server's replay can use. Same list as the backend
// (protocol_login.GOOGLE_COOKIE_NAMES); a jar with only __Secure-*PSID is refused there.
const GOOGLE_LOGIN_COOKIE_NAMES = ["SID", "HSID", "SSID", "APISID", "SAPISID"];
// A change to one of these means the login state changed or Google rotated the session:
// re-push (debounced, gap-delayed) so the server's copy stays usable. 3.8.0 adds the
// device-bound (DBSC) names. Deliberately NOT OSID/__Secure-OSID (rewritten on every Flow
// page load) nor *SIDCC (rewritten on almost every Google request).
const GOOGLE_COOKIE_TRIGGER_NAMES = new Set([
  "SID", "HSID", "SSID", "LSID",
  "__Secure-1PSID", "__Secure-3PSID",
  "__Secure-1PSIDTS", "__Secure-3PSIDTS",
  "__Secure-1PSIDRTS", "__Secure-3PSIDRTS", "__Secure-STRP",
]);
const COOKIE_SYNC_MAX_COOKIES = 300;
const COOKIE_SYNC_MAX_BYTES = 256 * 1024;       // = the server's COOKIE_SYNC_MAX_BYTES (src/api/admin.py)
// A cookie change is DELAYED, never dropped (3.7.x dropped every change within 10 min of the
// last push, which left server sessions dead until the hourly push).
const COOKIE_SYNC_DEBOUNCE_MS = 10 * 1000;      // Google rewrites several cookies in a burst
const COOKIE_SYNC_MAX_WAIT_MS = 60 * 1000;      // ... but a burst never postpones the push longer than this
const COOKIE_SYNC_MIN_GAP_MS = 90 * 1000;       // and it never goes out sooner than this after the last push
const COOKIE_SYNC_MAX_TRIES = 3;                // a push that did not get through is retried (gap doubling), then left to the periodic push
const ALARM_COOKIE_PUSH = "flow2api_cookie_push"; // chrome.alarms survive the service worker sleeping; setTimeout did not

// --- pure helpers (unit-tested from Node) -------------------------------------

// Exact host or subdomain of google.com (cookie domains may carry a leading dot).
function googleHostMatches(host) {
  const h = String(host || "").toLowerCase().replace(/^\./, "");
  return h === "google.com" || h.endsWith(".google.com");
}

// Serialise this profile's Google cookies as the JSON list the backend stores (3.8.0: each item
// keeps where Chrome holds it — {name, value, domain, path, hostOnly, secure, httpOnly, sameSite,
// expirationDate} — so the server can put every cookie on the right host). Deduped by
// name + domain + path (first occurrence wins), sorted; partitioned, expired and non-google.com
// cookies are skipped. Returns null when no Google login cookie is present (signed out) or the
// export would exceed the caps (a pathological jar is not worth storing).
function serializeGoogleCookies(cookies, now = Date.now()) {
  const jar = new Map();
  for (const c of (cookies || [])) {
    if (!c || !c.name || !c.value) continue;
    if (!googleHostMatches(c.domain)) continue;
    if (c.partitionKey) continue;                                        // CHIPS: belongs to a top-level site, not the login
    if (c.expirationDate && c.expirationDate * 1000 <= now) continue;    // expired
    const path = c.path || "/";
    const key = `${c.name}\n${String(c.domain).toLowerCase()}\n${path}`;
    if (jar.has(key)) continue;
    const item = {
      name: c.name, value: String(c.value), domain: c.domain, path,
      hostOnly: !!c.hostOnly, secure: !!c.secure, httpOnly: !!c.httpOnly, sameSite: c.sameSite || "unspecified",
    };
    if (c.expirationDate) item.expirationDate = c.expirationDate;      // absent = session cookie
    jar.set(key, item);
  }
  const list = Array.from(jar.values());
  const names = new Set(list.map((i) => i.name));
  if (!GOOGLE_LOGIN_COOKIE_NAMES.some((n) => names.has(n))) return null;
  const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
  list.sort((a, b) => cmp(a.name, b.name) || cmp(a.domain, b.domain) || cmp(a.path, b.path));
  if (list.length > COOKIE_SYNC_MAX_COOKIES) return null;
  const json = JSON.stringify(list);
  if (json.length > COOKIE_SYNC_MAX_BYTES) return null;
  return { json, count: list.length, loginNames: GOOGLE_LOGIN_COOKIE_NAMES.filter((n) => names.has(n)) };
}

// Does this cookies.onChanged event warrant a re-push?
function isGoogleLoginCookieChange(changeInfo) {
  const c = changeInfo && changeInfo.cookie;
  if (!c || !c.name) return false;
  if (!GOOGLE_COOKIE_TRIGGER_NAMES.has(c.name)) return false;
  return googleHostMatches(c.domain);
}

// When the owed cookie-triggered push goes out (ms epoch; never earlier than `now`). Debounced:
// it waits for DEBOUNCE of quiet after the latest change, at most MAX_WAIT after the first one;
// and never sooner than MIN_GAP after the last push attempt (doubled per failed try).
// pending = {since, lastChangeAt, tries}.
function cookieSyncPushDueAt(pending, lastAttemptAt, now) {
  const quiet = Math.min(pending.lastChangeAt + COOKIE_SYNC_DEBOUNCE_MS, pending.since + COOKIE_SYNC_MAX_WAIT_MS);
  const gap = lastAttemptAt ? lastAttemptAt + COOKIE_SYNC_MIN_GAP_MS * Math.pow(2, pending.tries || 0) : 0;
  return Math.max(quiet, gap, now);
}

// The owed push after a push attempt that read the cookies at `builtAt`. Still owed when the cookies
// changed again after that read; settled when the server answered (any verdict) or the push was not
// sendable (signed out, OFF); retried after a transport failure (network / timeout / server 5xx),
// at most MAX_TRIES times, then the periodic push takes over. Returns the new pending or null.
function cookieSyncPendingAfterPush(pending, builtAt, delivered) {
  if (!pending) return null;
  if (pending.lastChangeAt > builtAt) return pending;
  if (delivered) return null;
  const tries = (pending.tries || 0) + 1;
  return tries >= COOKIE_SYNC_MAX_TRIES ? null : { ...pending, tries };
}

// What the server's answer to a session push means. `reason` is the status the server's
// refresh_session command reads (browser_captcha_extension.py): refreshed / relogin_required /
// account_mismatch / logged_out / network.
function describePushAnswer(httpStatus, data) {
  const d = data || {};
  const detail = String(d.detail || d.message || "").slice(0, 200);
  if (httpStatus >= 200 && httpStatus < 300) {
    if (d.action === "relogin_required") return { reason: "relogin_required", verified: false, detail };
    return { reason: "refreshed", verified: d.credential_verified === true, detail };
  }
  // 400 = the server could not sign in to Flow with these cookies (a verdict, not a blip).
  if (httpStatus === 400) return { reason: "logged_out", rejected: true, detail };
  if (httpStatus === 409) return { reason: "account_mismatch", mismatch: /account mismatch/i.test(detail), detail };
  return { reason: "network", detail: detail || `server ${httpStatus}` };
}

// A push bound to a remembered token id is refused for good when that row is gone (404) or now
// belongs to another account (409 account mismatch: the profile switched Google accounts). Then the
// remembered id is forgotten and the push is retried once by email.
function shouldRetryUnbound(httpStatus, answer, usedBoundId) {
  if (!usedBoundId) return false;
  return httpStatus === 404 || (httpStatus === 409 && !!(answer && answer.mismatch));
}

// --- controller (service worker only) ----------------------------------------

const COOKIE_SYNC_STORE_DEFAULTS = {
  cookieSyncEnabled: true, cookieSyncState: null, cookieSyncLastPushAt: 0,
  cookieSyncPendingClear: null,   // {seq} while an OFF has not been acknowledged by the server
  cookieSyncPendingPush: null,    // {since, lastChangeAt, tries} while a cookie change is not pushed yet (3.8.0)
  cookieSyncLastAttemptAt: 0,     // when the last push was sent (any trigger): the gap is measured from it
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

async function forgetBoundTokenId() {
  await chrome.storage.local.set({ boundTokenId: null });
}

// Read + serialise this profile's Google login cookies. Null = signed out / unusable.
async function exportGoogleCookies() {
  let cookies = [];
  try {
    for (const url of GOOGLE_COOKIE_URLS) cookies = cookies.concat(await chrome.cookies.getAll({ url }));
  } catch (e) {
    await log("WARN", "Could not read Google cookies", { error: e && e.message });
    return null;
  }
  return serializeGoogleCookies(cookies);
}

// Fields refreshSession() adds to every session push:
//   ON  + signed in : google_cookies=<json>, protocol_mode="protocol", cookie_sync_seq
//   ON  + signed out: nothing (no push goes out; the popup says why)
//   OFF             : no push at all (3.8.0) — the OFF switch deletes the server copy itself
// `seq` is Date.now() at build time: the server ignores a write older than its last
// one, so a slow ON push can never undo a later OFF.
// Every ON/OFF bumps the generation; a reply to a push built under an older generation
// is ignored (it could report "stored" after a later OFF).
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
  if (st.cookieSyncEnabled === false) return { fields: {}, seq, gen, count: 0, enabled: false };
  const exported = await exportGoogleCookies();
  if (!exported) {
    await setCookieSyncState({ status: "signed_out", at: Date.now() });
    return { fields: {}, seq, gen, count: 0, enabled: true };
  }
  return { fields: { google_cookies: exported.json, protocol_mode: "protocol" }, seq, gen, count: exported.count, enabled: true, loginNames: exported.loginNames };
}

// Called by refreshSession() with the server's 2xx answer (its `cookie_sync` object).
async function noteCookieSyncResult(sent, result) {
  if (!sent || !sent.enabled || !sent.count) return;
  if (sent.gen !== cookieSyncGen) return; // built before a later ON/OFF: says nothing about now
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

// Called by refreshSession() when the server refused or did not answer a push (3.8.0): the popup
// must never keep saying "Server copy updated" after a push failed.
async function noteCookieSyncFailure(sent, answer) {
  if (!sent || sent.gen !== cookieSyncGen) return;
  const at = Date.now();
  if (answer.rejected) await setCookieSyncState({ status: "rejected", at, message: answer.detail });
  else if (answer.reason === "account_mismatch") await setCookieSyncState({ status: "error", at, message: `Flow2API refused this push (${answer.detail || "account mismatch"}).` });
  else await setCookieSyncState({ status: "retry", at, message: answer.detail });
}

// Body of the clear call. 3.7.5: carries this profile's route key, so a server that keeps an
// account for one server browser (Ultra browsers) can refuse a clear from any other profile;
// accounts not kept that way are cleared exactly as before.
function buildCookieClearBody(tokenId, seq, routeKey, version) {
  const body = { action: "clear", token_id: tokenId, cookie_sync_seq: seq, ext_version: version };
  const rk = String(routeKey || "").trim();
  if (rk) body.route_key = rk;
  return body;
}

// OFF must delete the server copy even when Google is down: a dedicated clear call that
// needs no Google round-trip, retried every minute until the server acknowledges it.
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
      body: JSON.stringify(buildCookieClearBody(tokenId, seq, settings.routeKey, extVersion())),
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
    await log("INFO", "Use this account in Flow2API: ON — sharing the Google login now");
    return refreshSession();
  }
  const seq = nextCookieSyncSeq();
  await chrome.storage.local.set({ cookieSyncEnabled: false, cookieSyncPendingClear: { seq }, cookieSyncPendingPush: null, cookieSyncState: { status: "clearing", at: Date.now() } });
  // The account is stopped on purpose: no "sign in" nagging (red badge) while it is OFF. ON pushes
  // again and sets these states from the server's answer.
  await chrome.storage.local.remove(["authState", "grantExpired"]);
  await refreshBadge();
  await log("INFO", "Stop using this account in Flow2API — asking Flow2API to delete its copy of the Google login");
  const r = await clearCookiesOnServer(seq);
  if (!r.ok) await setCookieSyncState({ status: "clearing", at: Date.now(), message: r.reason });
  return r;
}

// --- cookie-triggered push: delayed (chrome.alarms), never dropped (3.8.0) ---

// Read-modify-writes of the owed push are serialized, so a cookie change landing while a push
// settles can never be lost.
let pendingPushQueue = Promise.resolve();
function mutatePendingPush(fn) {
  pendingPushQueue = pendingPushQueue.then(async () => {
    const st = await cookieSyncStore();
    return fn(st);
  }).catch(() => {});
  return pendingPushQueue;
}

function armCookiePushAlarm(when) {
  try { chrome.alarms.create(ALARM_COOKIE_PUSH, { when }); } catch (_) {}
}

// Google rotated or replaced a login cookie: owe a push and arm the alarm for it.
function noteGoogleCookieChange(reason) {
  return mutatePendingPush(async (st) => {
    if (st.cookieSyncEnabled === false) return;
    const now = Date.now();
    const pending = st.cookieSyncPendingPush
      ? { ...st.cookieSyncPendingPush, lastChangeAt: now }
      : { since: now, lastChangeAt: now, tries: 0, reason: reason || "cookie_changed" };
    await chrome.storage.local.set({ cookieSyncPendingPush: pending });
    armCookiePushAlarm(cookieSyncPushDueAt(pending, st.cookieSyncLastAttemptAt, now));
  });
}

// refreshSession() calls this right before it sends a push (any trigger).
async function noteCookiePushAttempt() {
  await chrome.storage.local.set({ cookieSyncLastAttemptAt: Date.now() });
}

// refreshSession() calls this after every attempt; builtAt = when it started reading the cookies.
function settlePendingCookiePush(builtAt, delivered) {
  return mutatePendingPush(async (st) => {
    const before = st.cookieSyncPendingPush;
    if (!before) return;
    const next = cookieSyncPendingAfterPush(before, builtAt, delivered);
    if (next === before) return;   // changed again meanwhile: its alarm is already armed
    await chrome.storage.local.set({ cookieSyncPendingPush: next });
    if (next) armCookiePushAlarm(cookieSyncPushDueAt(next, st.cookieSyncLastAttemptAt, Date.now()));
    else if (!delivered) await log("WARN", "Cookie push did not get through after retries; the periodic push carries it");
  });
}

// The alarm (or the 1-minute keep-alive, as a backstop) runs the owed push once it is due.
let cookiePushRunning = false;
async function runPendingCookiePush() {
  // Claimed before the first await: the alarm and the keep-alive can both arrive here at once.
  if (cookiePushRunning) return;
  cookiePushRunning = true;
  try {
    const st = await cookieSyncStore();
    const pending = st.cookieSyncPendingPush;
    if (!pending) return;
    if (st.cookieSyncEnabled === false) { await chrome.storage.local.set({ cookieSyncPendingPush: null }); return; }
    const now = Date.now();
    const due = cookieSyncPushDueAt(pending, st.cookieSyncLastAttemptAt, now);
    if (due > now + 1000) { armCookiePushAlarm(due); return; }
    await log("INFO", "Google login cookie changed — sharing the new login", { reason: pending.reason, waitedS: Math.round((now - pending.since) / 1000), tries: pending.tries || 0 });
    await refreshSession();
  } catch (_) {
  } finally {
    cookiePushRunning = false;
  }
}

// Listeners only in the service worker (the popup loads none of this file).
if (typeof chrome !== "undefined" && chrome.cookies && chrome.cookies.onChanged) {
  chrome.cookies.onChanged.addListener((changeInfo) => {
    if (!isGoogleLoginCookieChange(changeInfo)) return;
    noteGoogleCookieChange(changeInfo.cause || "cookie_changed");
  });
}
if (typeof chrome !== "undefined" && chrome.alarms && chrome.alarms.onAlarm) {
  chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === ALARM_COOKIE_PUSH) runPendingCookiePush().catch(() => {});
  });
}

// Node test hook (the service worker has no `module`).
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    googleHostMatches, serializeGoogleCookies, isGoogleLoginCookieChange, cookieSyncPushDueAt, cookieSyncPendingAfterPush,
    describePushAnswer, shouldRetryUnbound, nextCookieSyncSeq, GOOGLE_COOKIE_TRIGGER_NAMES, COOKIE_SYNC_MAX_COOKIES,
    COOKIE_SYNC_MAX_BYTES, COOKIE_SYNC_DEBOUNCE_MS, COOKIE_SYNC_MAX_WAIT_MS, COOKIE_SYNC_MIN_GAP_MS, COOKIE_SYNC_MAX_TRIES,
    buildCookieClearBody,
  };
}
