// Node tests for the pure helpers in worker-extension/cookie_sync.js.
// Run: node --test tests/ext/cookie_sync.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const {
  googleHostMatches, serializeGoogleCookies, isGoogleLoginCookieChange, cookieSyncPushDueAt, cookieSyncPendingAfterPush,
  describePushAnswer, shouldRetryUnbound, nextCookieSyncSeq, COOKIE_SYNC_MAX_COOKIES, buildCookieClearBody,
  COOKIE_SYNC_DEBOUNCE_MS, COOKIE_SYNC_MAX_WAIT_MS, COOKIE_SYNC_MIN_GAP_MS, COOKIE_SYNC_MAX_TRIES,
} = require("../../worker-extension/cookie_sync.js");

const NOW = 1_800_000_000_000;
const ck = (over = {}) => ({ name: "SID", value: "v", domain: ".google.com", path: "/", hostOnly: false, secure: true, httpOnly: false, sameSite: "no_restriction", ...over });

test("googleHostMatches accepts google.com and subdomains only", () => {
  for (const h of ["google.com", ".google.com", "accounts.google.com", ".ACCOUNTS.google.com", "flow.google.com"]) assert.equal(googleHostMatches(h), true, h);
  for (const h of ["notgoogle.com", "google.com.evil.io", "labs.google", "", null]) assert.equal(googleHostMatches(h), false, String(h));
});

test("serializeGoogleCookies keeps where each cookie lives (3.8.0 item shape)", () => {
  const out = serializeGoogleCookies([
    ck({ name: "SID", value: "s", expirationDate: NOW / 1000 + 3600 }),
    ck({ name: "LSID", value: "l", domain: "accounts.google.com", hostOnly: true, httpOnly: true, sameSite: "lax" }),
  ], NOW);
  assert.deepEqual(JSON.parse(out.json), [
    { name: "LSID", value: "l", domain: "accounts.google.com", path: "/", hostOnly: true, secure: true, httpOnly: true, sameSite: "lax" },
    { name: "SID", value: "s", domain: ".google.com", path: "/", hostOnly: false, secure: true, httpOnly: false, sameSite: "no_restriction", expirationDate: NOW / 1000 + 3600 },
  ]);
  assert.equal(out.count, 2);
  assert.deepEqual(out.loginNames, ["SID"]);
});

test("serializeGoogleCookies dedupes by name + domain + path, not by name alone", () => {
  // getAll for accounts.google.com and flow.google.com both return the .google.com cookies.
  const out = serializeGoogleCookies([
    ck({ name: "SID", value: "first" }),
    ck({ name: "OSID", value: "acc", domain: "accounts.google.com", hostOnly: true }),
    ck({ name: "SID", value: "second" }),                                   // same name+domain+path: dropped
    ck({ name: "OSID", value: "flow", domain: "flow.google.com", hostOnly: true }),   // same name, other host: kept
    ck({ name: "OSID", value: "flow-path", domain: "flow.google.com", path: "/x" }), // same name+host, other path: kept
  ], NOW);
  const items = JSON.parse(out.json).map((c) => `${c.name}@${c.domain}${c.path}=${c.value}`);
  assert.deepEqual(items, [
    "OSID@accounts.google.com/=acc", "OSID@flow.google.com/=flow", "OSID@flow.google.com/x=flow-path", "SID@.google.com/=first",
  ]);
});

test("serializeGoogleCookies skips partitioned, expired, empty and foreign cookies", () => {
  const out = serializeGoogleCookies([
    ck({ name: "SAPISID", value: "s" }),
    ck({ name: "part", partitionKey: { topLevelSite: "https://example.com" } }),
    ck({ name: "old", expirationDate: NOW / 1000 - 1 }),
    ck({ name: "session" }),                                   // no expirationDate = session cookie: kept
    ck({ name: "empty", value: "" }),
    ck({ name: "evil", domain: "evil.io" }),
    ck({ name: "labs", domain: "labs.google" }),
  ], NOW);
  assert.deepEqual(JSON.parse(out.json).map((c) => c.name), ["SAPISID", "session"]);
  assert.equal("expirationDate" in JSON.parse(out.json)[1], false);
});

test("serializeGoogleCookies returns null when signed out or over the caps", () => {
  assert.equal(serializeGoogleCookies([ck({ name: "NID" })], NOW), null);
  assert.equal(serializeGoogleCookies([ck({ name: "__Secure-1PSID" })], NOW), null);   // the server refuses PSID-only jars
  assert.equal(serializeGoogleCookies([ck({ name: "SID", expirationDate: NOW / 1000 - 5 })], NOW), null);
  assert.equal(serializeGoogleCookies([]), null);
  assert.equal(serializeGoogleCookies(null), null);
  const many = [ck()];
  for (let i = 0; i < COOKIE_SYNC_MAX_COOKIES; i++) many.push(ck({ name: "c" + i }));
  assert.equal(serializeGoogleCookies(many, NOW), null);
  assert.equal(serializeGoogleCookies([ck({ value: "x".repeat(300 * 1024) })], NOW), null);
});

test("isGoogleLoginCookieChange: login + device-bound names trigger; OSID and *SIDCC do not", () => {
  const fire = (name, domain = ".google.com") => isGoogleLoginCookieChange({ cookie: { name, domain } });
  for (const n of ["SID", "HSID", "SSID", "LSID", "__Secure-1PSID", "__Secure-3PSID", "__Secure-1PSIDTS", "__Secure-3PSIDTS",
    "__Secure-1PSIDRTS", "__Secure-3PSIDRTS", "__Secure-STRP"]) assert.equal(fire(n), true, n);
  assert.equal(fire("__Secure-1PSIDTS", "accounts.google.com"), true);
  for (const n of ["OSID", "__Secure-OSID", "SIDCC", "__Secure-1PSIDCC", "__Secure-3PSIDCC", "NID"]) assert.equal(fire(n), false, n);
  assert.equal(fire("SID", "evil.io"), false);
  assert.equal(isGoogleLoginCookieChange({}), false);
  assert.equal(isGoogleLoginCookieChange(null), false);
});

test("cookieSyncPushDueAt: debounced after the last change, capped by the max wait, never inside the gap", () => {
  const first = { since: NOW, lastChangeAt: NOW, tries: 0 };
  // Nothing pushed for a long time: ~10 s after the change.
  assert.equal(cookieSyncPushDueAt(first, 0, NOW), NOW + COOKIE_SYNC_DEBOUNCE_MS);
  assert.equal(cookieSyncPushDueAt(first, NOW - 3600_000, NOW), NOW + COOKIE_SYNC_DEBOUNCE_MS);
  // A burst of changes keeps pushing it out, but never beyond since + MAX_WAIT.
  const burst = { since: NOW, lastChangeAt: NOW + 55_000, tries: 0 };
  assert.equal(cookieSyncPushDueAt(burst, 0, NOW + 55_000), NOW + COOKIE_SYNC_MAX_WAIT_MS);
  // The last push went out 30 s ago: DELAYED to the end of the 90 s gap (3.7.x dropped it).
  assert.equal(cookieSyncPushDueAt(first, NOW - 30_000, NOW), NOW - 30_000 + COOKIE_SYNC_MIN_GAP_MS);
  // After a failed try the gap doubles.
  assert.equal(cookieSyncPushDueAt({ ...first, tries: 1 }, NOW, NOW), NOW + 2 * COOKIE_SYNC_MIN_GAP_MS);
  // Overdue (e.g. the worker slept through the alarm): now.
  assert.equal(cookieSyncPushDueAt(first, NOW - 3600_000, NOW + 600_000), NOW + 600_000);
});

test("cookieSyncPendingAfterPush: settled by a verdict, kept for a later change, retried then handed to the periodic push", () => {
  const p = { since: NOW, lastChangeAt: NOW, tries: 0 };
  assert.equal(cookieSyncPendingAfterPush(null, NOW + 1, true), null);
  assert.equal(cookieSyncPendingAfterPush(p, NOW + 1, true), null);                 // pushed after the change, server answered
  assert.equal(cookieSyncPendingAfterPush(p, NOW - 1, true), p);                    // push read the cookies before the change
  let next = cookieSyncPendingAfterPush(p, NOW + 1, false);                          // transport failure: retry
  assert.deepEqual(next, { ...p, tries: 1 });
  for (let i = 1; i < COOKIE_SYNC_MAX_TRIES - 1; i++) next = cookieSyncPendingAfterPush(next, NOW + 1, false);
  assert.equal(next.tries, COOKIE_SYNC_MAX_TRIES - 1);
  assert.equal(cookieSyncPendingAfterPush(next, NOW + 1, false), null);
});

test("describePushAnswer maps the server's answer to the refresh_session status", () => {
  assert.deepEqual(describePushAnswer(200, { action: "updated", credential_verified: true, message: "ok" }),
    { reason: "refreshed", verified: true, detail: "ok" });
  assert.equal(describePushAnswer(200, { action: "updated", credential_verified: false }).verified, false);
  assert.equal(describePushAnswer(200, { action: "added" }).verified, false);         // only an explicit true counts
  assert.equal(describePushAnswer(200, { action: "relogin_required" }).reason, "relogin_required");
  const r400 = describePushAnswer(400, { detail: "Invalid session token: UNAUTHENTICATED" });
  assert.equal(r400.reason, "logged_out"); assert.equal(r400.rejected, true); assert.match(r400.detail, /UNAUTHENTICATED/);
  assert.equal(describePushAnswer(409, { detail: "account mismatch: cookie is a@x, token 5 is b@x" }).mismatch, true);
  assert.equal(describePushAnswer(409, { detail: "refused: this account is owned by an Ultra server browser" }).mismatch, false);
  assert.equal(describePushAnswer(409, null).reason, "account_mismatch");
  assert.deepEqual(describePushAnswer(503, null), { reason: "network", detail: "server 503" });
  assert.deepEqual(describePushAnswer(0, { detail: "could not reach Flow2API" }), { reason: "network", detail: "could not reach Flow2API" });
});

test("shouldRetryUnbound: only a remembered id that is gone (404) or another account (409 mismatch)", () => {
  const mism = describePushAnswer(409, { detail: "account mismatch: session is not b@x" });
  const ultra = describePushAnswer(409, { detail: "refused: this account is owned by an Ultra server browser" });
  assert.equal(shouldRetryUnbound(409, mism, true), true);
  assert.equal(shouldRetryUnbound(404, describePushAnswer(404, { detail: "Token 7 not found" }), true), true);
  assert.equal(shouldRetryUnbound(409, mism, false), false);   // the server asked for that id (refresh_session): its probe
  assert.equal(shouldRetryUnbound(409, ultra, true), false);
  assert.equal(shouldRetryUnbound(400, describePushAnswer(400, {}), true), false);
});

test("nextCookieSyncSeq is strictly increasing even within one millisecond", () => {
  const a = nextCookieSyncSeq(), b = nextCookieSyncSeq(), c = nextCookieSyncSeq();
  assert.ok(a < b && b < c);
  assert.ok(a >= Date.now() - 5000);
});

test("buildCookieClearBody carries the route key when the profile has one (3.7.5), unchanged otherwise", () => {
  assert.deepEqual(buildCookieClearBody(55, 9, " ultra-flow-ultra-03-ab ", "3.7.5"),
    { action: "clear", token_id: 55, cookie_sync_seq: 9, ext_version: "3.7.5", route_key: "ultra-flow-ultra-03-ab" });
  assert.deepEqual(buildCookieClearBody(55, 9, "", "3.7.5"), { action: "clear", token_id: 55, cookie_sync_seq: 9, ext_version: "3.7.5" });
  assert.deepEqual(buildCookieClearBody(55, 9, undefined, "3.7.5"), { action: "clear", token_id: 55, cookie_sync_seq: 9, ext_version: "3.7.5" });
});
