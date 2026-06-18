const DEFAULTS = {
  serverBase: "https://flow.ashuthefire.com",
  apiKey: "han1234",
  connectionToken: "fahim",
  routeKey: "",
  clientLabel: "",
  refreshIntervalMinutes: 60,
  tabMode: "persistent",
  mintIntervalMs: 2000
};

const $ = (id) => document.getElementById(id);

function setStatus(msg, ok) {
  const el = $("status");
  el.textContent = msg;
  el.className = ok ? "ok" : "err";
}

function load() {
  chrome.storage.local.get(DEFAULTS, (s) => {
    $("serverBase").value = s.serverBase;
    $("apiKey").value = s.apiKey;
    $("connectionToken").value = s.connectionToken;
    $("routeKey").value = s.routeKey;
    $("clientLabel").value = s.clientLabel;
    $("refreshIntervalMinutes").value = s.refreshIntervalMinutes;
    $("tabMode").value = s.tabMode;
    $("mintIntervalMs").value = s.mintIntervalMs;
  });
  renderLogs();
}

function save() {
  const settings = {
    serverBase: $("serverBase").value.trim().replace(/\/+$/, ""),
    apiKey: $("apiKey").value.trim(),
    connectionToken: $("connectionToken").value.trim(),
    routeKey: $("routeKey").value.trim(),
    clientLabel: $("clientLabel").value.trim(),
    refreshIntervalMinutes: Math.max(5, parseInt($("refreshIntervalMinutes").value, 10) || 60),
    tabMode: $("tabMode").value === "ephemeral" ? "ephemeral" : "persistent",
    mintIntervalMs: Math.max(0, parseInt($("mintIntervalMs").value, 10) || 2000)
  };
  try { new URL(settings.serverBase); } catch (e) { setStatus("Server URL is invalid.", false); return; }
  if (!settings.apiKey) { setStatus("API Key is required.", false); return; }

  chrome.storage.local.set(settings, () => {
    chrome.runtime.sendMessage({ action: "settingsChanged" }, () => {});
    setStatus("Saved. Reconnecting…", true);
    setTimeout(renderLogs, 1500);
  });
}

function renderLogs() {
  chrome.runtime.sendMessage({ action: "getLogs" }, (resp) => {
    if (!resp || !resp.logs) return;
    const box = $("logs");
    box.innerHTML = resp.logs.slice(0, 30).map((l) => {
      const t = (l.ts || "").slice(11, 19);
      const d = l.details ? " " + JSON.stringify(l.details).slice(0, 120) : "";
      return `<div class="l-${l.level}">${t} ${l.message}${d}</div>`;
    }).join("");
  });
}

document.addEventListener("DOMContentLoaded", () => {
  load();
  $("saveBtn").addEventListener("click", save);
  $("testBtn").addEventListener("click", () => {
    chrome.runtime.sendMessage({ action: "testCaptchaConnection" }, () => {
      setStatus("Reconnecting captcha socket…", true);
      setTimeout(renderLogs, 1500);
    });
  });
  $("refreshBtn").addEventListener("click", () => {
    setStatus("Refreshing session…", true);
    chrome.runtime.sendMessage({ action: "refreshSessionNow" }, (r) => {
      if (r && r.success) setStatus("Session pushed ✓ " + (r.message || ""), true);
      else setStatus("Session refresh failed: " + ((r && r.error) || "?"), false);
      renderLogs();
    });
  });
  setInterval(renderLogs, 4000);
});
