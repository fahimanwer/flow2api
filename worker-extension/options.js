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
    // One place decides the wording (session_state.js): Google signed in ≠ Labs signed in.
    const d = FlowSessionState.describeSignIn(r || {});
    setStatus(d.kind, d.text);
    $("openLabsBtn").hidden = !d.labsButton;
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

// Away mode (cookie sync): switch + honest status line (what the last push proved).
function renderCookieSyncState(r) {
  const on = !(r && r.enabled === false);
  $("cookieSyncEnabled").checked = on;
  const lab = $("cookieSyncState");
  lab.textContent = on ? "ON" : "Off";
  lab.className = on ? "on" : "";
  const box = $("cookieSyncStatus");
  const st = (r && r.state) || null;
  let cls = "", text = "";
  if (!on) {
    if (st && st.status === "clearing") { cls = "warn"; text = "Off. Deletion pending — Flow2API has not confirmed yet (retrying every minute)."; }
    else { text = "Off — Flow2API keeps this account working only while this laptop is open."; }
  } else if (st && st.status === "stored") {
    const ago = st.at ? Math.max(0, Math.round((Date.now() - st.at) / 60000)) : null;
    const when = ago == null ? "" : (ago < 1 ? " just now" : ` ${ago} min ago`);
    cls = "ok"; text = `✅ Server copy updated${when} (${st.count || 0} cookies). Flow2API can sign this account in again while you are away.`;
  } else if (st && st.status === "signed_out") { cls = "warn"; text = "Not signed in to Google in this Chrome. Sign in, then the next push shares the login on its own."; }
  else if (st && st.status === "error") { cls = "err"; text = st.message || "The last push failed. It retries every hour."; }
  else if (st && st.status === "cleared") { text = "Server copy deleted."; }
  else { text = "Waiting for the next session push…"; }
  const when = (r && r.lastPushAt) ? `<span class="when">Last confirmed: ${fmtAgo(r.lastPushAt)}</span>` : "";
  box.hidden = false;
  box.className = cls;
  box.innerHTML = text.replace(/</g, "&lt;") + when;
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

  $("openLabsBtn").addEventListener("click", () => {
    chrome.runtime.sendMessage({ action: "openLabsSignIn" }, () => {});
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
