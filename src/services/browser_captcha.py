"""
Local RT-based reCAPTCHA solving service (closed-loop version, no fake_useragent)
Supports: auto Session Token refresh, externally triggered fingerprint switch, persistent retries
"""
import os
import sys
import subprocess
import signal
# Fix playwright asyncio compatibility on Windows
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")

import asyncio
import time
import re
import random
import uuid
import hashlib
import json
from copy import deepcopy
from typing import Optional, Dict, Any, List, Union
from datetime import datetime
from urllib.parse import urlparse, unquote, parse_qs

from ..core.logger import debug_logger
from ..core.browser_runtime_status import (
    fail_runtime_prepare,
    finish_runtime_prepare,
    progress_runtime_prepare,
)
from ..core.config import config
from .browser_cookie_utils import build_cookie_signature


# ==================== Docker detection ====================
def _is_running_in_docker() -> bool:
    """Detect whether we are running in a Docker container"""
    # Method 1: check for /.dockerenv
    if os.path.exists('/.dockerenv'):
        return True
    # Method 2: check cgroup
    try:
        with open('/proc/1/cgroup', 'r') as f:
            content = f.read()
            if 'docker' in content or 'kubepods' in content or 'containerd' in content:
                return True
    except:
        pass
    # Method 3: check environment variables
    if os.environ.get('DOCKER_CONTAINER') or os.environ.get('KUBERNETES_SERVICE_HOST'):
        return True
    return False


IS_DOCKER = _is_running_in_docker()


def _is_truthy_env(name: str) -> bool:
    """Return whether an environment variable is true."""
    value = os.environ.get(name, "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


ALLOW_DOCKER_HEADED = (
    _is_truthy_env("ALLOW_DOCKER_HEADED_CAPTCHA")
    or _is_truthy_env("ALLOW_DOCKER_BROWSER_CAPTCHA")
)
DOCKER_HEADED_BLOCKED = IS_DOCKER and not ALLOW_DOCKER_HEADED

BROWSER_ENVIRONMENT_PATCH_MARKER = "__flow2apiBrowserEnvironmentPatchInstalled__"


# ==================== playwright auto-install ====================
def _run_pip_install(package: str, use_mirror: bool = False) -> bool:
    """Run pip install"""
    cmd = [sys.executable, '-m', 'pip', 'install', package]
    if use_mirror:
        cmd.extend(['-i', 'https://pypi.tuna.tsinghua.edu.cn/simple'])
    
    try:
        debug_logger.log_info(f"[BrowserCaptcha] Installing {package}...")
        progress_runtime_prepare("browser", f"[BrowserCaptcha] Installing {package}...")
        print(f"[BrowserCaptcha] Installing {package}...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            debug_logger.log_info(f"[BrowserCaptcha] ✅ {package} installed")
            print(f"[BrowserCaptcha] ✅ {package} installed")
            return True
        else:
            debug_logger.log_warning(f"[BrowserCaptcha] {package} install failed: {result.stderr[:200]}")
            fail_runtime_prepare("browser", f"{package} install failed. Check the network or Python environment.")
            return False
    except Exception as e:
        debug_logger.log_warning(f"[BrowserCaptcha] {package} install error: {e}")
        fail_runtime_prepare("browser", f"{package} install error: {e}")
        return False


def _run_playwright_install(use_mirror: bool = False) -> bool:
    """Install the playwright chromium browser"""
    cmd = [sys.executable, '-m', 'playwright', 'install', 'chromium']
    env = os.environ.copy()
    
    if use_mirror:
        # Use the China mirror
        env['PLAYWRIGHT_DOWNLOAD_HOST'] = 'https://npmmirror.com/mirrors/playwright'
    
    try:
        debug_logger.log_info("[BrowserCaptcha] Installing chromium browser...")
        progress_runtime_prepare("browser", "Installing chromium browser, please wait...")
        print("[BrowserCaptcha] Installing chromium browser...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env)
        if result.returncode == 0:
            debug_logger.log_info("[BrowserCaptcha] ✅ chromium browser installed")
            progress_runtime_prepare("browser", "chromium browser installed, running final checks...")
            print("[BrowserCaptcha] ✅ chromium browser installed")
            return True
        else:
            debug_logger.log_warning(f"[BrowserCaptcha] chromium install failed: {result.stderr[:200]}")
            fail_runtime_prepare("browser", "chromium browser install failed. Retry later or install it manually.")
            return False
    except Exception as e:
        debug_logger.log_warning(f"[BrowserCaptcha] chromium install error: {e}")
        fail_runtime_prepare("browser", f"chromium browser install error: {e}")
        return False


def _ensure_playwright_installed() -> bool:
    """Make sure playwright is installed"""
    try:
        import playwright
        debug_logger.log_info("[BrowserCaptcha] playwright is installed")
        return True
    except ImportError:
        pass
    
    debug_logger.log_info("[BrowserCaptcha] playwright not installed, installing automatically...")
    progress_runtime_prepare("browser", "[BrowserCaptcha] playwright not installed, installing automatically...")
    print("[BrowserCaptcha] playwright not installed, installing automatically...")
    
    # Try the official index first
    if _run_pip_install('playwright', use_mirror=False):
        return True
    
    # Official index failed, try the China mirror
    debug_logger.log_info("[BrowserCaptcha] Install from official index failed, trying China mirror...")
    progress_runtime_prepare("browser", "[BrowserCaptcha] Install from official index failed, trying China mirror...")
    print("[BrowserCaptcha] Install from official index failed, trying China mirror...")
    if _run_pip_install('playwright', use_mirror=True):
        return True
    
    debug_logger.log_error("[BrowserCaptcha] ❌ playwright auto-install failed, install manually: pip install playwright")
    fail_runtime_prepare("browser", "[BrowserCaptcha] ❌ playwright auto-install failed, install manually: pip install playwright")
    print("[BrowserCaptcha] ❌ playwright auto-install failed, install manually: pip install playwright")
    return False


def _ensure_browser_installed() -> bool:
    """Make sure the chromium browser is installed"""
    try:
        detect_script = (
            "from playwright.sync_api import sync_playwright\n"
            "with sync_playwright() as p:\n"
            "    print(p.chromium.executable_path or '')\n"
        )
        env = os.environ.copy()
        env.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "0") or "0")
        result = subprocess.run(
            [sys.executable, "-c", detect_script],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        browser_path = (result.stdout or "").strip().splitlines()
        browser_path = browser_path[-1].strip() if browser_path else ""
        if result.returncode == 0 and browser_path and os.path.exists(browser_path):
            debug_logger.log_info(f"[BrowserCaptcha] chromium browser is installed: {browser_path}")
            finish_runtime_prepare("browser", "chromium browser found, preparing instance...")
            return True
    except Exception as e:
        debug_logger.log_info(f"[BrowserCaptcha] Error while checking browser: {e}")
    
    debug_logger.log_info("[BrowserCaptcha] chromium browser not installed, installing automatically...")
    progress_runtime_prepare("browser", "[BrowserCaptcha] chromium browser not installed, installing automatically...")
    print("[BrowserCaptcha] chromium browser not installed, installing automatically...")
    
    # Try the official index first
    if _run_playwright_install(use_mirror=False):
        return True
    
    # Official index failed, try the China mirror
    debug_logger.log_info("[BrowserCaptcha] Install from official index failed, trying China mirror...")
    progress_runtime_prepare("browser", "[BrowserCaptcha] Install from official index failed, trying China mirror...")
    print("[BrowserCaptcha] Install from official index failed, trying China mirror...")
    if _run_playwright_install(use_mirror=True):
        return True
    
    debug_logger.log_error("[BrowserCaptcha] ❌ chromium browser auto-install failed, install manually: python -m playwright install chromium")
    fail_runtime_prepare("browser", "[BrowserCaptcha] ❌ chromium browser auto-install failed, install manually: python -m playwright install chromium")
    print("[BrowserCaptcha] ❌ chromium browser auto-install failed, install manually: python -m playwright install chromium")
    return False


# Try to import playwright
async_playwright = None
Route = None
BrowserContext = None
PLAYWRIGHT_AVAILABLE = False

if DOCKER_HEADED_BLOCKED:
    debug_logger.log_warning(
        "[BrowserCaptcha] Docker detected; headed browser captcha is disabled by default. "
        "To enable it, set ALLOW_DOCKER_HEADED_CAPTCHA=true and provide DISPLAY/Xvfb."
    )
    print("[BrowserCaptcha] ⚠️ Docker detected; headed browser captcha is disabled by default")
    print("[BrowserCaptcha] To enable it, set ALLOW_DOCKER_HEADED_CAPTCHA=true and provide DISPLAY/Xvfb")
else:
    if IS_DOCKER and ALLOW_DOCKER_HEADED:
        debug_logger.log_warning(
            "[BrowserCaptcha] Docker headed browser captcha allowlist is on; make sure DISPLAY/Xvfb is available"
        )
        print("[BrowserCaptcha] ✅ Docker headed browser captcha allowlist is on")
    if _ensure_playwright_installed():
        try:
            from playwright.async_api import async_playwright, Route, BrowserContext
            PLAYWRIGHT_AVAILABLE = True
            # Check and install the browser
            _ensure_browser_installed()
        except ImportError as e:
            debug_logger.log_error(f"[BrowserCaptcha] playwright import failed: {e}")
            print(f"[BrowserCaptcha] ❌ playwright import failed: {e}")


# Config
LABS_URL = "https://labs.google/fx/tools/flow"
BROWSER_SESSION_COOKIE_TARGET_URLS = (
    "https://labs.google/",
    "https://www.google.com/",
    "https://www.recaptcha.net/",
)

# ==========================================
# Proxy parsing helpers
# ==========================================
def parse_proxy_url(proxy_url: str) -> Optional[Dict[str, str]]:
    """Parse a proxy URL (socks5h:// is supported and treated as socks5 in Playwright)"""
    if not proxy_url: return None
    if not re.match(r'^(http|https|socks5h?|socks5)://', proxy_url): proxy_url = f"http://{proxy_url}"
    match = re.match(r'^(socks5h?|socks5|http|https)://(?:([^:]+):([^@]+)@)?([^:]+):(\d+)$', proxy_url)
    if match:
        protocol, username, password, host, port = match.groups()
        browser_protocol = "socks5" if protocol.startswith("socks5") else protocol
        proxy_config = {'server': f'{browser_protocol}://{host}:{port}'}
        if username and password:
            proxy_config['username'] = username
            proxy_config['password'] = password
        return proxy_config
    return None

def normalize_browser_proxy_url(proxy_url: str) -> tuple[Optional[str], Optional[str]]:
    """Normalize a browser proxy into a format Playwright/Chromium accepts.

    Chromium does not support socks5/socks5h proxy auth with username and password.
    `socks5(h)://user:pass@host:port` is downgraded to `http://user:pass@host:port`,
    which works with providers that offer both HTTP and SOCKS5 endpoints.

    Returns:
        (normalized_proxy_url, warning_message)
    """
    if not proxy_url:
        return None, None

    proxy_url = proxy_url.strip()
    match = re.match(r'^(socks5h?|socks5|http|https)://(?:([^:]+):([^@]+)@)?([^:]+):(\d+)$', proxy_url)
    if not match:
        if not re.match(r'^(http|https|socks5h?|socks5)://', proxy_url):
            proxy_url = f"http://{proxy_url}"
        return proxy_url, None

    protocol, username, password, host, port = match.groups()
    if protocol.startswith("socks5") and username and password:
        normalized = f"http://{username}:{password}@{host}:{port}"
        warning = (
            f"Authenticated {protocol.upper()} proxy detected. "
            "Chromium does not support socks5 username/password auth, "
            f"so the browser was started with an HTTP proxy instead: http://{host}:{port}"
        )
        return normalized, warning

    if protocol == "socks5h":
        proxy_url = f"socks5://{host}:{port}"

    return proxy_url, None


# Round-robin selection across multiple proxy URLs (comma / newline / semicolon
# separated). With browser_count > 1, each captcha browser is assigned a distinct
# egress IP so concurrent reCAPTCHA solves spread across several IPs instead of
# bursting one, avoiding PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC.
_browser_proxy_rr_index = 0


def split_browser_proxy_candidates(value: Optional[str]) -> list:
    if not value:
        return []
    return [p.strip() for p in re.split(r'[,\n;]+', value) if p.strip()]


def select_round_robin_proxy(value: Optional[str]) -> Optional[str]:
    """Pick the next proxy URL from a separated list, rotating on each call.

    A single URL (no separators) is returned unchanged, so existing single-proxy
    configs behave exactly as before.
    """
    global _browser_proxy_rr_index
    candidates = split_browser_proxy_candidates(value)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    chosen = candidates[_browser_proxy_rr_index % len(candidates)]
    _browser_proxy_rr_index = (_browser_proxy_rr_index + 1) % len(candidates)
    return chosen


def validate_browser_proxy_url(proxy_url: str) -> tuple[bool, str]:
    if not proxy_url: return True, None
    normalized_proxy_url, _ = normalize_browser_proxy_url(proxy_url.strip())
    parsed = parse_proxy_url(normalized_proxy_url)
    if not parsed: return False, "Invalid proxy format"
    return True, None

class TokenBrowser:
    """Simple browser: launch a new browser for each token, close it after use
    
    Each launch gets a new random UA, avoiding issues from long-running browsers
    """
    # UA pool updated on 2026-03-01 from browsers that scored >= 0.3.
    UA_LIST = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.210 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.265 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.172 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.6668.177 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.6613.186 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 Edg/132.0.0.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.210 Safari/537.36 Edg/132.0.2957.171",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.265 Safari/537.36 Edg/131.0.2903.146",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.172 Safari/537.36 Edg/130.0.2849.142",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.6668.177 Safari/537.36 Edg/129.0.2792.124",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.6613.186 Safari/537.36 Edg/128.0.2739.111",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.210 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.265 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.172 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.6613.186 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 Edg/132.0.0.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.210 Safari/537.36 Edg/132.0.2957.171",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.265 Safari/537.36 Edg/131.0.2903.146",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.172 Safari/537.36 Edg/130.0.2849.142",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.6668.177 Safari/537.36 Edg/129.0.2792.124",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.6613.186 Safari/537.36 Edg/128.0.2739.111",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:134.0) Gecko/20100101 Firefox/134.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.3; rv:133.0) Gecko/20100101 Firefox/133.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.2; rv:132.0) Gecko/20100101 Firefox/132.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.1; rv:131.0) Gecko/20100101 Firefox/131.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:129.0) Gecko/20100101 Firefox/129.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:128.0) Gecko/20100101 Firefox/128.0",
        "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.163 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 14; SM-S9180) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.260 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.172 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 12; M2102J20SG) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.6668.177 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 11; M2012K11AC) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.6613.186 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 14; SM-S9180) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.260 Mobile Safari/537.36 EdgA/131.0.2903.146",
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.172 Mobile Safari/537.36 EdgA/130.0.2849.142",
        "Mozilla/5.0 (Linux; Android 12; M2102J20SG) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.6668.177 Mobile Safari/537.36 EdgA/129.0.2792.124",
        "Mozilla/5.0 (Linux; Android 11; M2012K11AC) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.6613.186 Mobile Safari/537.36 EdgA/128.0.2739.111",
        "Mozilla/5.0 (Linux; Android 14; SM-S9180) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/28.0 Chrome/132.0.6834.163 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 13; SM-S9110) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/27.0 Chrome/130.0.6723.172 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 12; SM-G9910) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/26.0 Chrome/128.0.6613.186 Mobile Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/132.0.6834.95 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/131.0.6778.112 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) EdgiOS/132.2957.171 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) EdgiOS/131.2903.146 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.210 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.210 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.210 Safari/537.36 Edg/132.0.2957.171",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.210 Safari/537.36 Edg/132.0.2957.171",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.6834.210 Safari/537.36 OPR/117.0.0.0",
    ]
    
    # Resolution pool
    RESOLUTIONS = [
        (1920, 1080), (2560, 1440), (3840, 2160), (1366, 768), (1536, 864),
        (1600, 900), (1280, 720), (1360, 768), (1920, 1200),
        (1440, 900), (1680, 1050), (1280, 800), (2560, 1600),
        (2880, 1800), (3024, 1890), (3456, 2160),
        (1280, 1024), (1024, 768), (1400, 1050),
        (1920, 1280), (2736, 1824), (2880, 1920), (3000, 2000),
        (2256, 1504), (2496, 1664), (3240, 2160),
        (3200, 1800), (2304, 1440), (1800, 1200),
    ]
    
    def __init__(self, token_id: int, user_data_dir: str, db=None):
        self.token_id = token_id
        self.user_data_dir = user_data_dir
        self.db = db
        self._semaphore = asyncio.Semaphore(1)  # Only one active solve task is allowed per slot.
        self._solve_count = 0
        self._error_count = 0
        self._last_fingerprint: Optional[Dict[str, Any]] = None
        self._browser_proxy_active = False
        # Delay browser release after solve and track it by request_ref.
        self._pending_release_entries: Dict[str, Dict[str, Any]] = {}
        self._pending_release_lock = asyncio.Lock()
        # Browser mode keeps a shared in-memory browser instead of a persistent profile.
        self._shared_browser_lock = asyncio.Lock()
        self._shared_playwright = None
        self._shared_browser = None
        self._shared_context = None
        self._shared_keepalive_page = None
        self._shared_browser_pid: Optional[int] = None
        self._shared_bound_token_id: Optional[int] = None
        self._shared_bound_cookie_signature: Optional[str] = None
        self._pid_dir = os.path.join(os.getcwd(), "tmp", "browser_pids")
        self._pid_file = os.path.join(self._pid_dir, f"slot_{self.token_id}.pid")
        os.makedirs(self._pid_dir, exist_ok=True)
        self._shared_proxy_url: Optional[str] = None
        self._shared_launch_count = 0
        self._shared_reuse_count = 0
        self._consecutive_browser_failures = 0
        self._solve_inflight = 0
        self._last_idle_since = time.monotonic()
        self._refresh_browser_profile()

    def _refresh_browser_profile(self):
        """Refresh the in-memory browser fingerprint profile."""
        base_w, base_h = random.choice(self.RESOLUTIONS)
        self._profile_user_agent = random.choice(self.UA_LIST)
        self._profile_viewport = {
            "width": base_w,
            "height": base_h - random.randint(0, 80),
        }
        self._profile_env_seed = hashlib.sha256(
            f"{self.token_id}:{time.time_ns()}:{uuid.uuid4().hex}".encode("utf-8")
        ).hexdigest()
        self._profile_hardware_concurrency = random.choice([4, 6, 8, 8, 12, 16])
        self._profile_device_memory = random.choice([4, 8, 8, 16])
        self._profile_gpu = random.choice([
            {
                "vendor": "Google Inc. (Intel)",
                "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)",
                "unmaskedVendor": "Intel Inc.",
                "unmaskedRenderer": "Intel(R) UHD Graphics 620",
            },
            {
                "vendor": "Google Inc. (NVIDIA)",
                "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)",
                "unmaskedVendor": "NVIDIA Corporation",
                "unmaskedRenderer": "NVIDIA GeForce GTX 1650",
            },
            {
                "vendor": "Google Inc. (AMD)",
                "renderer": "ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
                "unmaskedVendor": "ATI Technologies Inc.",
                "unmaskedRenderer": "AMD Radeon(TM) Graphics",
            },
        ])

    def _build_browser_environment_patch_config(self) -> Dict[str, Any]:
        viewport = dict(self._profile_viewport or {})
        width = int(viewport.get("width") or 1366)
        height = int(viewport.get("height") or 768)
        seed = str(getattr(self, "_profile_env_seed", "") or "")
        if not seed:
            seed = hashlib.sha256(f"{self.token_id}:{width}:{height}".encode("utf-8")).hexdigest()
        digest = hashlib.sha256(seed.encode("utf-8")).digest()

        return {
            "seed": seed[:16],
            "navigator": {
                "language": "en-US",
                "languages": ["en-US", "en"],
                "hardwareConcurrency": int(getattr(self, "_profile_hardware_concurrency", 8) or 8),
                "deviceMemory": int(getattr(self, "_profile_device_memory", 8) or 8),
                "maxTouchPoints": 0,
                "pdfViewerEnabled": True,
                "webdriver": False,
            },
            "screen": {
                "width": width,
                "height": height,
                "availWidth": width,
                "availHeight": max(640, height - 40),
                "colorDepth": 24,
                "pixelDepth": 24,
            },
            "window": {
                "innerWidth": width,
                "innerHeight": height,
                "outerWidth": min(width + 16, width),
                "outerHeight": height + 88,
                "devicePixelRatio": 1,
            },
            "network": {
                "type": "wifi",
                "effectiveType": "4g",
                "rtt": 45 + int(digest[0] % 75),
                "downlink": round(6.0 + (int(digest[1] % 95) / 10.0), 1),
                "saveData": False,
            },
            "permissions": {
                "geolocation": "prompt",
                "notifications": "denied",
                "camera": "denied",
                "microphone": "denied",
                "display-capture": "denied",
            },
            "graphics": dict(getattr(self, "_profile_gpu", {}) or {}),
            "storage": {
                "quota": 120000000000 + int(digest[2] % 20) * 1000000000,
                "usage": 8000000 + int(digest[3] % 20) * 250000,
                "persisted": False,
            },
            "canvas": {
                "rgba": [
                    (int(digest[4]) % 5) - 2 or 1,
                    (int(digest[5]) % 5) - 2 or 1,
                    (int(digest[6]) % 5) - 2 or 1,
                    0,
                ],
                "pixelStep": 13 + (int(digest[7]) % 11),
            },
            "audio": {
                "floatDelta": round(((int(digest[8]) / 255.0) * 2.0 - 1.0) * 0.00003, 8),
                "byteDelta": (int(digest[9]) % 5) - 2 or 1,
                "stride": 17 + (int(digest[10]) % 13),
            },
            "mediaDevices": {
                "devices": [
                    {"kind": "audioinput", "deviceId": f"aid-{seed[:12]}", "groupId": f"grp-{seed[12:20]}", "label": ""},
                    {"kind": "videoinput", "deviceId": f"vid-{seed[20:32]}", "groupId": f"grp-{seed[12:20]}", "label": ""},
                    {"kind": "audiooutput", "deviceId": f"aod-{seed[32:44]}", "groupId": f"grp-{seed[44:52]}", "label": "Default Audio Output"},
                ],
            },
            "plugins": [
                {"name": "PDF Viewer", "filename": "internal-pdf-viewer", "description": "Portable Document Format", "mimeTypes": ["application/pdf", "text/pdf"]},
                {"name": "Chrome PDF Viewer", "filename": "internal-pdf-viewer", "description": "Portable Document Format", "mimeTypes": ["application/pdf", "text/pdf"]},
                {"name": "Chromium PDF Viewer", "filename": "internal-pdf-viewer", "description": "Portable Document Format", "mimeTypes": ["application/pdf", "text/pdf"]},
            ],
            "mimeTypes": [
                {"type": "application/pdf", "suffixes": "pdf", "description": "Portable Document Format", "pluginName": "PDF Viewer"},
                {"type": "text/pdf", "suffixes": "pdf", "description": "Portable Document Format", "pluginName": "PDF Viewer"},
            ],
        }

    def _build_browser_environment_patch_source(self) -> str:
        config_json = json.dumps(
            self._build_browser_environment_patch_config(),
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return (
            r"""
(() => {
    const marker = __MARKER_JSON__;
    if (window[marker]) {
        return;
    }
    const config = __CONFIG_JSON__;
    window[marker] = config.seed || true;

    const defineGetter = (target, key, getter, enumerable = true) => {
        if (!target) return;
        try {
            Object.defineProperty(target, key, { configurable: true, enumerable, get: getter });
        } catch (e) {}
    };
    const defineValue = (target, key, value, enumerable = false) => {
        if (!target) return;
        try {
            Object.defineProperty(target, key, { configurable: true, enumerable, writable: true, value });
        } catch (e) {
            try { target[key] = value; } catch (err) {}
        }
    };
    const makeNative = (fn, name) => {
        try {
            defineValue(fn, "name", name || fn.name || "");
            defineValue(fn, "toString", () => `function ${name || fn.name || ""}() { [native code] }`);
        } catch (e) {}
        return fn;
    };
    const clone = (value) => {
        try { return JSON.parse(JSON.stringify(value)); } catch (e) { return value; }
    };

    const nav = config.navigator || {};
    const navProto = Navigator.prototype;
    defineGetter(navProto, "webdriver", () => undefined);
    defineGetter(navigator, "webdriver", () => undefined);
    defineGetter(navProto, "language", () => nav.language || "en-US");
    defineGetter(navigator, "language", () => nav.language || "en-US");
    defineGetter(navProto, "languages", () => clone(nav.languages || ["en-US", "en"]));
    defineGetter(navigator, "languages", () => clone(nav.languages || ["en-US", "en"]));
    defineGetter(navProto, "hardwareConcurrency", () => Number(nav.hardwareConcurrency || 8));
    defineGetter(navigator, "hardwareConcurrency", () => Number(nav.hardwareConcurrency || 8));
    defineGetter(navProto, "deviceMemory", () => Number(nav.deviceMemory || 8));
    defineGetter(navigator, "deviceMemory", () => Number(nav.deviceMemory || 8));
    defineGetter(navProto, "maxTouchPoints", () => Number(nav.maxTouchPoints || 0));
    defineGetter(navigator, "maxTouchPoints", () => Number(nav.maxTouchPoints || 0));
    defineGetter(navProto, "pdfViewerEnabled", () => nav.pdfViewerEnabled !== false);
    defineGetter(navigator, "pdfViewerEnabled", () => nav.pdfViewerEnabled !== false);
    defineGetter(navProto, "cookieEnabled", () => true);
    defineGetter(navigator, "cookieEnabled", () => true);
    defineGetter(navProto, "onLine", () => true);
    defineGetter(navigator, "onLine", () => true);

    const screenProfile = config.screen || {};
    for (const key of ["width", "height", "availWidth", "availHeight", "colorDepth", "pixelDepth"]) {
        if (screenProfile[key] !== undefined) {
            defineGetter(Screen.prototype, key, () => Number(screenProfile[key]));
            defineGetter(screen, key, () => Number(screenProfile[key]));
        }
    }
    const win = config.window || {};
    for (const key of ["innerWidth", "innerHeight", "outerWidth", "outerHeight", "devicePixelRatio"]) {
        if (win[key] !== undefined) {
            defineGetter(window, key, () => Number(win[key]));
        }
    }

    defineGetter(Document.prototype, "visibilityState", () => "visible");
    defineGetter(document, "visibilityState", () => "visible");
    defineGetter(Document.prototype, "webkitVisibilityState", () => "visible");
    defineGetter(document, "webkitVisibilityState", () => "visible");
    defineGetter(Document.prototype, "hidden", () => false);
    defineGetter(document, "hidden", () => false);
    defineGetter(Document.prototype, "webkitHidden", () => false);
    defineGetter(document, "webkitHidden", () => false);
    try { document.hasFocus = makeNative(() => true, "hasFocus"); } catch (e) {}
    try { if (typeof window.focus === "function") window.focus(); } catch (e) {}

    const makeArrayLike = (items, namedKey) => {
        const arr = [];
        for (const item of items || []) {
            arr.push(item);
        }
        defineValue(arr, "item", makeNative((index) => arr[Number(index)] || null, "item"));
        defineValue(arr, "namedItem", makeNative((name) => arr.find((item) => item && item[namedKey] === name) || null, "namedItem"));
        defineValue(arr, "refresh", makeNative(() => undefined, "refresh"));
        for (const item of arr) {
            if (item && item[namedKey]) {
                try { Object.defineProperty(arr, item[namedKey], { configurable: true, enumerable: false, value: item }); } catch (e) {}
            }
        }
        return arr;
    };

    const mimeTypes = makeArrayLike((config.mimeTypes || []).map((item) => ({
        type: item.type,
        suffixes: item.suffixes || "",
        description: item.description || "",
        enabledPlugin: null,
    })), "type");
    const plugins = makeArrayLike((config.plugins || []).map((item) => {
        const plugin = {
            name: item.name,
            filename: item.filename || "",
            description: item.description || "",
            length: (item.mimeTypes || []).length,
        };
        (item.mimeTypes || []).forEach((type, index) => {
            const mt = mimeTypes.namedItem(type);
            if (mt) {
                mt.enabledPlugin = plugin;
                plugin[index] = mt;
                try { Object.defineProperty(plugin, type, { configurable: true, enumerable: false, value: mt }); } catch (e) {}
            }
        });
        plugin.item = makeNative((index) => plugin[Number(index)] || null, "item");
        plugin.namedItem = makeNative((name) => plugin[name] || null, "namedItem");
        return plugin;
    }), "name");
    defineGetter(navProto, "plugins", () => plugins);
    defineGetter(navigator, "plugins", () => plugins);
    defineGetter(navProto, "mimeTypes", () => mimeTypes);
    defineGetter(navigator, "mimeTypes", () => mimeTypes);

    const chromeObject = window.chrome || {};
    chromeObject.runtime = chromeObject.runtime || {
        PlatformOs: { MAC: "mac", WIN: "win", ANDROID: "android", CROS: "cros", LINUX: "linux", OPENBSD: "openbsd" },
        PlatformArch: { ARM: "arm", ARM64: "arm64", X86_32: "x86-32", X86_64: "x86-64" },
        PlatformNaclArch: { ARM: "arm", X86_32: "x86-32", X86_64: "x86-64" },
        RequestUpdateCheckStatus: { THROTTLED: "throttled", NO_UPDATE: "no_update", UPDATE_AVAILABLE: "update_available" },
        OnInstalledReason: { INSTALL: "install", UPDATE: "update", CHROME_UPDATE: "chrome_update", SHARED_MODULE_UPDATE: "shared_module_update" },
    };
    chromeObject.app = chromeObject.app || { isInstalled: false };
    chromeObject.csi = chromeObject.csi || makeNative(() => ({ startE: Date.now(), onloadT: Date.now(), pageT: 1, tran: 15 }), "csi");
    chromeObject.loadTimes = chromeObject.loadTimes || makeNative(() => ({
        requestTime: Date.now() / 1000,
        startLoadTime: Date.now() / 1000,
        commitLoadTime: Date.now() / 1000,
        finishDocumentLoadTime: Date.now() / 1000,
        finishLoadTime: Date.now() / 1000,
        firstPaintTime: Date.now() / 1000,
        firstPaintAfterLoadTime: 0,
        navigationType: "Other",
        wasFetchedViaSpdy: true,
        wasNpnNegotiated: true,
        npnNegotiatedProtocol: "h2",
        connectionInfo: "h2",
    }), "loadTimes");
    defineValue(window, "chrome", chromeObject, true);

    const permissions = config.permissions || {};
    if (navigator.permissions && typeof navigator.permissions.query === "function") {
        const originalQuery = navigator.permissions.query.bind(navigator.permissions);
        defineValue(navigator.permissions, "query", makeNative((descriptor) => {
            const name = descriptor && descriptor.name ? String(descriptor.name) : "";
            if (Object.prototype.hasOwnProperty.call(permissions, name)) {
                const status = {
                    name,
                    state: permissions[name],
                    onchange: null,
                    addEventListener: makeNative(() => undefined, "addEventListener"),
                    removeEventListener: makeNative(() => undefined, "removeEventListener"),
                    dispatchEvent: makeNative(() => true, "dispatchEvent"),
                };
                return Promise.resolve(status);
            }
            return originalQuery(descriptor);
        }, "query"));
    }
    try {
        if (window.Notification) {
            defineGetter(Notification, "permission", () => permissions.notifications || "denied");
            defineValue(Notification, "requestPermission", makeNative((callback) => {
                const value = permissions.notifications || "denied";
                if (typeof callback === "function") callback(value);
                return Promise.resolve(value);
            }, "requestPermission"));
        }
    } catch (e) {}

    const connectionProfile = config.network || {};
    const connection = navigator.connection || navigator.mozConnection || navigator.webkitConnection || {};
    for (const [key, value] of Object.entries(connectionProfile)) {
        defineGetter(connection, key, () => value);
    }
    if (!connection.addEventListener) defineValue(connection, "addEventListener", makeNative(() => undefined, "addEventListener"));
    if (!connection.removeEventListener) defineValue(connection, "removeEventListener", makeNative(() => undefined, "removeEventListener"));
    defineGetter(navProto, "connection", () => connection);
    defineGetter(navigator, "connection", () => connection);

    const mediaDevicesProfile = config.mediaDevices || {};
    const mediaDevices = navigator.mediaDevices || {};
    if (!mediaDevices.enumerateDevices) {
        defineValue(mediaDevices, "enumerateDevices", makeNative(() => Promise.resolve(clone(mediaDevicesProfile.devices || [])), "enumerateDevices"));
    }
    if (!mediaDevices.addEventListener) defineValue(mediaDevices, "addEventListener", makeNative(() => undefined, "addEventListener"));
    if (!mediaDevices.removeEventListener) defineValue(mediaDevices, "removeEventListener", makeNative(() => undefined, "removeEventListener"));
    defineGetter(navProto, "mediaDevices", () => mediaDevices);
    defineGetter(navigator, "mediaDevices", () => mediaDevices);

    const storageProfile = config.storage || {};
    if (navigator.storage) {
        if (typeof navigator.storage.estimate !== "function") {
            defineValue(navigator.storage, "estimate", makeNative(() => Promise.resolve({
                quota: Number(storageProfile.quota || 120000000000),
                usage: Number(storageProfile.usage || 8000000),
            }), "estimate"));
        }
        if (typeof navigator.storage.persisted !== "function") {
            defineValue(navigator.storage, "persisted", makeNative(() => Promise.resolve(!!storageProfile.persisted), "persisted"));
        }
    }

    const graphics = config.graphics || {};
    const patchWebGL = (proto) => {
        if (!proto || typeof proto.getParameter !== "function") return;
        const originalGetParameter = proto.getParameter;
        proto.getParameter = function(parameter) {
            try {
                const debugInfo = this.getExtension && this.getExtension("WEBGL_debug_renderer_info");
                if (debugInfo) {
                    if (parameter === debugInfo.UNMASKED_VENDOR_WEBGL) return graphics.unmaskedVendor || graphics.vendor || "Intel Inc.";
                    if (parameter === debugInfo.UNMASKED_RENDERER_WEBGL) return graphics.unmaskedRenderer || graphics.renderer || "Intel(R) UHD Graphics";
                }
                if (parameter === 37445) return graphics.unmaskedVendor || graphics.vendor || "Intel Inc.";
                if (parameter === 37446) return graphics.unmaskedRenderer || graphics.renderer || "Intel(R) UHD Graphics";
                if (parameter === 7936) return graphics.vendor || "Google Inc. (Intel)";
                if (parameter === 7937) return graphics.renderer || "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)";
            } catch (e) {}
            return originalGetParameter.apply(this, arguments);
        };
        makeNative(proto.getParameter, "getParameter");
    };
    try {
        patchWebGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
        patchWebGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);
    } catch (e) {}

    try {
        if (window.CanvasRenderingContext2D && CanvasRenderingContext2D.prototype) {
            const originalGetImageData = CanvasRenderingContext2D.prototype.getImageData;
            if (typeof originalGetImageData === "function") {
                CanvasRenderingContext2D.prototype.getImageData = function(...args) {
                    const imageData = originalGetImageData.apply(this, args);
                    try {
                        const data = imageData && imageData.data;
                        const rgba = config.canvas && config.canvas.rgba || [1, -1, 1, 0];
                        const step = Math.max(4, Number(config.canvas && config.canvas.pixelStep || 17) * 4);
                        for (let i = 0; data && i < data.length; i += step) {
                            data[i] = Math.max(0, Math.min(255, data[i] + rgba[0]));
                            data[i + 1] = Math.max(0, Math.min(255, data[i + 1] + rgba[1]));
                            data[i + 2] = Math.max(0, Math.min(255, data[i + 2] + rgba[2]));
                        }
                    } catch (e) {}
                    return imageData;
                };
                makeNative(CanvasRenderingContext2D.prototype.getImageData, "getImageData");
            }
        }
    } catch (e) {}

    try {
        if (window.AudioBuffer && AudioBuffer.prototype && typeof AudioBuffer.prototype.getChannelData === "function") {
            const originalGetChannelData = AudioBuffer.prototype.getChannelData;
            AudioBuffer.prototype.getChannelData = function(...args) {
                const channelData = originalGetChannelData.apply(this, args);
                try {
                    const stamp = "__flow2apiAudioPatch_" + (config.seed || "");
                    if (channelData && channelData.length && !channelData[stamp]) {
                        const stride = Math.max(1, Number(config.audio && config.audio.stride || 23));
                        const delta = Number(config.audio && config.audio.floatDelta || 0);
                        for (let i = 0; i < channelData.length; i += stride) {
                            channelData[i] = channelData[i] + delta;
                        }
                        defineValue(channelData, stamp, true);
                    }
                } catch (e) {}
                return channelData;
            };
            makeNative(AudioBuffer.prototype.getChannelData, "getChannelData");
        }
    } catch (e) {}
})();
"""
            .replace("__MARKER_JSON__", json.dumps(BROWSER_ENVIRONMENT_PATCH_MARKER))
            .replace("__CONFIG_JSON__", config_json)
        )

    async def _apply_browser_environment_patch(self, target, *, label: str) -> bool:
        if target is None:
            return False

        signature = str(getattr(self, "_profile_env_seed", "") or "")
        if getattr(target, "_flow2api_environment_patch_signature", None) == signature:
            return True

        try:
            await target.add_init_script(self._build_browser_environment_patch_source())
            try:
                target._flow2api_environment_patch_signature = signature
            except Exception:
                pass
            debug_logger.log_info(
                f"[BrowserCaptcha] Token-{self.token_id} injected browser environment patch script ({label})"
            )
            return True
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} failed to inject browser environment patch script ({label}): "
                f"{type(e).__name__}: {str(e)[:200]}"
            )
            return False

    def _get_slot_marker(self) -> str:
        return f"--flow2api-browser-slot={self.token_id}"

    def _read_pid_file(self) -> Optional[int]:
        try:
            if not os.path.exists(self._pid_file):
                return None
            with open(self._pid_file, 'r', encoding='utf-8') as handle:
                raw = (handle.read() or '').strip()
            return int(raw or '0') or None
        except Exception:
            return None

    def _write_pid_file(self, pid: Optional[int]):
        self._shared_browser_pid = pid
        try:
            if pid:
                with open(self._pid_file, 'w', encoding='utf-8') as handle:
                    handle.write(str(pid))
            elif os.path.exists(self._pid_file):
                os.remove(self._pid_file)
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] Token-{self.token_id} failed to write PID file: {e}")

    def _is_pid_running(self, pid: Optional[int]) -> bool:
        if not pid:
            return False
        try:
            if sys.platform.startswith('win'):
                result = subprocess.run(
                    ['tasklist', '/FI', f'PID eq {pid}'],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                return str(pid) in (result.stdout or '')
            os.kill(pid, 0)
            return True
        except Exception:
            return False

    def _pid_matches_slot(self, pid: Optional[int]) -> bool:
        if not pid:
            return False
        marker = self._get_slot_marker()
        try:
            if sys.platform.startswith('win'):
                result = subprocess.run(
                    [
                        'powershell',
                        '-NoProfile',
                        '-Command',
                        f'(Get-CimInstance Win32_Process -Filter "ProcessId = {pid}").CommandLine'
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                command_line = (result.stdout or '').strip()
            else:
                cmdline_path = f'/proc/{pid}/cmdline'
                if not os.path.exists(cmdline_path):
                    return False
                with open(cmdline_path, 'rb') as handle:
                    command_line = handle.read().decode('utf-8', errors='ignore').replace('\x00', ' ')
            return marker in command_line
        except Exception:
            return False

    async def _wait_pid_exit(self, pid: Optional[int], timeout_seconds: float = 5.0) -> bool:
        if not pid:
            return True
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if not self._is_pid_running(pid):
                return True
            await asyncio.sleep(0.2)
        return not self._is_pid_running(pid)

    def _kill_pid(self, pid: Optional[int], reason: str):
        if not pid:
            return
        try:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} browser process is still alive; force-killing PID={pid}, reason={reason}"
            )
            if sys.platform.startswith('win'):
                subprocess.run(
                    ['taskkill', '/PID', str(pid), '/T', '/F'],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            else:
                os.kill(pid, signal.SIGKILL)
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] Token-{self.token_id} failed to kill PID={pid}: {e}")

    async def _cleanup_stale_slot_process(self):
        stale_pid = self._read_pid_file()
        if not stale_pid:
            return
        if not self._is_pid_running(stale_pid):
            self._write_pid_file(None)
            return
        if not self._pid_matches_slot(stale_pid):
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} PID file points to a process that does not belong to this slot; ignoring PID={stale_pid}"
            )
            self._write_pid_file(None)
            return
        self._kill_pid(stale_pid, reason='stale_slot_process')
        await self._wait_pid_exit(stale_pid, timeout_seconds=3)
        self._write_pid_file(None)

    def _extract_browser_pid(self, browser) -> Optional[int]:
        candidates = [
            lambda obj: obj._impl_obj._connection._transport._proc.pid,
            lambda obj: obj._impl_obj._connection._transport._proc.pid if obj and obj._impl_obj else None,
        ]
        for getter in candidates:
            try:
                pid = getter(browser)
                if isinstance(pid, int) and pid > 0:
                    return pid
            except Exception:
                continue
        return None

    async def _ensure_shared_keepalive_page(self):
        """Ensure the shared browser always keeps one keepalive page alive."""
        keepalive_page = self._shared_keepalive_page
        try:
            if keepalive_page and not keepalive_page.is_closed():
                return keepalive_page
        except Exception:
            keepalive_page = None

        if not self._shared_context:
            return None

        keepalive_page = await self._shared_context.new_page()
        await self._apply_browser_environment_patch(keepalive_page, label="keepalive")
        try:
            await keepalive_page.goto("about:blank", wait_until="load", timeout=5000)
        except Exception:
            pass
        self._shared_keepalive_page = keepalive_page
        debug_logger.log_info(
            f"[BrowserCaptcha] Token-{self.token_id} keepalive page created"
        )
        return keepalive_page

    def _build_flow_project_url(self, project_id: Optional[str]) -> str:
        project_id = (project_id or "").strip()
        if project_id:
            return f"{LABS_URL}/project/{project_id}"
        return LABS_URL

    @staticmethod
    def _normalize_token_key(token_id: Optional[int]) -> Optional[int]:
        try:
            normalized = int(token_id or 0)
        except Exception:
            normalized = 0
        return normalized if normalized > 0 else None

    @classmethod
    def _build_token_session_cookie_targets(cls, session_token: str) -> List[Dict[str, Any]]:
        normalized_value = str(session_token or "").strip()
        if not normalized_value:
            return []

        expanded: List[Dict[str, Any]] = []
        for target_url in BROWSER_SESSION_COOKIE_TARGET_URLS:
            expanded.append({
                "name": "__Secure-next-auth.session-token",
                "value": normalized_value,
                "url": target_url,
                "secure": True,
                "httpOnly": True,
                "sameSite": "None",
            })
        return expanded

    async def _load_token_session_binding(self, token_id: Optional[int]) -> tuple[Optional[int], Optional[str], Optional[str]]:
        token_key = self._normalize_token_key(token_id)
        if token_key is None or not self.db:
            return token_key, None, None

        try:
            token = await self.db.get_token(token_key)
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] Token-{self.token_id} failed to read Session Token for token({token_key}): {e}")
            return token_key, None, None

        session_token = str(getattr(token, "st", "") or "").strip() if token else ""
        if not session_token:
            return token_key, None, None

        cookie_signature = build_cookie_signature(
            f"__Secure-next-auth.session-token={session_token}"
        ) or hashlib.sha256(session_token.encode("utf-8")).hexdigest()
        return token_key, session_token, cookie_signature

    async def _ensure_shared_token_binding(self, context, token_id: Optional[int]) -> bool:
        token_key, session_token, cookie_signature = await self._load_token_session_binding(token_id)

        if token_key is None:
            self._shared_bound_token_id = None
            self._shared_bound_cookie_signature = None
            return True

        if not session_token or not cookie_signature:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} no usable Session Token, cannot bind account login (token_id={token_key})"
            )
            return False

        if (
            self._shared_bound_token_id == token_key
            and self._shared_bound_cookie_signature == cookie_signature
        ):
            return True

        browser_cookies = self._build_token_session_cookie_targets(session_token)
        if not browser_cookies:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} failed to build Session Token cookies (token_id={token_key})"
            )
            return False

        try:
            await context.clear_cookies()
            await context.add_cookies(browser_cookies)
            self._shared_bound_token_id = token_key
            self._shared_bound_cookie_signature = cookie_signature
            debug_logger.log_info(
                f"[BrowserCaptcha] Token-{self.token_id} bound account token login to shared context "
                f"(token_id={token_key}, cookies={len(browser_cookies)})"
            )
            return True
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} failed to bind account token login "
                f"(token_id={token_key}): {type(e).__name__}: {str(e)[:200]}"
            )
            return False

    async def _prepare_flow_runtime_page(
        self,
        page,
        project_id: str,
        website_key: str,
        action: str,
        *,
        context_label: str = "",
    ) -> bool:
        """Open the real Flow page and warm it up."""
        primary_host = "https://www.recaptcha.net" if self._browser_proxy_active else "https://www.google.com"
        secondary_host = "https://www.google.com" if primary_host == "https://www.recaptcha.net" else "https://www.recaptcha.net"
        page_urls = [self._build_flow_project_url(project_id), LABS_URL]
        label = f"{context_label} " if context_label else ""

        loaded = False
        last_error: Optional[str] = None
        for index, target_url in enumerate(page_urls):
            try:
                debug_logger.log_info(
                    f"[BrowserCaptcha] Token-{self.token_id} {label}opening real Flow page: {target_url} (action={action})"
                )
                await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
                loaded = True
                break
            except Exception as e:
                last_error = f"{type(e).__name__}: {str(e)[:200]}"
                debug_logger.log_warning(
                    f"[BrowserCaptcha] Token-{self.token_id} {label}Flow page failed to open [{index + 1}/{len(page_urls)}]: {last_error}"
                )

        if not loaded:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} {label}could not open real Flow page: {last_error or 'unknown'}"
            )
            return False

        page_loaded = False
        for _ in range(20):
            try:
                ready_state = await page.evaluate("document.readyState")
                if ready_state == "complete":
                    page_loaded = True
                    break
            except Exception:
                pass
            await asyncio.sleep(0.5)
        if not page_loaded:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} {label}Flow page readyState not complete, continuing warmup"
            )

        try:
            await page.bring_to_front()
        except Exception:
            pass

        try:
            await page.mouse.move(320, 220)
            await page.mouse.move(560, 360, steps=16)
            await page.mouse.wheel(0, 260)
            await page.evaluate(
                """
                (() => {
                    try {
                        window.focus();
                        window.dispatchEvent(new Event('focus'));
                        document.dispatchEvent(new MouseEvent('mousemove', {
                            bubbles: true,
                            clientX: Math.max(32, Math.floor((window.innerWidth || 1280) * 0.42)),
                            clientY: Math.max(32, Math.floor((window.innerHeight || 720) * 0.36))
                        }));
                        window.scrollTo(0, Math.min(320, document.body?.scrollHeight || 320));
                    } catch (e) {}
                })()
                """
            )
        except Exception:
            pass

        warmup_seconds = float(getattr(config, "browser_flow_page_warmup_seconds", 6) or 6)
        if warmup_seconds > 0:
            debug_logger.log_info(
                f"[BrowserCaptcha] Token-{self.token_id} {label}real page warmup {warmup_seconds:.1f}s"
            )
            await asyncio.sleep(warmup_seconds)

        ready = await self._wait_for_enterprise_ready(
            page,
            website_key,
            primary_host,
            secondary_host,
            timeout_ms=15000,
            context_label=f"{label}real page",
        )
        if not ready:
            return False

        await self._capture_page_fingerprint(page)
        try:
            debug_logger.log_info(
                f"[BrowserCaptcha] Token-{self.token_id} {label}real page ready: url={page.url[:200]}"
            )
        except Exception:
            pass
        return True

    async def _resolve_proxy_runtime_config(self, token_proxy_url: Optional[str] = None) -> tuple:
        """Resolve runtime proxy configuration."""
        proxy_option = None
        raw_proxy_url = None
        proxy_source = "none"
        self._browser_proxy_active = False
        try:
            candidate_proxy_url = None
            if token_proxy_url and token_proxy_url.strip():
                candidate_proxy_url = select_round_robin_proxy(token_proxy_url)
                proxy_source = "token"
            elif self.db:
                captcha_config = await self.db.get_captcha_config()
                if captcha_config.browser_proxy_enabled and captcha_config.browser_proxy_url:
                    # browser_proxy_url may hold several comma-separated URLs; rotate
                    # across them so each concurrent browser uses a distinct egress IP.
                    candidate_proxy_url = select_round_robin_proxy(captcha_config.browser_proxy_url)
                    proxy_source = "global"

            if candidate_proxy_url:
                normalized_proxy_url, proxy_warning = normalize_browser_proxy_url(candidate_proxy_url)
                if proxy_warning:
                    debug_logger.log_warning(f"[BrowserCaptcha] Token-{self.token_id} {proxy_warning}")
                proxy_option = parse_proxy_url(normalized_proxy_url)
                if proxy_option:
                    raw_proxy_url = normalized_proxy_url
                    self._browser_proxy_active = True
                    debug_logger.log_info(
                        f"[BrowserCaptcha] Token-{self.token_id} using {proxy_source} proxy: {proxy_option['server']}"
                    )
                else:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] Token-{self.token_id} {proxy_source} proxy format is invalid and has been ignored"
                    )
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] Token-{self.token_id} failed to read proxy configuration: {e}")

        return proxy_option, raw_proxy_url, proxy_source

    async def _create_browser(self, token_proxy_url: Optional[str] = None, manage_slot_pid: bool = True) -> tuple:
        """Create a browser instance; shared-slot browsers track PIDs while temporary browsers do not."""
        width = self._profile_viewport["width"]
        height = self._profile_viewport["height"]
        viewport = {"width": width, "height": height}
        launch_in_background = bool(getattr(config, "browser_launch_background", True))

        if manage_slot_pid:
            await self._cleanup_stale_slot_process()
        playwright = await async_playwright().start()
        browser_executable_path = os.environ.get("BROWSER_EXECUTABLE_PATH", "").strip() or None
        proxy_option, raw_proxy_url, _ = await self._resolve_proxy_runtime_config(token_proxy_url=token_proxy_url)

        # Record only the proxy here; let the browser expose its real UA/UA-CH so user-agent and sec-ch-ua versions match.
        self._last_fingerprint = {
            "proxy_url": raw_proxy_url if raw_proxy_url else None,
        }

        try:
            browser_args = [
                '--disable-blink-features=AutomationControlled',
                '--disable-quic',
                '--disable-features=UseDnsHttpsSvcb',
                '--lang=en-US',
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--disable-setuid-sandbox',
                '--no-first-run',
                '--no-zygote',
                f'--window-size={width},{height}',
                '--disable-infobars',
                '--hide-scrollbars',
                '--profile-directory=Default',
                '--disable-extensions',
                '--disable-background-networking',
                '--disable-sync',
                '--disable-translate',
                '--disable-default-apps',
                '--no-default-browser-check',
            ]

            if launch_in_background:
                browser_args.extend([
                    '--start-minimized',
                    '--disable-background-timer-throttling',
                    '--disable-renderer-backgrounding',
                    '--disable-backgrounding-occluded-windows',
                    f'--flow2api-browser-slot={self.token_id}',
                ])
                if sys.platform.startswith("win"):
                    browser_args.append('--window-position=-32000,-32000')
                debug_logger.log_info(
                    f"[BrowserCaptcha] Token-{self.token_id} headed browser will launch in background mode"
                )

            if browser_executable_path:
                debug_logger.log_info(
                    f"[BrowserCaptcha] Token-{self.token_id} using custom browser executable: {browser_executable_path}"
                )

            browser = await playwright.chromium.launch(
                headless=False,
                executable_path=browser_executable_path,
                proxy=proxy_option,
                args=browser_args,
            )
            context = await browser.new_context(
                viewport=viewport,
                locale="en-US",
            )
            await self._apply_browser_environment_patch(context, label="context")
            browser_pid = self._extract_browser_pid(browser)
            if manage_slot_pid:
                self._write_pid_file(browser_pid)
            debug_logger.log_info(
                f"[BrowserCaptcha] Token-{self.token_id} shared browser started (proxy={'yes' if raw_proxy_url else 'no'})"
            )
            return playwright, browser, context
        except Exception as e:
            debug_logger.log_error(f"[BrowserCaptcha] Token-{self.token_id} browser launch failed: {type(e).__name__}: {str(e)[:200]}")
            try:
                if playwright:
                    await playwright.stop()
            except Exception:
                pass
            if manage_slot_pid:
                self._write_pid_file(None)
            raise

    async def _recycle_browser_locked(self, reason: str = "unknown", rotate_profile: bool = True):
        """Recycle the shared browser instance and reset its state."""
        playwright = self._shared_playwright
        browser = self._shared_browser
        context = self._shared_context
        keepalive_page = self._shared_keepalive_page
        browser_pid = self._shared_browser_pid or self._read_pid_file()
        had_browser = bool(playwright or browser or context or keepalive_page or browser_pid)

        self._shared_playwright = None
        self._shared_browser = None
        self._shared_context = None
        self._shared_keepalive_page = None
        self._shared_browser_pid = None
        self._shared_proxy_url = None
        self._shared_bound_token_id = None
        self._shared_bound_cookie_signature = None
        self._consecutive_browser_failures = 0
        self._shared_reuse_count = 0

        if rotate_profile:
            self._refresh_browser_profile()

        if had_browser:
            debug_logger.log_info(
                f"[BrowserCaptcha] Token-{self.token_id} shared browser recycled, reason={reason}"
            )
        await self._close_browser(playwright, browser, context, browser_pid=browser_pid)

    async def recycle_browser(self, reason: str = "unknown", rotate_profile: bool = True):
        """Recycle the current shared browser."""
        async with self._shared_browser_lock:
            await self._recycle_browser_locked(reason=reason, rotate_profile=rotate_profile)

    async def _get_or_create_shared_browser(
        self,
        token_proxy_url: Optional[str] = None,
        token_id: Optional[int] = None,
    ) -> tuple:
        """Get or create the shared browser for this slot."""
        _, expected_proxy_url, _ = await self._resolve_proxy_runtime_config(token_proxy_url=token_proxy_url)
        expected_token_key = self._normalize_token_key(token_id)

        async with self._shared_browser_lock:
            has_shared_browser = bool(self._shared_playwright and self._shared_browser and self._shared_context)

            if has_shared_browser:
                is_connected = True
                try:
                    checker = getattr(self._shared_browser, "is_connected", None)
                    if callable(checker):
                        is_connected = bool(checker())
                except Exception:
                    is_connected = False

                if not is_connected:
                    await self._recycle_browser_locked(reason="browser_disconnected", rotate_profile=False)
                    has_shared_browser = False

            if has_shared_browser and self._shared_proxy_url != expected_proxy_url:
                # If the proxy configuration changed, recycle the slot before reusing it.
                await self._recycle_browser_locked(reason="proxy_changed", rotate_profile=False)
                has_shared_browser = False

            if has_shared_browser:
                current_bound_token = self._shared_bound_token_id
                token_changed = (
                    expected_token_key != current_bound_token
                    and not (expected_token_key is None and current_bound_token is None)
                )
                if token_changed:
                    await self._recycle_browser_locked(
                        reason=f"token_binding_changed:{current_bound_token}->{expected_token_key}",
                        rotate_profile=False,
                    )
                    has_shared_browser = False

            if has_shared_browser:
                try:
                    await self._ensure_shared_keepalive_page()
                except Exception:
                    await self._recycle_browser_locked(reason="keepalive_page_broken", rotate_profile=False)
                    has_shared_browser = False

            if has_shared_browser:
                binding_ok = await self._ensure_shared_token_binding(self._shared_context, token_id)
                if not binding_ok:
                    await self._recycle_browser_locked(reason="token_binding_failed", rotate_profile=False)
                    has_shared_browser = False

            if has_shared_browser:
                self._shared_reuse_count += 1
                debug_logger.log_info(
                    f"[BrowserCaptcha] Token-{self.token_id} reusing shared browser (reuse={self._shared_reuse_count})"
                )
                return self._shared_playwright, self._shared_browser, self._shared_context

            playwright, browser, context = await self._create_browser(token_proxy_url=token_proxy_url)
            binding_ok = await self._ensure_shared_token_binding(context, token_id)
            if not binding_ok:
                await self._close_browser(
                    playwright,
                    browser,
                    context,
                    browser_pid=self._extract_browser_pid(browser),
                    clear_slot_pid=True,
                )
                raise RuntimeError("failed to bind token session context")
            self._shared_playwright = playwright
            self._shared_browser = browser
            self._shared_context = context
            await self._ensure_shared_keepalive_page()
            self._shared_proxy_url = (self._last_fingerprint or {}).get("proxy_url")
            self._shared_launch_count += 1
            self._shared_reuse_count = 0
            self.note_idle()
            return playwright, browser, context

    async def _capture_page_fingerprint(self, page):
        """Read UA and client hint headers from the browser page so they match the captcha browser."""
        try:
            fingerprint = await page.evaluate("""
                () => {
                    const ua = navigator.userAgent || "";
                    const lang = navigator.language || "";
                    const uaData = navigator.userAgentData || null;
                    let secChUa = "";
                    let secChUaMobile = "";
                    let secChUaPlatform = "";

                    if (uaData) {
                        if (Array.isArray(uaData.brands) && uaData.brands.length > 0) {
                            secChUa = uaData.brands
                                .map((item) => `"${item.brand}";v="${item.version}"`)
                                .join(", ");
                        }
                        secChUaMobile = uaData.mobile ? "?1" : "?0";
                        if (uaData.platform) {
                            secChUaPlatform = `"${uaData.platform}"`;
                        }
                    }

                    return {
                        user_agent: ua,
                        accept_language: lang,
                        sec_ch_ua: secChUa,
                        sec_ch_ua_mobile: secChUaMobile,
                        sec_ch_ua_platform: secChUaPlatform,
                    };
                }
            """)

            if not isinstance(fingerprint, dict):
                return

            if self._last_fingerprint is None:
                self._last_fingerprint = {}

            for key in ("user_agent", "accept_language", "sec_ch_ua", "sec_ch_ua_mobile", "sec_ch_ua_platform"):
                value = fingerprint.get(key)
                if isinstance(value, str) and value:
                    self._last_fingerprint[key] = value
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] Token-{self.token_id} failed to read browser fingerprint: {type(e).__name__}: {str(e)[:200]}")

    async def _verify_score_in_page(self, page, token: str, verify_url: str) -> Dict[str, Any]:
        """Read the score shown on the test page directly, since verify.php may disagree with the page."""
        _ = token
        _ = verify_url
        started_at = time.time()
        timeout_seconds = 25.0
        refresh_clicked = False
        last_snapshot: Dict[str, Any] = {}

        try:
            timeout_seconds = float(getattr(config, "browser_score_dom_wait_seconds", 25) or 25)
        except Exception:
            pass

        while (time.time() - started_at) < timeout_seconds:
            try:
                result = await page.evaluate(
                    """
                        () => {
                            const bodyText = ((document.body && document.body.innerText) || "")
                                .replace(/\\u00a0/g, " ")
                                .replace(/\\r/g, "");
                            const patterns = [
                                { source: "current_score", regex: /Your score is:\\s*([01](?:\\.\\d+)?)/i },
                                { source: "selected_score", regex: /Selected Score Test:[\\s\\S]{0,400}?Score:\\s*([01](?:\\.\\d+)?)/i },
                                { source: "history_score", regex: /(?:^|\\n)\\s*Score:\\s*([01](?:\\.\\d+)?)\\s*;/i },
                            ];
                            let score = null;
                            let source = "";
                            for (const item of patterns) {
                                const match = bodyText.match(item.regex);
                                if (!match) continue;
                                const parsed = Number(match[1]);
                                if (!Number.isNaN(parsed) && parsed >= 0 && parsed <= 1) {
                                    score = parsed;
                                    source = item.source;
                                    break;
                                }
                            }
                            const uaMatch = bodyText.match(/Current User Agent:\\s*([^\\n]+)/i);
                            const ipMatch = bodyText.match(/Current IP Address:\\s*([^\\n]+)/i);
                            return {
                                score,
                                source,
                                raw_text: bodyText.slice(0, 4000),
                                current_user_agent: uaMatch ? uaMatch[1].trim() : "",
                                current_ip_address: ipMatch ? ipMatch[1].trim() : "",
                                title: document.title || "",
                                url: location.href || "",
                            };
                        }
                    """
                )
            except Exception as e:
                result = {"error": f"{type(e).__name__}: {str(e)[:200]}"}

            if isinstance(result, dict):
                last_snapshot = result
                score = result.get("score")
                if isinstance(score, (int, float)):
                    elapsed_ms = int((time.time() - started_at) * 1000)
                    return {
                        "verify_mode": "browser_page_dom",
                        "verify_elapsed_ms": elapsed_ms,
                        "verify_http_status": None,
                        "verify_result": {
                            "success": True,
                            "score": score,
                            "source": result.get("source") or "antcpt_dom",
                            "raw_text": result.get("raw_text") or "",
                            "current_user_agent": result.get("current_user_agent") or "",
                            "current_ip_address": result.get("current_ip_address") or "",
                            "page_title": result.get("title") or "",
                            "page_url": result.get("url") or "",
                        },
                    }

            if not refresh_clicked and (time.time() - started_at) >= 2:
                refresh_clicked = True
                try:
                    await page.evaluate(
                        """
                            () => {
                                const nodes = Array.from(
                                    document.querySelectorAll('button, input[type="button"], input[type="submit"], a')
                                );
                                const target = nodes.find((node) => {
                                    const text = (node.innerText || node.textContent || node.value || "").trim();
                                    return /Refresh score now!?/i.test(text);
                                });
                                if (target) {
                                    target.click();
                                    return true;
                                }
                                return false;
                            }
                        """
                    )
                except Exception:
                    pass

            await asyncio.sleep(0.5)

        elapsed_ms = int((time.time() - started_at) * 1000)
        if not isinstance(last_snapshot, dict):
            last_snapshot = {"raw": last_snapshot}

        return {
            "verify_mode": "browser_page_dom",
            "verify_elapsed_ms": elapsed_ms,
            "verify_http_status": None,
            "verify_result": {
                "success": False,
                "score": None,
                "source": "antcpt_dom_timeout",
                "raw_text": last_snapshot.get("raw_text") or "",
                "current_user_agent": last_snapshot.get("current_user_agent") or "",
                "current_ip_address": last_snapshot.get("current_ip_address") or "",
                "page_title": last_snapshot.get("title") or "",
                "page_url": last_snapshot.get("url") or "",
                "error": last_snapshot.get("error") or "No score found on the page",
            },
        }
    
    async def _close_browser(
        self,
        playwright,
        browser,
        context,
        browser_pid: Optional[int] = None,
        clear_slot_pid: bool = True,
    ):
        """Close a browser instance and fall back to PID cleanup if needed."""
        is_shared_browser = any([
            context is not None and context is self._shared_context,
            browser is not None and browser is self._shared_browser,
            playwright is not None and playwright is self._shared_playwright,
        ])
        effective_pid = browser_pid or self._extract_browser_pid(browser)
        if clear_slot_pid and not effective_pid:
            effective_pid = self._shared_browser_pid or self._read_pid_file()
        if is_shared_browser:
            self._shared_playwright = None
            self._shared_browser = None
            self._shared_context = None
            self._shared_keepalive_page = None
            self._shared_browser_pid = None
            self._shared_proxy_url = None
            self._shared_bound_token_id = None
            self._shared_bound_cookie_signature = None
        try:
            if context:
                await asyncio.wait_for(context.close(), timeout=10)
        except Exception:
            pass
        try:
            if browser:
                await asyncio.wait_for(browser.close(), timeout=10)
        except Exception:
            pass
        try:
            if playwright:
                await asyncio.wait_for(playwright.stop(), timeout=10)
        except Exception:
            pass
        if effective_pid and not await self._wait_pid_exit(effective_pid, timeout_seconds=4):
            self._kill_pid(effective_pid, reason='close_timeout_or_orphan')
            await self._wait_pid_exit(effective_pid, timeout_seconds=2)
        if clear_slot_pid:
            self._write_pid_file(None)

    async def _wait_and_close_after_request(
        self,
        request_ref: str,
        release_event: asyncio.Event,
        wait_timeout: int,
        playwright,
        browser,
        context,
        action: str
    ):
        """Close the browser after the upstream request ends (with a timeout fallback)."""
        close_reason = "upstream request finished"
        try:
            await asyncio.wait_for(release_event.wait(), timeout=wait_timeout)
        except asyncio.TimeoutError:
            close_reason = f"timed out waiting for upstream request ({wait_timeout}s)"
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} {close_reason}, forcing close"
            )
        except Exception as e:
            close_reason = f"error waiting for upstream request: {type(e).__name__}"
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} {close_reason}, forcing close"
            )
        finally:
            await self._close_browser(playwright, browser, context)
            debug_logger.log_info(
                f"[BrowserCaptcha] Token-{self.token_id} {close_reason}, browser closed (action={action}, request_ref={request_ref[:8]})"
            )
            async with self._pending_release_lock:
                self._pending_release_entries.pop(request_ref, None)

    async def _defer_browser_close_until_request_done(
        self,
        playwright,
        browser,
        context,
        action: str
    ) -> str:
        """After a successful solve, delay closing the browser until the Flow request reports it is done."""
        flow_timeout = int(getattr(config, "flow_timeout", 300) or 300)
        upsample_timeout = int(getattr(config, "upsample_timeout", 300) or 300)
        if action == "IMAGE_GENERATION":
            # Image flow may include an upscale request; the wait must cover at least the flow/upsample timeout
            base_timeout = max(flow_timeout, upsample_timeout)
            wait_timeout = max(base_timeout + 180, 900)
        else:
            # Video requests have longer timeouts; use a bigger buffer so we do not close before the request ends
            wait_timeout = max(flow_timeout + 300, 1800)
        request_ref = uuid.uuid4().hex
        release_event = asyncio.Event()
        release_task = asyncio.create_task(
            self._wait_and_close_after_request(
                request_ref=request_ref,
                release_event=release_event,
                wait_timeout=wait_timeout,
                playwright=playwright,
                browser=browser,
                context=context,
                action=action,
            )
        )

        async with self._pending_release_lock:
            self._pending_release_entries[request_ref] = {
                "event": release_event,
                "task": release_task,
            }
        debug_logger.log_info(
            f"[BrowserCaptcha] Token-{self.token_id} solve succeeded, delaying close until upstream request finishes "
            f"(action={action}, timeout={wait_timeout}s, request_ref={request_ref[:8]})"
        )
        return request_ref

    async def notify_generation_request_finished(self, request_ref: Optional[str] = None):
        """Signal that the upstream image/video request for this token has finished."""
        async with self._pending_release_lock:
            release_event = None
            matched_ref = request_ref
            if matched_ref and matched_ref in self._pending_release_entries:
                entry = self._pending_release_entries.pop(matched_ref)
                release_event = entry.get("event")
            elif not matched_ref and self._pending_release_entries:
                # Legacy callers (no request_ref): release only the oldest pending item so other requests are not affected.
                matched_ref = next(iter(self._pending_release_entries.keys()))
                entry = self._pending_release_entries.pop(matched_ref)
                release_event = entry.get("event")
        if release_event and not release_event.is_set():
            release_event.set()
            debug_logger.log_info(
                f"[BrowserCaptcha] Token-{self.token_id} got upstream request finished signal, closing browser "
                f"(request_ref={(matched_ref or 'unknown')[:8]})"
            )

    async def force_close_pending_browser(self, request_ref: Optional[str] = None, close_all: bool = False):
        """Force close pending browsers tracked by this slot."""
        async with self._pending_release_lock:
            entries: List[Dict[str, Any]] = []
            if close_all:
                entries = list(self._pending_release_entries.values())
                self._pending_release_entries.clear()
            elif request_ref and request_ref in self._pending_release_entries:
                entry = self._pending_release_entries.pop(request_ref)
                entries = [entry]
            elif self._pending_release_entries:
                first_ref = next(iter(self._pending_release_entries.keys()))
                entry = self._pending_release_entries.pop(first_ref)
                entries = [entry]

        release_events = [entry.get("event") for entry in entries if isinstance(entry, dict)]
        release_tasks = [entry.get("task") for entry in entries if isinstance(entry, dict)]

        for release_event in release_events:
            if not release_event:
                continue
            if not release_event.is_set():
                release_event.set()
        for release_task in release_tasks:
            if not release_task:
                continue
            try:
                await asyncio.wait_for(release_task, timeout=5)
            except Exception:
                pass

        if close_all:
            await self.recycle_browser(reason="force_close_all", rotate_profile=False)

    async def _execute_captcha(self, context, project_id: str, website_key: str, action: str) -> Optional[str]:
        """Run the captcha solve in the given context"""
        page = None
        try:
            page = await context.new_page()
            await self._apply_browser_environment_patch(page, label="captcha_page")
            await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            ready = await self._prepare_flow_runtime_page(
                page,
                project_id,
                website_key,
                action,
                context_label="captcha",
            )
            if not ready:
                return None

            token = await asyncio.wait_for(
                page.evaluate(f"""
                    (actionName) => {{
                        return new Promise((resolve, reject) => {{
                            const timeout = setTimeout(() => reject(new Error('timeout')), 25000);
                            grecaptcha.enterprise.execute('{website_key}', {{action: actionName}})
                                .then(t => {{ clearTimeout(timeout); resolve(t); }})
                                .catch(e => {{ clearTimeout(timeout); reject(e); }});
                        }});
                    }}
                """, action),
                timeout=30
            )

            # Wait a few extra seconds so the enterprise request chain settles
            post_wait_seconds = float(getattr(config, "browser_recaptcha_settle_seconds", 3) or 3)
            if post_wait_seconds > 0:
                debug_logger.log_info(
                    f"[BrowserCaptcha] Token-{self.token_id} token obtained, waiting an extra {post_wait_seconds:.1f}s before returning"
                )
                await asyncio.sleep(post_wait_seconds)

            return token
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e)}"
            debug_logger.log_warning(f"[BrowserCaptcha] Token-{self.token_id} captcha solve failed: {msg[:200]}")
            return None
        finally:
            if page:
                try:
                    await page.close()
                except:
                    pass

    async def _execute_custom_captcha(
        self,
        context,
        website_url: str,
        website_key: str,
        action: str,
        verify_url: Optional[str] = None,
        enterprise: bool = False,
    ) -> Any:
        """Run reCAPTCHA on any site, for non-Flow uses such as score tests."""
        page = None
        try:
            page = await context.new_page()
            await self._apply_browser_environment_patch(page, label="custom_captcha_page")
            await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")

            primary_host = "https://www.recaptcha.net" if self._browser_proxy_active else "https://www.google.com"
            secondary_host = "https://www.google.com" if primary_host == "https://www.recaptcha.net" else "https://www.recaptcha.net"
            script_path = "recaptcha/enterprise.js" if enterprise else "recaptcha/api.js"
            execute_target = "grecaptcha.enterprise.execute" if enterprise else "grecaptcha.execute"
            ready_target = "grecaptcha.enterprise.ready" if enterprise else "grecaptcha.ready"
            wait_expression = (
                "typeof grecaptcha !== 'undefined' && typeof grecaptcha.enterprise !== 'undefined' && "
                "typeof grecaptcha.enterprise.execute === 'function'"
            ) if enterprise else (
                "typeof grecaptcha !== 'undefined' && typeof grecaptcha.execute === 'function'"
            )
            api_label = "enterprise.js" if enterprise else "api.js"

            debug_logger.log_info(
                f"[BrowserCaptcha] Token-{self.token_id} loading real custom page {api_label}: primary={primary_host}, secondary={secondary_host}, url={website_url}"
            )

            def handle_request_failed(request):
                try:
                    failed_url = request.url or ""
                    if not any(d in failed_url for d in ["google.com", "gstatic.com", "recaptcha.net", "antcpt.com"]):
                        return
                    failure = request.failure or ""
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] Token-{self.token_id} custom resource failed to load: url={failed_url[:200]}, error={failure}"
                    )
                except Exception:
                    pass

            page.on("requestfailed", handle_request_failed)

            try:
                await page.goto(website_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] Token-{self.token_id} custom page.goto failed: {type(e).__name__}: {str(e)[:200]}"
                )
                return None

            page_loaded = False
            for _ in range(20):
                try:
                    ready_state = await page.evaluate("document.readyState")
                    if ready_state == "complete":
                        page_loaded = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.5)
            if not page_loaded:
                debug_logger.log_warning(f"[BrowserCaptcha] Token-{self.token_id} custom page readyState not complete, continuing warmup")

            # Simulate natural foreground interaction instead of calling execute on a cold, blank context.
            try:
                await page.mouse.move(320, 220)
                await page.mouse.move(520, 320, steps=12)
                await page.mouse.wheel(0, 240)
                await page.bring_to_front()
                await page.evaluate("""
                    (() => {
                        try {
                            window.focus();
                            window.dispatchEvent(new Event('focus'));
                            document.dispatchEvent(new MouseEvent('mousemove', {
                                bubbles: true,
                                clientX: Math.max(32, Math.floor((window.innerWidth || 1280) * 0.4)),
                                clientY: Math.max(32, Math.floor((window.innerHeight || 720) * 0.35))
                            }));
                            window.scrollTo(0, Math.min(280, document.body?.scrollHeight || 280));
                        } catch (e) {}
                    })()
                """)
            except Exception:
                pass

            warmup_seconds = float(getattr(config, "browser_score_test_warmup_seconds", 12) or 12)
            if warmup_seconds > 0:
                debug_logger.log_info(
                    f"[BrowserCaptcha] Token-{self.token_id} real page warmup {warmup_seconds:.1f}s before custom solve"
                )
                await asyncio.sleep(warmup_seconds)

            try:
                await page.wait_for_function(wait_expression, timeout=15000)
            except Exception as e:
                debug_logger.log_warning(
                    f"[BrowserCaptcha] Token-{self.token_id} custom grecaptcha not ready, re-injecting script: {type(e).__name__}: {str(e)[:200]}"
                )
                try:
                    await page.evaluate(f"""
                        (primaryUrl, secondaryUrl) => {{
                            const existing = Array.from(document.scripts || []).some((script) => {{
                                const src = script?.src || "";
                                return src.includes('/recaptcha/');
                            }});
                            if (existing) return;
                            const urls = [primaryUrl, secondaryUrl];
                            const loadScript = (index) => {{
                                if (index >= urls.length) return;
                                const script = document.createElement('script');
                                script.src = urls[index];
                                script.async = true;
                                script.onerror = () => loadScript(index + 1);
                                document.head.appendChild(script);
                            }};
                            loadScript(0);
                        }}
                    """, f"{primary_host}/{script_path}?render={website_key}", f"{secondary_host}/{script_path}?render={website_key}")
                    await page.wait_for_function(wait_expression, timeout=15000)
                except Exception as inject_error:
                    debug_logger.log_warning(
                        f"[BrowserCaptcha] Token-{self.token_id} custom grecaptcha still not ready: {type(inject_error).__name__}: {str(inject_error)[:200]}"
                    )
                    return None

            await self._capture_page_fingerprint(page)

            token = await asyncio.wait_for(
                page.evaluate(
                    f"""
                        (actionName) => {{
                            return new Promise((resolve, reject) => {{
                                const timeout = setTimeout(() => reject(new Error('timeout')), 25000);
                                try {{
                                    {ready_target}(function() {{
                                        {execute_target}('{website_key}', {{action: actionName}})
                                            .then(t => {{
                                                clearTimeout(timeout);
                                                resolve(t);
                                            }})
                                            .catch(e => {{
                                                clearTimeout(timeout);
                                                reject(e);
                                            }});
                                    }});
                                }} catch (e) {{
                                    clearTimeout(timeout);
                                    reject(e);
                                }}
                            }});
                        }}
                    """,
                    action,
                ),
                timeout=30,
            )

            post_wait_seconds = float(getattr(config, "browser_recaptcha_settle_seconds", 3) or 3)
            if post_wait_seconds > 0:
                debug_logger.log_info(
                    f"[BrowserCaptcha] Token-{self.token_id} custom solve done, waiting an extra {post_wait_seconds:.1f}s before returning token"
                )
                await asyncio.sleep(post_wait_seconds)

            if verify_url:
                verify_payload = await self._verify_score_in_page(page, token, verify_url)
                return {
                    "token": token,
                    **verify_payload,
                }

            return token
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e)}"
            debug_logger.log_warning(f"[BrowserCaptcha] Token-{self.token_id} custom solve failed: {msg[:200]}")
            return None
        finally:
            if page:
                try:
                    await page.close()
                except:
                    pass

    def is_busy(self) -> bool:
        return self._solve_inflight > 0

    def note_idle(self):
        if self._solve_inflight <= 0:
            self._last_idle_since = time.monotonic()

    def idle_seconds(self) -> float:
        if self.is_busy():
            return 0.0
        return max(0.0, time.monotonic() - self._last_idle_since)

    def has_shared_browser(self) -> bool:
        return bool(self._shared_browser or self._shared_context or self._shared_keepalive_page)

    def get_last_fingerprint(self) -> Optional[Dict[str, Any]]:
        """Return the fingerprint snapshot of the most recent captcha browser."""
        if not self._last_fingerprint:
            return None
        return dict(self._last_fingerprint)

    async def _wait_for_enterprise_ready(
        self,
        page,
        website_key: str,
        primary_host: str,
        secondary_host: str,
        *,
        timeout_ms: int = 15000,
        context_label: str = "",
    ) -> bool:
        """Wait for grecaptcha.enterprise to be ready, re-injecting enterprise.js if needed."""
        wait_expression = (
            "typeof grecaptcha !== 'undefined' && "
            "typeof grecaptcha.enterprise !== 'undefined' && "
            "typeof grecaptcha.enterprise.execute === 'function'"
        )
        label = f"{context_label} " if context_label else ""
        try:
            await page.wait_for_function(wait_expression, timeout=timeout_ms)
            return True
        except Exception as e:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} {label}grecaptcha not ready, re-injecting script: "
                f"{type(e).__name__}: {str(e)[:200]}"
            )

        try:
            await page.evaluate(
                """
                (primaryUrl, secondaryUrl) => {
                    const existing = Array.from(document.scripts || []).some((script) => {
                        const src = script?.src || "";
                        return src.includes('/recaptcha/enterprise.js');
                    });
                    if (existing) return;
                    const urls = [primaryUrl, secondaryUrl];
                    const loadScript = (index) => {
                        if (index >= urls.length) return;
                        const script = document.createElement('script');
                        script.src = urls[index];
                        script.async = true;
                        script.onerror = () => loadScript(index + 1);
                        document.head.appendChild(script);
                    };
                    loadScript(0);
                }
                """,
                f"{primary_host}/recaptcha/enterprise.js?render={website_key}",
                f"{secondary_host}/recaptcha/enterprise.js?render={website_key}",
            )
            await page.wait_for_function(wait_expression, timeout=timeout_ms)
            return True
        except Exception as inject_error:
            debug_logger.log_warning(
                f"[BrowserCaptcha] Token-{self.token_id} {label}grecaptcha still not ready: "
                f"{type(inject_error).__name__}: {str(inject_error)[:200]}"
            )
            return False

    @staticmethod
    def _inject_recaptcha_token(payload: Any, token: str):
        """Recursively update every reCAPTCHA token field in the payload."""
        if isinstance(payload, dict):
            recaptcha_context = payload.get("recaptchaContext")
            if isinstance(recaptcha_context, dict):
                recaptcha_context["token"] = token
                recaptcha_context.setdefault(
                    "applicationType",
                    "RECAPTCHA_APPLICATION_TYPE_WEB",
                )
            for value in payload.values():
                TokenBrowser._inject_recaptcha_token(value, token)
        elif isinstance(payload, list):
            for item in payload:
                TokenBrowser._inject_recaptcha_token(item, token)

    async def submit_flow_request(
        self,
        project_id: str,
        website_key: str,
        action: str,
        url: str,
        at_token: str,
        json_data: Dict[str, Any],
        timeout: int,
        token_proxy_url: Optional[str] = None,
        token_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Solve captcha and submit the Flow request in the same browser context."""
        async with self._semaphore:
            self._solve_inflight += 1
            max_retries = max(3, int(getattr(config, "browser_captcha_max_retries", 5) or 5))

            try:
                for attempt in range(max_retries):
                    page = None
                    try:
                        start_ts = time.time()
                        _, _, context = await self._get_or_create_shared_browser(
                            token_proxy_url=token_proxy_url,
                            token_id=token_id,
                        )

                        page = await context.new_page()
                        await self._apply_browser_environment_patch(page, label="browser_submit_page")
                        await page.add_init_script(
                            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                        )

                        debug_logger.log_info(
                            f"[BrowserCaptcha] Token-{self.token_id} submitting Flow request in browser: "
                            f"action={action}, project_id={project_id}"
                        )
                        ready = await self._prepare_flow_runtime_page(
                            page,
                            project_id,
                            website_key,
                            action,
                            context_label="in-browser submit",
                        )
                        if not ready:
                            raise RuntimeError("grecaptcha.enterprise not ready")

                        payload_for_submit = deepcopy(json_data)
                        response_payload = await asyncio.wait_for(
                            page.evaluate(
                                """
                                async ({ websiteKey, actionName, targetUrl, bearerToken, payload, timeoutMs }) => {
                                    const solveToken = () => new Promise((resolve, reject) => {
                                        const timer = setTimeout(() => reject(new Error('captcha_timeout')), 25000);
                                        try {
                                            grecaptcha.enterprise.execute(websiteKey, { action: actionName })
                                                .then((token) => {
                                                    clearTimeout(timer);
                                                    resolve(token);
                                                })
                                                .catch((error) => {
                                                    clearTimeout(timer);
                                                    reject(error);
                                                });
                                        } catch (error) {
                                            clearTimeout(timer);
                                            reject(error);
                                        }
                                    });

                                    const patchToken = (value, token) => {
                                        if (!value) return;
                                        if (Array.isArray(value)) {
                                            value.forEach((item) => patchToken(item, token));
                                            return;
                                        }
                                        if (typeof value !== 'object') return;
                                        if (value.recaptchaContext && typeof value.recaptchaContext === 'object') {
                                            value.recaptchaContext.token = token;
                                            if (!value.recaptchaContext.applicationType) {
                                                value.recaptchaContext.applicationType = 'RECAPTCHA_APPLICATION_TYPE_WEB';
                                            }
                                        }
                                        Object.values(value).forEach((item) => patchToken(item, token));
                                    };

                                    const token = await solveToken();
                                    const body = typeof structuredClone === 'function'
                                        ? structuredClone(payload)
                                        : JSON.parse(JSON.stringify(payload));
                                    patchToken(body, token);

                                    const controller = new AbortController();
                                    const abortTimer = setTimeout(() => controller.abort('flow_fetch_timeout'), timeoutMs);
                                    try {
                                        const response = await fetch(targetUrl, {
                                            method: 'POST',
                                            headers: {
                                                'authorization': `Bearer ${bearerToken}`,
                                                'content-type': 'text/plain;charset=UTF-8',
                                            },
                                            credentials: 'include',
                                            body: JSON.stringify(body),
                                            signal: controller.signal,
                                        });
                                        const text = await response.text();
                                        const headers = {};
                                        response.headers.forEach((value, key) => {
                                            headers[key] = value;
                                        });
                                        return {
                                            ok: response.ok,
                                            status: response.status,
                                            text,
                                            headers,
                                            token,
                                        };
                                    } finally {
                                        clearTimeout(abortTimer);
                                    }
                                }
                                """,
                                {
                                    "websiteKey": website_key,
                                    "actionName": action,
                                    "targetUrl": url,
                                    "bearerToken": at_token,
                                    "payload": payload_for_submit,
                                    "timeoutMs": max(5000, int(timeout * 1000)),
                                },
                            ),
                            timeout=max(35, timeout + 10),
                        )

                        if not isinstance(response_payload, dict):
                            raise RuntimeError("in-browser submit returned an unexpected format")

                        self._solve_count += 1
                        self._consecutive_browser_failures = 0
                        debug_logger.log_info(
                            f"[BrowserCaptcha] Token-{self.token_id} in-browser submit done "
                            f"(status={response_payload.get('status')}, "
                            f"{(time.time() - start_ts) * 1000:.0f}ms, "
                            f"launches={self._shared_launch_count}, reuse={self._shared_reuse_count})"
                        )
                        return response_payload
                    except Exception as e:
                        self._error_count += 1
                        self._consecutive_browser_failures += 1
                        error_message = f"{type(e).__name__}: {str(e)}"
                        debug_logger.log_error(
                            f"[BrowserCaptcha] Token-{self.token_id} in-browser submit failed: "
                            f"{error_message[:240]}"
                        )
                        error_lower = error_message.lower()
                        if any(
                            keyword in error_lower
                            for keyword in [
                                "context or browser has been closed",
                                "target closed",
                                "browser has been closed",
                                "connection closed",
                                "crash",
                                "closed",
                            ]
                        ):
                            await self.recycle_browser(
                                reason="browser_submit_runtime_error",
                                rotate_profile=False,
                            )
                    finally:
                        if page:
                            try:
                                await page.close()
                            except Exception:
                                pass

                    if attempt < max_retries - 1:
                        await asyncio.sleep(1)

                raise RuntimeError("in-browser Flow request submit failed")
            finally:
                self._solve_inflight = max(0, self._solve_inflight - 1)
                self.note_idle()
    
    async def get_token(
        self,
        project_id: str,
        website_key: str,
        action: str = "IMAGE_GENERATION",
        token_proxy_url: Optional[str] = None,
        token_id: Optional[int] = None,
    ) -> tuple[Optional[str], Optional[str]]:
        """Get a token from the shared browser unless a fatal browser error occurs."""
        async with self._semaphore:
            self._solve_inflight += 1
            max_retries = 3

            try:
                for attempt in range(max_retries):
                    try:
                        start_ts = time.time()
                        _, _, context = await self._get_or_create_shared_browser(
                            token_proxy_url=token_proxy_url,
                            token_id=token_id,
                        )

                        token = await self._execute_captcha(context, project_id, website_key, action)
                        if token:
                            self._solve_count += 1
                            self._consecutive_browser_failures = 0
                            debug_logger.log_info(
                                f"[BrowserCaptcha] Token-{self.token_id} token acquired ({(time.time()-start_ts)*1000:.0f}ms, launches={self._shared_launch_count}, reuse={self._shared_reuse_count})"
                            )
                            return token, None

                        self._error_count += 1
                        self._consecutive_browser_failures += 1
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] Token-{self.token_id} token attempt {attempt + 1}/{max_retries} failed"
                        )
                        if self._consecutive_browser_failures >= 2:
                            await self.recycle_browser(reason=f"captcha_failed_{attempt + 1}", rotate_profile=False)
                    except Exception as e:
                        self._error_count += 1
                        self._consecutive_browser_failures += 1
                        error_message = f"{type(e).__name__}: {str(e)}"
                        debug_logger.log_error(
                            f"[BrowserCaptcha] Token-{self.token_id} browser error: {error_message[:200]}"
                        )
                        error_lower = error_message.lower()
                        if any(keyword in error_lower for keyword in [
                            "context or browser has been closed",
                            "target closed",
                            "browser has been closed",
                            "connection closed",
                            "crash",
                            "closed",
                        ]):
                            await self.recycle_browser(reason="browser_runtime_error", rotate_profile=False)

                    if attempt < max_retries - 1:
                        await asyncio.sleep(1)

                return None, None
            finally:
                self._solve_inflight = max(0, self._solve_inflight - 1)
                self.note_idle()

    async def get_custom_token(
        self,
        website_url: str,
        website_key: str,
        action: str = "homepage",
        enterprise: bool = False,
    ) -> Optional[str]:
        """Get a custom reCAPTCHA token using a temporary browser."""
        async with self._semaphore:
            self._solve_inflight += 1
            max_retries = 3

            try:
                for attempt in range(max_retries):
                    playwright = None
                    browser = None
                    context = None
                    try:
                        start_ts = time.time()
                        playwright, browser, context = await self._create_browser(manage_slot_pid=False)
                        token = await self._execute_custom_captcha(
                            context=context,
                            website_url=website_url,
                            website_key=website_key,
                            action=action,
                            enterprise=enterprise,
                        )

                        if token:
                            self._solve_count += 1
                            debug_logger.log_info(
                                f"[BrowserCaptcha] Token-{self.token_id} custom token acquired ({(time.time()-start_ts)*1000:.0f}ms)"
                            )
                            return token

                        self._error_count += 1
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] Token-{self.token_id} custom token attempt {attempt+1}/{max_retries} failed"
                        )
                    except Exception as e:
                        self._error_count += 1
                        debug_logger.log_error(
                            f"[BrowserCaptcha] Token-{self.token_id} custom browser error: {type(e).__name__}: {str(e)[:200]}"
                        )
                    finally:
                        await self._close_browser(
                            playwright,
                            browser,
                            context,
                            browser_pid=self._extract_browser_pid(browser),
                            clear_slot_pid=False,
                        )

                    if attempt < max_retries - 1:
                        await asyncio.sleep(1)

                return None
            finally:
                self._solve_inflight = max(0, self._solve_inflight - 1)
                self.note_idle()

    async def get_custom_score(
        self,
        website_url: str,
        website_key: str,
        verify_url: str,
        action: str = "homepage",
        enterprise: bool = False,
    ) -> Dict[str, Any]:
        """Get a custom token and verify its score using a temporary browser."""
        async with self._semaphore:
            self._solve_inflight += 1
            max_retries = 3

            try:
                for attempt in range(max_retries):
                    playwright = None
                    browser = None
                    context = None
                    try:
                        started_at = time.time()
                        playwright, browser, context = await self._create_browser(manage_slot_pid=False)
                        payload = await self._execute_custom_captcha(
                            context=context,
                            website_url=website_url,
                            website_key=website_key,
                            action=action,
                            verify_url=verify_url,
                            enterprise=enterprise,
                        )

                        if isinstance(payload, dict) and payload.get("token"):
                            self._solve_count += 1
                            payload.setdefault("token_elapsed_ms", int((time.time() - started_at) * 1000))
                            debug_logger.log_info(
                                f"[BrowserCaptcha] Token-{self.token_id} in-page score verification succeeded ({(time.time()-started_at)*1000:.0f}ms)"
                            )
                            return payload

                        self._error_count += 1
                        debug_logger.log_warning(
                            f"[BrowserCaptcha] Token-{self.token_id} in-page score attempt {attempt+1}/{max_retries} failed"
                        )
                    except Exception as e:
                        self._error_count += 1
                        debug_logger.log_error(
                            f"[BrowserCaptcha] Token-{self.token_id} in-page score browser error: {type(e).__name__}: {str(e)[:200]}"
                        )
                    finally:
                        await self._close_browser(
                            playwright,
                            browser,
                            context,
                            browser_pid=self._extract_browser_pid(browser),
                            clear_slot_pid=False,
                        )

                    if attempt < max_retries - 1:
                        await asyncio.sleep(1)

                return {
                    "token": None,
                    "verify_mode": "browser_page",
                    "verify_elapsed_ms": 0,
                    "verify_http_status": None,
                    "verify_result": {}
                }
            finally:
                self._solve_inflight = max(0, self._solve_inflight - 1)
                self.note_idle()


class BrowserCaptchaService:
    """Multi-browser round-robin captcha service (singleton)
    
    Browser count is configurable; each browser opens 1 tab and requests are assigned round-robin
    """
    
    _instance: Optional['BrowserCaptchaService'] = None
    _lock = asyncio.Lock()
    
    def __init__(self, db=None):
        self.db = db
        self.website_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
        self.base_user_data_dir = os.path.join(os.getcwd(), "browser_data_rt")
        self._browsers: Dict[int, TokenBrowser] = {}
        self._browsers_lock = asyncio.Lock()
        self._slot_allocation_lock = asyncio.Lock()
        self._slot_reservations: Dict[int, int] = {}
        
        # ???????
        self._browser_count = 1  # ?? 1 ?????????
        self._round_robin_index = 0  # ????
        # ????
        self._stats = {
            "req_total": 0,
            "gen_ok": 0,
            "gen_fail": 0,
            "api_403": 0
        }
        
        # ?????? _load_browser_count ???????
        self._token_semaphore = None
        self._idle_reaper_task: Optional[asyncio.Task] = None
    
    async def _ensure_idle_reaper(self):
        if self._idle_reaper_task is None or self._idle_reaper_task.done():
            self._idle_reaper_task = asyncio.create_task(self._idle_reaper_loop())

    async def _idle_reaper_loop(self):
        while True:
            try:
                await asyncio.sleep(15)
                idle_ttl = int(getattr(config, "browser_idle_ttl_seconds", 600) or 600)
                browsers = []
                async with self._browsers_lock:
                    browsers = list(self._browsers.values())
                for browser in browsers:
                    try:
                        if browser.is_busy():
                            continue
                        if not browser.has_shared_browser():
                            continue
                        if browser.idle_seconds() < idle_ttl:
                            continue
                        await browser.recycle_browser(reason=f"idle_ttl_{idle_ttl}s", rotate_profile=False)
                    except Exception as e:
                        debug_logger.log_warning(f"[BrowserCaptcha] idle reaper failed: {e}")
            except asyncio.CancelledError:
                return
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] idle reaper loop error: {e}")

    @classmethod
    async def get_instance(cls, db=None) -> 'BrowserCaptchaService':
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db)
                    # Load browser_count from the database
                    await cls._instance._load_browser_count()
                    await cls._instance._ensure_idle_reaper()
        return cls._instance
    
    def _check_available(self):
        """Check whether the service is available"""
        if DOCKER_HEADED_BLOCKED:
            raise RuntimeError(
                "Docker detected; headed browser captcha is disabled by default. "
                "To enable it, set the env var ALLOW_DOCKER_HEADED_CAPTCHA=true and provide DISPLAY/Xvfb."
            )
        if IS_DOCKER and not os.environ.get("DISPLAY"):
            raise RuntimeError(
                "Docker headed browser captcha is on, but DISPLAY is not set. "
                "Set DISPLAY (e.g. :99) and start Xvfb."
            )
        if not PLAYWRIGHT_AVAILABLE or async_playwright is None:
            raise RuntimeError(
                "playwright is not installed or not usable. "
                "Install manually: pip install playwright && python -m playwright install chromium"
            )
    
    async def _load_browser_count(self):
        """Load browser count from the database"""
        if self.db:
            try:
                captcha_config = await self.db.get_captcha_config()
                self._browser_count = max(1, captcha_config.browser_count)
                debug_logger.log_info(f"[BrowserCaptcha] browser count: {self._browser_count}")
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] failed to load browser_count: {e}, using default 1")
                self._browser_count = 1
        # Concurrency limit = browser count (no longer hardcoded)
        self._token_semaphore = asyncio.Semaphore(self._browser_count)
        debug_logger.log_info(f"[BrowserCaptcha] concurrency limit: {self._browser_count}")
    
    async def reload_browser_count(self):
        """???????????????????????"""
        old_count = self._browser_count
        await self._load_browser_count()
        
        browsers_to_close: List[TokenBrowser] = []
        await self._ensure_idle_reaper()
        if self._browser_count < old_count:
            async with self._browsers_lock:
                for browser_id in list(self._browsers.keys()):
                    if browser_id >= self._browser_count:
                        browsers_to_close.append(self._browsers.pop(browser_id))
                        debug_logger.log_info(f"[BrowserCaptcha] ????????? {browser_id}")

        for browser in browsers_to_close:
            try:
                await browser.force_close_pending_browser(close_all=True)
                await browser.recycle_browser(reason="browser_slot_removed", rotate_profile=False)
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] ???????????: {e}")

            async with self._slot_allocation_lock:
                self._slot_reservations = {
                    slot_id: count
                    for slot_id, count in self._slot_reservations.items()
                    if 0 <= slot_id < self._browser_count and count > 0
                }

        if self._browser_count > old_count:
            warmup_tasks = [
                self._warmup_browser_slot(browser_id)
                for browser_id in range(old_count, self._browser_count)
            ]
            if warmup_tasks:
                await asyncio.gather(*warmup_tasks, return_exceptions=True)

    def _log_stats(self):
        total = self._stats["req_total"]
        gen_fail = self._stats["gen_fail"]
        api_403 = self._stats["api_403"]
        gen_ok = self._stats["gen_ok"]
        
        valid_success = gen_ok - api_403
        if valid_success < 0: valid_success = 0
        
        rate = (valid_success / total * 100) if total > 0 else 0.0

    
    async def _warmup_browser_slot(self, browser_id: int):
        browser = await self._get_or_create_browser(browser_id)
        try:
            await browser._get_or_create_shared_browser()
            debug_logger.log_info(f"[BrowserCaptcha] warmed browser slot {browser_id}")
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] warmup for slot {browser_id} failed: {e}")

    async def warmup_browser_slots(self):
        tasks = [self._warmup_browser_slot(browser_id) for browser_id in range(self._browser_count)]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _is_slot_busy_for_allocation(self, slot_id: int) -> bool:
        if self._slot_reservations.get(slot_id, 0) > 0:
            return True
        browser = self._browsers.get(slot_id)
        return bool(browser and getattr(browser, 'is_busy', lambda: False)())

    def _has_warmed_browser_for_allocation(self, slot_id: int) -> bool:
        browser = self._browsers.get(slot_id)
        return bool(browser and getattr(browser, 'has_shared_browser', lambda: False)())

    def _reserve_slot_locked(self, slot_id: int):
        self._slot_reservations[slot_id] = self._slot_reservations.get(slot_id, 0) + 1

    async def _release_slot_reservation(self, slot_id: Optional[int]):
        if slot_id is None:
            return
        async with self._slot_allocation_lock:
            current = self._slot_reservations.get(slot_id, 0)
            if current <= 1:
                self._slot_reservations.pop(slot_id, None)
            else:
                self._slot_reservations[slot_id] = current - 1

    async def _select_browser_id(self, project_id: Optional[str]) -> int:
        # browser mode no longer pins a slot per project_id.
        # Prefer an idle warmed shared browser, then an idle cold slot; if all are busy, wait round-robin.
        async with self._slot_allocation_lock:
            async with self._browsers_lock:
                warmed_idle_slot: Optional[int] = None
                idle_slot: Optional[int] = None

                for offset in range(self._browser_count):
                    slot_id = (self._round_robin_index + offset) % self._browser_count
                    if self._is_slot_busy_for_allocation(slot_id):
                        continue

                    if idle_slot is None:
                        idle_slot = slot_id
                    if warmed_idle_slot is None and self._has_warmed_browser_for_allocation(slot_id):
                        warmed_idle_slot = slot_id
                        break

                selected_slot = warmed_idle_slot if warmed_idle_slot is not None else idle_slot
                if selected_slot is not None:
                    self._round_robin_index = (selected_slot + 1) % self._browser_count
                    self._reserve_slot_locked(selected_slot)
                    return selected_slot

                slot_id = self._get_next_browser_id()
                self._reserve_slot_locked(slot_id)
            return slot_id

    async def _get_or_create_browser(self, browser_id: int) -> TokenBrowser:
        """Get or create the browser instance with the given ID"""
        async with self._browsers_lock:
            if browser_id not in self._browsers:
                user_data_dir = os.path.join(self.base_user_data_dir, f"browser_{browser_id}")
                browser = TokenBrowser(browser_id, user_data_dir, db=self.db)
                self._browsers[browser_id] = browser
                debug_logger.log_info(f"[BrowserCaptcha] creating browser instance {browser_id}")
            return self._browsers[browser_id]
    
    def _get_next_browser_id(self) -> int:
        """Get the next browser ID (round-robin)"""
        browser_id = self._round_robin_index % self._browser_count
        self._round_robin_index += 1
        return browser_id

    @staticmethod
    def _compose_browser_ref(browser_id: int, request_ref: Optional[str]) -> Union[int, str]:
        """Combine browser_id and request_ref into a request handle that can be passed back."""
        if request_ref:
            return f"{browser_id}:{request_ref}"
        return browser_id

    @staticmethod
    def _parse_browser_ref(browser_ref: Optional[Union[int, str]]) -> tuple[Optional[int], Optional[str]]:
        """Parse a request handle; also accepts a legacy plain int browser_id."""
        if browser_ref is None:
            return None, None

        if isinstance(browser_ref, int):
            return browser_ref, None

        if isinstance(browser_ref, str):
            raw = browser_ref.strip()
            if raw.isdigit():
                return int(raw), None
            browser_id_part, sep, request_ref = raw.partition(":")
            if sep and browser_id_part.isdigit() and request_ref:
                return int(browser_id_part), request_ref

        return None, None

    async def _resolve_token_proxy_url(self, token_id: Optional[int]) -> Optional[str]:
        """Read the per-token captcha proxy, falling back to the global setting when empty."""
        if not token_id or not self.db:
            return None
        try:
            token = await self.db.get_token(token_id)
            if token and token.captcha_proxy_url and token.captcha_proxy_url.strip():
                return token.captcha_proxy_url.strip()
        except Exception as e:
            debug_logger.log_warning(f"[BrowserCaptcha] failed to read captcha proxy for token({token_id}): {e}")
        return None
    
    async def get_token(self, project_id: str, action: str = "IMAGE_GENERATION", token_id: int = None) -> tuple[Optional[str], Union[int, str]]:
        """Get a reCAPTCHA token (picks a slot from the shared browser pool)
        
        Args:
            project_id: project ID
            action: reCAPTCHA action
            token_id: account token id (only used to read the per-token captcha proxy)
        
        Returns:
            (token, browser_ref) tuple; browser_ref holds browser_id and the per-request request_ref
        """
        # Check whether the service is available
        self._check_available()
        
        self._stats["req_total"] += 1
        token_proxy_url = await self._resolve_token_proxy_url(token_id)
        
        token: Optional[str] = None
        request_ref: Optional[str] = None

        # Global concurrency limit (if configured)
        if self._token_semaphore:
            async with self._token_semaphore:
                browser_id = await self._select_browser_id(project_id)
                try:
                    browser = await self._get_or_create_browser(browser_id)
                    token, request_ref = await browser.get_token(
                        project_id,
                        self.website_key,
                        action,
                        token_proxy_url=token_proxy_url,
                        token_id=token_id,
                    )
                finally:
                    await self._release_slot_reservation(browser_id)

            if token:
                self._stats["gen_ok"] += 1
            else:
                self._stats["gen_fail"] += 1
                
            self._log_stats()
            return token, self._compose_browser_ref(browser_id, request_ref)
        
        browser_id = await self._select_browser_id(project_id)
        try:
            browser = await self._get_or_create_browser(browser_id)
            token, request_ref = await browser.get_token(
                project_id,
                self.website_key,
                action,
                token_proxy_url=token_proxy_url,
                token_id=token_id,
            )
        finally:
            await self._release_slot_reservation(browser_id)

        if token:
            self._stats["gen_ok"] += 1
        else:
            self._stats["gen_fail"] += 1
            
        self._log_stats()
        return token, self._compose_browser_ref(browser_id, request_ref)

    async def get_custom_token(
        self,
        website_url: str,
        website_key: str,
        action: str = "homepage",
        enterprise: bool = False,
    ) -> tuple[Optional[str], int]:
        """Get a reCAPTCHA token for any site, for score tests."""
        self._check_available()

        if self._token_semaphore:
            async with self._token_semaphore:
                browser_id = self._get_next_browser_id()
                browser = await self._get_or_create_browser(browser_id)
                token = await browser.get_custom_token(
                    website_url=website_url,
                    website_key=website_key,
                    action=action,
                    enterprise=enterprise,
                )
            return token, browser_id

        browser_id = self._get_next_browser_id()
        browser = await self._get_or_create_browser(browser_id)
        token = await browser.get_custom_token(
            website_url=website_url,
            website_key=website_key,
            action=action,
            enterprise=enterprise,
        )
        return token, browser_id

    async def get_custom_score(
        self,
        website_url: str,
        website_key: str,
        verify_url: str,
        action: str = "homepage",
        enterprise: bool = False,
    ) -> tuple[Dict[str, Any], int]:
        """Get a token and check its score inside the browser page."""
        self._check_available()

        if self._token_semaphore:
            async with self._token_semaphore:
                browser_id = self._get_next_browser_id()
                browser = await self._get_or_create_browser(browser_id)
                payload = await browser.get_custom_score(
                    website_url=website_url,
                    website_key=website_key,
                    verify_url=verify_url,
                    action=action,
                    enterprise=enterprise,
                )
            return payload, browser_id

        browser_id = self._get_next_browser_id()
        browser = await self._get_or_create_browser(browser_id)
        payload = await browser.get_custom_score(
            website_url=website_url,
            website_key=website_key,
            verify_url=verify_url,
            action=action,
            enterprise=enterprise,
        )
        return payload, browser_id

    async def get_fingerprint(self, browser_ref: Optional[Union[int, str]]) -> Optional[Dict[str, Any]]:
        """Get the fingerprint snapshot from the given browser's most recent solve."""
        browser_id, _ = self._parse_browser_ref(browser_ref)
        if browser_id is None:
            return None

        async with self._browsers_lock:
            browser = self._browsers.get(browser_id)
            if not browser:
                return None
            return browser.get_last_fingerprint()

    async def submit_flow_request(
        self,
        project_id: str,
        action: str,
        token_id: Optional[int],
        url: str,
        at_token: str,
        json_data: Dict[str, Any],
        timeout: int,
    ) -> tuple[Dict[str, Any], Union[int, str], Optional[Dict[str, Any]]]:
        """In browser mode, solve captcha and submit the Flow request in the same browser context."""
        self._check_available()

        token_proxy_url = await self._resolve_token_proxy_url(token_id)
        browser_id = await self._select_browser_id(project_id)
        try:
            browser = await self._get_or_create_browser(browser_id)
            response_payload = await browser.submit_flow_request(
                project_id=project_id,
                website_key=self.website_key,
                action=action,
                url=url,
                at_token=at_token,
                json_data=json_data,
                timeout=timeout,
                token_proxy_url=token_proxy_url,
                token_id=token_id,
            )
            fingerprint = browser.get_last_fingerprint()
            return response_payload, self._compose_browser_ref(browser_id, None), fingerprint
        finally:
            await self._release_slot_reservation(browser_id)

    async def report_error(self, browser_ref: Optional[Union[int, str]] = None, error_reason: Optional[str] = None):
        """Handle upstream errors; recycle the browser only for explicit reCAPTCHA evaluation failures."""
        browser_id, _ = self._parse_browser_ref(browser_ref)

        async with self._browsers_lock:
            browser = self._browsers.get(browser_id) if browser_id is not None else None
            error_lower = (error_reason or "").lower()
            has_recaptcha = "recaptcha" in error_lower
            should_recycle = has_recaptcha and (
                "evaluation failed" in error_lower
                or "verification failed" in error_lower or "验证失败" in (error_reason or "")
                or "failed" in error_lower
            )
            if should_recycle:
                self._stats["api_403"] += 1
            if browser_id is not None:
                debug_logger.log_info(
                    f"[BrowserCaptcha] browser {browser_id} failure reported, reason={error_reason or 'unknown'}, recycle={should_recycle}"
                )

        if browser and should_recycle:
            try:
                await browser.recycle_browser(
                    reason=error_reason or "recaptcha_evaluation_failed",
                    rotate_profile=True,
                )
            except Exception as e:
                debug_logger.log_warning(f"[BrowserCaptcha] browser {browser_id} recycle failed: {e}")

    async def report_request_finished(self, browser_ref: Optional[Union[int, str]] = None):
        """Caller signals the request is done; browser mode keeps the resident browser and does not close it on success."""
        browser_id, _ = self._parse_browser_ref(browser_ref)
        if browser_id is None:
            return

        async with self._browsers_lock:
            browser = self._browsers.get(browser_id)

        if browser:
            keepalive_alive = False
            keepalive_page = getattr(browser, '_shared_keepalive_page', None)
            try:
                keepalive_alive = bool(keepalive_page and not keepalive_page.is_closed())
            except Exception:
                keepalive_alive = False
            debug_logger.log_info(
                f"[BrowserCaptcha] browser {browser_id} request finished; keepalive_alive={keepalive_alive}"
            )

    async def remove_browser(self, browser_id: int):
        async with self._browsers_lock:
            if browser_id in self._browsers:
                self._browsers.pop(browser_id)

    async def close(self):
        async with self._browsers_lock:
            browsers = list(self._browsers.values())
            self._browsers.clear()

        if self._idle_reaper_task and not self._idle_reaper_task.done():
            self._idle_reaper_task.cancel()
            try:
                await self._idle_reaper_task
            except asyncio.CancelledError:
                pass

        for browser in browsers:
            try:
                await browser.force_close_pending_browser(close_all=True)
                await browser.recycle_browser(reason="service_shutdown", rotate_profile=False)
            except Exception:
                pass
            
    async def open_login_browser(self): return {"success": False, "error": "Not implemented"}
    async def create_browser_for_token(self, t, s=None): pass
    def get_stats(self): 
        browsers = list(self._browsers.values())
        busy_browser_count = sum(1 for browser in browsers if getattr(browser, "is_busy", lambda: False)())
        shared_browser_count = sum(1 for browser in browsers if getattr(browser, "has_shared_browser", lambda: False)())
        base_stats = {
            "total_solve_count": self._stats["gen_ok"],
            "total_error_count": self._stats["gen_fail"],
            "risk_403_count": self._stats["api_403"],
            "browser_count": len(self._browsers),
            "configured_browser_count": self._browser_count,
            "busy_browser_count": busy_browser_count,
            "idle_browser_count": max(self._browser_count - busy_browser_count, 0),
            "shared_browser_count": shared_browser_count,
            "project_affinity_count": 0,
            "browsers": []
        }
        return base_stats

