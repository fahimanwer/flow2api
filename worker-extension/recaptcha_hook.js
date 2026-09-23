// Runs in the page's MAIN world at document_start on flow.google.com, before any
// Flow script. Since 2026-09-23 Flow's bundle keeps the genuine
// grecaptcha.enterprise.execute in a private closure and overwrites the public one
// with a wrapper that stamps every outside caller's token with the action
// "extension_hijack_detected" (Google then answers "reCAPTCHA evaluation failed").
// We capture the genuine function the instant reCAPTCHA defines it, so the later
// wrapper is shown to the page but never replaces what we hold. The mint code in
// background.js prefers window.__f2aRealExecute when it is present.
(() => {
  const SLOT = "__f2aRealExecute";
  if (Object.prototype.hasOwnProperty.call(window, SLOT)) return;
  let real = null;
  Object.defineProperty(window, SLOT, { configurable: false, enumerable: false, get() { return real; } });

  const hookExecute = (ent) => {
    if (!ent || typeof ent !== "object") return;
    const d = Object.getOwnPropertyDescriptor(ent, "execute");
    if (d && !d.configurable) return;
    let cur = d && "value" in d ? d.value : (d && d.get ? d.get.call(ent) : undefined);
    if (typeof cur === "function" && !real) real = cur.bind(ent);
    Object.defineProperty(ent, "execute", {
      configurable: true, enumerable: true,
      get() { return cur; },
      set(v) { if (typeof v === "function" && !real) real = v.bind(ent); cur = v; },
    });
  };

  const hookEnterprise = (g) => {
    if (!g || typeof g !== "object") return;
    const d = Object.getOwnPropertyDescriptor(g, "enterprise");
    if (d && !d.configurable) return;
    let cur = d && "value" in d ? d.value : undefined;
    if (cur) hookExecute(cur);
    Object.defineProperty(g, "enterprise", {
      configurable: true, enumerable: true,
      get() { return cur; },
      set(v) { cur = v; hookExecute(v); },
    });
  };

  const d0 = Object.getOwnPropertyDescriptor(window, "grecaptcha");
  let g = d0 && "value" in d0 ? d0.value : undefined;
  if (g) hookEnterprise(g);
  Object.defineProperty(window, "grecaptcha", {
    configurable: true, enumerable: true,
    get() { return g; },
    set(v) { g = v; hookEnterprise(v); },
  });
})();
