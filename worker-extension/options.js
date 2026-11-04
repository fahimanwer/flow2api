// Staff popup: status-only. Everything (server, key, token, residential proxy) is
// baked in and automatic — there is intentionally nothing to configure here.
const $ = (id) => document.getElementById(id);

function renderLogs() {
  chrome.runtime.sendMessage({ action: "getLogs" }, (resp) => {
    if (chrome.runtime.lastError || !resp || !resp.logs) return;
    const box = $("logs");
    if (!box) return;
    box.innerHTML = resp.logs.slice(0, 25).map((l) => {
      const t = (l.ts || "").slice(11, 19);
      const d = l.details ? " " + JSON.stringify(l.details).slice(0, 120) : "";
      return `<div class="l-${l.level}">${t} ${l.message}${d}</div>`;
    }).join("");
  });
}

function setStatus(kind, text) {
  const el = $("statusBig");
  el.className = kind;
  el.textContent = text;
}

function refreshStatus() {
  chrome.runtime.sendMessage({ action: "getConnState" }, (r) => {
    if (chrome.runtime.lastError) return;
    // One place decides the wording (session_state.js), from the server's verdict on the last push.
    const d = FlowSessionState.describeSignIn(r || {});
    setStatus(d.kind, d.text);
    $("openFlowBtn").hidden = !d.flowButton;
  });
}

function loadUpdateInfo() {
  chrome.runtime.sendMessage({ action: "getUpdateInfo" }, (resp) => {
    if (chrome.runtime.lastError || !resp) return;
    const info = resp.updateInfo || {};
    if (info.current) $("verLine").textContent = "v" + info.current;
    const banner = $("updateBanner");
    if (info.updateAvailable && info.downloadUrl) {
      $("ubVersion").textContent = "v" + info.latest;
      $("ubDownload").href = info.downloadUrl;
      banner.style.display = "block";
    } else {
      banner.style.display = "none";
    }
  });
}

function fmtAgo(ts) {
  if (!ts) return "";
  const s = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 86400) return Math.round(s / 3600) + " h ago";
  return Math.round(s / 86400) + " d ago";
}

// Suno: switch + honest status line. The popup only shows what the last push proved;
// "Login shared" never claims a song was generated.
function renderSunoState(r) {
  const on = !!(r && r.enabled);
  $("sunoEnabled").checked = on;
  const lab = $("sunoState");
  lab.textContent = on ? "ON" : "Off";
  lab.className = on ? "on" : "";
  const box = $("sunoStatus");
  const btn = $("sunoSyncBtn");
  btn.hidden = !on;
  if (!on) { box.hidden = true; return; }
  box.hidden = false;
  const st = (r && r.state) || { status: "syncing" };
  let cls = "", text = "";
  if (st.status === "connected") {
    const a = st.account || {};
    const who = a.display_name ? `as ${a.display_name}` : (a.id ? `(account #${a.id})` : "");
    const credits = (typeof a.credits === "number") ? ` · ${a.credits} credits` : " · credits unknown";
    const plan = a.plan ? ` · ${a.plan}` : "";
    if (a.operator_disabled) { cls = "warn"; text = `Login shared ${who}, but the admin has paused this account in Flow2API.`; }
    else if (a.status && a.status !== "ready") { cls = "warn"; text = `Login shared ${who}, Flow2API status: ${a.status}. Try Sync, or sign out of suno.com and back in.`; }
    else { cls = "ok"; text = `✅ Login shared with Flow2API ${who}${plan}${credits}`; }
  } else if (st.status === "syncing") { cls = ""; text = "Syncing…"; }
  else if (st.status === "signed_out") { cls = "warn"; text = "Not signed in to Suno here. Open suno.com, sign in with this Chrome, then come back (it syncs on its own)."; }
  else if (st.status === "unverified") { cls = "warn"; text = st.message || "Flow2API couldn't verify this Suno login."; }
  else { cls = "err"; text = st.message || "Something went wrong."; }
  const when = (r && r.lastPushAt) ? `<span class="when">Last successful sync: ${fmtAgo(r.lastPushAt)}</span>` : "";
  box.className = cls;
  box.innerHTML = text.replace(/</g, "&lt;") + when;
}

// Account sharing (cookie sync): switch + honest status line (what the last push proved;
// one place decides the wording: session_state.js describeCookieSyncState).
function renderCookieSyncState(r) {
  const on = !(r && r.enabled === false);
  $("cookieSyncEnabled").checked = on;
  const lab = $("cookieSyncState");
  lab.textContent = on ? "ON" : "Stopped";
  lab.className = on ? "on" : "";
  $("cookieSyncHint").innerHTML = on
    ? "Flow2API signs in to Flow with this Chrome's Google login, so this must stay ON. This Chrome must stay running; the account pauses about 20–60 min after Chrome stops. Turning it off means: <b>Stop using this account in Flow2API</b>."
    : "Flow2API does not use this account now. Turn it back ON to use it again (this Chrome must be signed in to Google).";
  const d = FlowSessionState.describeCookieSyncState(on, (r && r.state) || null);
  const when = (on && r && r.lastPushAt) ? `<span class="when">Last confirmed: ${fmtAgo(r.lastPushAt)}</span>` : "";
  const box = $("cookieSyncStatus");
  box.hidden = false;
  box.className = d.cls;
  box.innerHTML = d.text.replace(/</g, "&lt;") + when;
}

function loadCookieSyncState() {
  chrome.runtime.sendMessage({ action: "getCookieSyncState" }, (r) => {
    if (chrome.runtime.lastError) return;
    renderCookieSyncState(r);
  });
}

function loadSunoState() {
  chrome.runtime.sendMessage({ action: "getSunoState" }, (r) => {
    if (chrome.runtime.lastError) return;
    renderSunoState(r);
  });
}

function loadFailedMode() {
  chrome.storage.local.get(["failedImageMode"], ({ failedImageMode }) => {
    const on = failedImageMode === true;
    $("failedImageMode").checked = on;
    const s = $("failedModeState");
    s.textContent = on ? "ON" : "Off";
    s.className = on ? "on" : "";
  });
}

document.addEventListener("DOMContentLoaded", () => {
  refreshStatus();
  renderLogs();
  loadFailedMode();
  loadSunoState();
  loadCookieSyncState();
  loadUpdateInfo();

  $("cookieSyncEnabled").addEventListener("change", (e) => {
    const on = e.target.checked;
    // Since 2026-10-23 the shared login IS the account's Flow login: OFF stops the account.
    if (!on && !confirm("Stop using this account in Flow2API?\n\nFlow2API deletes its copy of this Chrome's Google login, and the account stops working in Flow2API until you turn this back on.")) {
      e.target.checked = true;
      return;
    }
    renderCookieSyncState({ enabled: on, state: { status: on ? "syncing" : "clearing" } });
    chrome.runtime.sendMessage({ action: "cookieSyncSetEnabled", enabled: on }, () => {
      setTimeout(() => { loadCookieSyncState(); renderLogs(); }, 1500);
    });
  });

  $("sunoEnabled").addEventListener("change", (e) => {
    const on = e.target.checked;
    renderSunoState({ enabled: on, state: on ? { status: "syncing" } : null });
    chrome.runtime.sendMessage({ action: "sunoSetEnabled", enabled: on }, () => {
      setTimeout(() => { loadSunoState(); renderLogs(); }, 1200);
    });
  });
  $("sunoSyncBtn").addEventListener("click", () => {
    renderSunoState({ enabled: true, state: { status: "syncing" } });
    chrome.runtime.sendMessage({ action: "sunoSyncNow" }, () => {
      setTimeout(() => { loadSunoState(); renderLogs(); }, 1500);
    });
  });

  $("failedImageMode").addEventListener("change", (e) => {
    const on = e.target.checked;
    const s = $("failedModeState");
    s.textContent = on ? "ON" : "Off";
    s.className = on ? "on" : "";
    // Persist, reconnect (re-register with the new pool), and push the session now so the
    // backend updates this account's pool immediately.
    chrome.storage.local.set({ failedImageMode: on }, () => {
      chrome.runtime.sendMessage({ action: "settingsChanged" }, () => {});
      chrome.runtime.sendMessage({ action: "refreshSessionNow" }, () => {
        setTimeout(() => { refreshStatus(); renderLogs(); }, 1500);
      });
    });
  });

  $("openFlowBtn").addEventListener("click", () => {
    chrome.runtime.sendMessage({ action: "openFlowSignIn" }, () => {});
  });

  $("reconnectBtn").addEventListener("click", () => {
    setStatus("checking", "Reconnecting…");
    // Reconnect the captcha socket, then push a fresh session (also reports this
    // profile's residential IP + browser UA to the backend).
    chrome.runtime.sendMessage({ action: "testCaptchaConnection" }, () => {
      chrome.runtime.sendMessage({ action: "refreshSessionNow" }, () => {
        setTimeout(() => { refreshStatus(); renderLogs(); }, 2500);
      });
    });
  });

  setInterval(() => { refreshStatus(); renderLogs(); loadSunoState(); loadCookieSyncState(); }, 4000);
  setInterval(loadUpdateInfo, 12000);   // re-check so the banner appears even if the popup is left open
});
