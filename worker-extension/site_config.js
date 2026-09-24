// Per-install overrides for a browser run on a server (e.g. flow-ultra-01 on cf-worker-01).
//
// A server-run worker needs three things staff laptops must not have: one fixed proxy for this profile,
// a label, and the proxy for EVERY host (the Google sign-in included, so Google sees one steady location).
// They live in `site.json` next to manifest.json. That file is NEVER part of the published zip, so the
// box can take every published update unchanged and keep its own settings (the updater copies it back in).
// Pure helpers; Node-tested (tests/ext/site_config.test.mjs).

/** Keep only the keys a site file may set, with the right types. Anything else is ignored. */
function sanitizeSiteConfig(raw) {
  const out = {};
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return out;
  if (typeof raw.proxyUrl === "string" && /^(https?|socks[45]):\/\//i.test(raw.proxyUrl.trim())) out.proxyUrl = raw.proxyUrl.trim();
  if (typeof raw.clientLabel === "string" && raw.clientLabel.trim()) out.clientLabel = raw.clientLabel.trim().slice(0, 60);
  if (raw.proxyAllHosts === true) out.proxyAllHosts = true;
  return out;
}

/** PAC script: Flow + reCAPTCHA through the proxy (staff default), or everything except local hosts (site). */
function buildPacScript(proxyToken, allHosts) {
  const q = String(proxyToken).replace(/'/g, "");
  const lines = ["function FindProxyForURL(url, host) {", "  var P = '" + q + "';"];
  if (allHosts) {
    lines.push("  if (isPlainHostName(host) || host === '127.0.0.1' || host === 'localhost') return 'DIRECT';");
    lines.push("  return P;");
  } else {
    lines.push("  if (dnsDomainIs(host, 'labs.google')) return P;");
    lines.push("  if (dnsDomainIs(host, 'flow.google.com')) return P;");
    lines.push("  if (shExpMatch(url, '*://www.google.com/recaptcha/*')) return P;");
    lines.push("  if (shExpMatch(url, '*://www.gstatic.com/recaptcha/*')) return P;");
    lines.push("  if (dnsDomainIs(host, 'recaptcha.net')) return P;");
    lines.push("  return 'DIRECT';");
  }
  lines.push("}");
  return lines.join("\n");
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { sanitizeSiteConfig, buildPacScript };
} else {
  globalThis.FlowSiteConfig = { sanitizeSiteConfig, buildPacScript };
}
