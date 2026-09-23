// Runs in the page's MAIN world at document_start on the mint page
// (flow.google.com/about), before any Flow script. Since 2026-09-23 Flow's bundle
// keeps the genuine grecaptcha.enterprise.execute in a private closure and
// overwrites the public one with a wrapper that stamps every outside caller's
// token with the action "extension_hijack_detected" (Google then answers
// "reCAPTCHA evaluation failed"). /about does not load reCAPTCHA today; if a
// future build does, this captures the genuine function the instant reCAPTCHA
// defines it, so the later wrapper is shown to the page but never replaces what
// we hold. background.js prefers window.__f2aRealExecute when it is present.
//
// Every accessor installed here is idempotent (marked with F2A): reCAPTCHA's own
// loader re-runs `window.grecaptcha = window.grecaptcha || {}`, and a naive
// re-hook would read the accessor's missing `value` and wipe `enterprise`.
(() => {
  const SLOT = "__f2aRealExecute";
  if (Object.prototype.hasOwnProperty.call(window, SLOT)) return;
  const F2A = Symbol("f2a-hook");
  let real = null;
  Object.defineProperty(window, SLOT, { configurable: false, enumerable: false, get() { return real; } });

  const current = (obj, key) => {
    const d = Object.getOwnPropertyDescriptor(obj, key);
    if (!d) return { d, value: undefined };
    if ("value" in d) return { d, value: d.value };
    return { d, value: d.get ? d.get.call(obj) : undefined };
  };

  const hookExecute = (ent) => {
    if (!ent || (typeof ent !== "object" && typeof ent !== "function")) return;
    const { d, value } = current(ent, "execute");
    if (d && d.get && d.get[F2A]) { if (typeof value === "function" && !real) real = value.bind(ent); return; }
    if (d && !d.configurable) { if (typeof value === "function" && !real) real = value.bind(ent); return; }
    let cur = value;
    if (typeof cur === "function" && !real) real = cur.bind(ent);
    const get = function () { return cur; };
    get[F2A] = true;
    Object.defineProperty(ent, "execute", {
      configurable: true, enumerable: true, get,
      set(v) { if (typeof v === "function" && !real) real = v.bind(ent); cur = v; },
    });
  };

  const hookEnterprise = (g) => {
    if (!g || (typeof g !== "object" && typeof g !== "function")) return;
    const { d, value } = current(g, "enterprise");
    if (d && d.get && d.get[F2A]) { hookExecute(value); return; }
    if (d && !d.configurable) { hookExecute(value); return; }
    let cur = value;
    if (cur) hookExecute(cur);
    const get = function () { return cur; };
    get[F2A] = true;
    Object.defineProperty(g, "enterprise", {
      configurable: true, enumerable: true, get,
      set(v) { cur = v; hookExecute(v); },
    });
  };

  const { d: d0, value: g0 } = current(window, "grecaptcha");
  if (d0 && d0.get && d0.get[F2A]) return;
  let g = g0;
  if (g) hookEnterprise(g);
  const get = function () { return g; };
  get[F2A] = true;
  Object.defineProperty(window, "grecaptcha", {
    configurable: true, enumerable: true, get,
    set(v) { g = v; hookEnterprise(v); },
  });
})();
