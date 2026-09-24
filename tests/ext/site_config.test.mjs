// Run: node --test tests/ext/site_config.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { sanitizeSiteConfig, buildPacScript } = require("../../worker-extension/site_config.js");

test("sanitizeSiteConfig keeps only known keys with valid types", () => {
  assert.deepEqual(sanitizeSiteConfig({ proxyUrl: " http://u:p@h:8005 ", clientLabel: "flow-ultra-01", proxyAllHosts: true, apiKey: "x" }),
    { proxyUrl: "http://u:p@h:8005", clientLabel: "flow-ultra-01", proxyAllHosts: true });
  assert.deepEqual(sanitizeSiteConfig({ proxyUrl: "ftp://h:1", proxyAllHosts: "yes" }), {});
  assert.deepEqual(sanitizeSiteConfig(null), {});
  assert.deepEqual(sanitizeSiteConfig([1]), {});
});

test("buildPacScript: staff default proxies Flow + reCAPTCHA only; site mode proxies everything but local", () => {
  const staff = buildPacScript("PROXY h:8005", false);
  assert.match(staff, /flow\.google\.com/); assert.match(staff, /return 'DIRECT';\n}$/);
  const all = buildPacScript("PROXY h:8005", true);
  assert.match(all, /isPlainHostName/); assert.match(all, /return P;\n}$/);
  assert.doesNotMatch(all, /flow\.google\.com/);
  const fn = new Function(all + "; return FindProxyForURL;")();
  // PAC globals are not defined in Node; the local-host branch short-circuits before any lookup.
  globalThis.isPlainHostName = (h) => !h.includes(".");
  assert.equal(fn("https://accounts.google.com/", "accounts.google.com"), "PROXY h:8005");
  assert.equal(fn("http://localhost/", "localhost"), "DIRECT");
});
