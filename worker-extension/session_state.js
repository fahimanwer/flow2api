// Sign-in state helpers shared by the service worker and the popup (pure; Node-testable).
//
// Google Labs (labs.google/fx) has its OWN sign-in on top of the Google account: being signed in to
// Google in this Chrome is not enough — the Labs page must have been through "Sign in with Google"
// once, which writes the NextAuth session cookie this worker reads. Staff saw "not logged in" right
// after signing in to Google and did not know why (2026-09-22, flow-ultra-01 setup).

const FLOW_SESSION_COOKIE = "__Secure-next-auth.session-token";
const FLOW_COOKIE_DOMAINS = ["labs.google", "flow.google.com"];
const LABS_SIGNIN_URL = "https://labs.google/fx";
/** Project id from a Flow project URL (flow.google.com/project/<uuid> or the old labs tools URL), else "". */
function flowProjectIdFromUrl(url) {
  const m = /^https:\/\/(?:flow\.google\.com|labs\.google)\/(?:fx\/tools\/flow\/)?project\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:[/?#]|$)/i.exec(String(url || ""));
  return m ? m[1].toLowerCase() : "";
}

/** True when a chrome.cookies.onChanged event is about the Flow/Labs session cookie APPEARING (not removed). */
function isFlowSessionCookieSet(changeInfo) {
  const c = changeInfo && changeInfo.cookie;
  if (!c || c.name !== FLOW_SESSION_COOKIE || !c.value) return false;
  if (changeInfo.removed) return false;
  const d = String(c.domain || "").replace(/^\./, "").toLowerCase();
  return FLOW_COOKIE_DOMAINS.some((h) => d === h || d.endsWith("." + h));
}

/**
 * What the popup should say. Inputs are booleans the service worker measured:
 *   googleSignedIn — a Google account cookie exists in this Chrome (accounts.google.com)
 *   labsSignedIn   — the Labs session cookie exists (the one this worker pushes)
 *   connected      — the captcha WebSocket is open
 *   loginRequired  — the worker's own "Labs login required" breaker is armed
 *   grantExpired   — the server said the Google access token is dead (only a sign-out/in fixes it)
 * Returns { kind: "connected"|"disconnected"|"checking", text, labsButton: boolean }.
 */
function describeSignIn(s) {
  s = s || {};
  if (s.grantExpired) {
    return { kind: "disconnected", labsButton: true,
      text: "⚠️ Google needs you to sign in again: sign OUT of Google Labs, sign back IN, then click Reconnect" };
  }
  if (!s.labsSignedIn || s.loginRequired) {
    if (s.googleSignedIn) {
      return { kind: "disconnected", labsButton: true,
        text: "Signed in to Google, but NOT to Google Labs yet — Labs is a separate sign-in. " +
              "Click \"Open Google Labs\", press \"Sign in with Google\" there (2 clicks). This connects by itself after that." };
    }
    return { kind: "disconnected", labsButton: true,
      text: "Not signed in in this Chrome. Click \"Open Google Labs\", sign in to Google, then press Labs' own " +
            "\"Sign in with Google\" button. This connects by itself after that." };
  }
  if (s.connected) return { kind: "connected", labsButton: false, text: "✅ Connected — working automatically" };
  return { kind: "disconnected", labsButton: false,
    text: "Signed in to Google Labs, but not connected to the server yet — click Reconnect (or wait a few seconds)" };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { isFlowSessionCookieSet, describeSignIn, flowProjectIdFromUrl, FLOW_SESSION_COOKIE, LABS_SIGNIN_URL };
} else {
  globalThis.FlowSessionState = { isFlowSessionCookieSet, describeSignIn, flowProjectIdFromUrl, FLOW_SESSION_COOKIE, LABS_SIGNIN_URL };
}
