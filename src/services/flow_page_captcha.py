"""Server-side reCAPTCHA fallback: mint a token on flow.google.com/about with a
Playwright Chromium that egresses through the account's own proxy.

Why this exists (2026-09-23): Google's Flow build only accepts reCAPTCHA tokens
minted on flow.google.com by a browser it trusts. Staff Chromes (worker extension
3.6.x) do that; when a worker is offline, minted a token Google refuses, or could
not mint at all, this service mints instead, so a Google rule change never again
needs 40 manual extension reloads.

What was proven live before this was written (tmp/server_captcha_fallback_plan.md):
- /about loads no reCAPTCHA; we load enterprise.js with Flow's site key ourselves
  (context bypass_csp) and call execute(); redeem accepts the token (HTTP 200) when
  the browser identity is consistent (no user-agent override; redeem MUST use the UA
  this Chromium reports), automation flags hidden, headed on Xvfb, mint and redeem
  through the same account proxy.
- Chromium hangs on every Google host through these proxies unless launched with
  --disable-http2 (--disable-quic kept as well).
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from typing import Any, Dict, Optional

from ..core.config import config
from ..core.logger import debug_logger, mask_proxy_url

RECAPTCHA_SITE_KEY = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
MINT_URL = "https://flow.google.com/about"
DEFAULT_MINT_DEADLINE_SECONDS = 30.0
PAGE_LOAD_TIMEOUT_MS = 15000
EXECUTE_TIMEOUT_MS = 10000
READY_TIMEOUT_MS = 15000
IDLE_BROWSER_TTL_SECONDS = 600
WARMUP_DWELL_SECONDS = 4.0

_PROXY_RE = re.compile(r"^(https?|socks5h?)://(?:([^:]+):([^@]+)@)?([^:/]+):(\d+)/?$")

# Runs before any page script: hide the automation flag, mark the page as ours.
_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
"""

# Load reCAPTCHA enterprise on a page that has none, marked as OUR instance.
# Resolves with a verdict object; never rejects (a rejection would lose the reason).
_INJECT_SCRIPT = """
([siteKey]) => new Promise((resolve) => {
  try {
    if (window.grecaptcha && window.grecaptcha.enterprise && !window.__f2aServerInjected) {
      resolve({ ok: false, err: 'page loaded reCAPTCHA itself (possible execute trap)' });
      return;
    }
    if (window.__f2aServerInjected && window.grecaptcha && window.grecaptcha.enterprise) {
      resolve({ ok: true, reused: true });
      return;
    }
    Object.defineProperty(window, '__f2aServerInjected', { value: true, enumerable: false, configurable: true });
    const s = document.createElement('script');
    s.src = 'https://www.google.com/recaptcha/enterprise.js?render=' + siteKey;
    s.onload = () => resolve({ ok: true, reused: false });
    s.onerror = () => resolve({ ok: false, err: 'failed to load enterprise.js' });
    (document.head || document.documentElement).appendChild(s);
    setTimeout(() => resolve({ ok: false, err: 'timeout loading enterprise.js' }), 12000);
  } catch (e) { resolve({ ok: false, err: String(e && e.message || e) }); }
})
"""

_EXECUTE_SCRIPT = """
([siteKey, action, timeoutMs]) => new Promise((resolve) => {
  let settled = false;
  const done = (v) => { if (!settled) { settled = true; resolve(v); } };
  try {
    if (!(window.grecaptcha && window.grecaptcha.enterprise && window.grecaptcha.enterprise.execute)) {
      done({ ok: false, err: 'grecaptcha.enterprise not ready' }); return;
    }
    grecaptcha.enterprise.ready(() => {
      grecaptcha.enterprise.execute(siteKey, { action })
        .then((t) => done(t ? { ok: true, token: t } : { ok: false, err: 'empty token' }))
        .catch((e) => done({ ok: false, err: String(e && e.message || e) }));
    });
    setTimeout(() => done({ ok: false, err: 'timeout executing reCAPTCHA' }), timeoutMs);
  } catch (e) { done({ ok: false, err: String(e && e.message || e) }); }
})
"""


def _parse_proxy(proxy_url: str) -> Optional[Dict[str, str]]:
    m = _PROXY_RE.match((proxy_url or "").strip())
    if not m:
        return None
    scheme, user, password, host, port = m.groups()
    server_scheme = "socks5" if scheme.startswith("socks5") else "http"
    out: Dict[str, str] = {"server": f"{server_scheme}://{host}:{port}"}
    if user:
        out["username"] = user
        out["password"] = password or ""
    return out


class _BrowserSlot:
    """One Chromium (one proxy) with a single warm page on the mint URL."""

    def __init__(self, proxy_url: str):
        self.proxy_url = proxy_url
        self.browser = None
        self.context = None
        self.page = None
        self.user_agent: str = ""
        self.lock = asyncio.Lock()      # one mint at a time on this page
        self.last_used = time.monotonic()
        self.dirty = True               # page must be (re)loaded before the next mint
        self.busy = False

    async def close(self) -> None:
        for obj in (self.context, self.browser):
            try:
                if obj is not None:
                    await obj.close()
            except Exception:
                pass
        self.browser = self.context = self.page = None


class FlowPageCaptchaService:
    """Process singleton. All waits are cut from one absolute deadline per mint."""

    _instance: Optional["FlowPageCaptchaService"] = None
    _instance_lock = asyncio.Lock()

    def __init__(self) -> None:
        self._playwright = None
        self._pw_lock = asyncio.Lock()          # atomic slot reservation / eviction
        self._slots: Dict[str, _BrowserSlot] = {}
        self._available: Optional[bool] = None  # cached capability probe
        self._unavailable_reason = ""
        self._warned_unavailable = False
        self._waiters = 0
        self.stats: Dict[str, int] = {"ok": 0, "fail": 0, "timeout": 0, "launches": 0}

    @classmethod
    async def get_instance(cls) -> "FlowPageCaptchaService":
        async with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ------------------------------------------------------------ capability
    def is_available(self) -> bool:
        """Cheap, cached: can this process mint at all? Never launches a browser."""
        if self._available is not None:
            return self._available
        reason = ""
        try:
            import playwright  # noqa: F401
            from playwright.async_api import async_playwright  # noqa: F401
        except Exception as exc:  # pragma: no cover - depends on the image
            reason = f"playwright not importable: {exc}"
        if not reason and not self._chromium_present():
            reason = "no Playwright Chromium executable in this image"
        self._available = not reason
        self._unavailable_reason = reason
        if reason and not self._warned_unavailable:
            self._warned_unavailable = True
            debug_logger.op_warning(f"[FALLBACK_MINT] unavailable in this deployment: {reason}")
        return self._available

    @staticmethod
    def _chromium_present() -> bool:
        exe = os.environ.get("BROWSER_EXECUTABLE_PATH", "").strip()
        if exe and os.path.exists(exe):
            return True
        for base in (
            os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip(),
            os.path.expanduser("~/.cache/ms-playwright"),
            "/ms-playwright",
        ):
            if base and os.path.isdir(base) and any(n.startswith("chromium") for n in os.listdir(base)):
                return True
        return bool(shutil.which("chromium") or shutil.which("chromium-browser"))

    def _headed(self) -> bool:
        display = os.environ.get("DISPLAY", "").strip()
        allow = os.environ.get("ALLOW_DOCKER_HEADED_CAPTCHA", "true").strip().lower() in ("1", "true", "yes")
        return bool(display) and allow

    def status(self) -> Dict[str, Any]:
        return {
            "available": self.is_available(),
            "unavailable_reason": self._unavailable_reason,
            "enabled": bool(config.captcha_server_fallback_enabled),
            "max_browsers": int(config.captcha_server_fallback_max_browsers),
            "browsers": len(self._slots),
            "busy": sum(1 for s in self._slots.values() if s.busy),
            "waiters": self._waiters,
            "headed": self._headed(),
            **self.stats,
        }

    # ------------------------------------------------------------------ mint
    async def mint(
        self,
        action: str,
        proxy_url: str,
        token_id: Optional[int] = None,
        deadline_at: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return {"token", "user_agent", "ms"} or None. Never raises except CancelledError."""
        if not config.captcha_server_fallback_enabled or not self.is_available():
            return None
        proxy = _parse_proxy(proxy_url)
        if not proxy:
            debug_logger.op_warning(f"[FALLBACK_MINT] token={token_id} no usable proxy; refusing to mint from the server IP")
            return None
        started = time.monotonic()
        deadline = deadline_at if deadline_at is not None else started + DEFAULT_MINT_DEADLINE_SECONDS
        egress = mask_proxy_url(proxy_url)
        outcome = "fail"
        slot: Optional[_BrowserSlot] = None
        try:
            slot = await self._acquire_slot(proxy_url, deadline)
            if slot is None:
                outcome = "timeout"
                debug_logger.op_warning(f"[FALLBACK_MINT] token={token_id} egress={egress} no browser slot before deadline")
                return None
            queue_ms = int((time.monotonic() - started) * 1000)
            result = await asyncio.wait_for(
                self._mint_on_slot(slot, action, token_id),
                timeout=max(0.5, deadline - time.monotonic()),
            )
            if result:
                outcome = "ok"
                ms = int((time.monotonic() - started) * 1000)
                debug_logger.event(
                    f"[FALLBACK_MINT] token={token_id} egress={egress} action={action} "
                    f"queue_ms={queue_ms} total_ms={ms} outcome=ok"
                )
                return {"token": result, "user_agent": slot.user_agent, "ms": ms}
            return None
        except asyncio.TimeoutError:
            outcome = "timeout"
            if slot is not None:
                slot.dirty = True  # a still-running execute must not overlap the next mint
            debug_logger.op_warning(f"[FALLBACK_MINT] token={token_id} egress={egress} action={action} outcome=timeout")
            return None
        except asyncio.CancelledError:
            if slot is not None:
                slot.dirty = True
            raise
        except Exception as exc:
            debug_logger.op_warning(f"[FALLBACK_MINT] token={token_id} egress={egress} action={action} outcome=error {type(exc).__name__}: {str(exc)[:160]}")
            return None
        finally:
            self.stats[outcome] = self.stats.get(outcome, 0) + 1
            if slot is not None:
                slot.busy = False
                slot.last_used = time.monotonic()
                if slot.lock.locked():
                    slot.lock.release()

    async def _acquire_slot(self, proxy_url: str, deadline: float) -> Optional[_BrowserSlot]:
        """Reserve the slot for proxy_url (creating/evicting under one lock), then
        take its per-page lock. Returns None when the deadline passes first."""
        self._waiters += 1
        try:
            while True:
                async with self._pw_lock:
                    slot = self._slots.get(proxy_url)
                    if slot is None:
                        cap = max(1, int(config.captcha_server_fallback_max_browsers))
                        if len(self._slots) >= cap:
                            victim = self._idle_victim()
                            if victim is None:
                                slot = None
                            else:
                                await self._close_slot(victim.proxy_url, "evicted for another proxy")
                        if slot is None and len(self._slots) < cap:
                            slot = _BrowserSlot(proxy_url)
                            self._slots[proxy_url] = slot
                if slot is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    try:
                        await asyncio.wait_for(slot.lock.acquire(), timeout=remaining)
                    except asyncio.TimeoutError:
                        return None
                    if self._slots.get(proxy_url) is not slot:
                        slot.lock.release()  # evicted while we waited; try again
                        continue
                    slot.busy = True
                    return slot
                if deadline - time.monotonic() <= 0:
                    return None
                await asyncio.sleep(0.25)
        finally:
            self._waiters -= 1

    def _idle_victim(self) -> Optional[_BrowserSlot]:
        idle = [s for s in self._slots.values() if not s.busy and not s.lock.locked()]
        if not idle:
            return None
        return min(idle, key=lambda s: s.last_used)

    async def _ensure_playwright(self):
        if self._playwright is None:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
        return self._playwright

    async def _launch(self, slot: _BrowserSlot) -> None:
        pw = await self._ensure_playwright()
        args = [
            "--no-sandbox", "--disable-dev-shm-usage", "--disable-http2", "--disable-quic",
            "--disable-blink-features=AutomationControlled", "--lang=en-US", "--window-size=1280,800",
            "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
        ]
        exe = os.environ.get("BROWSER_EXECUTABLE_PATH", "").strip() or None
        try:
            slot.browser = await pw.chromium.launch(
                headless=not self._headed(), proxy=_parse_proxy(slot.proxy_url), args=args, executable_path=exe,
            )
            slot.context = await slot.browser.new_context(
                bypass_csp=True, viewport={"width": 1280, "height": 800}, locale="en-US", timezone_id="Asia/Kolkata",
            )
            await slot.context.add_init_script(_INIT_SCRIPT)
            slot.page = await slot.context.new_page()
            slot.dirty = True
            self.stats["launches"] += 1
        except Exception:
            await slot.close()
            raise

    async def _load_page(self, slot: _BrowserSlot) -> None:
        page = slot.page
        await page.goto(MINT_URL, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        slot.user_agent = await page.evaluate("navigator.userAgent")
        try:
            await page.mouse.move(300, 200)
            await page.mouse.move(640, 420, steps=12)
            await page.mouse.wheel(0, 300)
        except Exception:
            pass
        await page.wait_for_timeout(int(WARMUP_DWELL_SECONDS * 1000))
        verdict = await page.evaluate(_INJECT_SCRIPT, [RECAPTCHA_SITE_KEY])
        if not verdict or not verdict.get("ok"):
            raise RuntimeError(f"reCAPTCHA load failed: {(verdict or {}).get('err', 'unknown')}")
        if not await self._wait_ready(page, READY_TIMEOUT_MS):
            raise RuntimeError("reCAPTCHA did not become ready after loading")
        slot.dirty = False

    @staticmethod
    async def _wait_ready(page, timeout_ms: int) -> bool:
        """grecaptcha.enterprise.execute exists (the inner library has loaded). On a busy
        box this takes longer than the page load itself, so it gets its own budget."""
        waited = 0
        while waited <= timeout_ms:
            if await page.evaluate("!!(window.grecaptcha&&grecaptcha.enterprise&&grecaptcha.enterprise.execute)"):
                return True
            await page.wait_for_timeout(250)
            waited += 250
        return False

    async def _mint_on_slot(self, slot: _BrowserSlot, action: str, token_id: Optional[int]) -> Optional[str]:
        if slot.browser is None or slot.page is None or slot.page.is_closed():
            await slot.close()
            await self._launch(slot)
        page = slot.page
        if slot.dirty or not (page.url or "").startswith("https://flow.google.com/"):
            await self._load_page(slot)
        elif not await self._wait_ready(page, 5000):
            slot.dirty = True
            await self._load_page(slot)
        verdict = await page.evaluate(_EXECUTE_SCRIPT, [RECAPTCHA_SITE_KEY, action, EXECUTE_TIMEOUT_MS])
        if verdict and verdict.get("ok") and verdict.get("token"):
            return verdict["token"]
        slot.dirty = True  # reload before the next attempt
        debug_logger.op_warning(
            f"[FALLBACK_MINT] token={token_id} egress={mask_proxy_url(slot.proxy_url)} action={action} "
            f"outcome=fail reason={(verdict or {}).get('err', 'no verdict')}"
        )
        return None

    # -------------------------------------------------------------- lifecycle
    async def _close_slot(self, proxy_url: str, why: str) -> None:
        slot = self._slots.pop(proxy_url, None)
        if slot is not None:
            await slot.close()
            debug_logger.event(f"[FALLBACK_MINT] closed browser egress={mask_proxy_url(proxy_url)} ({why})")

    async def sweep_idle(self, ttl_seconds: float = IDLE_BROWSER_TTL_SECONDS) -> int:
        """Close idle browsers (all of them when the feature is disabled or over the cap)."""
        now = time.monotonic()
        cap = max(1, int(config.captcha_server_fallback_max_browsers))
        closed = 0
        async with self._pw_lock:
            for url, slot in list(self._slots.items()):
                if slot.busy or slot.lock.locked():
                    continue
                over_cap = len(self._slots) > cap
                if (not config.captcha_server_fallback_enabled) or over_cap or now - slot.last_used > ttl_seconds:
                    await self._close_slot(url, "disabled" if not config.captcha_server_fallback_enabled else ("over cap" if over_cap else "idle"))
                    closed += 1
        return closed

    async def close(self) -> None:
        async with self._pw_lock:
            for url in list(self._slots.keys()):
                await self._close_slot(url, "shutdown")
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
