"""Flow cookie sessions + flow.google.com batchexecute transport (2026-10-23).

Google retired the Labs session API on ~2026-10-21: `labs.google/fx/api/auth/session` now answers
"Authentication endpoint has been deprecated and disabled." and `/api/auth/csrf` is 410, so there is
no ST -> access-token step any more, and aisandbox-pa REST refuses cookie auth (401, tested
2026-10-23). Flow's web app talks to `flow.google.com/_/AiSandboxAngularFrontend/data/batchexecute`
with the account's Google cookies; upstream (TheSmallHanCat/flow2api) moved to the same RPCs.

What this module does, proven live on 2026-10-23 through an account's own proxy:
- the worker extension's `google_cookies` (what Chrome sends to accounts.google.com, name/value list)
  are split into `accounts.google.com` host cookies and `.google.com` cookies;
- `GET flow.google.com/projects` bounces through `accounts.google.com/ServiceLogin` and
  `flow.google.com/accounts/SetOSID`, which gives this jar its own OSID / __Secure-OSID;
- the landed page carries the bootstrap values every RPC needs (bl, f.sid, xsrf `at`) and the email.

A session is addressed by an opaque handle (`FLOWCK.<email hash>.<nonce>`) that the rest of the code
keeps in the old access-token slot, so routing, health and push logic above the transport stay as they were.
"""

import asyncio
import hashlib
import json
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from http.cookiejar import Cookie, CookieJar
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode, urljoin, urlparse

from curl_cffi.requests import AsyncSession

HANDLE_PREFIX = "FLOWCK."
FLOW_HOST = "flow.google.com"
FLOW_ORIGIN = "https://flow.google.com"
BOOTSTRAP_PATH = "/projects"
BATCHEXECUTE_URL = f"{FLOW_ORIGIN}/_/AiSandboxAngularFrontend/data/batchexecute"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)
# Cookies Chrome keeps host-only on accounts.google.com; every other Google login cookie is .google.com.
ACCOUNTS_HOST_COOKIES = frozenset({"LSID", "ACCOUNT_CHOOSER", "OTZ"})
# Hosts a login bounce may pass through. Anything else is refused (never follow a redirect off Google).
BOUNCE_HOSTS = frozenset({FLOW_HOST, "accounts.google.com"})
BOOTSTRAP_TTL_SECONDS = 600
SESSION_TTL_SECONDS = 2 * 3600
MAX_SESSIONS = 500

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_BL_RE = re.compile(r"boq_labs-ai-sandbox-frontend_[A-Za-z0-9_.-]+")
_FSID_RE = re.compile(r'"FdrFJe":"(-?[0-9]+)"')
_XSRF_PAGE_RE = re.compile(r'"SNlM0e"\s*:\s*"((?:\\.|[^"\\])+)"')
_XSRF_RPC_RE = re.compile(r'\["xsrf"\s*,\s*"((?:\\.|[^"\\])*)"')


class FlowCookieAuthError(Exception):
    """The Google login behind a cookie session is dead or stale (re-push needed).

    The text carries UNAUTHENTICATED so every existing auth classifier treats it as a dead session.
    """

    def __init__(self, message: str):
        super().__init__(f"UNAUTHENTICATED: {message}")
        self.status_code = 401
        self.reason = "UNAUTHENTICATED"


class FlowRpcError(Exception):
    """A batchexecute call Google answered with an error (not a transport failure)."""

    def __init__(self, rpc_id: str, code: str, status_code: int = 400):
        self.rpc_id = rpc_id
        self.code = str(code)
        self.status_code = status_code
        super().__init__(f"Flow RPC {rpc_id} rejected: {self.code}")


def _cookie(name: str, value: str, domain: str, path: str = "/") -> Cookie:
    dotted = domain.startswith(".")
    return Cookie(
        0, name, value, None, False, domain, dotted, dotted, path or "/", True,
        True, None, False, None, None, {},
    )


def parse_cookie_items(raw: Any) -> List[Dict[str, str]]:
    """Return [{name, value, domain?, path?}] from a JSON list, {"cookies": [...]}, dict or `a=b; c=d` text."""
    data = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            data = json.loads(text)
        except ValueError:
            data = None
            items = []
            for part in text.replace("\n", ";").split(";"):
                if "=" in part:
                    name, value = part.split("=", 1)
                    if name.strip() and value.strip():
                        items.append({"name": name.strip(), "value": value.strip()})
            return items
    if isinstance(data, dict) and isinstance(data.get("cookies"), list):
        data = data["cookies"]
    if isinstance(data, dict):
        return [{"name": str(k), "value": str(v)} for k, v in data.items() if k and v]
    items: List[Dict[str, str]] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "").strip()
            if name and value:
                items.append({
                    "name": name, "value": value,
                    "domain": str(item.get("domain") or "").strip(),
                    "path": str(item.get("path") or "/").strip() or "/",
                })
    return items


def build_cookie_jar(raw: Any) -> CookieJar:
    """Place each Google cookie on the host Chrome would send it to.

    Name/value-only lists (worker extension 3.7.x) carry no domain: `__Host-*` and the accounts-only
    names go to accounts.google.com, the rest to .google.com. Items that carry a domain keep it.
    """
    jar = CookieJar()
    for item in parse_cookie_items(raw):
        name, value = item["name"], item["value"]
        domain = item.get("domain") or ""
        if not domain:
            domain = "accounts.google.com" if (name in ACCOUNTS_HOST_COOKIES or name.startswith("__Host-")) else ".google.com"
        if not domain.lstrip(".").endswith("google.com"):
            continue
        if name.startswith("__Host-"):
            domain = domain.lstrip(".")
        jar.set_cookie(_cookie(name, value, domain, item.get("path") or "/"))
    return jar


def has_login_cookies(raw: Any) -> bool:
    names = {item["name"] for item in parse_cookie_items(raw)}
    return bool(names & {"SID", "__Secure-1PSID", "__Secure-3PSID"})


def cookies_fingerprint(raw: Any) -> str:
    pairs = sorted((i["name"], i["value"]) for i in parse_cookie_items(raw))
    return hashlib.sha256(json.dumps(pairs).encode("utf-8")).hexdigest()[:16]


def email_hash(email: str) -> str:
    return hashlib.sha256((email or "").strip().lower().encode("utf-8")).hexdigest()[:12]


def is_session_handle(value: Optional[str]) -> bool:
    return isinstance(value, str) and value.startswith(HANDLE_PREFIX)


def parse_batchexecute_frames(text: str) -> List[List[Any]]:
    frames: List[List[Any]] = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line.startswith("["):
            continue
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        if isinstance(frame, list):
            frames.append(frame)
    return frames


def _first_error_code(value: Any) -> str:
    if isinstance(value, str):
        return value if value.startswith(("PUBLIC_ERROR_", "ERROR_")) else ""
    if isinstance(value, (list, tuple)):
        for item in value:
            code = _first_error_code(item)
            if code:
                return code
    return ""


def extract_rpc_payload(frames: List[List[Any]], rpc_id: str) -> Any:
    """Return the decoded wrb.fr payload for rpc_id, raising on `er` frames / PUBLIC_ERROR codes."""
    for frame in frames:
        for item in frame:
            if not isinstance(item, list) or len(item) < 3:
                continue
            if item[0] == "er":
                code = item[5] if len(item) > 5 and item[5] is not None else (item[6] if len(item) > 6 else None)
                if str(code) == "401":
                    raise FlowCookieAuthError(f"Flow refused the Google login (rpc={rpc_id}, code=401)")
                raise FlowRpcError(rpc_id, f"code={code if code is not None else 'unknown'}",
                                   status_code=int(code) if str(code).isdigit() and 400 <= int(code) < 600 else 400)
            if item[0] != "wrb.fr" or item[1] != rpc_id:
                continue
            detail = item[5] if len(item) > 5 else None
            code = _first_error_code(detail)
            if code:
                raise FlowRpcError(rpc_id, code, status_code=_status_for_code(code))
            if item[2] is None:
                if detail is not None:
                    raise FlowRpcError(rpc_id, f"detail={json.dumps(detail)[:200]}")
                return None
            try:
                return json.loads(item[2])
            except (TypeError, ValueError):
                raise FlowRpcError(rpc_id, "undecodable payload")
    return None


def _status_for_code(code: str) -> int:
    upper = code.upper()
    if "UNAUTHENTICATED" in upper:
        return 401
    if "QUOTA" in upper or "TOO_MUCH_TRAFFIC" in upper or "RESOURCE_EXHAUSTED" in upper:
        return 429
    if "UNUSUAL_ACTIVITY" in upper or "PERMISSION" in upper:
        return 403
    return 400


@dataclass
class FlowCookieSession:
    handle: str
    email: str
    proxy: Optional[str]
    jar: CookieJar
    source_fingerprint: str
    token_id: Optional[int] = None
    user_agent: str = DEFAULT_USER_AGENT
    bl: str = ""
    fsid: str = ""
    xsrf: str = ""
    bootstrap_at: float = 0.0
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def bootstrap_fresh(self) -> bool:
        return bool(self.bl and self.fsid) and (time.time() - self.bootstrap_at) < BOOTSTRAP_TTL_SECONDS


class FlowCookieSessions:
    """In-memory registry of logged-in Flow cookie sessions, keyed by handle."""

    def __init__(self):
        self._by_handle: Dict[str, FlowCookieSession] = {}
        self._nonce = uuid.uuid4().hex[:10]

    def get(self, handle: str) -> Optional[FlowCookieSession]:
        session = self._by_handle.get(handle)
        if session and time.time() - session.last_used_at > SESSION_TTL_SECONDS:
            self._by_handle.pop(handle, None)
            return None
        return session

    def find(self, email: str, source_fingerprint: str, proxy: Optional[str]) -> Optional[FlowCookieSession]:
        session = self.get(self.handle_for(email))
        if session and session.source_fingerprint == source_fingerprint and session.proxy == proxy:
            return session
        return None

    def handle_for(self, email: str) -> str:
        # One handle per account per process: the DB `at` column stays stable between refreshes.
        return f"{HANDLE_PREFIX}{email_hash(email)}.{self._nonce}"

    def put(self, session: FlowCookieSession) -> None:
        if len(self._by_handle) >= MAX_SESSIONS:
            oldest = sorted(self._by_handle.values(), key=lambda s: s.last_used_at)[: MAX_SESSIONS // 10]
            for item in oldest:
                self._by_handle.pop(item.handle, None)
        self._by_handle[session.handle] = session

    def alias(self, handle: str, session: FlowCookieSession) -> None:
        """A handle stored by an earlier process (DB `at`) now points at this live session."""
        self._by_handle[handle] = session

    def drop(self, handle: str) -> None:
        session = self._by_handle.pop(handle, None)
        if session is not None:
            for key in [k for k, v in self._by_handle.items() if v is session]:
                self._by_handle.pop(key, None)

    def drop_email(self, email: str) -> None:
        self.drop(self.handle_for((email or "").strip().lower()))


_ACCOUNT_EMAIL_RE = re.compile(r'"oPEP7c"\s*:\s*("(?:[^"\\]|\\.)*")')


def _find_email(text: str, expected: Optional[str]) -> str:
    """The signed-in account: Flow's bootstrap field oPEP7c (upstream's identity source); a page-wide
    email scan is only the fallback, preferring the expected account when it is among them."""
    match = _ACCOUNT_EMAIL_RE.search(text or "")
    if match:
        try:
            value = json.loads(match.group(1))
        except ValueError:
            value = ""
        if isinstance(value, str) and "@" in value:
            return value.strip().lower()
    found = []
    for match in _EMAIL_RE.findall(text or ""):
        lowered = match.lower()
        if lowered.endswith(("@google.com", "@example.com")) or lowered in found:
            continue
        found.append(lowered)
    if expected and expected.strip().lower() in found:
        return expected.strip().lower()
    return found[0] if found else ""


async def load_bootstrap(
    session: FlowCookieSession,
    timeout: int = 30,
    expected_email: Optional[str] = None,
    impersonate: str = "chrome",
) -> None:
    """Load flow.google.com/projects with the session jar (bouncing through accounts.google.com when
    the jar has no OSID yet) and store bl / f.sid / xsrf / email on the session."""
    if not session.proxy:
        raise RuntimeError("Flow cookie login refused: no proxy for this account (never log in direct)")
    url = f"{FLOW_ORIGIN}{BOOTSTRAP_PATH}"
    headers = {
        "User-Agent": session.user_agent or DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    response = None
    async with AsyncSession(trust_env=False, cookies=session.jar, impersonate=impersonate) as http:
        for _ in range(10):
            response = await http.get(url, headers=headers, proxy=session.proxy,
                                      timeout=max(10, min(int(timeout or 30), 45)), allow_redirects=False)
            location = response.headers.get("location") or ""
            if not (300 <= response.status_code < 400 and location):
                break
            url = urljoin(url, location)
            host = (urlparse(url).hostname or "").lower()
            if urlparse(url).scheme != "https" or host not in BOUNCE_HOSTS:
                raise FlowCookieAuthError(f"Flow login bounced off Google to {host or 'nowhere'}")
        else:
            raise RuntimeError("Flow cookie login exceeded the redirect limit")
        _merge_jar(session.jar, http.cookies.jar)
    final_host = (urlparse(url).hostname or "").lower()
    text = response.text if response is not None else ""
    if response is None or response.status_code in (401, 403) or final_host != FLOW_HOST:
        raise FlowCookieAuthError(
            f"Google did not sign this account in to Flow (landed on {final_host}, HTTP "
            f"{getattr(response, 'status_code', '?')}); the browser must push fresh cookies"
        )
    if response.status_code >= 400:
        raise RuntimeError(f"Flow bootstrap failed: HTTP {response.status_code}")
    bl = _BL_RE.search(text)
    fsid = _FSID_RE.search(text)
    if not bl or not fsid:
        raise FlowCookieAuthError("Flow page loaded without its signed-in bootstrap data")
    xsrf = _XSRF_PAGE_RE.search(text)
    email = _find_email(text, expected_email or session.email)
    if not email:
        raise FlowCookieAuthError("Flow page shows no signed-in account")
    session.bl = bl.group(0)
    session.fsid = fsid.group(1)
    session.xsrf = _json_unescape(xsrf.group(1)) if xsrf else ""
    session.email = email
    session.bootstrap_at = time.time()


def _json_unescape(value: str) -> str:
    try:
        return json.loads('"' + value + '"')
    except ValueError:
        return value


def _merge_jar(target: CookieJar, source: CookieJar) -> None:
    if target is source:
        return
    for cookie in source:
        target.set_cookie(cookie)


async def call_rpc(
    session: FlowCookieSession,
    rpc_id: str,
    argument: Any,
    source_path: str = BOOTSTRAP_PATH,
    timeout: int = 60,
    proxy: Optional[str] = None,
    user_agent: Optional[str] = None,
    impersonate: str = "chrome",
) -> Any:
    """POST one batchexecute RPC and return its decoded payload (None when Google sends none)."""
    if not session.bootstrap_fresh():
        async with session.lock:
            if not session.bootstrap_fresh():
                await load_bootstrap(session, timeout=min(timeout, 30))
    session.last_used_at = time.time()
    egress = proxy or session.proxy
    if not egress:
        raise RuntimeError("Flow RPC refused: no proxy for this account (never call Google direct)")
    url = (
        f"{BATCHEXECUTE_URL}?rpcids={quote(rpc_id, safe='')}"
        f"&source-path={quote(source_path, safe='')}&bl={quote(session.bl, safe='')}"
        f"&f.sid={session.fsid}&hl=en&_reqid={random.randint(10_000, 990_000)}&rt=c"
    )
    fields = {"f.req": json.dumps([[[rpc_id, json.dumps(argument, separators=(",", ":")), None, "generic"]]],
                                  separators=(",", ":"))}
    if session.xsrf:
        fields["at"] = session.xsrf
    headers = {
        "User-Agent": user_agent or session.user_agent or DEFAULT_USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Origin": FLOW_ORIGIN,
        "Referer": f"{FLOW_ORIGIN}{source_path}",
        "x-same-domain": "1",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    async with AsyncSession(trust_env=False, cookies=session.jar, impersonate=impersonate) as http:
        response = await http.post(url, data=urlencode(fields), headers=headers, proxy=egress, timeout=timeout)
        text = response.text
        xsrf = _XSRF_RPC_RE.search(text or "")
        if xsrf and response.status_code < 500:
            # Google rotated the page token: retry once with the one it handed back.
            session.xsrf = _json_unescape(xsrf.group(1))
            fields["at"] = session.xsrf
            response = await http.post(url, data=urlencode(fields), headers=headers, proxy=egress, timeout=timeout)
            text = response.text
        _merge_jar(session.jar, http.cookies.jar)
    if response.status_code in (401, 403) and "wrb.fr" not in (text or ""):
        session.bootstrap_at = 0.0
        raise FlowCookieAuthError(f"Flow RPC {rpc_id} answered HTTP {response.status_code}")
    if response.status_code >= 400:
        session.bootstrap_at = 0.0
        raise FlowRpcError(rpc_id, f"HTTP {response.status_code}: {(text or '')[:160]}", status_code=response.status_code)
    return extract_rpc_payload(parse_batchexecute_frames(text), rpc_id)


async def establish(
    registry: FlowCookieSessions,
    google_cookies: Any,
    proxy: Optional[str],
    expected_email: Optional[str] = None,
    token_id: Optional[int] = None,
    user_agent: Optional[str] = None,
    timeout: int = 30,
) -> FlowCookieSession:
    """Reuse a live session for these exact cookies + proxy, else log in to Flow with them."""
    if not has_login_cookies(google_cookies):
        raise FlowCookieAuthError("no Google login cookies stored for this account")
    fingerprint = cookies_fingerprint(google_cookies)
    if expected_email:
        existing = registry.find(expected_email.strip().lower(), fingerprint, proxy)
        if existing and existing.bootstrap_fresh():
            existing.last_used_at = time.time()
            if token_id is not None:
                existing.token_id = token_id
            return existing
    session = FlowCookieSession(
        handle="",
        email=(expected_email or "").strip().lower(),
        proxy=proxy,
        jar=build_cookie_jar(google_cookies),
        source_fingerprint=fingerprint,
        token_id=token_id,
        user_agent=user_agent or DEFAULT_USER_AGENT,
    )
    await load_bootstrap(session, timeout=timeout, expected_email=expected_email)
    session.handle = registry.handle_for(session.email)
    registry.put(session)
    return session


def session_summary(session: FlowCookieSession) -> Dict[str, Any]:
    return {"email": session.email, "token_id": session.token_id, "bootstrap_age_s": int(time.time() - session.bootstrap_at)}


def split_handle(handle: str) -> Tuple[str, str]:
    parts = (handle or "").split(".")
    return (parts[1], parts[2]) if len(parts) >= 3 else ("", "")
