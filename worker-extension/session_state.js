// Sign-in state helpers shared by the service worker and the popup (pure; Node-testable).
//
// 3.8.0: Google retired the Labs sign-in on 2026-10-21. The account's Flow login on the server is
// this Chrome's Google login (shared by the session push), so "signed in" means: signed in to
// Google in this Chrome, and the server could sign in to flow.google.com with it.

const FLOW_SIGNIN_URL = "https://flow.google.com/";
/** Project id from a Flow project URL (flow.google.com/project/<uuid> or the old labs tools URL), else "". */
function flowProjectIdFromUrl(url) {
  const m = /^https:\/\/(?:flow\.google\.com|labs\.google)\/(?:fx\/tools\/flow\/)?project\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:[/?#]|$)/i.exec(String(url || ""));
  return m ? m[1].toLowerCase() : "";
}

/**
 * What the popup should say. Inputs the service worker measured:
 *   googleSignedIn — a Google account cookie exists in this Chrome (accounts.google.com)
 *   connected      — the captcha WebSocket is open
 *   loginRequired  — the last push could not use this Chrome's login; loginKind says why:
 *                    "signed_out" (no Google login cookies) or "rejected" (server answered 400)
 *   grantExpired   — the server said the Google access token is dead (only a sign-out/in fixes it)
 *   sharingOff     — the "Use this account in Flow2API" switch is OFF
 * Returns { kind: "connected"|"disconnected"|"checking", text, flowButton: boolean }.
 */
function describeSignIn(s) {
  s = s || {};
  if (s.sharingOff) {
    return { kind: "disconnected", flowButton: false,
      text: "Stopped: Flow2API does not use this account (the switch below is OFF)." };
  }
  if (s.grantExpired) {
    return { kind: "disconnected", flowButton: true,
      text: "⚠️ Google needs you to sign in again: sign OUT of Google in this Chrome, sign back IN, then click Reconnect" };
  }
  if (!s.googleSignedIn || (s.loginRequired && s.loginKind === "signed_out")) {
    return { kind: "disconnected", flowButton: true,
      text: "Not signed in to Google in this Chrome. Click \"Open flow.google.com\" and sign in there. This connects by itself after that." };
  }
  if (s.loginRequired && s.loginKind === "rejected") {
    return { kind: "disconnected", flowButton: true,
      text: "Open flow.google.com in this Chrome and check you are signed in (Flow2API could not sign in to Flow with this Chrome's login)." };
  }
  if (s.connected) return { kind: "connected", flowButton: false, text: "✅ Connected — working automatically" };
  return { kind: "disconnected", flowButton: false,
    text: "Signed in to Google, but not connected to the server yet — click Reconnect (or wait a few seconds)" };
}

/** The account-sharing status line, from what the last push proved (never claims more). */
function describeCookieSyncState(enabled, state, now = Date.now()) {
  if (!enabled) {
    if (state && state.status === "clearing") return { cls: "warn", text: "Stopped. Deletion pending — Flow2API has not confirmed yet (retrying every minute)." };
    return { cls: "", text: "Stopped — Flow2API does not use this account. Turn the switch back on to use it again." };
  }
  const st = state || {};
  if (st.status === "stored") {
    const ago = st.at ? Math.max(0, Math.round((now - st.at) / 60000)) : null;
    const when = ago == null ? "" : (ago < 1 ? " just now" : ` ${ago} min ago`);
    return { cls: "ok", text: `✅ Server copy updated${when} (${st.count || 0} cookies).` };
  }
  if (st.status === "signed_out") return { cls: "warn", text: "Not signed in to Google in this Chrome. Sign in to Google; the login is shared on its own after that." };
  if (st.status === "rejected") return { cls: "err", text: "Open flow.google.com in this Chrome and check you are signed in. Flow2API could not sign in to Flow with this login." };
  if (st.status === "retry") return { cls: "warn", text: `The last push did not get through${st.message ? ` (${st.message})` : ""}. It retries on its own.` };
  if (st.status === "cleared") return { cls: "", text: "Server copy deleted." };
  if (st.status === "clearing") return { cls: "warn", text: "Deletion pending — Flow2API has not confirmed yet (retrying every minute)." };
  if (st.status === "error") return { cls: "err", text: st.message || "The last push failed. It retries on its own (at least every 20 min)." };
  return { cls: "", text: "Waiting for the next push…" };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { describeSignIn, describeCookieSyncState, flowProjectIdFromUrl, FLOW_SIGNIN_URL };
} else {
  globalThis.FlowSessionState = { describeSignIn, describeCookieSyncState, flowProjectIdFromUrl, FLOW_SIGNIN_URL };
}
