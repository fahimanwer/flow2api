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
    if (r && r.connected) {
      setStatus("connected", "✅ Connected — working automatically");
    } else {
      setStatus("disconnected", "Not connected yet — make sure you're signed in to Google Labs, then click Reconnect");
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  refreshStatus();
  renderLogs();

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

  setInterval(() => { refreshStatus(); renderLogs(); }, 4000);
});
