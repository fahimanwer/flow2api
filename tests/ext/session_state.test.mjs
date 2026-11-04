// Node tests for worker-extension/session_state.js. Run: node --test tests/ext/session_state.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { describeSignIn, describeCookieSyncState, flowProjectIdFromUrl, FLOW_SIGNIN_URL } = require("../../worker-extension/session_state.js");

test("no Google Labs wording anywhere (the Labs sign-in is retired since 2026-10-21)", () => {
  const states = [
    {}, { googleSignedIn: false }, { googleSignedIn: true, connected: true }, { googleSignedIn: true },
    { googleSignedIn: true, loginRequired: true, loginKind: "rejected" }, { googleSignedIn: true, loginRequired: true, loginKind: "signed_out" },
    { grantExpired: true, googleSignedIn: true }, { googleSignedIn: true, sharingOff: true },
  ];
  for (const s of states) assert.doesNotMatch(describeSignIn(s).text, /Labs/, JSON.stringify(s));
  for (const st of ["stored", "signed_out", "rejected", "retry", "cleared", "clearing", "error", "syncing"]) {
    assert.doesNotMatch(describeCookieSyncState(true, { status: st }).text, /Labs/, st);
  }
  assert.equal(FLOW_SIGNIN_URL, "https://flow.google.com/");
});

test("describeSignIn: not signed in to Google, with the flow.google.com button", () => {
  for (const s of [{ googleSignedIn: false, connected: true }, { googleSignedIn: true, loginRequired: true, loginKind: "signed_out" }]) {
    const d = describeSignIn(s);
    assert.equal(d.kind, "disconnected");
    assert.match(d.text, /Not signed in to Google in this Chrome/);
    assert.equal(d.flowButton, true);
  }
});

test("describeSignIn: a refused push (400) says to check flow.google.com", () => {
  const d = describeSignIn({ googleSignedIn: true, connected: true, loginRequired: true, loginKind: "rejected" });
  assert.equal(d.kind, "disconnected");
  assert.match(d.text, /^Open flow\.google\.com in this Chrome and check you are signed in/);
  assert.equal(d.flowButton, true);
});

test("describeSignIn: grant expired, sharing off, connected, and connecting", () => {
  assert.match(describeSignIn({ grantExpired: true, googleSignedIn: true, connected: true }).text, /sign OUT of Google in this Chrome/);
  const off = describeSignIn({ googleSignedIn: true, connected: true, sharingOff: true });
  assert.equal(off.kind, "disconnected"); assert.match(off.text, /does not use this account/);
  // OFF is a choice: it outranks any sign-in problem (nothing to sign in for).
  assert.match(describeSignIn({ sharingOff: true, googleSignedIn: false, loginRequired: true, loginKind: "rejected", grantExpired: true }).text, /^Stopped/);
  const ok = describeSignIn({ googleSignedIn: true, connected: true });
  assert.equal(ok.kind, "connected"); assert.equal(ok.flowButton, false);
  const wait = describeSignIn({ googleSignedIn: true, connected: false });
  assert.equal(wait.kind, "disconnected"); assert.match(wait.text, /not connected to the server yet/); assert.equal(wait.flowButton, false);
});

test("describeCookieSyncState never claims more than the last push proved", () => {
  assert.match(describeCookieSyncState(false, null).text, /Stopped — Flow2API does not use this account/);
  assert.equal(describeCookieSyncState(false, { status: "clearing" }).cls, "warn");
  assert.match(describeCookieSyncState(true, { status: "stored", at: 1000, count: 23 }, 1000 + 5 * 60000).text, /5 min ago \(23 cookies\)/);
  assert.equal(describeCookieSyncState(true, { status: "stored", at: 1000, count: 1 }, 1000).cls, "ok");
  assert.equal(describeCookieSyncState(true, { status: "signed_out" }).cls, "warn");
  const rej = describeCookieSyncState(true, { status: "rejected", message: "Invalid session token" });
  assert.equal(rej.cls, "err"); assert.match(rej.text, /^Open flow\.google\.com in this Chrome and check you are signed in/);
  assert.doesNotMatch(rej.text, /Server copy updated/);
  assert.match(describeCookieSyncState(true, { status: "retry", message: "server 503" }).text, /did not get through \(server 503\)/);
  assert.equal(describeCookieSyncState(true, { status: "error", message: "boom" }).text, "boom");
  assert.match(describeCookieSyncState(true, null).text, /Waiting/);
});

test("flowProjectIdFromUrl: new and old project URLs, nothing else", () => {
  assert.equal(flowProjectIdFromUrl("https://flow.google.com/project/B157461C-fa6f-486f-ba90-5bec305f4959"), "b157461c-fa6f-486f-ba90-5bec305f4959");
  assert.equal(flowProjectIdFromUrl("https://labs.google/fx/tools/flow/project/b157461c-fa6f-486f-ba90-5bec305f4959?x=1"), "b157461c-fa6f-486f-ba90-5bec305f4959");
  assert.equal(flowProjectIdFromUrl("https://flow.google.com/"), "");
  assert.equal(flowProjectIdFromUrl("https://evil.com/project/b157461c-fa6f-486f-ba90-5bec305f4959"), "");
  assert.equal(flowProjectIdFromUrl("https://flow.google.com/project/not-a-uuid"), "");
});
