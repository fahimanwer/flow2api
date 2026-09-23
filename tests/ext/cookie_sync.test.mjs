// Node tests for the pure helpers in worker-extension/cookie_sync.js.
// Run: node --test tests/ext/cookie_sync.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const {
  googleHostMatches, serializeGoogleCookies, isGoogleLoginCookieChange, cookieSyncPushAllowed,
  describeCookieSyncState, nextCookieSyncSeq, COOKIE_SYNC_MAX_COOKIES,
} = require("../../worker-extension/cookie_sync.js");

test("googleHostMatches accepts google.com and subdomains only", () => {
  for (const h of ["google.com", ".google.com", "accounts.google.com", ".ACCOUNTS.google.com"]) assert.equal(googleHostMatches(h), true, h);
  for (const h of ["notgoogle.com", "google.com.evil.io", "labs.google", "", null]) assert.equal(googleHostMatches(h), false, String(h));
});

test("serializeGoogleCookies: sorted JSON list, first occurrence wins, foreign domains dropped", () => {
  const out = serializeGoogleCookies([
    { name: "SAPISID", value: "s", domain: ".google.com" },
    { name: "SID", value: "first", domain: ".google.com" },
    { name: "SID", value: "second", domain: "accounts.google.com" },
    { name: "evil", value: "x", domain: "evil.io" },
    { name: "empty", value: "", domain: ".google.com" },
  ]);
  assert.deepEqual(JSON.parse(out.json), [{ name: "SAPISID", value: "s" }, { name: "SID", value: "first" }]);
  assert.equal(out.count, 2);
  assert.deepEqual(out.loginNames, ["SID", "SAPISID"]);
});

test("serializeGoogleCookies returns null when signed out or over the caps", () => {
  assert.equal(serializeGoogleCookies([{ name: "NID", value: "n", domain: ".google.com" }]), null);
  assert.equal(serializeGoogleCookies([]), null);
  assert.equal(serializeGoogleCookies(null), null);
  const many = [{ name: "SID", value: "s", domain: ".google.com" }];
  for (let i = 0; i < COOKIE_SYNC_MAX_COOKIES; i++) many.push({ name: "c" + i, value: "v", domain: ".google.com" });
  assert.equal(serializeGoogleCookies(many), null);
  assert.equal(serializeGoogleCookies([{ name: "SID", value: "x".repeat(300 * 1024), domain: ".google.com" }]), null);
});

test("isGoogleLoginCookieChange fires only for login cookies on google.com hosts", () => {
  assert.equal(isGoogleLoginCookieChange({ cookie: { name: "SID", domain: ".google.com" } }), true);
  assert.equal(isGoogleLoginCookieChange({ cookie: { name: "__Secure-1PSIDTS", domain: "accounts.google.com" } }), true);
  assert.equal(isGoogleLoginCookieChange({ cookie: { name: "NID", domain: ".google.com" } }), false);
  assert.equal(isGoogleLoginCookieChange({ cookie: { name: "SID", domain: "evil.io" } }), false);
  assert.equal(isGoogleLoginCookieChange({}), false);
  assert.equal(isGoogleLoginCookieChange(null), false);
});

test("cookieSyncPushAllowed enforces the minimum gap", () => {
  assert.equal(cookieSyncPushAllowed(0, 1000), true);
  assert.equal(cookieSyncPushAllowed(1000, 1000 + 9 * 60 * 1000), false);
  assert.equal(cookieSyncPushAllowed(1000, 1000 + 10 * 60 * 1000), true);
});

test("describeCookieSyncState never claims more than the last push proved", () => {
  assert.match(describeCookieSyncState(false, null).text, /Off/);
  assert.match(describeCookieSyncState(true, { status: "stored", at: 1000, count: 23 }, 1000 + 5 * 60000).text, /5 min ago \(23 cookies\)/);
  assert.equal(describeCookieSyncState(true, { status: "stored", at: 1000, count: 1 }, 1000).cls, "ok");
  assert.equal(describeCookieSyncState(true, { status: "signed_out" }).cls, "warn");
  assert.equal(describeCookieSyncState(true, { status: "error", message: "boom" }).text, "boom");
  assert.match(describeCookieSyncState(true, null).text, /Waiting/);
});

test("nextCookieSyncSeq is strictly increasing even within one millisecond", () => {
  const a = nextCookieSyncSeq(), b = nextCookieSyncSeq(), c = nextCookieSyncSeq();
  assert.ok(a < b && b < c);
  assert.ok(a >= Date.now() - 5000);
});
