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
 */

const RECAPTCHA_SITE_KEY = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV";
const LABS_URL = "https://labs.google/fx/tools/flow";
const SESSION_COOKIE = "__Secure-next-auth.session-token";

const ALARM_SESSION = "flow2api_session_refresh";
const ALARM_KEEPALIVE = "flow2api_keepalive";

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
  mintIntervalMs: 2000    // min spacing between reCAPTCHA mints; paces one browser under Google's rate limit
};

let ws = null;
let lastMintAt = 0;       // timestamp of the last reCAPTCHA mint (for pacing)
let heartbeatInterval = null;
let reconnectTimer = null;
let persistentTabId = null;
let tokenQueue = Promise.resolve();   // serialize token requests
let connecting = false;

/* ----------------------------- settings ----------------------------- */

function getSettings() {
  return new Promise((resolve) => {
    chrome.storage.local.get(DEFAULT_SETTINGS, (stored) => {
      resolve({
        serverBase: (stored.serverBase || DEFAULT_SETTINGS.serverBase).trim().replace(/\/+$/, ""),
        apiKey: (stored.apiKey || "").trim(),
        connectionToken: (stored.connectionToken || "").trim(),
        routeKey: (stored.routeKey || "").trim(),
        clientLabel: (stored.clientLabel || "").trim(),
        refreshIntervalMinutes: Math.max(5, parseInt(stored.refreshIntervalMinutes, 10) || 60),
        tabMode: stored.tabMode === "ephemeral" ? "ephemeral" : "persistent",
        mintIntervalMs: Math.max(0, parseInt(stored.mintIntervalMs, 10) || 2000)
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

/* ------------------------------- logging ----------------------------- */

async function log(level, message, details) {
  const entry = { ts: new Date().toISOString(), level, message, details: details || null };
  console.log(`[Flow2API ${level}] ${message}`, details || "");
  const { logs = [] } = await chrome.storage.local.get(["logs"]);
  logs.unshift(entry);
  if (logs.length > 80) logs.splice(80);
  await chrome.storage.local.set({ logs });
}

/* --------------------------- persistent tab -------------------------- */

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

// Space out reCAPTCHA mints so a single browser stays under Google's rate limit
// (avoids PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC under burst load).
async function paceMint(intervalMs) {
  if (!intervalMs) return;
  const wait = lastMintAt + intervalMs - Date.now();
  if (wait > 0) await sleep(wait);
  lastMintAt = Date.now();
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

function tabExists(tabId) {
  return new Promise((resolve) => {
    if (tabId == null) { resolve(false); return; }
    chrome.tabs.get(tabId, (tab) => {
      resolve(!chrome.runtime.lastError && !!tab);
    });
  });
}

// Create a fresh hidden Labs tab and return its id (ready + settled).
async function createLabsTab() {
  const tab = await chrome.tabs.create({ url: LABS_URL, active: false });
  await waitForTabComplete(tab.id);
  await sleep(1200); // let grecaptcha settle
  return tab.id;
}

// Ensure a persistent tab exists; returns its id.
async function ensurePersistentTab() {
  if (await tabExists(persistentTabId)) return persistentTabId;
  persistentTabId = await createLabsTab();
  await log("INFO", "Persistent Labs tab opened", { tabId: persistentTabId });
  return persistentTabId;
}

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

async function handleGetToken(data, settings) {
  const action = data.action || "IMAGE_GENERATION";
  const timeoutMs = action === "VIDEO_GENERATION" ? 30000 : 20000;

  // Try up to twice: a stale persistent tab is recreated on the second attempt.
  for (let attempt = 1; attempt <= 2; attempt++) {
    let ephemeralTabId = null;
    try {
      let tabId;
      if (settings.tabMode === "ephemeral") {
        ephemeralTabId = await createLabsTab();
        tabId = ephemeralTabId;
      } else {
        if (attempt === 2) { persistentTabId = null; } // force recreate on retry
        tabId = await ensurePersistentTab();
      }

      await paceMint(settings.mintIntervalMs);
      const token = await mintTokenInTab(tabId, action, timeoutMs);
      sendWS({ req_id: data.req_id, status: "success", token });
      return;
    } catch (e) {
      await log("ERROR", `token attempt ${attempt} failed`, { error: String(e && e.message || e) });
      if (attempt === 2) {
        sendWS({ req_id: data.req_id, status: "error", error: "worker failed: " + String(e && e.message || e) });
      }
    } finally {
      if (ephemeralTabId != null) {
        try { await chrome.tabs.remove(ephemeralTabId); } catch (_) {}
      }
    }
  }
}

/* ------------------------------ WebSocket ---------------------------- */

function sendWS(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    try { ws.send(JSON.stringify(obj)); } catch (_) {}
  }
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

  try {
    ws = new WebSocket(url.toString());
  } catch (e) {
    connecting = false;
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    connecting = false;
    log("SUCCESS", "Captcha WebSocket connected", { routeKey: settings.routeKey || "(empty)" });
    sendWS({ type: "register", route_key: settings.routeKey, client_label: settings.clientLabel });
    if (heartbeatInterval) clearInterval(heartbeatInterval);
    heartbeatInterval = setInterval(() => sendWS({ type: "ping" }), 20000);
    // Warm the persistent tab so the first real request is fast.
    if (settings.tabMode === "persistent") ensurePersistentTab().catch(() => {});
  };

  ws.onmessage = (event) => {
    let data;
    try { data = JSON.parse(event.data); } catch (_) { return; }
    if (data.type === "register_ack") return;
    if (data.type === "get_token") {
      tokenQueue = tokenQueue.then(() => handleGetToken(data, settings)).catch(() => {});
    }
  };

  ws.onclose = () => {
    connecting = false;
    if (heartbeatInterval) clearInterval(heartbeatInterval);
    ws = null;
    scheduleReconnect();
  };

  ws.onerror = () => { try { ws.close(); } catch (_) {} };
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

async function refreshSession() {
  const settings = await getSettings();
  if (!settings.serverBase || !settings.connectionToken) {
    await log("INFO", "Session refresh skipped (need Server URL + Connection Token)");
    return { success: false, error: "not configured" };
  }
  const { updateUrl } = deriveUrls(settings);

  try {
    // Make sure a Labs tab is loaded so the session cookie is fresh/active.
    if (settings.tabMode === "persistent") {
      await ensurePersistentTab();
    }
    // Read the session-token cookie directly (no extra tab needed).
    let cookie = await chrome.cookies.get({ url: "https://labs.google", name: SESSION_COOKIE });
    if (!cookie) {
      const all = await chrome.cookies.getAll({ domain: "labs.google" });
      cookie = all.find((c) => c.name === SESSION_COOKIE) || null;
    }
    if (!cookie || !cookie.value) {
      await log("ERROR", "session-token cookie not found — are you logged into Google Labs?");
      return { success: false, error: "session-token not found (log into Google Labs)" };
    }

    const resp = await fetch(updateUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": `Bearer ${settings.connectionToken}` },
      body: JSON.stringify({ session_token: cookie.value })
    });
    if (!resp.ok) {
      const txt = await resp.text();
      await log("ERROR", "Session push failed", { status: resp.status, body: txt.slice(0, 200) });
      return { success: false, error: `server ${resp.status}` };
    }
    const result = await resp.json();
    await log("SUCCESS", "Session token pushed to Flow2API", { action: result.action, message: result.message });
    return { success: true, message: result.message, action: result.action };
  } catch (e) {
    await log("ERROR", "Session refresh error", { error: e.message });
    return { success: false, error: e.message };
  }
}

/* ------------------------------- alarms ------------------------------ */

async function setupAlarms() {
  const settings = await getSettings();
  await chrome.alarms.clear(ALARM_SESSION);
  await chrome.alarms.clear(ALARM_KEEPALIVE);
  chrome.alarms.create(ALARM_SESSION, { periodInMinutes: settings.refreshIntervalMinutes, delayInMinutes: 0.1 });
  chrome.alarms.create(ALARM_KEEPALIVE, { periodInMinutes: 1 }); // revive SW/socket/tab
}

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === ALARM_SESSION) {
    await refreshSession();
  } else if (alarm.name === ALARM_KEEPALIVE) {
    connectWS();
    const settings = await getSettings();
    if (settings.tabMode === "persistent" && ws && ws.readyState === WebSocket.OPEN) {
      ensurePersistentTab().catch(() => {});
    }
  }
});

/* ------------------------------ lifecycle ---------------------------- */

chrome.tabs.onRemoved.addListener((tabId) => {
  if (tabId === persistentTabId) persistentTabId = null;
});

chrome.runtime.onInstalled.addListener(async () => {
  await log("INFO", "Flow2API Worker installed");
  await setupAlarms();
  connectWS();
  // Kick off an immediate session push so the backend is valid right away.
  refreshSession().catch(() => {});
});

chrome.runtime.onStartup.addListener(async () => {
  await setupAlarms();
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
    setupAlarms();
    closeSocket(); connectWS();
    sendResponse({ ok: true });
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

// Boot (covers the service-worker waking up).
setupAlarms();
connectWS();
