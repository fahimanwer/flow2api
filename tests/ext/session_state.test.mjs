// Node tests for worker-extension/session_state.js. Run: node --test tests/ext/session_state.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { isFlowSessionCookieSet, describeSignIn } = require("../../worker-extension/session_state.js");

const cookie = (over = {}) => ({ name: "__Secure-next-auth.session-token", value: "v", domain: "labs.google", ...over });

test("isFlowSessionCookieSet: the Labs/Flow session cookie appearing, nothing else", () => {
  assert.equal(isFlowSessionCookieSet({ removed: false, cookie: cookie() }), true);
  assert.equal(isFlowSessionCookieSet({ removed: false, cookie: cookie({ domain: ".flow.google.com" }) }), true);
  assert.equal(isFlowSessionCookieSet({ removed: true, cookie: cookie() }), false);
  assert.equal(isFlowSessionCookieSet({ removed: false, cookie: cookie({ value: "" }) }), false);
  assert.equal(isFlowSessionCookieSet({ removed: false, cookie: cookie({ name: "SID" }) }), false);
  assert.equal(isFlowSessionCookieSet({ removed: false, cookie: cookie({ domain: "evil-labs.google.com" }) }), false);
  assert.equal(isFlowSessionCookieSet(null), false);
});

test("describeSignIn: Google signed in but not Labs is named as such, with the Labs button", () => {
  const d = describeSignIn({ googleSignedIn: true, labsSignedIn: false, connected: true });
  assert.equal(d.kind, "disconnected");
  assert.match(d.text, /NOT to Google Labs/);
  assert.equal(d.labsButton, true);
});

test("describeSignIn: nothing signed in, breaker armed, grant expired, connected, and connecting", () => {
  assert.match(describeSignIn({ googleSignedIn: false, labsSignedIn: false }).text, /Not signed in in this Chrome/);
  assert.match(describeSignIn({ googleSignedIn: true, labsSignedIn: true, loginRequired: true }).text, /NOT to Google Labs/);
  assert.match(describeSignIn({ grantExpired: true, labsSignedIn: true, connected: true }).text, /sign OUT of Google Labs/);
  const ok = describeSignIn({ googleSignedIn: true, labsSignedIn: true, connected: true });
  assert.equal(ok.kind, "connected"); assert.equal(ok.labsButton, false);
  const wait = describeSignIn({ googleSignedIn: true, labsSignedIn: true, connected: false });
  assert.equal(wait.kind, "disconnected"); assert.match(wait.text, /not connected to the server yet/); assert.equal(wait.labsButton, false);
});

test("flowProjectIdFromUrl: new and old project URLs, nothing else", async () => {
  const { flowProjectIdFromUrl } = require("../../worker-extension/session_state.js");
  assert.equal(flowProjectIdFromUrl("https://flow.google.com/project/B157461C-fa6f-486f-ba90-5bec305f4959"), "b157461c-fa6f-486f-ba90-5bec305f4959");
  assert.equal(flowProjectIdFromUrl("https://labs.google/fx/tools/flow/project/b157461c-fa6f-486f-ba90-5bec305f4959?x=1"), "b157461c-fa6f-486f-ba90-5bec305f4959");
  assert.equal(flowProjectIdFromUrl("https://flow.google.com/"), "");
  assert.equal(flowProjectIdFromUrl("https://evil.com/project/b157461c-fa6f-486f-ba90-5bec305f4959"), "");
  assert.equal(flowProjectIdFromUrl("https://flow.google.com/project/not-a-uuid"), "");
});
