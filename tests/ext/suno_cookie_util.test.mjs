// Node tests for the pure helpers in worker-extension/suno.js.
// Run: node --test tests/ext/suno_cookie_util.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { sunoHostMatches, mergeSunoCookies, pickSunoAccount } = require("../../worker-extension/suno.js");

test("sunoHostMatches accepts suno.com and subdomains only", () => {
  for (const h of ["suno.com", ".suno.com", "auth.suno.com", ".AUTH.suno.com", "studio-api.prod.suno.com"]) {
    assert.equal(sunoHostMatches(h), true, h);
  }
  for (const h of ["notsuno.com", "suno.com.evil.io", "example.com", "", null, "sunocom"]) {
    assert.equal(sunoHostMatches(h), false, String(h));
  }
});

test("mergeSunoCookies prefers the auth-host credential and keeps first occurrence", () => {
  const auth = [{ name: "__client", value: "AUTH" }, { name: "shared", value: "a" }];
  const site = [{ name: "__client", value: "SITE" }, { name: "shared", value: "s" }, { name: "ajs_anonymous_id", value: "dev" }];
  const out = mergeSunoCookies(auth, site);
  assert.equal(out.clientValue, "AUTH");
  assert.equal(out.header, "__client=AUTH; shared=a; ajs_anonymous_id=dev");
});

test("mergeSunoCookies returns null without __client and skips empty values", () => {
  assert.equal(mergeSunoCookies([], [{ name: "x", value: "1" }]), null);
  assert.equal(mergeSunoCookies([{ name: "__client", value: "" }], []), null);
  assert.equal(mergeSunoCookies(null, undefined), null);
  const out = mergeSunoCookies([{ name: "__client", value: "c" }, { name: "empty", value: "" }], []);
  assert.equal(out.header, "__client=c");
});

test("pickSunoAccount never carries credential fields through", () => {
  const out = pickSunoAccount({ id: 3, display_name: "lap", status: "ready", cookies: "__client=secret", clerk_sid: "sid", credits: 12 });
  assert.deepEqual(Object.keys(out).sort(), ["credits", "credits_checked_at", "display_name", "id", "operator_disabled", "plan", "status"]);
  assert.equal(out.credits, 12);
  assert.equal(out.operator_disabled, false);
});
