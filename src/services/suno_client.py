"""HTTP client for Suno's web API.

Endpoints and payload fields were read from Suno's own web bundle on 2026-09-16
(see ``docs/suno-provider-plan.md``). This deliberately does NOT follow
``gcui-art/suno-api``: that project still calls ``/api/generate/v2/``,
``/api/feed/v2`` and ``cdn1.suno.ai``, all of which Suno retired, which is why
its issue tracker reports 422s on generation and dead downloads.

Nothing here touches the database or job state; it is a thin, testable transport.
"""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from curl_cffi.requests import AsyncSession

from ..core.logger import debug_logger

try:  # pragma: no cover - import guard mirrors flow_client
    import httpx
except ImportError:  # pragma: no cover
    httpx = None


class SunoAPIError(Exception):
    """Upstream Suno failure with the HTTP status preserved."""

    def __init__(self, status_code: int, message: str, code: str = "", body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code or ""
        self.message = message
        self.body = body


class SunoAuthError(SunoAPIError):
    """Session/cookie rejected upstream. The account needs a fresh cookie."""


class SunoRateLimited(SunoAPIError):
    """429 from Suno. Transient; back off, never treat as a job failure."""


class SunoSession:
    """Live credentials for one Suno account.

    ``cookies`` is the mutable jar. Clerk rotates ``__client`` through
    ``set-cookie`` on every token call, so the jar must be persisted back to the
    account row after each refresh or the next refresh fails.
    """

    __slots__ = ("cookies", "sid", "jwt", "jwt_obtained_at", "device_id", "dirty")

    def __init__(self, cookies: Dict[str, str], sid: Optional[str] = None,
                 jwt: Optional[str] = None, device_id: Optional[str] = None):
        self.cookies = dict(cookies)
        self.sid = sid
        self.jwt = jwt
        self.jwt_obtained_at: float = 0.0
        self.device_id = device_id or cookies.get("ajs_anonymous_id")
        # Set whenever an upstream response changed the jar (a rotated
        # ``__client``). The owner of the session persists and clears it.
        self.dirty: bool = False

    @property
    def client_cookie(self) -> Optional[str]:
        return self.cookies.get("__client")

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items() if v)

    def cookie_string(self) -> str:
        """Serialized jar, for persisting back to the account row."""
        return self.cookie_header()


def parse_cookie_string(raw: str) -> Dict[str, str]:
    """Parse a browser ``Cookie:`` header value into a dict.

    Accepts what the owner copies out of DevTools, including a leading
    ``Cookie:`` label and newline wrapping.
    """
    if not raw:
        return {}
    text = raw.strip()
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1]
    text = text.replace("\n", " ").replace("\r", " ")

    jar: Dict[str, str] = {}
    for part in text.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name:
            jar[name] = value.strip()
    return jar


def _merge_set_cookie(jar: Dict[str, str], set_cookie_values: List[str]) -> bool:
    """Fold ``Set-Cookie`` response headers back into the jar.

    Returns True when any value actually changed, so the caller knows the jar
    now differs from what is persisted.
    """
    changed = False
    for header in set_cookie_values or []:
        first = header.split(";", 1)[0].strip()
        if "=" not in first:
            continue
        name, value = first.split("=", 1)
        name = name.strip()
        value = value.strip()
        if name and jar.get(name) != value:
            jar[name] = value
            changed = True
    return changed


class SunoClient:
    """Transport for one Suno account's web session."""

    STUDIO_BASE = "https://studio-api.prod.suno.com"
    CLERK_BASE = "https://auth.suno.com"
    CLERK_API_VERSION = "2025-11-10"
    CLERK_JS_VERSION = "5.117.0"

    # The web client is a browser; impersonate one so Suno's edge sees a
    # consistent TLS fingerprint rather than a bare Python client.
    IMPERSONATE = "chrome124"
    DEFAULT_TIMEOUT = 30
    GENERATE_TIMEOUT = 45
    JWT_TTL_SECONDS = 45  # Suno's JWT is short-lived; refresh generously.

    def __init__(self, proxy_manager=None):
        self.proxy_manager = proxy_manager

    # ------------------------------------------------------------- internals

    async def _proxy(self) -> Optional[str]:
        if not self.proxy_manager:
            return None
        try:
            return await self.proxy_manager.get_request_proxy_url()
        except Exception:  # pragma: no cover - proxy config is best effort
            return None

    def _base_headers(self, session: SunoSession) -> Dict[str, str]:
        headers = {
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://suno.com",
            "Referer": "https://suno.com/",
            "Content-Type": "text/plain;charset=UTF-8",
        }
        if session.device_id:
            headers["Device-Id"] = f'"{session.device_id}"'
        return headers

    async def _request(
        self,
        session: SunoSession,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        bearer: Optional[str] = None,
        cookie_auth: bool = False,
        timeout: Optional[int] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        expect_json: bool = True,
    ) -> Tuple[int, Any]:
        """One upstream call. Never logs request or response bodies: they carry
        the Clerk JWT and the cookie jar."""
        headers = self._base_headers(session)
        if extra_headers:
            headers.update(extra_headers)
        if cookie_auth:
            # Clerk's own endpoints authenticate with the raw __client cookie.
            if not session.client_cookie:
                raise SunoAuthError(401, "Missing __client cookie for this account.", "no_client_cookie")
            headers["Authorization"] = session.client_cookie
        elif bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        cookie_header = session.cookie_header()
        if cookie_header:
            headers["Cookie"] = cookie_header

        proxy_url = await self._proxy()
        request_timeout = timeout or self.DEFAULT_TIMEOUT
        started = time.time()

        kwargs: Dict[str, Any] = {
            "headers": headers,
            "proxy": proxy_url,
            "timeout": request_timeout,
            "impersonate": self.IMPERSONATE,
        }
        try:
            async with AsyncSession(trust_env=False) as http:
                if method.upper() == "GET":
                    response = await http.get(url, **kwargs)
                else:
                    if json_body is not None:
                        kwargs["data"] = json.dumps(json_body)
                    response = await http.post(url, **kwargs)
        except Exception as exc:
            raise SunoAPIError(0, f"Suno request failed: {exc}", "transport_error") from exc

        duration_ms = (time.time() - started) * 1000
        # Path + status only. Bodies hold credentials.
        debug_logger.log_info(
            f"[SUNO] {method.upper()} {urlsplit(url).path} -> {response.status_code} "
            f"({duration_ms:.0f}ms)"
        )

        set_cookie = response.headers.get_list("set-cookie") if hasattr(response.headers, "get_list") else []
        if set_cookie and _merge_set_cookie(session.cookies, list(set_cookie)):
            session.dirty = True

        data: Any = None
        if expect_json:
            try:
                data = response.json()
            except Exception:
                data = None

        status = response.status_code
        if status == 401 or status == 403:
            raise SunoAuthError(status, _error_message(data, "Suno rejected the session."),
                                _error_code(data), data)
        if status == 429:
            raise SunoRateLimited(status, _error_message(data, "Suno rate limited this account."),
                                  _error_code(data), data)
        if status >= 400:
            raise SunoAPIError(status, _error_message(data, f"Suno returned HTTP {status}."),
                               _error_code(data), data)
        return status, data

    # ------------------------------------------------------------------ auth

    async def refresh_session(self, session: SunoSession) -> SunoSession:
        """Exchange the ``__client`` cookie for a fresh bearer JWT.

        Two Clerk calls: resolve the active session id, then mint a token. Both
        authenticate with the cookie, and both may rotate it.
        """
        if not session.sid:
            _status, data = await self._request(
                session, "GET",
                f"{self.CLERK_BASE}/v1/client"
                f"?__clerk_api_version={self.CLERK_API_VERSION}"
                f"&_clerk_js_version={self.CLERK_JS_VERSION}",
                cookie_auth=True,
            )
            sid = (((data or {}).get("response") or {}).get("last_active_session_id"))
            if not sid:
                raise SunoAuthError(
                    401,
                    "Clerk returned no active session; the Suno cookie is stale or signed out.",
                    "no_session",
                    data,
                )
            session.sid = sid

        _status, data = await self._request(
            session, "POST",
            f"{self.CLERK_BASE}/v1/client/sessions/{session.sid}/tokens"
            f"?__clerk_api_version={self.CLERK_API_VERSION}"
            f"&_clerk_js_version={self.CLERK_JS_VERSION}",
            cookie_auth=True,
        )
        jwt = (data or {}).get("jwt")
        if not jwt:
            raise SunoAuthError(401, "Clerk did not return a JWT for this session.", "no_jwt", data)

        session.jwt = jwt
        session.jwt_obtained_at = time.time()
        return session

    async def ensure_token(self, session: SunoSession) -> str:
        """Return a usable bearer, refreshing when stale."""
        if not session.jwt or (time.time() - session.jwt_obtained_at) > self.JWT_TTL_SECONDS:
            await self.refresh_session(session)
        return session.jwt  # type: ignore[return-value]

    # --------------------------------------------------------------- account

    async def billing_info(self, session: SunoSession) -> Dict[str, Any]:
        """Credits and plan. ``GET /api/billing/info/``."""
        token = await self.ensure_token(session)
        _status, data = await self._request(
            session, "GET", f"{self.STUDIO_BASE}/api/billing/info/", bearer=token
        )
        return data or {}

    async def session_info(self, session: SunoSession) -> Dict[str, Any]:
        """Upstream user identity, used to key accounts and block duplicates."""
        token = await self.ensure_token(session)
        _status, data = await self._request(
            session, "GET", f"{self.STUDIO_BASE}/api/session/", bearer=token
        )
        return data or {}

    # --------------------------------------------------------------- captcha

    async def captcha_check(self, session: SunoSession, ctype: str = "generation") -> Dict[str, Any]:
        """``POST /api/c/check`` -> ``{required, captcha_version}``.

        Suno's own client calls this immediately before generating and sends
        ``token: null`` when ``required`` is false.
        """
        token = await self.ensure_token(session)
        _status, data = await self._request(
            session, "POST", f"{self.STUDIO_BASE}/api/c/check",
            json_body={"ctype": ctype}, bearer=token,
        )
        data = data or {}
        return {
            "required": bool(data.get("required")),
            "captcha_version": data.get("captcha_version"),
        }

    # -------------------------------------------------------------- generate

    async def generate(self, session: SunoSession, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """``POST /api/generate/v2-web/`` -> the created clips (normally two)."""
        token = await self.ensure_token(session)
        _status, data = await self._request(
            session, "POST", f"{self.STUDIO_BASE}/api/generate/v2-web/",
            json_body=payload, bearer=token, timeout=self.GENERATE_TIMEOUT,
        )
        clips = (data or {}).get("clips")
        if not isinstance(clips, list) or not clips:
            raise SunoAPIError(
                502, "Suno accepted the request but returned no clips.", "no_clips", data
            )
        return clips

    async def feed(self, session: SunoSession, clip_ids: List[str]) -> List[Dict[str, Any]]:
        """``POST /api/feed/v3`` filtered to specific clip ids.

        The exact filter key is the one unverified part of the contract; the
        response shape (``{clips: [...]}``) is confirmed. If Suno ignores the
        filter we still match by id below, so a wrong key degrades to a wider
        read rather than wrong results.
        """
        token = await self.ensure_token(session)
        _status, data = await self._request(
            session, "POST", f"{self.STUDIO_BASE}/api/feed/v3",
            json_body={"limit": max(len(clip_ids), 1), "filters": {"clip_ids": clip_ids}},
            bearer=token,
        )
        clips = (data or {}).get("clips")
        if not isinstance(clips, list):
            return []
        wanted = set(clip_ids)
        return [c for c in clips if isinstance(c, dict) and c.get("id") in wanted]

    async def get_clip(self, session: SunoSession, clip_id: str) -> Dict[str, Any]:
        """``GET /api/clip/{id}`` - single-clip fallback when the feed filter misses."""
        token = await self.ensure_token(session)
        _status, data = await self._request(
            session, "GET", f"{self.STUDIO_BASE}/api/clip/{clip_id}", bearer=token
        )
        return data or {}

    # -------------------------------------------------------------- download

    async def download_url(self, session: SunoSession, clip_id: str,
                           fmt: str = "mp3") -> Dict[str, Any]:
        """``GET /api/download/clip/{id}?format=`` -> ``{status|url}``.

        This is the path Suno's own download button uses. It replaces the dead
        ``audio_url`` / ``cdn1.suno.ai`` routes. A ``processing`` status means
        the asset is still being packaged; the caller retries.
        """
        token = await self.ensure_token(session)
        _status, data = await self._request(
            session, "GET",
            f"{self.STUDIO_BASE}/api/download/clip/{clip_id}?format={fmt}",
            bearer=token,
        )
        return data or {}

    # Hosts a resolved download URL may point at. A signed CDN link is the
    # normal case; anything else is refused rather than followed blindly.
    ALLOWED_DOWNLOAD_SUFFIXES = (
        ".suno.ai", ".suno.com", ".cloudfront.net", ".amazonaws.com",
    )
    MAX_DOWNLOAD_REDIRECTS = 5

    @classmethod
    def validate_download_url(cls, url: str) -> str:
        """Refuse download URLs that do not point at Suno's own media hosts.

        The resolved URL comes from an upstream response, so it is data, not a
        trusted instruction: without this check a changed or hostile response
        could aim our server-side fetch at an arbitrary host.
        """
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise SunoAPIError(502, "Suno returned a non-HTTP download URL.", "bad_download_url")
        host = (parts.hostname or "").lower()
        if not host:
            raise SunoAPIError(502, "Suno returned a download URL with no host.", "bad_download_url")
        if not any(host == suffix.lstrip(".") or host.endswith(suffix)
                   for suffix in cls.ALLOWED_DOWNLOAD_SUFFIXES):
            raise SunoAPIError(
                502, f"Refusing to fetch Suno audio from unexpected host '{host}'.",
                "bad_download_host",
            )
        return url

    async def stream_download(self, url: str, *, chunk_size: int = 64 * 1024,
                              max_bytes: int = 200 * 1024 * 1024,
                              timeout: int = 120) -> AsyncIterator[bytes]:
        """Stream a resolved download URL in a credential-free context.

        Neither the Clerk bearer nor the account cookie jar is attached: the URL
        is a signed CDN link and carries its own authorization. Redirects are
        followed manually so every hop is host-checked, and the upstream
        response is closed when the caller stops consuming (the ``async with``
        unwinds on generator close).
        """
        if httpx is None:  # pragma: no cover
            raise SunoAPIError(500, "httpx is required to stream Suno audio.", "no_httpx")

        proxy_url = None
        if self.proxy_manager:
            try:
                proxy_url = await self.proxy_manager.get_media_proxy_url()
            except Exception:  # pragma: no cover
                proxy_url = None

        current = self.validate_download_url(url)
        sent = 0
        # cookies={} keeps httpx from establishing any jar of its own.
        async with httpx.AsyncClient(
            proxy=proxy_url, timeout=timeout, follow_redirects=False, cookies={},
        ) as client:
            for _hop in range(self.MAX_DOWNLOAD_REDIRECTS + 1):
                async with client.stream("GET", current, headers={"Accept": "*/*"}) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise SunoAPIError(502, "Suno download redirect had no target.",
                                               "bad_download_url")
                        current = self.validate_download_url(str(response.url.join(location)))
                        continue
                    if response.status_code >= 400:
                        raise SunoAPIError(
                            response.status_code,
                            f"Suno audio download failed with HTTP {response.status_code}.",
                            "download_failed",
                        )
                    async for chunk in response.aiter_bytes(chunk_size):
                        sent += len(chunk)
                        if sent > max_bytes:
                            raise SunoAPIError(502, "Suno audio exceeded the size limit.",
                                               "too_large")
                        yield chunk
                    return
        raise SunoAPIError(502, "Too many redirects fetching Suno audio.", "too_many_redirects")


def _error_message(data: Any, fallback: str) -> str:
    if isinstance(data, dict):
        for key in ("detail", "message", "error"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict):
                nested = value.get("message") or value.get("detail")
                if isinstance(nested, str) and nested:
                    return nested
    return fallback


def _error_code(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("error_type", "code", "type"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
    return ""
