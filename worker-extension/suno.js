/* Flow2API Worker — optional Suno login export.
 *
 * Loaded by background.js via importScripts(), so it shares that file's globals
 * (getSettings, log, extVersion, PUSH_TIMEOUT_MS). Opt-in per Chrome profile: when
 * the person switches "Suno music" ON in the popup, this module reads the Suno
 * login cookie their own browsing created and POSTs it to the backend, then keeps
 * it current whenever the cookie changes. It never opens Suno tabs and it never
 * touches the Flow captcha socket or the per-profile proxy.
 *
 * Only a Cookie header string leaves the browser, to /api/plugin/suno-cookie,
 * authenticated with the same plugin connection token that already pushes the
 * Google Flow cookie. The backend keys the account by the Suno user the cookie
 * proves; it never returns credentials.
 */

const SUNO_SITE_URL = "https://suno.com/";
const SUNO_AUTH_URL = "https://auth.suno.com/";     // Clerk frontend API; holds __client
const SUNO_CLIENT_COOKIE = "__client";
const SUNO_DEBOUNCE_MS = 5000;                       // a sign-in writes several cookies in a burst
const SUNO_RESYNC_MS = 6 * 60 * 60 * 1000;           // re-push an unchanged cookie at most every 6 h

// --- pure helpers (unit-tested from Node) -------------------------------------

// Exact domain or subdomain of suno.com. Cookie domains may carry a leading dot.
function sunoHostMatches(host) {
  const h = String(host || "").toLowerCase().replace(/^\./, "");
  return h === "suno.com" || h.endsWith(".suno.com");
}

// Build the Cookie header the backend expects. `authCookies` are the cookies Chrome
// would send to auth.suno.com (that is where the Clerk credential lives), `siteCookies`
// the ones for suno.com. First occurrence of a name wins, so the credential is always
// the one Clerk itself would have received. Returns null when no __client is present.
function mergeSunoCookies(authCookies, siteCookies) {
  const jar = new Map();
  for (const list of [authCookies, siteCookies]) {
    for (const c of (list || [])) {
      if (c && c.name && c.value && !jar.has(c.name)) jar.set(c.name, c.value);
    }
  }
  const clientValue = jar.get(SUNO_CLIENT_COOKIE) || "";
  if (!clientValue) return null;
  const header = Array.from(jar.entries()).map(([k, v]) => `${k}=${v}`).join("; ");
  return { header, clientValue };
}

// The backend's public account projection, trimmed to what the popup shows.
function pickSunoAccount(account) {
  const a = account || {};
  return {
    id: a.id,
    display_name: a.display_name || null,
    status: a.status || null,
    operator_disabled: !!a.operator_disabled,
    plan: a.plan || null,
    credits: (typeof a.credits === "number") ? a.credits : null,
    credits_checked_at: a.credits_checked_at || null
  };
}

// --- controller ---------------------------------------------------------------
// One generation counter guards against stale work: every ON/OFF bumps it, and a
// push that started under an older generation neither sends nor records anything.

let sunoGen = 0;
let sunoTimer = null;                   // pending debounced sync
let sunoAbort = null;                   // AbortController of the in-flight push
let sunoChain = Promise.resolve();      // serializes pushes

const SUNO_STORE_DEFAULTS = { sunoEnabled: false, sunoState: null, sunoLastClient: "", sunoLastPushAt: 0 };

function sunoStore() {
  return new Promise((resolve) => chrome.storage.local.get(SUNO_STORE_DEFAULTS, resolve));
}

async function setSunoState(state) {
  await chrome.storage.local.set({ sunoState: state });
}

async function getSunoState() {
  const st = await sunoStore();
  return { enabled: st.sunoEnabled === true, state: st.sunoState, lastPushAt: st.sunoLastPushAt || 0 };
}

function cancelPendingSuno() {
  if (sunoTimer) { clearTimeout(sunoTimer); sunoTimer = null; }
  if (sunoAbort) { try { sunoAbort.abort(); } catch (_) {} sunoAbort = null; }
}

async function sunoSetEnabled(enabled) {
  sunoGen++;
  cancelPendingSuno();
  if (enabled) {
    await chrome.storage.local.set({ sunoEnabled: true, sunoState: { status: "syncing", at: Date.now() } });
    await log("INFO", "Suno login sharing turned ON");
    return sunoSync("toggle_on", { force: true });
  }
  // OFF stops future syncing only. The account already saved on the backend stays
  // until an admin removes it; the popup copy says so.
  await chrome.storage.local.set({ sunoEnabled: false, sunoState: null, sunoLastClient: "", sunoLastPushAt: 0 });
  await log("INFO", "Suno login sharing turned OFF (backend account kept)");
  return { ok: true, reason: "disabled" };
}

// Debounced entry point used by cookie events and boot. Re-reads the real cookie
// state when it fires, so an overwrite (removal + insert) never decides anything.
function scheduleSunoSync(reason, delayMs = SUNO_DEBOUNCE_MS) {
  if (sunoTimer) clearTimeout(sunoTimer);
  sunoTimer = setTimeout(() => {
    sunoTimer = null;
    sunoSync(reason).catch(() => {});
  }, delayMs);
}

function sunoSync(reason, opts = {}) {
  // Capture the generation NOW, not when the queued work starts, so an operation
  // queued before an OFF/ON flip cannot inherit the newer generation.
  const gen = sunoGen;
  sunoChain = sunoChain
    .then(() => _sunoSync(reason, Object.assign({ gen }, opts)))
    .catch((e) => ({ ok: false, reason: "error", error: String((e && e.message) || e) }));
  return sunoChain;
}

async function _sunoSync(reason, { force = false, gen = sunoGen } = {}) {
  const st = await sunoStore();
  if (st.sunoEnabled !== true) return { ok: false, reason: "disabled" };

  const settings = await getSettings();
  if (!settings.serverBase || !settings.connectionToken) {
    await setSunoState({ status: "error", message: "Worker is not configured (server / connection token).", at: Date.now() });
    return { ok: false, reason: "not_configured" };
  }

  let authCookies = [], siteCookies = [];
  try {
    authCookies = await chrome.cookies.getAll({ url: SUNO_AUTH_URL });
    siteCookies = await chrome.cookies.getAll({ url: SUNO_SITE_URL });
  } catch (e) {
    if (gen !== sunoGen) return { ok: false, reason: "superseded" };
    await setSunoState({ status: "error", message: "Could not read Suno cookies: " + e.message, at: Date.now() });
    return { ok: false, reason: "cookie_read_failed" };
  }
  if (gen !== sunoGen) return { ok: false, reason: "superseded" };

  const merged = mergeSunoCookies(authCookies, siteCookies);
  if (!merged) {
    // Not signed in (or signed out). Nothing is sent; the person signs in and the
    // cookie event brings us back here.
    await setSunoState({ status: "signed_out", at: Date.now() });
    return { ok: false, reason: "signed_out" };
  }

  const unchanged = st.sunoLastClient === merged.clientValue;
  const recent = (Date.now() - (st.sunoLastPushAt || 0)) < SUNO_RESYNC_MS;
  const healthy = !!(st.sunoState && st.sunoState.status === "connected");
  // Never suppress a retry while the last known state is an error: only a healthy,
  // recently pushed, unchanged cookie is skipped.
  if (!force && unchanged && recent && healthy) {
    // Same cookie, pushed recently: nothing to do. The 6 h bound guarantees the
    // backend still hears from us even if no cookie event ever fires again.
    return { ok: true, reason: "fresh" };
  }

  await setSunoState(Object.assign({}, st.sunoState || {}, { status: "syncing", at: Date.now() }));
  // OFF may have flipped during that write, before any request exists to abort.
  if (gen !== sunoGen) return { ok: false, reason: "superseded" };

  const base = new URL(settings.serverBase);
  const url = `${base.protocol}//${base.host}/api/plugin/suno-cookie`;
  const body = { cookie: merged.header, ext_version: extVersion(), force: !!force };
  if (settings.clientLabel) body.display_name = settings.clientLabel;

  const abort = new AbortController();
  sunoAbort = abort;
  const timer = setTimeout(() => abort.abort(), PUSH_TIMEOUT_MS);
  let resp, text = "";
  try {
    resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": `Bearer ${settings.connectionToken}` },
      body: JSON.stringify(body),
      signal: abort.signal
    });
    text = await resp.text();
  } catch (e) {
    clearTimeout(timer);
    if (sunoAbort === abort) sunoAbort = null;
    if (gen !== sunoGen) return { ok: false, reason: "superseded" };
    const message = e && e.name === "AbortError" ? "Timed out talking to Flow2API." : ("Network error: " + (e && e.message));
    await setSunoState({ status: "error", message, at: Date.now() });
    await log("ERROR", "Suno login push failed", { reason, error: message });
    return { ok: false, reason: "network" };
  }
  clearTimeout(timer);
  if (sunoAbort === abort) sunoAbort = null;
  // OFF happened while the request was in flight: the backend may well have saved it
  // (that is fine and documented), but this profile no longer records anything.
  if (gen !== sunoGen) return { ok: false, reason: "superseded" };

  let data = null;
  try { data = JSON.parse(text); } catch (_) {}

  if (resp.ok) {
    const account = pickSunoAccount(data && data.account);
    await chrome.storage.local.set({
      sunoLastClient: merged.clientValue,
      sunoLastPushAt: Date.now(),
      sunoState: { status: "connected", action: (data && data.action) || "ok", account, at: Date.now() }
    });
    await log("SUCCESS", "Suno login shared with Flow2API", { reason, action: data && data.action, account: account.display_name || account.id });
    return { ok: true, reason: (data && data.action) || "ok" };
  }

  const err = (data && data.detail && data.detail.error) || (data && data.error) || {};
  const code = err.code || "";
  const detail = err.message || (typeof (data && data.detail) === "string" ? data.detail : "") || text.slice(0, 160);
  let state;
  if (resp.status === 400 && code === "cookie_rejected") {
    // Suno refused the cookie. That usually means the browser login is stale, but the
    // client classifies every upstream 401/403 this way, so do not claim "signed out".
    state = { status: "unverified", message: "Flow2API couldn't verify this Suno login. Sign out of suno.com, sign back in, then press 'Sync Suno now'.", at: Date.now() };
  } else if (resp.status === 401) {
    state = { status: "error", message: "Flow2API refused this worker's connection token. Ask the admin to check the worker setup.", at: Date.now() };
  } else if (resp.status === 400 && code === "no_identity") {
    state = { status: "error", message: "Suno did not report who this login belongs to. Use suno.com once, then press 'Sync Suno now'.", at: Date.now() };
  } else {
    state = { status: "error", message: `Flow2API error ${resp.status}: ${detail}`, at: Date.now() };
  }
  await setSunoState(state);
  await log("ERROR", "Suno login push rejected", { reason, status: resp.status, code, detail: String(detail).slice(0, 120) });
  return { ok: false, reason: code || `http_${resp.status}` };
}

// Cookie events. Chrome reports an overwrite as a removal followed by an insert, so
// no single event is trusted: any change to __client on a suno.com host schedules a
// debounced re-read, and the re-read decides signed-in vs signed-out.
if (typeof chrome !== "undefined" && chrome.cookies && chrome.cookies.onChanged) {
  chrome.cookies.onChanged.addListener((info) => {
    const c = info && info.cookie;
    if (!c || c.name !== SUNO_CLIENT_COOKIE || !sunoHostMatches(c.domain)) return;
    chrome.storage.local.get({ sunoEnabled: false }, ({ sunoEnabled }) => {
      if (sunoEnabled === true) scheduleSunoSync("cookie_changed");
    });
  });
}

// Node test hook (the service worker has no `module`).
if (typeof module !== "undefined" && module.exports) {
  module.exports = { sunoHostMatches, mergeSunoCookies, pickSunoAccount };
}
