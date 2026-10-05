#!/usr/bin/env python3
"""In-container CDP helper for the Ultra host agent (standard library only).

The agent never reaches Chrome's debug port directly: it is bound to the CONTAINER's loopback (127.0.0.1:9222).
For every step the agent runs
    docker exec -i <container> python3 -c '<loader>'
and sends {"src": <this file>, "cmd": ..., "params": {...}} on stdin, so secrets (a password, the plugin
connection token) never appear in a command line or a file. One JSON line comes back on stdout:
{"ok": true, "result": ...} or {"ok": false, "error": "..."}. Nothing here prints a parameter value.

Commands: targets, open_tab, close_tab, navigate, read, type, click, key, screenshot, cookies, ext_bootstrap.
"""
import base64
import json
import os
import random
import socket
import sys
import time
import urllib.request

DEBUG = "127.0.0.1:9222"


class CDP:
    def __init__(self, timeout=30):
        ver = json.load(urllib.request.urlopen(f"http://{DEBUG}/json/version", timeout=10))
        path = "/" + ver["webSocketDebuggerUrl"].split("/", 3)[3]
        host, port = DEBUG.split(":")
        self.s = socket.create_connection((host, int(port)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET {path} HTTP/1.1\r\nHost: {DEBUG}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.s.recv(4096)
            if not chunk:
                raise EOFError("CDP handshake closed")
            resp += chunk
        if b" 101 " not in resp.split(b"\r\n")[0]:
            raise RuntimeError("CDP handshake refused")
        self.buf = resp.split(b"\r\n\r\n", 1)[1]
        self.next_id = 0

    def _rd(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(max(65536, n - len(self.buf)))
            if not chunk:
                raise EOFError("CDP socket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _frame(self):
        h = self._rd(2)
        fin, op, n = h[0] & 0x80, h[0] & 0x0F, h[1] & 0x7F
        if n == 126:
            n = int.from_bytes(self._rd(2), "big")
        elif n == 127:
            n = int.from_bytes(self._rd(8), "big")
        if h[1] & 0x80:
            mask = self._rd(4)
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(self._rd(n)))
        else:
            data = self._rd(n)
        return fin, op, data

    def _recv(self):
        parts = []
        while True:
            fin, op, data = self._frame()
            if op == 0x9:  # ping
                self._send_raw(0xA, data)
                continue
            if op == 0x8:
                raise EOFError("CDP closed the socket")
            parts.append(data)
            if fin:
                return json.loads(b"".join(parts))

    def _send_raw(self, op, data):
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            hdr = bytes([0x80 | op, 0x80 | n])
        elif n < 65536:
            hdr = bytes([0x80 | op, 0x80 | 126]) + n.to_bytes(2, "big")
        else:
            hdr = bytes([0x80 | op, 0x80 | 127]) + n.to_bytes(8, "big")
        self.s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def call(self, method, params=None, session=None):
        self.next_id += 1
        msg = {"id": self.next_id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self._send_raw(0x1, json.dumps(msg).encode())
        while True:
            m = self._recv()
            if m.get("id") == msg["id"]:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error'].get('message')}")
                return m.get("result") or {}

    def attach(self, target_id):
        return self.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]

    def evaluate(self, session, expression, await_promise=False):
        r = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                            "awaitPromise": await_promise}, session)
        if r.get("exceptionDetails"):
            raise RuntimeError("page script failed: " + str(r["exceptionDetails"].get("text", ""))[:200])
        return (r.get("result") or {}).get("value")


def _targets(cdp):
    return [{"id": t["targetId"], "type": t["type"], "url": t.get("url", ""), "title": t.get("title", "")}
            for t in cdp.call("Target.getTargets")["targetInfos"]]


# Deep (shadow-root aware) search for visible inputs and clickable elements, run inside the page.
_DEEP_JS = r"""
(() => {
  const all = [];
  const walk = (root) => { for (const el of root.querySelectorAll('*')) { all.push(el); if (el.shadowRoot) walk(el.shadowRoot); } };
  walk(document);
  const vis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const label = (el) => (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 80);
  return { all, vis, label };
})()
"""


def cmd_read(cdp, p):
    s = cdp.attach(p["target_id"])
    js = "(() => { const D = " + _DEEP_JS + r""";
      const inputs = D.all.filter(e => e.tagName === 'INPUT' && D.vis(e))
        .map(e => ({type: e.type, name: e.name || '', id: e.id || '', aria: e.getAttribute('aria-label') || '', autocomplete: e.autocomplete || ''}));
      const buttons = D.all.filter(e => (e.tagName === 'BUTTON' || e.getAttribute('role') === 'button' || e.getAttribute('role') === 'link' || e.tagName === 'A') && D.vis(e))
        .map(D.label).filter(Boolean).slice(0, 60);
      const projects = Array.from(document.querySelectorAll('a[href*="/project/"]')).map(a => a.href).slice(0, 20);
      return {url: location.href, title: document.title, text: (document.body ? document.body.innerText : '').slice(0, 6000),
              inputs, buttons, projects};
    })()"""
    return cdp.evaluate(s, js)


def cmd_type(cdp, p):
    s = cdp.attach(p["target_id"])
    sel = json.dumps(p["selector"])
    focused = cdp.evaluate(s, "(() => { const el = document.querySelector(" + sel + "); if (!el) return false;"
                              " el.focus(); el.click(); return document.activeElement === el; })()")
    if not focused:
        raise RuntimeError("field not found")
    lo, hi = int(p.get("min_ms", 80)), int(p.get("max_ms", 150))
    for ch in p["text"]:
        cdp.call("Input.insertText", {"text": ch}, s)
        time.sleep(random.uniform(lo, hi) / 1000.0)
    return {"typed": len(p["text"])}


def cmd_click(cdp, p):
    s = cdp.attach(p["target_id"])
    want = json.dumps({"selector": p.get("selector") or "", "text": p.get("text") or "", "exact": bool(p.get("exact"))})
    js = "(() => { const D = " + _DEEP_JS + "; const w = " + want + r""";
      let el = null;
      if (w.selector) { el = document.querySelector(w.selector); }
      if (!el && w.text) {
        const t = w.text.toLowerCase();
        const cands = D.all.filter(e => (e.tagName === 'BUTTON' || e.getAttribute('role') === 'button' || e.tagName === 'A'
          || e.getAttribute('role') === 'link' || e.tagName === 'LI' || e.getAttribute('data-identifier')) && D.vis(e));
        el = cands.find(e => w.exact ? D.label(e).toLowerCase() === t : D.label(e).toLowerCase().includes(t)) || null;
      }
      if (!el) return null;
      el.scrollIntoView({block: 'center'});
      const r = el.getBoundingClientRect();
      return {x: r.left + r.width / 2, y: r.top + r.height / 2, label: D.label(el)};
    })()"""
    pt = cdp.evaluate(s, js)
    if not pt:
        raise RuntimeError("element not found")
    for typ in ("mouseMoved", "mousePressed", "mouseReleased"):
        cdp.call("Input.dispatchMouseEvent", {"type": typ, "x": pt["x"], "y": pt["y"], "button": "left", "clickCount": 1}, s)
        time.sleep(0.05)
    return {"clicked": pt["label"]}


def cmd_key(cdp, p):
    s = cdp.attach(p["target_id"])
    key = p.get("key", "Enter")
    for typ in ("keyDown", "keyUp"):
        cdp.call("Input.dispatchKeyEvent", {"type": typ, "key": key, "code": key, "windowsVirtualKeyCode": 13 if key == "Enter" else 0}, s)
    return {"key": key}


def cmd_screenshot(cdp, p):
    tid = p.get("target_id")
    if not tid:
        pages = [t for t in _targets(cdp) if t["type"] == "page" and not t["url"].startswith("chrome-extension://")]
        if not pages:
            raise RuntimeError("no page to capture")
        tid = pages[0]["id"]
    s = cdp.attach(tid)
    return {"png_b64": cdp.call("Page.captureScreenshot", {"format": "png"}, s)["data"], "target_id": tid}


def cmd_cookies(cdp, p):
    names = sorted({c["name"] for c in cdp.call("Storage.getCookies").get("cookies", [])
                    if str(c.get("domain", "")).lstrip(".").endswith("google.com")})
    login = [n for n in names if n in ("SID", "__Secure-1PSID", "__Secure-3PSID", "SAPISID", "HSID")]
    return {"google_signed_in": bool({"SID", "__Secure-1PSID"} & set(names)), "login_cookie_names": login}


def cmd_ext_bootstrap(cdp, p):
    sw = [t for t in _targets(cdp) if t["type"] == "service_worker" and t["url"].startswith("chrome-extension://")
          and t["url"].endswith("/background.js")]
    if not sw:
        raise RuntimeError("worker extension service worker not found")
    s = cdp.attach(sw[0]["id"])
    cfg = json.dumps({"serverBase": p["serverBase"], "connectionToken": p["connectionToken"], "routeKey": p["routeKey"]})
    out = cdp.evaluate(s, "(async () => { const cfg = " + cfg + ";"
                          " if (typeof globalThis.flowBootstrap === 'function') { const r = await globalThis.flowBootstrap(cfg);"
                          "   return {ok: !!r.ok, routeKey: r.routeKey || '', version: r.version || '', via: 'flowBootstrap', error: r.error || ''}; }"
                          " await chrome.storage.local.set(cfg);"
                          " const s = await chrome.storage.local.get(['routeKey']);"
                          " return {ok: true, routeKey: s.routeKey || '', version: chrome.runtime.getManifest().version, via: 'storage'}; })()",
                       await_promise=True)
    return out


def cmd_ext_state(cdp, p):
    sw = [t for t in _targets(cdp) if t["type"] == "service_worker" and t["url"].endswith("/background.js")]
    if not sw:
        return {"routeKey": None}
    s = cdp.attach(sw[0]["id"])
    return cdp.evaluate(s, "(async () => { const s = await chrome.storage.local.get(['routeKey', 'autoRouteKey']);"
                           " return {routeKey: s.routeKey || s.autoRouteKey || null, version: chrome.runtime.getManifest().version}; })()",
                        await_promise=True)


def dispatch(cmd, p):
    cdp = CDP()
    if cmd == "targets":
        return _targets(cdp)
    if cmd == "open_tab":
        return {"target_id": cdp.call("Target.createTarget", {"url": p.get("url", "about:blank"), "background": bool(p.get("background", True))})["targetId"]}
    if cmd == "close_tab":
        cdp.call("Target.closeTarget", {"targetId": p["target_id"]})
        return {}
    if cmd == "navigate":
        cdp.call("Page.navigate", {"url": p["url"]}, cdp.attach(p["target_id"]))
        return {}
    handlers = {"read": cmd_read, "type": cmd_type, "click": cmd_click, "key": cmd_key, "screenshot": cmd_screenshot,
                "cookies": cmd_cookies, "ext_bootstrap": cmd_ext_bootstrap, "ext_state": cmd_ext_state}
    if cmd not in handlers:
        raise RuntimeError(f"unknown command {cmd}")
    return handlers[cmd](cdp, p)


def main(cmd, params):
    try:
        out = {"ok": True, "result": dispatch(cmd, params)}
    except Exception as e:
        out = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


if __name__ == "fu_helper":
    main(CMD, PARAMS)  # noqa: F821 — injected by the agent's loader
elif __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "targets", json.loads(sys.argv[2]) if len(sys.argv) > 2 else {})
