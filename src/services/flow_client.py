"""Flow API Client for VideoFX (Veo)"""
import asyncio
import json
import contextvars
import time
import uuid
import urllib.parse
import random
import base64
import gzip
import ssl
import re
from typing import Dict, Any, Optional, List, Union, Callable, Awaitable
from urllib.parse import quote, urljoin, urlparse
import urllib.error
import urllib.request
from curl_cffi.requests import AsyncSession
from ..core.logger import debug_logger, mask_proxy_url
from ..core.config import config, get_yescaptcha_min_score

try:
    import httpx
except ImportError:
    httpx = None


class FlowAPIError(Exception):
    """Structured Flow API HTTP error that preserves the upstream status code
    and Google API ``reason`` (e.g. ``UNAUTHENTICATED``).

    ``str(self)`` reproduces the legacy message text verbatim so every existing
    caller that inspects the message string keeps working unchanged. New callers
    can branch on ``status_code`` / ``reason`` for robust classification instead
    of substring matching.
    """

    def __init__(self, status_code: int, message: str, reason: str = ""):
        self.status_code = status_code
        self.reason = reason or ""
        self.message = message
        super().__init__(message)


def classify_upsample_error(error: BaseException) -> str:
    """Sort a failed 2K/4K enlarge call (live evidence 26 Sep 2026):
    quota   - PER_MODEL_DAILY_QUOTA_REACHED: the account's upscaler quota is spent until the PT reset
    access  - MODEL_ACCESS_DENIED: the account's tier cannot enlarge to this size (4K on Pro)
    traffic - UNUSUAL_ACTIVITY(_TOO_MUCH_TRAFFIC) / reCAPTCHA evaluation failed: Google refuses this
              account/IP right now; hammering it every 3 s only feeds the score
    refused - the prompt/image was refused (UNSAFE, SEXUAL, MINOR…)
    other   - network, 5xx, anything else (normal retry budget)"""
    text = f"{getattr(error, 'reason', '') or ''} {error}".upper()
    if "PER_MODEL_DAILY_QUOTA" in text or "QUOTA_REACHED" in text:
        return "quota"
    if "MODEL_ACCESS_DENIED" in text:
        return "access"
    if "UNUSUAL_ACTIVITY" in text or "RECAPTCHA EVALUATION FAILED" in text:
        return "traffic"
    if any(k in text for k in ("PUBLIC_ERROR_UNSAFE", "PUBLIC_ERROR_SEXUAL", "PUBLIC_ERROR_MINOR", "PUBLIC_ERROR_PROMINENT")):
        return "refused"
    return "other"


UPSAMPLE_NO_RETRY_KINDS = ("quota", "access", "traffic", "refused")


class FlowClient:
    """VideoFX API client"""

    FLOW_PUBLIC_API_KEY = "AIzaSyBtrm0o5ab1c-Ec8ZuLcGt3oJAA5VWt3pY"
    FLOW_BROWSER_CHANNEL_HEADER = "stable"
    FLOW_BROWSER_COPYRIGHT_HEADER = "Copyright 2026 Google LLC. All Rights Reserved."
    FLOW_BROWSER_VALIDATION_HEADER = "MRCPrt/rS3JY47x2Yiz9h3ag4U8="
    FLOW_BROWSER_YEAR_HEADER = "2026"
    FLOW_FRONTEND_EXPERIMENT_IDS = (
        "106184493,106256669,105798603,106281924,106259075,106262194,"
        "105993823,106104244,105484652,1714252,105928947,106238955,"
        "106225453,106131447,1706538,119157484,106243706,105746691,"
        "106151974,106125249,106001691,106077941"
    )
    YESCAPTCHA_SLOT_BACKOFF_SECONDS = (3, 5, 8, 13, 20)

    def __init__(self, proxy_manager, db=None):
        self.proxy_manager = proxy_manager
        self.db = db  # Database instance for captcha config
        self.labs_base_url = config.flow_labs_base_url  # https://labs.google/fx/api
        self.api_base_url = config.flow_api_base_url    # https://aisandbox-pa.googleapis.com/v1
        self.timeout = config.flow_timeout
        # Cached User-Agent per account
        self._user_agent_cache = {}
        # Browser fingerprint bound to the current request (contextvar, so concurrent requests do not mix)
        self._request_fingerprint_ctx: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
            "flow_request_fingerprint",
            default=None
        )
        # 2026-09-23 server-side reCAPTCHA fallback. Per request (contextvar):
        # where the last token came from ("extension" | "server") and whether the
        # rest of this request must mint on the server (set once Google rejected an
        # extension-minted token; covers every later stage, e.g. Omni approval).
        self._mint_source_ctx: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
            "flow_mint_source", default=None
        )
        self._mint_override_ctx: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
            "flow_mint_override", default=None
        )
        self._current_token_id_ctx: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
            "flow_mint_token_id", default=None
        )
        self._remote_browser_prefill_last_sent: Dict[str, float] = {}

        # Minimal browser-style headers only (what current Flow upstream still expects).
        # UA / Accept-Language / UA-CH all come from the runtime fingerprint bound to
        # the request chain (extension-reported real browser), not a random platform.
        self._default_client_headers = {
            "sec-ch-ua-mobile": "?0",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
        }
        # Dispatch policy is "send as soon as requested":
        # no local batching or queuing of submits, so a burst is not staggered into steps.


    def _generate_user_agent(self, account_id: str = None) -> str:
        """Build a stable User-Agent from the account ID
        
        Args:
            account_id: account key (e.g. email or token_id); same account gets the same UA
            
        Returns:
            User-Agent string
        """
        # No account ID: return a random UA
        if not account_id:
            account_id = f"random_{random.randint(1, 999999)}"
        
        # Return the cached value if present
        if account_id in self._user_agent_cache:
            return self._user_agent_cache[account_id]
        
        # Seed the RNG with the account ID so the same account always gets the same UA
        import hashlib
        seed = int(hashlib.md5(account_id.encode()).hexdigest()[:8], 16)
        rng = random.Random(seed)
        
        # Fallback only when there is no runtime fingerprint; matches current upstream Windows Chrome style
        chrome_versions = ["149.0.0.0"]
        ch_version = rng.choice(chrome_versions)
        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{ch_version} Safari/537.36"
        )
        
        # Cache the result
        self._user_agent_cache[account_id] = user_agent
        
        return user_agent

    def _set_request_fingerprint(self, fingerprint: Optional[Dict[str, Any]]):
        """Set the browser fingerprint for the current request."""
        self._request_fingerprint_ctx.set(dict(fingerprint) if fingerprint else None)

    def get_request_fingerprint(self) -> Optional[Dict[str, Any]]:
        """Get a snapshot of the browser fingerprint bound to the current request."""
        fingerprint = self._request_fingerprint_ctx.get()
        if not isinstance(fingerprint, dict) or not fingerprint:
            return None
        return dict(fingerprint)

    def reset_mint_context(self, token_id: Optional[int] = None):
        """Start of a generation request: forget the previous request's mint source/override."""
        self._mint_source_ctx.set(None)
        self._mint_override_ctx.set(None)
        self._current_token_id_ctx.set(token_id)

    def last_mint_source(self) -> Optional[str]:
        return self._mint_source_ctx.get()

    @staticmethod
    def _classify_flow_fail(status_code: Optional[int], text: str) -> str:
        """One classifier for anti-bot failures, shared by the transports, the
        FLOW_FAIL log line and the fallback decision. Returns RECAPTCHA / QUOTA /
        AUTH/ST_EXPIRED / HTTP."""
        _low = (text or "").lower()
        if status_code == 401 or "unauthenticated" in _low:
            return "AUTH/ST_EXPIRED"
        if any(m in _low for m in ("resource_exhausted", "resource has been exhausted", "quota")):
            return "QUOTA"
        if any(m in _low for m in ("unsafe_generation", "prominent_people", "public_error_unsafe", "policy")):
            return "HTTP"
        if any(m in _low for m in ("recaptcha", "captcha", "evaluation failed", "unusual_activity")):
            return "RECAPTCHA"
        return "HTTP"

    @staticmethod
    def _is_recaptcha_rejection(error: BaseException) -> bool:
        """True only for an upstream reCAPTCHA rejection (classified by the transport),
        never for a local mint failure, quota, prompt refusal or an unrelated 403."""
        return getattr(error, "flow_fail_class", None) == "RECAPTCHA"

    def clear_request_fingerprint(self):
        """Clear the browser fingerprint bound to the current request."""
        self._set_request_fingerprint(None)

    def _get_primary_accept_language(self, fallback: str = "en-US,en;q=0.9") -> str:
        fingerprint = self.get_request_fingerprint()
        if isinstance(fingerprint, dict):
            accept_language = str(fingerprint.get("accept_language") or "").strip()
            if accept_language:
                return self._normalize_accept_language_header(accept_language, fallback=fallback)
        return self._normalize_accept_language_header(fallback, fallback=fallback)

    def _get_primary_locale_code(self, fallback: str = "en-US") -> str:
        fingerprint = self.get_request_fingerprint()
        if isinstance(fingerprint, dict):
            language = str(fingerprint.get("language") or "").strip()
            if language:
                return language
            accept_language = str(fingerprint.get("accept_language") or "").strip()
            if accept_language:
                primary = accept_language.split(",", 1)[0].strip()
                if primary:
                    return primary
        return fallback

    def _infer_sec_ch_ua_from_user_agent(self, user_agent: Optional[str]) -> str:
        ua = str(user_agent or "").strip()
        if not ua:
            return ""
        import re
        match = re.search(r"(?:Chrome|Chromium)/(\d+)", ua, re.IGNORECASE)
        major = match.group(1) if match else "124"
        return f'"Google Chrome";v="{major}", "Chromium";v="{major}", "Not)A;Brand";v="24"'

    def _normalize_sec_ch_ua_header(
        self,
        sec_ch_ua: Optional[str],
        *,
        user_agent: Optional[str] = None,
    ) -> str:
        raw = str(sec_ch_ua or "").strip()
        inferred = self._infer_sec_ch_ua_from_user_agent(user_agent)
        if not raw:
            return inferred
        ua_text = str(user_agent or "").lower()
        if "chrome/" in ua_text and "google chrome" not in raw.lower():
            return inferred
        return raw or inferred

    def _build_fingerprint_from_user_agent(
        self,
        user_agent: Optional[str],
        *,
        accept_language: Optional[str] = None,
        proxy_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        ua = str(user_agent or "").strip()
        if not ua:
            return {}
        ua_lower = ua.lower()
        platform = '"Windows"'
        mobile = "?0"
        if "android" in ua_lower:
            platform = '"Android"'
            mobile = "?1"
        elif "iphone" in ua_lower or "ipad" in ua_lower or "ios" in ua_lower:
            platform = '"iOS"'
            mobile = "?1"
        elif "mac" in ua_lower:
            platform = '"macOS"'
        elif "linux" in ua_lower or "x11" in ua_lower:
            platform = '"Linux"'
        fingerprint: Dict[str, Any] = {
            "user_agent": ua,
            "sec_ch_ua": self._infer_sec_ch_ua_from_user_agent(ua),
            "sec_ch_ua_mobile": mobile,
            "sec_ch_ua_platform": platform,
        }
        normalized_accept_language = self._normalize_accept_language_header(accept_language)
        if normalized_accept_language:
            fingerprint["accept_language"] = normalized_accept_language
        if proxy_url:
            fingerprint["proxy_url"] = proxy_url
        return fingerprint

    def _normalize_accept_language_header(
        self,
        accept_language: Optional[str],
        fallback: str = "en-US,en;q=0.9",
    ) -> str:
        raw = str(accept_language or "").strip()
        if not raw:
            return fallback
        if "," in raw:
            normalized_parts: list[str] = []
            for index, item in enumerate(raw.split(",")):
                candidate = str(item or "").strip()
                if not candidate:
                    continue
                language = candidate.split(";", 1)[0].strip()
                if not language:
                    continue
                if index == 0:
                    normalized_parts.append(language)
                    continue
                q_match = re.search(r";\s*q=([0-9.]+)", candidate, re.IGNORECASE)
                q_value = q_match.group(1) if q_match else f"{max(0.1, 1 - (index * 0.1)):.1f}"
                normalized_parts.append(f"{language};q={q_value}")
            if normalized_parts:
                return ",".join(normalized_parts)
            return fallback
        if "-" in raw:
            primary = raw.split("-", 1)[0].strip()
            if len(primary) == 2 and primary.isalpha():
                return f"{raw},{primary};q=0.9"
        return raw

    def _get_effective_request_user_agent(self, account_id: Optional[str] = None) -> str:
        fingerprint = self.get_request_fingerprint()
        if isinstance(fingerprint, dict):
            user_agent = str(fingerprint.get("user_agent") or "").strip()
            if user_agent:
                return user_agent
        return self._generate_user_agent(account_id)

    @staticmethod
    def _should_attach_runtime_session_cookies(url: str) -> bool:
        host = str(urlparse(str(url or "")).hostname or "").lower()
        if not host:
            return False
        return any(
            host == candidate or host.endswith(f".{candidate}")
            for candidate in (
                "google.com",
                "googleapis.com",
                "labs.google",
                "recaptcha.net",
            )
        )

    @staticmethod
    def _merge_cookie_header(
        existing_cookie_header: Optional[str],
        extra_cookies: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        cookie_items: Dict[str, str] = {}
        raw_existing = str(existing_cookie_header or "").strip()
        if raw_existing:
            for part in raw_existing.split(";"):
                item = str(part or "").strip()
                if not item or "=" not in item:
                    continue
                key, value = item.split("=", 1)
                key = str(key or "").strip()
                value = str(value or "").strip()
                if key:
                    cookie_items[key] = value

        if isinstance(extra_cookies, dict):
            for key, value in extra_cookies.items():
                normalized_key = str(key or "").strip()
                normalized_value = str(value or "").strip()
                if normalized_key and normalized_value and normalized_key not in cookie_items:
                    cookie_items[normalized_key] = normalized_value

        if not cookie_items:
            return raw_existing or None
        return "; ".join(f"{key}={value}" for key, value in cookie_items.items())

    def _build_flow_project_page_url(self, project_id: str) -> str:
        return f"https://labs.google/fx/tools/flow/project/{project_id}"

    def _build_current_flow_media_headers(
        self,
        *,
        content_type: str = "application/json",
    ) -> Dict[str, str]:
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": self._get_primary_accept_language(fallback="en-US,en;q=0.9"),
            "Content-Type": content_type,
            "Origin": "https://labs.google",
            "Priority": "u=1, i",
            "Referer": "https://labs.google/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
            "sec-fetch-storage-access": "active",
        }
        headers.setdefault("x-browser-channel", self.FLOW_BROWSER_CHANNEL_HEADER)
        headers.setdefault("x-browser-copyright", self.FLOW_BROWSER_COPYRIGHT_HEADER)
        headers.setdefault("x-browser-validation", self.FLOW_BROWSER_VALIDATION_HEADER)
        headers.setdefault("x-browser-year", self.FLOW_BROWSER_YEAR_HEADER)
        return headers

    def _build_labs_request_context_headers(self, project_id: Optional[str]) -> Dict[str, str]:
        return self._build_current_flow_media_headers()

    def _compact_json_dumps(self, payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _encode_trpc_input(self, payload: Dict[str, Any]) -> str:
        return quote(self._compact_json_dumps(payload), safe="")

    @staticmethod
    def _extract_project_id_from_request_payload(payload: Optional[Dict[str, Any]]) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        client_context = payload.get("clientContext")
        if isinstance(client_context, dict):
            project_id = str(client_context.get("projectId") or "").strip()
            if project_id:
                return project_id
        requests = payload.get("requests")
        if isinstance(requests, list):
            for item in requests:
                if not isinstance(item, dict):
                    continue
                item_client_context = item.get("clientContext")
                if not isinstance(item_client_context, dict):
                    continue
                project_id = str(item_client_context.get("projectId") or "").strip()
                if project_id:
                    return project_id
        return None

    async def _get_token_st_by_id(self, token_id: Optional[int]) -> Optional[str]:
        if not token_id or self.db is None or not hasattr(self.db, "get_token"):
            return None
        try:
            token = await self.db.get_token(int(token_id))
            st_value = str(getattr(token, "st", "") or "").strip() if token else ""
            return st_value or None
        except Exception as e:
            debug_logger.log_warning(f"[VIDEO WARMUP] Failed to read ST for Token-{token_id}: {e}")
            return None

    async def _make_request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        raw_body: Optional[Union[str, bytes]] = None,
        use_st: bool = False,
        st_token: Optional[str] = None,
        use_at: bool = False,
        at_token: Optional[str] = None,
        timeout: Optional[int] = None,
        use_media_proxy: bool = False,
        respect_fingerprint_proxy: bool = True,
        force_no_proxy: bool = False,
        allow_urllib_fallback: bool = True,
        apply_default_client_headers: bool = True,
        impersonate: str = "chrome124",
    ) -> Dict[str, Any]:
        """Shared HTTP request handler"""
        fingerprint = self._request_fingerprint_ctx.get()

        proxy_url = None
        if not force_no_proxy:
            if self.proxy_manager:
                if use_media_proxy and hasattr(self.proxy_manager, "get_media_proxy_url"):
                    proxy_url = await self.proxy_manager.get_media_proxy_url()
                elif hasattr(self.proxy_manager, "get_request_proxy_url"):
                    proxy_url = await self.proxy_manager.get_request_proxy_url()
                else:
                    proxy_url = await self.proxy_manager.get_proxy_url()

            if respect_fingerprint_proxy and isinstance(fingerprint, dict) and "proxy_url" in fingerprint:
                proxy_url = fingerprint.get("proxy_url")
                if proxy_url == "":
                    proxy_url = None
        request_timeout = timeout or self.timeout

        if headers is None:
            headers = {}
        else:
            headers = dict(headers)

        if use_st and st_token:
            headers["Cookie"] = f"__Secure-next-auth.session-token={st_token}"

        if use_at and at_token:
            headers["authorization"] = f"Bearer {at_token}"

        account_id = None
        if st_token:
            account_id = st_token[:16]
        elif at_token:
            account_id = at_token[:16]

        fingerprint_user_agent = None
        if isinstance(fingerprint, dict):
            _fp_log = dict(fingerprint)
            if "proxy_url" in _fp_log:
                _fp_log["proxy_url"] = mask_proxy_url(_fp_log.get("proxy_url"))
            debug_logger.log_info(f"[FINGERPRINT] Browser fingerprint bound to this request: {_fp_log}")
            fingerprint_user_agent = fingerprint.get("user_agent")

        # UA comes from the request-chain fingerprint (real browser); generated UA is the
        # never-None fallback. Accept-Language falls back to en-US — every worker browser
        # in this fleet is English; a zh-CN default would contradict the minted fingerprint.
        effective_user_agent = str(fingerprint_user_agent or self._generate_user_agent(account_id)).strip()
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("User-Agent", effective_user_agent)
        headers.setdefault("Accept-Language", self._get_primary_accept_language(fallback="en-US,en;q=0.9"))

        if isinstance(fingerprint, dict):
            if fingerprint.get("accept_language"):
                headers.setdefault("Accept-Language", fingerprint["accept_language"])
            if fingerprint.get("sec_ch_ua"):
                headers["sec-ch-ua"] = self._normalize_sec_ch_ua_header(
                    fingerprint["sec_ch_ua"],
                    user_agent=headers.get("User-Agent"),
                )
            if fingerprint.get("sec_ch_ua_mobile"):
                headers["sec-ch-ua-mobile"] = fingerprint["sec_ch_ua_mobile"]
            if fingerprint.get("sec_ch_ua_platform"):
                headers["sec-ch-ua-platform"] = fingerprint["sec_ch_ua_platform"]

        if apply_default_client_headers:
            for key, value in self._default_client_headers.items():
                headers.setdefault(key, value)

        inferred_fingerprint = self._build_fingerprint_from_user_agent(
            headers.get("User-Agent"),
            accept_language=headers.get("Accept-Language"),
            proxy_url=proxy_url,
        )
        if not headers.get("sec-ch-ua") and inferred_fingerprint.get("sec_ch_ua"):
            headers["sec-ch-ua"] = inferred_fingerprint["sec_ch_ua"]
        if not headers.get("sec-ch-ua-platform") and inferred_fingerprint.get("sec_ch_ua_platform"):
            headers["sec-ch-ua-platform"] = inferred_fingerprint["sec_ch_ua_platform"]
        if not headers.get("sec-ch-ua-mobile") and inferred_fingerprint.get("sec_ch_ua_mobile"):
            headers["sec-ch-ua-mobile"] = inferred_fingerprint["sec_ch_ua_mobile"]

        if isinstance(fingerprint, dict) and self._should_attach_runtime_session_cookies(url):
            origin = str(fingerprint.get("origin") or "").strip() or "https://labs.google"
            referer = str(fingerprint.get("referer") or "").strip()
            if not referer:
                fingerprint_project_id = str(fingerprint.get("project_id") or "").strip()
                if fingerprint_project_id:
                    referer = self._build_flow_project_page_url(fingerprint_project_id)
            if origin:
                headers.setdefault("Origin", origin)
            if referer:
                headers.setdefault("Referer", referer)
            merged_cookie_header = self._merge_cookie_header(
                headers.get("Cookie"),
                fingerprint.get("session_cookies"),
            )
            if merged_cookie_header:
                headers["Cookie"] = merged_cookie_header

        if self._should_attach_runtime_session_cookies(url):
            derived_project_id = self._extract_project_id_from_request_payload(json_data)
            headers.setdefault("Origin", "https://labs.google")
            if derived_project_id:
                headers.setdefault("Referer", self._build_flow_project_page_url(derived_project_id))

        request_body_for_log = raw_body if raw_body is not None else json_data
        if config.debug_enabled:
            if isinstance(fingerprint, dict):
                debug_logger.log_info(
                    f"[FINGERPRINT] Submitting with captcha browser fingerprint: UA={headers.get('User-Agent', '')[:120]}, proxy={mask_proxy_url(proxy_url)}"
                )
            debug_logger.log_request(
                method=method,
                url=url,
                headers=headers,
                body=request_body_for_log,
                proxy=mask_proxy_url(proxy_url)
            )

        start_time = time.time()

        try:
            async with AsyncSession(trust_env=False) as session:
                if method.upper() == "GET":
                    response = await session.get(
                        url,
                        headers=headers,
                        proxy=proxy_url,
                        timeout=request_timeout,
                        impersonate=impersonate,
                    )
                else:
                    request_kwargs = {
                        "headers": headers,
                        "proxy": proxy_url,
                        "timeout": request_timeout,
                        "impersonate": impersonate,
                    }
                    if raw_body is not None:
                        request_kwargs["data"] = raw_body
                    else:
                        request_kwargs["json"] = json_data
                    response = await session.post(url, **request_kwargs)

                duration_ms = (time.time() - start_time) * 1000

                if config.debug_enabled:
                    debug_logger.log_response(
                        status_code=response.status_code,
                        headers=dict(response.headers),
                        body=response.text,
                        duration_ms=duration_ms
                    )

                if response.status_code >= 400:
                    error_reason = f"HTTP Error {response.status_code}"
                    parsed_reason = ""
                    try:
                        error_body = response.json()
                        if "error" in error_body:
                            error_info = error_body["error"]
                            error_message = error_info.get("message", "")
                            details = error_info.get("details", [])
                            for detail in details:
                                if detail.get("reason"):
                                    parsed_reason = detail.get("reason")
                                    error_reason = parsed_reason
                                    break
                            if error_message:
                                error_reason = f"{error_reason}: {error_message}"
                    except Exception:
                        error_reason = f"HTTP Error {response.status_code}: {response.text[:200]}"

                    debug_logger.log_error(f"[API FAILED] URL: {url}")
                    debug_logger.log_error(f"[API FAILED] Request Body: {request_body_for_log}")
                    debug_logger.log_error(f"[API FAILED] Response: {response.text}")

                    # Always-on operational signal: classify anti-bot failures and
                    # expose the egress used, so reCAPTCHA/quota/auth failures are
                    # visible in `docker logs` WITHOUT the gated firehose. The redeem
                    # IP printed here is what to compare against the mint IP.
                    _cls = self._classify_flow_fail(response.status_code, f"{error_reason} {response.text[:300]}")
                    debug_logger.op_warning(
                        f"[FLOW_FAIL] class={_cls} http={response.status_code} "
                        f"egress={mask_proxy_url(proxy_url)} reason={error_reason[:160]}"
                    )

                    api_error = FlowAPIError(response.status_code, error_reason, parsed_reason)
                    api_error.flow_fail_class = _cls
                    raise api_error

                return response.json()

        except FlowAPIError:
            # Structured HTTP error (4xx/5xx). Not a transport failure, so never
            # retry via urllib and never re-wrap — preserve status_code/reason
            # so callers can classify (e.g. 401/UNAUTHENTICATED => ST expired).
            raise
        except Exception as e:
            error_msg = str(e)
            if "HTTP Error" not in error_msg and not any(x in error_msg for x in ["PUBLIC_ERROR", "INVALID_ARGUMENT"]):
                debug_logger.log_error(f"[API FAILED] URL: {url}")
                debug_logger.log_error(f"[API FAILED] Request Body: {request_body_for_log}")
                debug_logger.log_error(f"[API FAILED] Exception: {error_msg}")

            if allow_urllib_fallback and self._should_fallback_to_urllib(error_msg):
                debug_logger.log_warning(
                    f"[HTTP FALLBACK] curl_cffi request failed, falling back to urllib: {method.upper()} {url}"
                )
                try:
                    return await asyncio.to_thread(
                        self._sync_json_request_via_urllib,
                        method.upper(),
                        url,
                        headers,
                        json_data,
                        proxy_url,
                        request_timeout,
                    )
                except FlowAPIError:
                    # urllib reached the server and got an HTTP error — preserve
                    # the structured status/reason instead of masking it as a
                    # generic transport failure.
                    raise
                except Exception as fallback_error:
                    debug_logger.log_error(
                        f"[HTTP FALLBACK] urllib fallback also failed: {fallback_error}"
                    )
                    raise Exception(
                        f"Flow API request failed: curl={error_msg}; urllib={fallback_error}"
                    )

            wrapped = Exception(f"Flow API request failed: {error_msg}")
            inherited = getattr(e, "flow_fail_class", None)
            if inherited is None and ("HTTP Error" in error_msg or "PUBLIC_ERROR" in error_msg):
                inherited = self._classify_flow_fail(None, error_msg)  # urllib fallback path: same classifier
            wrapped.flow_fail_class = inherited
            raise wrapped

    async def _make_text_request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        raw_body: Optional[Union[str, bytes]] = None,
        use_st: bool = False,
        st_token: Optional[str] = None,
        use_at: bool = False,
        at_token: Optional[str] = None,
        timeout: Optional[int] = None,
        respect_fingerprint_proxy: bool = True,
        force_no_proxy: bool = False,
        apply_default_client_headers: bool = True,
        impersonate: str = "chrome124",
    ) -> str:
        """Run a raw text request (e.g. SSE) and return the response text."""
        fingerprint = self._request_fingerprint_ctx.get()

        proxy_url = None
        if not force_no_proxy:
            if self.proxy_manager:
                if hasattr(self.proxy_manager, "get_request_proxy_url"):
                    proxy_url = await self.proxy_manager.get_request_proxy_url()
                else:
                    proxy_url = await self.proxy_manager.get_proxy_url()

            if respect_fingerprint_proxy and isinstance(fingerprint, dict) and "proxy_url" in fingerprint:
                proxy_url = fingerprint.get("proxy_url")
                if proxy_url == "":
                    proxy_url = None

        request_timeout = timeout or self.timeout

        if headers is None:
            headers = {}
        else:
            headers = dict(headers)

        if use_st and st_token:
            headers["Cookie"] = f"__Secure-next-auth.session-token={st_token}"

        if use_at and at_token:
            headers["authorization"] = f"Bearer {at_token}"

        account_id = None
        if st_token:
            account_id = st_token[:16]
        elif at_token:
            account_id = at_token[:16]

        fingerprint_user_agent = None
        if isinstance(fingerprint, dict):
            fingerprint_user_agent = fingerprint.get("user_agent")

        effective_user_agent = str(fingerprint_user_agent or self._generate_user_agent(account_id)).strip()
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("User-Agent", effective_user_agent)
        headers.setdefault("Accept-Language", self._get_primary_accept_language(fallback="en-US,en;q=0.9"))

        if isinstance(fingerprint, dict):
            if fingerprint.get("accept_language"):
                headers.setdefault("Accept-Language", fingerprint["accept_language"])
            if fingerprint.get("sec_ch_ua"):
                headers["sec-ch-ua"] = self._normalize_sec_ch_ua_header(
                    fingerprint["sec_ch_ua"],
                    user_agent=headers.get("User-Agent"),
                )
            if fingerprint.get("sec_ch_ua_mobile"):
                headers["sec-ch-ua-mobile"] = fingerprint["sec_ch_ua_mobile"]
            if fingerprint.get("sec_ch_ua_platform"):
                headers["sec-ch-ua-platform"] = fingerprint["sec_ch_ua_platform"]
            if self._should_attach_runtime_session_cookies(url):
                origin = str(fingerprint.get("origin") or "").strip() or "https://labs.google"
                referer = str(fingerprint.get("referer") or "").strip()
                if not referer:
                    fingerprint_project_id = str(fingerprint.get("project_id") or "").strip()
                    if fingerprint_project_id:
                        referer = self._build_flow_project_page_url(fingerprint_project_id)
                if origin:
                    headers.setdefault("Origin", origin)
                if referer:
                    headers.setdefault("Referer", referer)
            if self._should_attach_runtime_session_cookies(url):
                merged_cookie_header = self._merge_cookie_header(
                    headers.get("Cookie"),
                    fingerprint.get("session_cookies"),
                )
                if merged_cookie_header:
                    headers["Cookie"] = merged_cookie_header

        if apply_default_client_headers:
            for key, value in self._default_client_headers.items():
                headers.setdefault(key, value)

        if self._should_attach_runtime_session_cookies(url):
            derived_project_id = self._extract_project_id_from_request_payload(json_data)
            headers.setdefault("Origin", "https://labs.google")
            if derived_project_id:
                headers.setdefault("Referer", self._build_flow_project_page_url(derived_project_id))

        inferred_fingerprint = self._build_fingerprint_from_user_agent(
            headers.get("User-Agent"),
            accept_language=headers.get("Accept-Language"),
            proxy_url=proxy_url,
        )
        if not headers.get("sec-ch-ua") and inferred_fingerprint.get("sec_ch_ua"):
            headers["sec-ch-ua"] = inferred_fingerprint["sec_ch_ua"]
        if not headers.get("sec-ch-ua-platform") and inferred_fingerprint.get("sec_ch_ua_platform"):
            headers["sec-ch-ua-platform"] = inferred_fingerprint["sec_ch_ua_platform"]
        if not headers.get("sec-ch-ua-mobile") and inferred_fingerprint.get("sec_ch_ua_mobile"):
            headers["sec-ch-ua-mobile"] = inferred_fingerprint["sec_ch_ua_mobile"]

        request_body_for_log = raw_body if raw_body is not None else json_data
        if config.debug_enabled:
            if isinstance(fingerprint, dict):
                proxy_for_log = proxy_url if proxy_url else "direct"
                debug_logger.log_info(
                    f"[FINGERPRINT] Submitting text request with captcha browser fingerprint: UA={headers.get('User-Agent', '')[:120]}, proxy={proxy_for_log}"
                )
            debug_logger.log_request(
                method=method,
                url=url,
                headers=headers,
                body=request_body_for_log,
                proxy=proxy_url
            )

        start_time = time.time()

        try:
            async with AsyncSession(trust_env=False) as session:
                if method.upper() == "GET":
                    response = await session.get(
                        url,
                        headers=headers,
                        proxy=proxy_url,
                        timeout=request_timeout,
                        impersonate=impersonate,
                    )
                else:
                    request_kwargs = {
                        "headers": headers,
                        "proxy": proxy_url,
                        "timeout": request_timeout,
                        "impersonate": impersonate,
                    }
                    if raw_body is not None:
                        request_kwargs["data"] = raw_body
                    else:
                        request_kwargs["json"] = json_data
                    response = await session.post(url, **request_kwargs)

                duration_ms = (time.time() - start_time) * 1000
                response_text = response.text

                if config.debug_enabled:
                    debug_logger.log_response(
                        status_code=response.status_code,
                        headers=dict(response.headers),
                        body=response_text,
                        duration_ms=duration_ms
                    )

                if response.status_code >= 400:
                    error_reason = f"HTTP Error {response.status_code}: {response_text[:500]}"
                    try:
                        error_body = response.json()
                        if "error" in error_body:
                            error_info = error_body["error"]
                            error_message = error_info.get("message", "")
                            details = error_info.get("details", [])
                            for detail in details:
                                if detail.get("reason"):
                                    error_reason = detail.get("reason")
                                    break
                            if error_message:
                                error_reason = f"{error_reason}: {error_message}"
                    except Exception:
                        pass

                    debug_logger.log_error(f"[API FAILED] URL: {url}")
                    debug_logger.log_error(f"[API FAILED] Request Body: {request_body_for_log}")
                    debug_logger.log_error(f"[API FAILED] Response: {response_text}")
                    text_error = Exception(error_reason)
                    text_error.flow_fail_class = self._classify_flow_fail(response.status_code, f"{error_reason} {response_text[:300]}")
                    raise text_error

                return response_text

        except Exception as e:
            error_msg = str(e)
            if "HTTP Error" not in error_msg and not any(x in error_msg for x in ["PUBLIC_ERROR", "INVALID_ARGUMENT"]):
                debug_logger.log_error(f"[API FAILED] URL: {url}")
                debug_logger.log_error(f"[API FAILED] Request Body: {request_body_for_log}")
                debug_logger.log_error(f"[API FAILED] Exception: {error_msg}")
            wrapped = Exception(f"Flow API text request failed: {error_msg}")
            wrapped.flow_fail_class = getattr(e, "flow_fail_class", None)
            raise wrapped

    def _should_fallback_to_urllib(self, error_message: str) -> bool:
        """Decide whether to fall back from curl_cffi to urllib."""
        error_lower = (error_message or "").lower()
        return any(
            keyword in error_lower
            for keyword in [
                "curl: (6)",
                "curl: (7)",
                "curl: (28)",
                "curl: (35)",
                "curl: (52)",
                "curl: (56)",
                "connection timed out",
                "could not connect",
                "failed to connect",
                "ssl connect error",
                "tls connect error",
                "network is unreachable",
            ]
        )

    def _sync_json_request_via_urllib(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, Any]],
        json_data: Optional[Dict[str, Any]],
        proxy_url: Optional[str],
        timeout: int,
    ) -> Dict[str, Any]:
        """Run a JSON request with urllib as the network fallback for curl_cffi."""
        request_headers = dict(headers or {})
        request_headers.setdefault("Accept", "application/json")
        request_headers["Accept-Encoding"] = "identity"

        data = None
        if method.upper() != "GET" and json_data is not None:
            data = json.dumps(json_data, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"

        handlers = [urllib.request.HTTPSHandler(context=ssl.create_default_context())]
        if proxy_url:
            handlers.append(
                urllib.request.ProxyHandler(
                    {"http": proxy_url, "https": proxy_url}
                )
            )

        opener = urllib.request.build_opener(*handlers)
        request = urllib.request.Request(
            url=url,
            data=data,
            headers=request_headers,
            method=method.upper(),
        )

        try:
            with opener.open(
                request,
                timeout=timeout,
            ) as response:
                payload = response.read()
                status_code = int(response.getcode() or 0)
                content_encoding = str(response.headers.get("Content-Encoding") or "").lower()
        except urllib.error.HTTPError as exc:
            payload = exc.read() if hasattr(exc, "read") else b""
            status_code = int(getattr(exc, "code", 500) or 500)
            content_encoding = str(getattr(exc, "headers", {}).get("Content-Encoding") or "").lower()
            if content_encoding == "gzip" and payload:
                try:
                    payload = gzip.decompress(payload)
                except Exception:
                    pass
            body_text = payload.decode("utf-8", errors="replace")
            raise FlowAPIError(status_code, f"HTTP Error {status_code}: {body_text[:200]}") from exc
        except Exception as exc:
            raise Exception(str(exc)) from exc

        if content_encoding == "gzip" and payload:
            try:
                payload = gzip.decompress(payload)
            except Exception:
                pass
        body_text = payload.decode("utf-8", errors="replace")
        if status_code >= 400:
            raise FlowAPIError(status_code, f"HTTP Error {status_code}: {body_text[:200]}")

        try:
            return json.loads(body_text) if body_text else {}
        except Exception as exc:
            raise Exception(f"Invalid JSON response: {body_text[:200]}") from exc

    def _is_timeout_error(self, error: Exception) -> bool:
        """Detect a network timeout so we can fail fast and retry."""
        error_lower = str(error).lower()
        return any(keyword in error_lower for keyword in [
            "timed out",
            "timeout",
            "curl: (28)",
            "connection timed out",
            "operation timed out",
        ])

    def _is_proxy_connection_error(self, error: Exception) -> bool:
        """Detect connection failures caused by an unavailable local/upstream proxy."""
        error_lower = str(error).lower()
        return any(keyword in error_lower for keyword in [
            "failed to connect to 127.0.0.1 port",
            "failed to connect to localhost port",
            "proxyerror",
            "proxy error",
            "failed to connect to proxy",
            "couldn't connect to server",
            "curl: (7)",
        ])

    def _is_retryable_network_error(self, error_str: str) -> bool:
        """Detect retryable TLS/connection network errors."""
        error_lower = (error_str or "").lower()
        return any(keyword in error_lower for keyword in [
            "curl: (35)",
            "curl: (52)",
            "curl: (56)",
            "ssl_error_syscall",
            "tls connect error",
            "ssl connect error",
            "connection reset",
            "connection aborted",
            "connection was reset",
            "connection timed out",
            "curl: (28)",
            "timed out",
            "timeout",
            "unexpected eof",
            "empty reply from server",
            "recv failure",
            "send failure",
            "connection refused",
            "network is unreachable",
            "remote host closed connection",
        ])

    def _get_control_plane_timeout(self) -> int:
        """Timeout for light control-plane requests, so auth/project calls do not hang."""
        return max(5, min(int(self.timeout or 0) or 120, 10))

    def _get_video_submit_timeout(self) -> int:
        """Video submit should return an operation fast; one hung call must not stall the whole request."""
        return max(30, min(int(self.timeout or 0) or 120, 75))

    def _get_video_poll_timeout(self) -> int:
        """Video status checks are light polls; the timeout should not run far past the next poll."""
        return max(10, min(int(self.timeout or 0) or 120, 45))

    def _resolve_generation_retry_budget(self, base_max_retries: int, error: Optional[Union[Exception, str]] = None) -> int:
        """Compute the total retries allowed for this generation request."""
        effective_max_retries = max(1, int(base_max_retries or 1))
        if error is None:
            return effective_max_retries

        error_str = str(error)
        error_lower = error_str.lower()
        if "recaptcha evaluation failed" in error_lower or "recaptcha 验证失败" in error_str:
            # Dedicated (smaller) budget — do NOT inherit the generic upstream retry
            # count. Each retry on a flagged account lowers its score further.
            return max(1, int(config.browser_captcha_generation_retries or 2))
        return effective_max_retries

    def _build_realistic_video_submit_headers(self) -> Dict[str, str]:
        """Build video submit headers matching captured upstream traffic."""
        return self._build_current_flow_media_headers(content_type="text/plain;charset=UTF-8")

    def _resolve_runtime_impersonate(self, fallback: str = "chrome124") -> str:
        resolved = self._resolve_impersonate_from_fingerprint(fallback=fallback)
        return resolved or fallback

    def _resolve_impersonate_from_fingerprint(self, fallback: str = "chrome124") -> str:
        """Pick the closest curl_cffi impersonate target for the bound browser fingerprint."""
        fingerprint = self.get_request_fingerprint()
        if not isinstance(fingerprint, dict):
            return fallback

        ua = str(fingerprint.get("user_agent") or "").strip()
        ua_lower = ua.lower()
        if not ua_lower:
            return fallback

        if "android" in ua_lower:
            return "chrome_android"
        if "edg/" in ua_lower or " edge/" in ua_lower:
            return "edge"
        if "safari/" in ua_lower and "chrome/" not in ua_lower and "chromium/" not in ua_lower:
            if "iphone" in ua_lower or "ipad" in ua_lower or "ios" in ua_lower:
                return "safari_ios"
            return "safari"

        if "chrome/" not in ua_lower and "chromium/" not in ua_lower:
            return fallback

        import re
        match = re.search(r"(?:chrome|chromium)/(\d+)", ua_lower)
        if not match:
            return "chrome"

        major = int(match.group(1))
        supported = [99, 100, 101, 104, 107, 110, 116, 119, 120, 123, 124]
        if major in supported:
            return f"chrome{major}"
        if major > max(supported):
            return "chrome"
        lower_or_equal = [v for v in supported if v <= major]
        if lower_or_equal:
            return f"chrome{max(lower_or_equal)}"
        return fallback

    async def _make_video_api_request(
        self,
        url: str,
        json_data: Dict[str, Any],
        at: str,
        timeout: int,
        project_id: Optional[str] = None,
        token_id: Optional[int] = None,
        action: str = "VIDEO_GENERATION",
    ) -> Dict[str, Any]:
        """Hard deadline for video API calls, so a rare curl_cffi hang does not stall the request."""
        raw_body = json.dumps(json_data, ensure_ascii=False, separators=(",", ":"))
        headers = self._build_realistic_video_submit_headers()
        headers.update(self._build_labs_request_context_headers(project_id))
        try:
            return await asyncio.wait_for(
                self._make_request(
                    method="POST",
                    url=url,
                    headers=headers,
                    json_data=json_data,
                    raw_body=raw_body,
                    use_at=True,
                    at_token=at,
                    timeout=timeout,
                    allow_urllib_fallback=False,
                    apply_default_client_headers=False,
                    impersonate=self._resolve_runtime_impersonate(),
                ),
                timeout=timeout + 5
            )
        except asyncio.TimeoutError as exc:
            raise Exception(f"Flow video API request timed out after {timeout}s") from exc

    async def _acquire_image_launch_gate(
        self,
        token_id: Optional[int],
        token_image_concurrency: Optional[int],
    ) -> tuple[bool, int, int]:
        """Image requests are not queued locally; go straight to token fetch and upstream submit."""
        return True, 0, 0

    async def _release_image_launch_gate(self, token_id: Optional[int]):
        """Kept for interface shape; there is no local dispatch state to release."""
        return

    async def _acquire_video_launch_gate(
        self,
        token_id: Optional[int],
        token_video_concurrency: Optional[int],
    ) -> tuple[bool, int, int]:
        """Video requests are not queued locally; go straight to token fetch and upstream submit."""
        return True, 0, 0

    async def _release_video_launch_gate(self, token_id: Optional[int]):
        """Kept for interface shape; there is no local dispatch state to release."""
        return

    async def _make_image_generation_request(
        self,
        url: str,
        json_data: Dict[str, Any],
        at: str,
        attempt_trace: Optional[Dict[str, Any]] = None,
        project_id: Optional[str] = None,
        token_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Image generation uses a shorter timeout and retries fast on network timeout."""
        request_timeout = config.flow_image_request_timeout
        total_attempts = max(1, config.flow_image_timeout_retry_count + 1)
        retry_delay = config.flow_image_timeout_retry_delay

        # For browser/remote-browser captcha, prefer the same egress used for the captcha.
        # Otherwise switching to the media proxy on the first try tends to fail reCAPTCHA and worsen tail latency.
        fingerprint = self._request_fingerprint_ctx.get()
        has_fingerprint_context = bool(isinstance(fingerprint, dict) and fingerprint)

        has_media_proxy = False
        if self.proxy_manager and config.flow_image_timeout_use_media_proxy_fallback:
            try:
                has_media_proxy = bool(await self.proxy_manager.get_media_proxy_url())
            except Exception:
                has_media_proxy = False
        prefer_media_first = bool(has_media_proxy and config.flow_image_prefer_media_proxy)

        if has_fingerprint_context and prefer_media_first:
            prefer_media_first = False
            debug_logger.log_info(
                "[IMAGE] Captcha browser fingerprint present: first try uses the captcha route; "
                "media proxy is only a fallback on network timeout."
            )

        last_error: Optional[Exception] = None

        for attempt_index in range(total_attempts):
            if has_media_proxy:
                # With two tries, use primary route then backup route, so we do not keep hitting the bad route first.
                if attempt_index == 0:
                    prefer_media_proxy = prefer_media_first
                elif attempt_index == 1:
                    prefer_media_proxy = not prefer_media_first
                else:
                    prefer_media_proxy = prefer_media_first
            else:
                prefer_media_proxy = False
            route_label = "media proxy route" if prefer_media_proxy else "captcha route"
            http_attempt_started_at = time.time()
            http_attempt_info: Optional[Dict[str, Any]] = None
            if isinstance(attempt_trace, dict):
                http_attempt_info = {
                    "attempt": attempt_index + 1,
                    "route": route_label,
                    "timeout_seconds": request_timeout,
                    "used_media_proxy": bool(prefer_media_proxy),
                }
            try:
                if config.captcha_method == "browser" and project_id:
                    from .browser_captcha import BrowserCaptchaService

                    service = await BrowserCaptchaService.get_instance(self.db)
                    response_payload, _browser_ref, fingerprint = await service.submit_flow_request(
                        project_id=project_id,
                        action="IMAGE_GENERATION",
                        token_id=token_id,
                        url=url,
                        at_token=at,
                        json_data=json_data,
                        timeout=request_timeout,
                    )
                    self._set_request_fingerprint(fingerprint if fingerprint else None)

                    status_code = int(response_payload.get("status") or 0)
                    response_text = response_payload.get("text") or ""
                    if status_code >= 400:
                        error_reason = f"HTTP Error {status_code}"
                        parsed_body = None
                        try:
                            parsed_body = json.loads(response_text) if response_text else None
                        except Exception:
                            parsed_body = None
                        if isinstance(parsed_body, dict) and "error" in parsed_body:
                            error_info = parsed_body["error"] or {}
                            error_message = error_info.get("message", "")
                            details = error_info.get("details", [])
                            for detail in details or []:
                                if isinstance(detail, dict) and detail.get("reason"):
                                    error_reason = detail.get("reason")
                                    break
                            if error_message:
                                error_reason = f"{error_reason}: {error_message}"
                        elif response_text:
                            error_reason = f"HTTP Error {status_code}: {response_text[:200]}"
                        raise Exception(error_reason)

                    result = json.loads(response_text) if response_text else {}
                else:
                    result = await self._make_request(
                        method="POST",
                        url=url,
                        headers=self._build_labs_request_context_headers(project_id),
                        json_data=json_data,
                        use_at=True,
                        at_token=at,
                        timeout=request_timeout,
                        use_media_proxy=prefer_media_proxy,
                        respect_fingerprint_proxy=not prefer_media_proxy,
                    )
                if http_attempt_info is not None:
                    http_attempt_info["duration_ms"] = int((time.time() - http_attempt_started_at) * 1000)
                    http_attempt_info["success"] = True
                    attempt_trace.setdefault("http_attempts", []).append(http_attempt_info)
                return result
            except Exception as e:
                last_error = e
                if http_attempt_info is not None:
                    http_attempt_info["duration_ms"] = int((time.time() - http_attempt_started_at) * 1000)
                    http_attempt_info["success"] = False
                    http_attempt_info["timeout_error"] = bool(self._is_timeout_error(e))
                    http_attempt_info["error"] = str(e)[:240]
                    attempt_trace.setdefault("http_attempts", []).append(http_attempt_info)
                if not self._is_timeout_error(e) or attempt_index >= total_attempts - 1:
                    raise

                if has_media_proxy and total_attempts > 1:
                    next_prefer_media_proxy = (
                        not prefer_media_proxy if attempt_index == 0 else prefer_media_proxy
                    )
                else:
                    next_prefer_media_proxy = prefer_media_proxy
                next_route_label = "media proxy route" if next_prefer_media_proxy else "captcha route"
                debug_logger.log_warning(
                    f"[IMAGE] Image generation request timed out, retrying fast "
                    f"({attempt_index + 2}/{total_attempts}), current route={route_label}, "
                    f"next route={next_route_label}, timeout={request_timeout}s"
                )
                if retry_delay > 0:
                    await asyncio.sleep(retry_delay)

        if last_error is not None:
            raise last_error
        raise RuntimeError("Image generation request failed")

    # ========== Auth (uses ST) ==========

    async def st_to_at(self, st: str) -> dict:
        """Convert ST to AT

        Args:
            st: Session Token

        Returns:
            {
                "access_token": "AT",
                "expires": "2025-11-15T04:46:04.000Z",
                "user": {...}
            }
        """
        url = f"{self.labs_base_url}/auth/session"
        try:
            return await self._make_request(
                method="GET",
                url=url,
                use_st=True,
                st_token=st,
                timeout=self._get_control_plane_timeout(),
            )
        except Exception as e:
            if not self._is_proxy_connection_error(e):
                raise

            debug_logger.log_warning(
                f"[AUTH] ST->AT failed via configured proxy, retrying direct connection: {e}"
            )
            return await self._make_request(
                method="GET",
                url=url,
                use_st=True,
                st_token=st,
                timeout=self._get_control_plane_timeout(),
                force_no_proxy=True,
            )

    # ========== Projects (uses ST) ==========

    async def create_project(self, st: str, title: str) -> str:
        """Create a project and return project_id

        Args:
            st: Session Token
            title: project title

        Returns:
            project_id (UUID)
        """
        url = f"{self.labs_base_url}/trpc/project.createProject"
        json_data = {
            "json": {
                "projectTitle": title,
                "toolName": "PINHOLE"
            }
        }
        max_retries = config.flow_max_retries
        request_timeout = max(self._get_control_plane_timeout(), min(self.timeout, 15))
        last_error: Optional[Exception] = None

        for retry_attempt in range(max_retries):
            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_st=True,
                    st_token=st,
                    timeout=request_timeout,
                )
                project_result = (
                    result.get("result", {})
                    .get("data", {})
                    .get("json", {})
                    .get("result", {})
                )
                project_id = project_result.get("projectId")
                if not project_id:
                    raise Exception("Invalid project.createProject response: missing projectId")
                return project_id
            except Exception as e:
                last_error = e
                retry_reason = "network timeout" if self._is_timeout_error(e) else self._get_retry_reason(str(e))
                if retry_reason and retry_attempt < max_retries - 1:
                    debug_logger.log_warning(
                        f"[PROJECT] Create project failed, retrying ({retry_attempt + 2}/{max_retries}) "
                        f"title={title!r}, reason={retry_reason}: {e}"
                    )
                    await asyncio.sleep(1)
                    continue
                raise

        if last_error is not None:
            raise last_error
        raise RuntimeError("Failed to create project")

    async def delete_project(self, st: str, project_id: str):
        """Delete a project

        Args:
            st: Session Token
            project_id: project ID
        """
        url = f"{self.labs_base_url}/trpc/project.deleteProject"
        json_data = {
            "json": {
                "projectToDeleteId": project_id
            }
        }

        await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_st=True,
            st_token=st,
            timeout=self._get_control_plane_timeout(),
        )

    # ========== Media fetch (uses AT) ==========

    async def get_media(self, at: str, media_name: str) -> dict:
        """Fetch media content (video comes back as base64)

        Google's batchCheckAsyncVideoGenerationStatus does not return a download URL
        after a video succeeds. Fetch the video via GET /v1/media/{name}.

        Args:
            at: Access Token
            media_name: media name (UUID)

        Returns:
            {
                "name": "uuid",
                "video": {
                    "encodedVideo": "base64...",
                    "seed": 74602,
                    "prompt": "...",
                    "model": "veo_3_1_t2v_fast",
                    "aspectRatio": "VIDEO_ASPECT_RATIO_LANDSCAPE"
                }
            }
        """
        url = f"{self.api_base_url}/media/{media_name}"
        return await self._make_request(
            method="GET",
            url=url,
            use_at=True,
            at_token=at,
            timeout=max(60, int(self.timeout or 120)),
        )

    # ========== Credits (uses AT) ==========

    async def get_credits(self, at: str) -> dict:
        """Get credit balance

        Args:
            at: Access Token

        Returns:
            {
                "credits": 920,
                "userPaygateTier": "PAYGATE_TIER_ONE"
            }
        """
        url = f"{self.api_base_url}/credits"
        result = await self._make_request(
            method="GET",
            url=url,
            use_at=True,
            at_token=at,
            timeout=self._get_control_plane_timeout(),
        )
        return result

    # ========== Image upload (uses AT) ==========

    def _detect_image_mime_type(self, image_bytes: bytes) -> str:
        """Detect image MIME type from magic bytes

        Args:
            image_bytes: image bytes

        Returns:
            MIME type string, default image/jpeg
        """
        if len(image_bytes) < 12:
            return "image/jpeg"

        # WebP: RIFF....WEBP
        if image_bytes[:4] == b'RIFF' and image_bytes[8:12] == b'WEBP':
            return "image/webp"
        # PNG: 89 50 4E 47
        if image_bytes[:4] == b'\x89PNG':
            return "image/png"
        # JPEG: FF D8 FF
        if image_bytes[:3] == b'\xff\xd8\xff':
            return "image/jpeg"
        # GIF: GIF87a or GIF89a
        if image_bytes[:6] in (b'GIF87a', b'GIF89a'):
            return "image/gif"
        # BMP: BM
        if image_bytes[:2] == b'BM':
            return "image/bmp"
        # JPEG 2000: 00 00 00 0C 6A 50
        if image_bytes[:6] == b'\x00\x00\x00\x0cjP':
            return "image/jp2"

        return "image/jpeg"

    def _convert_to_jpeg(self, image_bytes: bytes) -> bytes:
        """Convert an image to JPEG

        Args:
            image_bytes: original image bytes

        Returns:
            JPEG image bytes
        """
        from io import BytesIO
        from PIL import Image

        img = Image.open(BytesIO(image_bytes))
        # Convert to RGB if there is an alpha channel
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGB')
        
        output = BytesIO()
        img.save(output, format='JPEG', quality=95)
        return output.getvalue()

    async def upload_image(
        self,
        at: str,
        image_bytes: bytes,
        aspect_ratio: str = "IMAGE_ASPECT_RATIO_LANDSCAPE",
        project_id: Optional[str] = None
    ) -> str:
        """Upload an image and return mediaId

        Args:
            at: Access Token
            image_bytes: image bytes
            aspect_ratio: image or video aspect ratio (converted to image format)
            project_id: project ID (used by the new upload endpoint)

        Returns:
            mediaId
        """
        # Convert video aspect_ratio to image aspect_ratio
        # VIDEO_ASPECT_RATIO_LANDSCAPE -> IMAGE_ASPECT_RATIO_LANDSCAPE
        # VIDEO_ASPECT_RATIO_PORTRAIT -> IMAGE_ASPECT_RATIO_PORTRAIT
        if aspect_ratio.startswith("VIDEO_"):
            aspect_ratio = aspect_ratio.replace("VIDEO_", "IMAGE_")

        # Detect image MIME type
        mime_type = self._detect_image_mime_type(image_bytes)

        # Encode as base64 (no prefix)
        image_base64 = base64.b64encode(image_bytes).decode('utf-8')

        # Try the new upload endpoint first: /v1/flow/uploadImage
        # On failure fall back to the old endpoint for compatibility
        ext = "png" if "png" in mime_type else "jpg"
        upload_file_name = f"flow2api_upload_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.{ext}"
        new_url = f"{self.api_base_url}/flow/uploadImage"
        normalized_project_id = str(project_id or "").strip()
        new_client_context = {
            "sessionId": self._generate_session_id(),
            "tool": "PINHOLE"
        }
        if normalized_project_id:
            new_client_context["projectId"] = normalized_project_id

        new_json_data = {
            "clientContext": new_client_context,
            "fileName": upload_file_name,
            "imageBytes": image_base64,
            "isHidden": False,
            "isUserUploaded": True,
            "mimeType": mime_type
        }

        # Compatibility fallback: old endpoint :uploadUserImage
        legacy_url = f"{self.api_base_url}:uploadUserImage"
        legacy_json_data = {
            "imageInput": {
                "rawImageBytes": image_base64,
                "mimeType": mime_type,
                "isUserUploaded": True,
                "aspectRatio": aspect_ratio
            },
            "clientContext": {
                "sessionId": self._generate_session_id(),
                "tool": "ASSET_MANAGER"
            }
        }
        max_retries = config.flow_max_retries
        last_error: Optional[Exception] = None

        captcha_method = getattr(config, "captcha_method", "personal")
        if captcha_method == "personal":
            try:
                from .browser_captcha_personal import BrowserCaptchaService
                service = await BrowserCaptchaService.get_instance(self.db)
                fingerprint = service.get_last_fingerprint()
                if not fingerprint:
                    await service.get_token(project_id, "uploadUserImage")
                    fingerprint = service.get_last_fingerprint()
                self._set_request_fingerprint(fingerprint)
            except Exception as e:
                debug_logger.log_error(f"[UPLOAD] Failed to pre-fetch fingerprint: {e}")

        for retry_attempt in range(max_retries):
            try:
                new_result = await self._make_request(
                    method="POST",
                    url=new_url,
                    json_data=new_json_data,
                    use_at=True,
                    at_token=at,
                    use_media_proxy=True
                )
                media_id = (
                    self._extract_media_name(new_result.get("media"))
                    or new_result.get("mediaGenerationId", {}).get("mediaGenerationId")
                )
                if media_id:
                    return media_id
                raise Exception(f"Invalid upload response: missing media id, keys={list(new_result.keys())}")
            except Exception as new_upload_error:
                last_error = new_upload_error
                retry_reason = "network timeout" if self._is_timeout_error(new_upload_error) else self._get_retry_reason(str(new_upload_error))

                # The old endpoint has no projectId, so a project-scoped upload that falls back may land in the wrong project.
                if normalized_project_id:
                    if retry_reason and retry_attempt < max_retries - 1:
                        debug_logger.log_warning(
                            f"[UPLOAD] Project-scoped upload hit {retry_reason}, retrying new endpoint "
                            f"({retry_attempt + 2}/{max_retries}, project_id={normalized_project_id})..."
                        )
                        await asyncio.sleep(1)
                        continue
                    raise RuntimeError(
                        "Project-scoped image upload failed via /flow/uploadImage; "
                        "legacy :uploadUserImage fallback is disabled because it may attach media "
                        f"to a different project (project_id={normalized_project_id})."
                    ) from new_upload_error

                debug_logger.log_warning(
                    f"[UPLOAD] New upload API failed, fallback to legacy endpoint: {new_upload_error}"
                )

            try:
                legacy_result = await self._make_request(
                    method="POST",
                    url=legacy_url,
                    json_data=legacy_json_data,
                    use_at=True,
                    at_token=at,
                    use_media_proxy=True
                )

                media_id = (
                    legacy_result.get("mediaGenerationId", {}).get("mediaGenerationId")
                    or legacy_result.get("media", {}).get("name")
                )
                if media_id:
                    return media_id
                raise Exception(f"Legacy upload response missing media id: keys={list(legacy_result.keys())}")
            except Exception as legacy_upload_error:
                last_error = legacy_upload_error
                retry_reason = self._get_retry_reason(str(legacy_upload_error))
                if retry_reason and retry_attempt < max_retries - 1:
                    debug_logger.log_warning(
                        f"[UPLOAD] Upload hit {retry_reason}, retrying ({retry_attempt + 2}/{max_retries})..."
                    )
                    await asyncio.sleep(1)
                    continue
                raise

        if last_error is not None:
            raise last_error
        raise RuntimeError("Failed to upload image")

    # ========== Image generation (uses AT) - synchronous ==========

    async def generate_image(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_name: str,
        aspect_ratio: str,
        image_inputs: Optional[List[Dict]] = None,
        token_id: Optional[int] = None,
        token_image_concurrency: Optional[int] = None,
        progress_callback: Optional[Callable[[str, int], Awaitable[None]]] = None,
        prompt_parts: Optional[List[Dict[str, Any]]] = None,
        reference_entities: Optional[List[str]] = None,
    ) -> tuple[dict, str, Dict[str, Any]]:
        """Generate images (synchronous)

        Args:
            at: Access Token
            project_id: project ID
            prompt: prompt text
            model_name: NARWHAL / GEM_PIX / GEM_PIX_2 / IMAGEN_3_5
            aspect_ratio: image aspect ratio
            image_inputs: reference images (for image-to-image)

        Returns:
            (result, session_id, perf_trace)
            result: generation result from upstream
            session_id: sessionId used by the successful image request
            perf_trace: retry and timing trace
        """
        url = f"{self.api_base_url}/projects/{project_id}/flowMedia:batchGenerateImages"

        # 403/reCAPTCHA retry logic
        max_retries = config.flow_max_retries
        last_error = None
        perf_trace: Dict[str, Any] = {
            "max_retries": max_retries,
            "generation_attempts": [],
        }
        
        for retry_attempt in range(max_retries):
            attempt_trace: Dict[str, Any] = {
                "attempt": retry_attempt + 1,
                "recaptcha_ok": False,
            }
            attempt_started_at = time.time()
            # Get a fresh reCAPTCHA token on every retry
            recaptcha_started_at = time.time()
            if progress_callback is not None:
                await progress_callback("solving_image_captcha", 38)
            launch_gate_acquired = False
            launch_ok, launch_queue_ms, launch_stagger_ms = await self._acquire_image_launch_gate(
                token_id=token_id,
                token_image_concurrency=token_image_concurrency,
            )
            attempt_trace["launch_queue_ms"] = launch_queue_ms
            attempt_trace["launch_stagger_ms"] = launch_stagger_ms
            if not launch_ok:
                last_error = Exception("Image launch queue wait timeout")
                attempt_trace["success"] = False
                attempt_trace["error"] = str(last_error)
                attempt_trace["duration_ms"] = int((time.time() - attempt_started_at) * 1000)
                perf_trace["generation_attempts"].append(attempt_trace)
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="IMAGE_GENERATION",
                    token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_image_launch_gate(token_id)
            attempt_trace["recaptcha_ms"] = int((time.time() - recaptcha_started_at) * 1000)
            attempt_trace["recaptcha_ok"] = bool(recaptcha_token)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                attempt_trace["success"] = False
                attempt_trace["error"] = str(last_error)
                attempt_trace["duration_ms"] = int((time.time() - attempt_started_at) * 1000)
                perf_trace["generation_attempts"].append(attempt_trace)
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[IMAGE] Generation ",
                )
                if should_retry:
                    continue
                raise last_error
            if progress_callback is not None:
                await progress_callback("submitting_image", 48)
            session_id = self._generate_session_id()

            # Build request - new endpoint sends clientContext both at top level and inside requests
            client_context = {
                "recaptchaContext": {
                    "token": recaptcha_token,
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                },
                "sessionId": session_id,
                "projectId": project_id,
                "tool": "PINHOLE"
            }

            # New image endpoint uses structured prompt + new media flag
            request_data = {
                "clientContext": client_context,
                "seed": random.randint(1, 999999),
                "imageModelName": model_name,
                "imageAspectRatio": aspect_ratio,
                "structuredPrompt": {
                    "parts": prompt_parts or [{
                        "text": prompt
                    }]
                },
                "imageInputs": image_inputs or []
            }
            if reference_entities:
                # Flow Characters: named entities the prompt refers to (@Name -> entity part above)
                request_data["referenceEntities"] = [{"entityId": eid} for eid in reference_entities]

            json_data = {
                "clientContext": client_context,
                "mediaGenerationContext": {
                    "batchId": str(uuid.uuid4())
                },
                "useNewMedia": True,
                "requests": [request_data]
            }

            try:
                result = await self._make_image_generation_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    attempt_trace=attempt_trace,
                    project_id=project_id,
                    token_id=token_id,
                )
                attempt_trace["success"] = True
                attempt_trace["duration_ms"] = int((time.time() - attempt_started_at) * 1000)
                perf_trace["generation_attempts"].append(attempt_trace)
                perf_trace["final_success_attempt"] = retry_attempt + 1
                return result, session_id, perf_trace
            except Exception as e:
                last_error = e
                attempt_trace["success"] = False
                attempt_trace["error"] = str(e)[:240]
                attempt_trace["duration_ms"] = int((time.time() - attempt_started_at) * 1000)
                perf_trace["generation_attempts"].append(attempt_trace)
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[IMAGE] Generation ",
                )
                if should_retry:
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
        
        # All retries failed
        perf_trace["final_success_attempt"] = None
        raise last_error

    async def upsample_image(
        self,
        at: str,
        project_id: str,
        media_id: str,
        target_resolution: str = "UPSAMPLE_IMAGE_RESOLUTION_4K",
        user_paygate_tier: str = "PAYGATE_TIER_NOT_PAID",
        session_id: Optional[str] = None,
        token_id: Optional[int] = None
    ) -> str:
        """Upscale an image to 2K/4K

        Args:
            at: Access Token
            project_id: project ID
            media_id: image mediaId (media[0]["name"] from batchGenerateImages)
            target_resolution: UPSAMPLE_IMAGE_RESOLUTION_2K or UPSAMPLE_IMAGE_RESOLUTION_4K
            user_paygate_tier: user tier (e.g. PAYGATE_TIER_NOT_PAID / PAYGATE_TIER_ONE)
            session_id: optional, reuse the sessionId from image generation

        Returns:
            base64-encoded image data
        """
        url = f"{self.api_base_url}/flow/upsampleImage"

        # 403/reCAPTCHA/500 retry logic - uses the configured max retries
        max_retries = config.flow_max_retries
        last_error = None

        for retry_attempt in range(max_retries):
            # Get reCAPTCHA token - uses IMAGE_GENERATION action
            recaptcha_token, browser_id = await self._get_recaptcha_token(
                project_id,
                action="IMAGE_GENERATION",
                token_id=token_id
            )
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[IMAGE UPSAMPLE] Upscale ",
                )
                if should_retry:
                    continue
                raise last_error
            upsample_session_id = session_id or self._generate_session_id()

            json_data = {
                "mediaId": media_id,
                "targetResolution": target_resolution,
                "clientContext": {
                    "recaptchaContext": {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                    },
                    "sessionId": upsample_session_id,
                    "projectId": project_id,
                    "tool": "PINHOLE",
                    "userPaygateTier": user_paygate_tier
                }
            }

            # 4K/2K upscale has its own timeout because the base64 response is large
            try:
                result = await self._make_request(
                    method="POST",
                    url=url,
                    json_data=json_data,
                    use_at=True,
                    at_token=at,
                    timeout=config.upsample_timeout
                )

                # Return the base64-encoded image
                return result.get("encodedImage", "")
            except Exception as e:
                last_error = e
                if classify_upsample_error(e) in UPSAMPLE_NO_RETRY_KINDS:
                    # Quota / tier / "too much traffic" / refusals: never retried here. The caller
                    # (generation_handler) owns that policy — before 26 Sep both layers retried,
                    # up to flow_max_retries² calls per request. Keep the server-mint switch.
                    await self._maybe_switch_to_server_mint(e, "[IMAGE UPSAMPLE] Upscale ")
                    raise
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[IMAGE UPSAMPLE] Upscale ",
                )
                if should_retry:
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)

        raise last_error

    # ========== Video generation (uses AT) - asynchronous ==========

    def _extract_media_name(self, media: Any) -> Optional[str]:
        """Extract the media id from a new-style media object or list."""
        if isinstance(media, list):
            for item in media:
                media_name = self._extract_media_name(item)
                if media_name:
                    return media_name
            return None
        if isinstance(media, dict):
            name = media.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        return None

    def _build_video_text_input(
        self, prompt: str, use_v2_model_config: bool = False, prompt_parts: Optional[List[Dict[str, Any]]] = None
    ) -> Dict[str, Any]:
        # Flow video now always uses structuredPrompt; the old prompt field is no longer supported.
        return {
            "structuredPrompt": {
                "parts": prompt_parts or [{
                    "text": prompt
                }]
            }
        }

    def _build_video_media_generation_context(self, batch_id: Optional[str] = None) -> Dict[str, Any]:
        return {
            "batchId": batch_id or str(uuid.uuid4()),
            "audioFailurePreference": "BLOCK_SILENCED_VIDEOS",
        }

    def _find_nested_string(self, value: Any, keys: tuple[str, ...]) -> Optional[str]:
        if isinstance(value, dict):
            for key in keys:
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
            for candidate in value.values():
                found = self._find_nested_string(candidate, keys)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = self._find_nested_string(item, keys)
                if found:
                    return found
        return None

    def _truncate_large_debug_value(self, data: Any, max_length: int = 240) -> Any:
        """Truncate very long fields in debug output to keep the console readable."""
        if isinstance(data, dict):
            return {
                key: self._truncate_large_debug_value(value, max_length=max_length)
                for key, value in data.items()
            }
        if isinstance(data, list):
            return [self._truncate_large_debug_value(item, max_length=max_length) for item in data]
        if isinstance(data, str) and len(data) > max_length:
            return f"{data[:max_length]}... (truncated, total {len(data)} chars)"
        return data

    def _extract_video_status_from_media(self, media: Dict[str, Any]) -> tuple[Optional[str], Dict[str, Any]]:
        status_block = (
            media.get("mediaMetadata", {}).get("mediaStatus", {})
            or media.get("mediaStatus", {})
            or {}
        )
        status = (
            status_block.get("mediaGenerationStatus")
            or status_block.get("status")
            or media.get("status")
        )
        return status, status_block if isinstance(status_block, dict) else {}

    def _extract_video_url_from_media(self, media: Dict[str, Any]) -> Optional[str]:
        video = media.get("video") if isinstance(media.get("video"), dict) else {}
        candidates = [
            self._find_nested_string(video, ("fifeUrl", "videoUrl", "outputUri", "downloadUri")),
            self._find_nested_string(media, ("fifeUrl", "videoUrl", "outputUri", "downloadUri")),
            self._find_nested_string(video, ("uri", "url")),
        ]
        for candidate in candidates:
            if candidate and (candidate.startswith("http://") or candidate.startswith("https://") or candidate.startswith("/")):
                return candidate
        return None

    def _extract_video_metadata_from_media(
        self,
        media: Dict[str, Any],
    ) -> Dict[str, Any]:
        video = media.get("video") if isinstance(media.get("video"), dict) else {}
        generated_video = video.get("generatedVideo") if isinstance(video.get("generatedVideo"), dict) else {}
        request_data = (
            media.get("mediaMetadata", {})
            .get("requestData", {})
            .get("videoGenerationRequestData", {})
            if isinstance(media.get("mediaMetadata"), dict)
            else {}
        )
        control_input = request_data.get("videoModelControlInput", {}) if isinstance(request_data, dict) else {}
        dimensions = video.get("dimensions") if isinstance(video.get("dimensions"), dict) else {}

        media_name = self._extract_media_name(media)
        aspect_ratio = (
            self._find_nested_string(generated_video, ("aspectRatio",))
            or self._find_nested_string(video, ("aspectRatio", "videoAspectRatio"))
            or self._find_nested_string(control_input, ("videoAspectRatio", "aspectRatio"))
            or self._find_nested_string(media.get("mediaMetadata", {}), ("videoAspectRatio", "aspectRatio"))
        )
        model_name = (
            self._find_nested_string(generated_video, ("model",))
            or self._find_nested_string(control_input, ("videoModelName", "videoModelKey"))
        )
        duration = (
            self._find_nested_string(dimensions, ("length", "duration"))
            or self._find_nested_string(generated_video, ("length", "duration"))
        )

        video_metadata: Dict[str, Any] = {}
        if media_name:
            video_metadata["mediaName"] = media_name
            video_metadata["mediaGenerationId"] = media_name
        if aspect_ratio:
            video_metadata["aspectRatio"] = aspect_ratio
        if model_name:
            video_metadata["model"] = model_name
        if duration:
            video_metadata["duration"] = duration

        embedded_url = self._extract_video_url_from_media(media)
        if embedded_url:
            video_metadata["embeddedUrl"] = embedded_url
            video_metadata["fifeUrl"] = embedded_url

        return video_metadata

    def _media_to_video_operation(
        self,
        media: Dict[str, Any],
        fallback_project_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(media, dict):
            return None

        media_name = self._extract_media_name(media)
        video = media.get("video") if isinstance(media.get("video"), dict) else {}
        video_operation = video.get("operation") if isinstance(video.get("operation"), dict) else {}
        operation_name = (
            video_operation.get("name")
            or self._find_nested_string(video_operation, ("name",))
            or media_name
        )
        if not operation_name:
            return None

        project_id = media.get("projectId") or fallback_project_id
        status, status_block = self._extract_video_status_from_media(media)
        operation: Dict[str, Any] = {
            "operation": {
                "name": operation_name,
            },
            "status": status or "MEDIA_GENERATION_STATUS_PENDING",
        }
        if media_name:
            operation["mediaName"] = media_name
        if project_id:
            operation["projectId"] = project_id

        scene_id = (
            media.get("sceneId")
            or media.get("workflowStepId")
            or video_operation.get("sceneId")
        )
        if scene_id:
            operation["sceneId"] = scene_id

        video_metadata = self._extract_video_metadata_from_media(media)
        if video_metadata:
            operation["operation"]["metadata"] = {"video": video_metadata}

        error = status_block.get("error") if isinstance(status_block, dict) else None
        if isinstance(error, dict):
            operation["operation"]["error"] = error

        return operation

    def _merge_video_operations_with_media(
        self,
        operations: List[Dict[str, Any]],
        media_operations: List[Dict[str, Any]],
        fallback_project_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        media_by_name: Dict[str, Dict[str, Any]] = {}
        for item in media_operations:
            media_name = item.get("mediaName") or (item.get("operation") or {}).get("name")
            if media_name:
                media_by_name[media_name] = item

        merged: List[Dict[str, Any]] = []
        for raw_operation in operations:
            operation = dict(raw_operation) if isinstance(raw_operation, dict) else {}
            operation_body = dict(operation.get("operation") or {})
            operation["operation"] = operation_body
            name = operation_body.get("name") or operation.get("mediaName")
            media_operation = media_by_name.get(name) if name else None
            if media_operation:
                operation.setdefault("mediaName", media_operation.get("mediaName"))
                operation.setdefault("projectId", media_operation.get("projectId"))
                operation.setdefault("status", media_operation.get("status"))
                operation.setdefault("sceneId", media_operation.get("sceneId"))
                if "metadata" not in operation_body and (media_operation.get("operation") or {}).get("metadata"):
                    operation_body["metadata"] = (media_operation.get("operation") or {}).get("metadata")
                if "error" not in operation_body and (media_operation.get("operation") or {}).get("error"):
                    operation_body["error"] = (media_operation.get("operation") or {}).get("error")
            elif fallback_project_id:
                operation.setdefault("projectId", fallback_project_id)
            merged.append(operation)

        return merged

    def _normalize_video_generation_response(
        self,
        result: Dict[str, Any],
        fallback_project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not isinstance(result, dict):
            return result

        normalized = dict(result)
        media_items = normalized.get("media")
        media_operations: List[Dict[str, Any]] = []
        if isinstance(media_items, list):
            for media in media_items:
                operation = self._media_to_video_operation(media, fallback_project_id=fallback_project_id)
                if operation:
                    media_operations.append(operation)

        operations = normalized.get("operations")
        if isinstance(operations, list) and operations:
            normalized["operations"] = self._merge_video_operations_with_media(
                operations,
                media_operations,
                fallback_project_id=fallback_project_id,
            )
        elif media_operations:
            normalized["operations"] = media_operations

        return normalized

    def _build_video_media_generation_context(self, batch_id: str) -> Dict[str, Any]:
        return {
            "batchId": batch_id,
            "audioFailurePreference": "BLOCK_SILENCED_VIDEOS",
        }

    def _operations_to_media_refs(
        self,
        operations: List[Dict[str, Any]],
    ) -> List[Dict[str, str]]:
        media_refs: List[Dict[str, str]] = []
        for operation in operations or []:
            if not isinstance(operation, dict):
                continue
            operation_body = operation.get("operation") or {}
            media_name = (
                operation.get("mediaName")
                or operation.get("name")
                or operation_body.get("name")
            )
            project_id = (
                operation.get("projectId")
                or operation.get("project_id")
                or operation_body.get("projectId")
            )
            if isinstance(media_name, str) and media_name.strip() and isinstance(project_id, str) and project_id.strip():
                media_refs.append({
                    "name": media_name.strip(),
                    "projectId": project_id.strip(),
                })
        return media_refs

    async def generate_video_text(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        use_v2_model_config: bool = False,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
        output_resolution: Optional[str] = None,
    ) -> dict:
        """Text-to-video; returns task_id

        output_resolution: optional upstream outputSpec.resolution (e.g.
        VIDEO_RESOLUTION_720P for the omni-flash models).

        Args:
            at: Access Token
            project_id: project ID
            prompt: prompt text
            model_key: veo_3_1_t2v_fast, etc.
            aspect_ratio: video aspect ratio
            user_paygate_tier: user tier

        Returns:
            {
                "operations": [{
                    "operation": {"name": "task_id"},
                    "sceneId": "uuid",
                    "status": "MEDIA_GENERATION_STATUS_PENDING"
                }],
                "remainingCredits": 900
            }
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoText"

        # 403/reCAPTCHA retry logic - reCAPTCHA evaluation failed gets its own retry budget
        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        last_error = None
        retry_attempt = 0
        session_id = self._generate_session_id()
        batch_id = str(uuid.uuid4())
        video_context_warmed = False
        while retry_attempt < max_retries:
            # Get a fresh reCAPTCHA token on every retry - video uses VIDEO_GENERATION action
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO T2V] Generation ",
                )
                if should_retry:
                    retry_attempt += 1
                    continue
                raise last_error
            if not video_context_warmed:
                await self._warmup_flow_video_frontend_context(
                    at=at,
                    project_id=project_id,
                    token_id=token_id,
                    session_id=session_id,
                    user_paygate_tier=user_paygate_tier,
                    prompt=prompt,
                    model_key=model_key,
                    aspect_ratio=aspect_ratio,
                )
                video_context_warmed = True
            client_context = {
                "recaptchaContext": {
                    "token": recaptcha_token,
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                },
                "sessionId": session_id,
                "projectId": project_id,
                "tool": "PINHOLE",
                "userPaygateTier": user_paygate_tier
            }
            request_seed = random.randint(1, 99999)
            request_data = {
                "aspectRatio": aspect_ratio,
                "seed": request_seed,
                "textInput": self._build_video_text_input(prompt, use_v2_model_config=True),
                "videoModelKey": model_key,
                "metadata": {}
            }
            if output_resolution:
                request_data["outputSpec"] = {"resolution": output_resolution}
            json_data = {
                "mediaGenerationContext": self._build_video_media_generation_context(batch_id),
                "clientContext": client_context,
                "requests": [request_data],
                "useV2ModelConfig": True,
            }

            try:
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout(),
                    project_id=project_id,
                    token_id=token_id,
                    action="VIDEO_GENERATION",
                )
                return self._normalize_video_generation_response(result, fallback_project_id=project_id)
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO T2V] Generation ",
                    defer_browser_error_notification=True,
                )
                if should_retry:
                    max_retries = self._resolve_generation_retry_budget(max_retries, e)
                    retry_attempt += 1
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
            retry_attempt += 1
        
        # All retries failed
        raise last_error

    def _build_browser_style_control_headers(
        self,
        referer: str,
        origin: Optional[str] = None,
        account_id: Optional[str] = None,
        content_type: Optional[str] = None,
        accept_language: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Referer": referer,
            "User-Agent": self._get_effective_request_user_agent(account_id),
            "Accept-Language": accept_language or self._get_primary_accept_language(fallback="en-US,en;q=0.9"),
            "Priority": "u=1, i",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
        }
        if origin:
            headers["Origin"] = origin
            if origin == "https://labs.google":
                headers.setdefault("sec-fetch-storage-access", "active")
        if content_type:
            headers["Content-Type"] = content_type
        if api_key:
            headers["x-goog-api-key"] = api_key
        headers.setdefault("x-browser-channel", self.FLOW_BROWSER_CHANNEL_HEADER)
        headers.setdefault("x-browser-copyright", self.FLOW_BROWSER_COPYRIGHT_HEADER)
        headers.setdefault("x-browser-validation", self.FLOW_BROWSER_VALIDATION_HEADER)
        headers.setdefault("x-browser-year", self.FLOW_BROWSER_YEAR_HEADER)
        return headers

    async def _labs_trpc_get_with_st(
        self,
        path_with_query: str,
        st: str,
        project_id: str,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        page_url = self._build_flow_project_page_url(project_id)
        return await self._make_request(
            method="GET",
            url=f"{self.labs_base_url}/trpc/{path_with_query}",
            headers=self._build_browser_style_control_headers(
                referer=page_url,
                account_id=st[:16],
                content_type="application/json",
            ),
            use_st=True,
            st_token=st,
            timeout=timeout or self._get_control_plane_timeout(),
            apply_default_client_headers=False,
            allow_urllib_fallback=False,
            impersonate=self._resolve_runtime_impersonate(),
        )

    async def _labs_trpc_post_with_st(
        self,
        trpc_path: str,
        payload: Dict[str, Any],
        st: str,
        project_id: str,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        page_url = self._build_flow_project_page_url(project_id)
        return await self._make_request(
            method="POST",
            url=f"{self.labs_base_url}/trpc/{trpc_path}",
            headers=self._build_browser_style_control_headers(
                referer=page_url,
                origin="https://labs.google",
                account_id=st[:16],
                content_type="application/json",
            ),
            json_data=payload,
            use_st=True,
            st_token=st,
            timeout=timeout or self._get_control_plane_timeout(),
            apply_default_client_headers=False,
            allow_urllib_fallback=False,
            impersonate=self._resolve_runtime_impersonate(),
        )

    async def _aisandbox_request(
        self,
        method: str,
        path: str,
        at: Optional[str],
        *,
        json_data: Optional[Dict[str, Any]] = None,
        raw_body: Optional[Union[str, bytes]] = None,
        content_type: Optional[str] = "text/plain;charset=UTF-8",
        accept_language: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: Optional[int] = None,
        account_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        headers = self._build_browser_style_control_headers(
            referer="https://labs.google/",
            origin="https://labs.google",
            account_id=account_id,
            content_type=content_type,
            accept_language=accept_language,
            api_key=api_key,
        )
        return await self._make_request(
            method=method,
            url=f"{self.api_base_url}{path}",
            headers=headers,
            json_data=json_data,
            raw_body=raw_body,
            use_at=bool(at),
            at_token=at,
            timeout=timeout or self._get_control_plane_timeout(),
            apply_default_client_headers=False,
            allow_urllib_fallback=False,
            impersonate=self._resolve_runtime_impersonate(),
        )

    async def _warmup_flow_video_frontend_context(
        self,
        *,
        at: str,
        project_id: str,
        token_id: Optional[int],
        session_id: str,
        user_paygate_tier: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
    ) -> None:
        """Run the minimal first-party init/telemetry calls the real page makes before a video submit, in the same order."""
        account_id = at[:16] if at else None
        page_url = self._build_flow_project_page_url(project_id)
        session_create_time = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

        st = await self._get_token_st_by_id(token_id)
        if st:
            null_meta_input = self._encode_trpc_input({
                "json": None,
                "meta": {"values": ["undefined"]},
            })
            labs_get_paths = [
                f"general.fetchUserPreferences?input={null_meta_input}",
                f"videoFx.getFlowAppConfig?input={null_meta_input}",
                f"videoFx.getUserSettings?input={null_meta_input}",
            ]
            for path_with_query in labs_get_paths:
                try:
                    await self._labs_trpc_get_with_st(path_with_query, st=st, project_id=project_id)
                except Exception as e:
                    debug_logger.log_warning(f"[VIDEO WARMUP] Labs GET failed ({path_with_query}): {e}")

            labs_batch_log_payload = {
                "json": {
                    "appEvents": [{
                        "event": "PAGE_VIEW",
                        "eventProperties": [
                            {"key": "URL", "stringValue": page_url},
                            {"key": "USER_AGENT", "stringValue": self._get_effective_request_user_agent(st[:16])},
                            {"key": "IS_DESKTOP"},
                        ],
                        "activeExperiments": [],
                        "eventMetadata": {"sessionId": session_id},
                        "eventTime": session_create_time,
                    }]
                }
            }
            try:
                await self._labs_trpc_post_with_st(
                    "general.submitBatchLog",
                    payload=labs_batch_log_payload,
                    st=st,
                    project_id=project_id,
                )
            except Exception as e:
                debug_logger.log_warning(f"[VIDEO WARMUP] Labs submitBatchLog failed: {e}")

        try:
            await self._aisandbox_request(
                "POST",
                ":checkAppAvailability",
                at=None,
                raw_body=self._compact_json_dumps({"clientContext": {"tool": "PINHOLE"}}),
                api_key=self.FLOW_PUBLIC_API_KEY,
                account_id=account_id,
            )
        except Exception as e:
            debug_logger.log_warning(f"[VIDEO WARMUP] checkAppAvailability failed: {e}")

        debug_logger.log_info(
            f"[VIDEO WARMUP] Minimal video init calls done: project_id={project_id}, "
            f"session_id={session_id}, model_key={model_key}, aspect_ratio={aspect_ratio}, "
            f"user_paygate_tier={user_paygate_tier}, prompt_len={len(prompt or '')}"
        )

    def _video_aspect_ratio_to_agent_aspect_ratio(self, aspect_ratio: str) -> str:
        mapping = {
            "VIDEO_ASPECT_RATIO_LANDSCAPE": "16:9",
            "VIDEO_ASPECT_RATIO_PORTRAIT": "9:16",
            "VIDEO_ASPECT_RATIO_SQUARE": "1:1",
        }
        return mapping.get(str(aspect_ratio or "").strip(), "16:9")

    def _parse_sse_json_events(self, raw_text: str) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        if not raw_text:
            return events

        for block in raw_text.split("\n\n"):
            block = block.strip()
            if not block:
                continue

            data_lines = [line[5:].strip() for line in block.splitlines() if line.startswith("data:")]
            if not data_lines:
                continue

            payload_text = "\n".join(data_lines).strip()
            if not payload_text or payload_text == "[DONE]":
                continue

            try:
                payload = json.loads(payload_text)
            except Exception:
                continue

            if isinstance(payload, dict):
                events.append(payload)

        return events

    def _extract_agent_session_id(self, sessions_payload: Dict[str, Any]) -> Optional[str]:
        if not isinstance(sessions_payload, dict):
            return None

        sessions = sessions_payload.get("sessions")
        if isinstance(sessions, list):
            for session in sessions:
                if not isinstance(session, dict):
                    continue
                session_id = str(session.get("agentSessionId") or "").strip()
                if session_id:
                    return session_id

        session_info = sessions_payload.get("sessionInfo")
        if isinstance(session_info, dict):
            session_id = str(session_info.get("agentSessionId") or "").strip()
            if session_id:
                return session_id

        return None

    def _extract_flow_entity_id(self, entity_payload: Dict[str, Any]) -> Optional[str]:
        if not isinstance(entity_payload, dict):
            return None

        candidates = [entity_payload]
        result = entity_payload.get("result")
        if isinstance(result, dict):
            candidates.append(result)
            data = result.get("data")
            if isinstance(data, dict):
                candidates.append(data)
                json_node = data.get("json")
                if isinstance(json_node, dict):
                    candidates.append(json_node)
                    nested_result = json_node.get("result")
                    if isinstance(nested_result, dict):
                        candidates.append(nested_result)

        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            for key in ("entityId", "id", "parentEntityId"):
                entity_id = str(candidate.get(key) or "").strip()
                if entity_id:
                    return entity_id

        return None

    def _extract_turn_count(self, session_detail: Dict[str, Any]) -> int:
        if not isinstance(session_detail, dict):
            return 0
        turns = session_detail.get("turns")
        if isinstance(turns, list):
            return len(turns)
        return 0

    def _extract_generate_video_with_references_result(
        self,
        events: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        for event in events:
            if not isinstance(event, dict):
                continue
            agent_message = event.get("agentMessage")
            if not isinstance(agent_message, dict):
                continue
            agent_events = agent_message.get("agentEvents")
            if not isinstance(agent_events, list):
                continue
            for agent_event in agent_events:
                if not isinstance(agent_event, dict):
                    continue
                tool_result_wrapper = agent_event.get("toolResult")
                if not isinstance(tool_result_wrapper, dict):
                    continue
                if tool_result_wrapper.get("toolName") != "generate_video_with_references":
                    continue
                tool_result = tool_result_wrapper.get("toolResult")
                if isinstance(tool_result, dict):
                    return tool_result
        return None

    async def get_flow_creation_agent_session(
        self,
        at: str,
        project_id: str,
        *,
        account_id: Optional[str] = None,
        allow_global_fallback: bool = True,
    ) -> Optional[str]:
        sessions_result = await self._aisandbox_request(
            "GET",
            f"/flowCreationAgent/sessions?projectId={quote(project_id, safe='')}",
            at=at,
            content_type=None,
            account_id=account_id,
        )
        session_id = self._extract_agent_session_id(sessions_result)
        if session_id or not allow_global_fallback:
            return session_id

        global_sessions_result = await self._aisandbox_request(
            "GET",
            "/flowCreationAgent/sessions",
            at=at,
            content_type=None,
            account_id=account_id,
        )
        return self._extract_agent_session_id(global_sessions_result)

    async def get_flow_creation_agent_session_detail(
        self,
        at: str,
        agent_session_id: str,
        *,
        account_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self._aisandbox_request(
            "GET",
            f"/flowCreationAgent/sessions/{quote(agent_session_id, safe='')}",
            at=at,
            content_type=None,
            account_id=account_id,
        )

    async def create_flow_entity(
        self,
        st: str,
        project_id: str,
    ) -> str:
        payload = {"json": {"projectId": project_id}}
        result = await self._labs_trpc_post_with_st(
            "flow.createEntity",
            payload=payload,
            st=st,
            project_id=project_id,
        )
        entity_id = self._extract_flow_entity_id(result)
        if not entity_id:
            raise RuntimeError(f"flow.createEntity response has no entityId: keys={list(result.keys())}")
        return entity_id

    async def create_character_entity(self, at: str, project_id: str, display_name: str, account_id: Optional[str] = None) -> str:
        """Flow Characters (2026-09-23): create an empty CHARACTER entity in the project; photos are
        attached afterwards with copy_project_media_to_character_slot. Returns the entityId."""
        payload = {
            "entity": {
                "projectId": project_id,
                "entityInfo": {"entityType": "CHARACTER", "displayName": display_name, "characterInfo": {}},
            }
        }
        result = await self._aisandbox_request(
            "POST", "/flow/entities", at=at, raw_body=self._compact_json_dumps(payload), account_id=account_id,
        )
        entity_id = ((result or {}).get("entity") or {}).get("entityId") if isinstance(result, dict) else None
        if not entity_id:
            raise RuntimeError(f"Flow createEntity returned no entityId: {str(result)[:200]}")
        return str(entity_id)

    async def batch_get_entities(self, at: str, entity_ids: List[str], account_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """GET /flow/entities:batchGet -> the `results` list, one item per id
        ({"entity": …} or {"error": {"code": 5, …}} for a missing one)."""
        if not entity_ids:
            return []
        query = "&".join(f"entityIds={urllib.parse.quote(str(e), safe='')}" for e in entity_ids)
        result = await self._aisandbox_request(
            "GET", f"/flow/entities:batchGet?{query}", at=at, account_id=account_id,
        )
        results = (result or {}).get("results") if isinstance(result, dict) else None
        return list(results) if isinstance(results, list) else []

    async def copy_project_media_to_character_slot(
        self,
        at: str,
        *,
        project_id: str,
        media_id: str,
        entity_id: str,
        image_reference_index: int,
        account_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = {
            "mediaId": media_id,
            "destinationProjectId": project_id,
            "destinationMediaContext": {
                "entityContext": {
                    "entityId": entity_id,
                    "characterSlot": {
                        "imageReferenceIndex": int(image_reference_index),
                    },
                }
            },
        }
        return await self._aisandbox_request(
            "POST",
            "/flow:copyProjectMedia",
            at=at,
            raw_body=self._compact_json_dumps(payload),
            account_id=account_id,
        )

    async def stream_flow_creation_agent(
        self,
        at: str,
        payload: Dict[str, Any],
        *,
        project_id: Optional[str] = None,
        token_id: Optional[int] = None,
        action: str = "VIDEO_GENERATION",
        account_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        url = f"{self.api_base_url}/flowCreationAgent:streamChat?alt=sse"
        headers = self._build_browser_style_control_headers(
            referer="https://labs.google/",
            origin="https://labs.google",
            account_id=account_id,
            content_type="application/json",
            accept_language=self._get_primary_accept_language(),
        )
        headers["Accept"] = "text/event-stream, text/event-stream"

        if config.captcha_method == "browser" and project_id:
            from .browser_captcha import BrowserCaptchaService

            service = await BrowserCaptchaService.get_instance(self.db)
            response_payload, _browser_ref, fingerprint = await service.submit_flow_request(
                project_id=project_id,
                action=action,
                token_id=token_id,
                url=url,
                at_token=at,
                json_data=payload,
                timeout=self._get_video_submit_timeout(),
            )
            self._set_request_fingerprint(fingerprint if fingerprint else None)

            status_code = int(response_payload.get("status") or 0)
            raw_text = response_payload.get("text") or ""
            if status_code >= 400:
                error_reason = f"HTTP Error {status_code}"
                parsed_body = None
                try:
                    parsed_body = json.loads(raw_text) if raw_text else None
                except Exception:
                    parsed_body = None
                if isinstance(parsed_body, dict) and "error" in parsed_body:
                    error_info = parsed_body["error"] or {}
                    error_message = error_info.get("message", "")
                    details = error_info.get("details", [])
                    for detail in details or []:
                        if isinstance(detail, dict) and detail.get("reason"):
                            error_reason = detail.get("reason")
                            break
                    if error_message:
                        error_reason = f"{error_reason}: {error_message}"
                elif raw_text:
                    error_reason = f"HTTP Error {status_code}: {raw_text[:200]}"
                raise Exception(error_reason)
        else:
            raw_text = await self._make_text_request(
                method="POST",
                url=url,
                headers=headers,
                json_data=payload,
                use_at=True,
                at_token=at,
                timeout=self._get_video_submit_timeout(),
                apply_default_client_headers=False,
                impersonate=self._resolve_runtime_impersonate(),
            )
        return self._parse_sse_json_events(raw_text)

    async def generate_omni_reference_video(
        self,
        at: str,
        st: str,
        project_id: str,
        prompt: str,
        aspect_ratio: str,
        reference_media_ids: List[str],
        model_usage_key: str = "abra_r2v_10s",
        model_display_name: str = "Omni Flash",
        duration: int = 10,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not st:
            raise RuntimeError("Omni reference video needs an ST token, but this account has no ST")
        if not reference_media_ids:
            raise RuntimeError("Omni reference video needs at least 1 reference image")

        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        last_error = None
        retry_attempt = 0
        session_id = self._generate_session_id()
        entity_handle = "entity-0"
        video_context_warmed = False
        account_id = at[:16] if at else None
        entity_id: Optional[str] = None
        agent_aspect_ratio = self._video_aspect_ratio_to_agent_aspect_ratio(aspect_ratio)
        agent_prompt = (
            f"{prompt}\n\nUse a {agent_aspect_ratio} aspect ratio."
            if prompt
            else f"Use a {agent_aspect_ratio} aspect ratio."
        )

        while retry_attempt < max_retries:
            launch_gate_acquired = False
            approve_browser_id = None
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            browser_id = None
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id,
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)

            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO OMNI-R2V] Generation ",
                )
                if should_retry:
                    retry_attempt += 1
                    continue
                raise last_error

            try:
                if not entity_id:
                    entity_id = await self.create_flow_entity(st=st, project_id=project_id)
                    for index, media_id in enumerate(reference_media_ids):
                        await self.copy_project_media_to_character_slot(
                            at=at,
                            project_id=project_id,
                            media_id=media_id,
                            entity_id=entity_id,
                            image_reference_index=index,
                            account_id=account_id,
                        )
                    debug_logger.log_info(
                        f"[VIDEO OMNI-R2V] Created character entity and attached reference images: project_id={project_id}, entity_id={entity_id}, refs={len(reference_media_ids)}"
                    )

                if not video_context_warmed:
                    await self._warmup_flow_video_frontend_context(
                        at=at,
                        project_id=project_id,
                        token_id=token_id,
                        session_id=session_id,
                        user_paygate_tier=user_paygate_tier,
                        prompt=agent_prompt,
                        model_key=model_usage_key,
                        aspect_ratio=aspect_ratio,
                    )
                    video_context_warmed = True

                agent_session_id = await self.get_flow_creation_agent_session(
                    at=at,
                    project_id=project_id,
                    account_id=account_id,
                    allow_global_fallback=True,
                )
                if not agent_session_id:
                    raise RuntimeError(f"Flow Creation Agent Session not found: project_id={project_id}")

                session_detail = await self.get_flow_creation_agent_session_detail(
                    at=at,
                    agent_session_id=agent_session_id,
                    account_id=account_id,
                )
                current_turn_count = self._extract_turn_count(session_detail)
                next_turn_number = current_turn_count + 1

                prompt_payload = {
                    "agentSessionId": agent_session_id,
                    "agentClientContext": {
                        "projectId": f"projects/{project_id}",
                        "clientSessionId": session_id,
                        "recaptchaContext": {
                            "token": recaptcha_token,
                            "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                        },
                        "turnNumber": next_turn_number,
                    },
                    "userMessage": {
                        "userPrompt": {
                            "parts": [{"text": agent_prompt}],
                        },
                        "entityReferences": [
                            {
                                "entityId": entity_id,
                                "handle": entity_handle,
                            }
                        ],
                    },
                }

                prompt_events = await self.stream_flow_creation_agent(
                    at=at,
                    payload=prompt_payload,
                    project_id=project_id,
                    token_id=token_id,
                    account_id=account_id,
                )
                tool_result = self._extract_generate_video_with_references_result(prompt_events)

                if not tool_result:
                    approve_recaptcha_token, approve_browser_id = await self._get_recaptcha_token(
                        project_id,
                        action="VIDEO_GENERATION",
                        token_id=token_id,
                    )
                    if not approve_recaptcha_token:
                        # Same text as every other mint failure so the token manager
                        # applies the flat mint pause, not a reputation strike.
                        raise RuntimeError("Failed to obtain reCAPTCHA token (Omni approval stage)")
                    approve_payload = {
                        "agentSessionId": agent_session_id,
                        "agentClientContext": {
                            "projectId": f"projects/{project_id}",
                            "clientSessionId": session_id,
                            "recaptchaContext": {
                                "token": approve_recaptcha_token,
                                "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB",
                            },
                            "turnNumber": next_turn_number + 1,
                        },
                        "userMessage": {
                            "userPrompt": {
                                "parts": [{"text": "Approve"}],
                            }
                        },
                    }
                    approve_events = await self.stream_flow_creation_agent(
                        at=at,
                        payload=approve_payload,
                        project_id=project_id,
                        token_id=token_id,
                        account_id=account_id,
                    )
                    tool_result = self._extract_generate_video_with_references_result(approve_events)
                    await self._notify_browser_captcha_request_finished(approve_browser_id)
                    approve_browser_id = None

                if not tool_result:
                    raise RuntimeError("Omni reference video did not return a generate_video_with_references toolResult")

                media_id = str(tool_result.get("media_id") or "").strip()
                resolved_project_id = str(tool_result.get("project_id") or project_id).strip() or project_id
                if not media_id:
                    raise RuntimeError(f"Omni reference video toolResult has no media_id: {tool_result}")

                operation = {
                    "operation": {"name": media_id},
                    "name": media_id,
                    "mediaName": media_id,
                    "projectId": resolved_project_id,
                    "workflowId": tool_result.get("workflow_id"),
                    "batchId": tool_result.get("batch_id"),
                    "status": "MEDIA_GENERATION_STATUS_ACTIVE",
                }
                return {
                    "operations": [operation],
                    "agentToolResult": tool_result,
                    "projectId": resolved_project_id,
                    "entityId": entity_id,
                    "entityHandle": entity_handle,
                    "modelUsageKey": model_usage_key,
                    "aspectRatio": tool_result.get("aspect_ratio") or aspect_ratio,
                    "duration": duration,
                    "modelDisplayName": model_display_name,
                }
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO OMNI-R2V] Generation ",
                    defer_browser_error_notification=True,
                )
                if should_retry:
                    max_retries = self._resolve_generation_retry_budget(max_retries, e)
                    retry_attempt += 1
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
                if approve_browser_id:
                    await self._notify_browser_captcha_request_finished(approve_browser_id)

            retry_attempt += 1

        raise last_error

    async def generate_video_reference_images(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        reference_images: List[Dict],
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
        prompt_parts: Optional[List[Dict[str, Any]]] = None,
        reference_entities: Optional[List[str]] = None,
    ) -> dict:
        """Image-to-video; returns task_id

        Args:
            at: Access Token
            project_id: project ID
            prompt: prompt text
            model_key: veo_3_1_r2v_fast_landscape
            aspect_ratio: video aspect ratio
            reference_images: reference images [{"imageUsageType": "IMAGE_USAGE_TYPE_ASSET", "mediaId": "..."}]
            user_paygate_tier: user tier

        Returns:
            Same as generate_video_text
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoReferenceImages"

        # 403/reCAPTCHA retry logic - reCAPTCHA evaluation failed gets its own retry budget
        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        last_error = None
        retry_attempt = 0
        session_id = self._generate_session_id()
        batch_id = str(uuid.uuid4())
        video_context_warmed = False
        while retry_attempt < max_retries:
            # Get a fresh reCAPTCHA token on every retry - video uses VIDEO_GENERATION action
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO R2V] Generation ",
                )
                if should_retry:
                    retry_attempt += 1
                    continue
                raise last_error
            if not video_context_warmed:
                await self._warmup_flow_video_frontend_context(
                    at=at,
                    project_id=project_id,
                    token_id=token_id,
                    session_id=session_id,
                    user_paygate_tier=user_paygate_tier,
                    prompt=prompt,
                    model_key=model_key,
                    aspect_ratio=aspect_ratio,
                )
                video_context_warmed = True
            request_seed = random.randint(1, 99999)
            json_data = {
                "mediaGenerationContext": self._build_video_media_generation_context(batch_id),
                "clientContext": {
                    "recaptchaContext": {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                    },
                    "sessionId": session_id,
                    "projectId": project_id,
                    "tool": "PINHOLE",
                    "userPaygateTier": user_paygate_tier
                },
                "requests": [{
                    "aspectRatio": aspect_ratio,
                    "seed": request_seed,
                    "textInput": self._build_video_text_input(prompt, use_v2_model_config=True, prompt_parts=prompt_parts),
                    "videoModelKey": model_key,
                    "referenceImages": reference_images,
                    **({"referenceEntities": [{"entityId": eid} for eid in reference_entities]} if reference_entities else {}),
                    "metadata": {}
                }],
                "useV2ModelConfig": True
            }

            try:
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout(),
                    project_id=project_id,
                    token_id=token_id,
                    action="VIDEO_GENERATION",
                )
                return self._normalize_video_generation_response(result, fallback_project_id=project_id)
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO R2V] Generation ",
                    defer_browser_error_notification=True,
                )
                if should_retry:
                    max_retries = self._resolve_generation_retry_budget(max_retries, e)
                    retry_attempt += 1
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
            retry_attempt += 1
        
        # All retries failed
        raise last_error

    async def generate_video_start_end(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        start_media_id: str,
        end_media_id: str,
        use_v2_model_config: bool = False,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
    ) -> dict:
        """Video from start and end frames; returns task_id

        Args:
            at: Access Token
            project_id: project ID
            prompt: prompt text
            model_key: veo_3_1_i2v_s_fast_fl
            aspect_ratio: video aspect ratio
            start_media_id: start frame mediaId
            end_media_id: end frame mediaId
            user_paygate_tier: user tier

        Returns:
            Same as generate_video_text
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoStartAndEndImage"

        # 403/reCAPTCHA retry logic - reCAPTCHA evaluation failed gets its own retry budget
        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        last_error = None
        retry_attempt = 0
        session_id = self._generate_session_id()
        batch_id = str(uuid.uuid4())
        video_context_warmed = False
        while retry_attempt < max_retries:
            # Get a fresh reCAPTCHA token on every retry - video uses VIDEO_GENERATION action
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO I2V] Start/end frame generation ",
                )
                if should_retry:
                    retry_attempt += 1
                    continue
                raise last_error
            if not video_context_warmed:
                await self._warmup_flow_video_frontend_context(
                    at=at,
                    project_id=project_id,
                    token_id=token_id,
                    session_id=session_id,
                    user_paygate_tier=user_paygate_tier,
                    prompt=prompt,
                    model_key=model_key,
                    aspect_ratio=aspect_ratio,
                )
                video_context_warmed = True
            client_context = {
                "recaptchaContext": {
                    "token": recaptcha_token,
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                },
                "sessionId": session_id,
                "projectId": project_id,
                "tool": "PINHOLE",
                "userPaygateTier": user_paygate_tier
            }
            request_seed = random.randint(1, 99999)
            request_data = {
                "aspectRatio": aspect_ratio,
                "seed": request_seed,
                "textInput": self._build_video_text_input(prompt, use_v2_model_config=True),
                "videoModelKey": model_key,
                "startImage": {
                    "mediaId": start_media_id
                },
                "endImage": {
                    "mediaId": end_media_id
                },
                "metadata": {}
            }
            json_data = {
                "mediaGenerationContext": self._build_video_media_generation_context(batch_id),
                "clientContext": client_context,
                "requests": [request_data],
                "useV2ModelConfig": True,
            }

            try:
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout(),
                    project_id=project_id,
                    token_id=token_id,
                    action="VIDEO_GENERATION",
                )
                return self._normalize_video_generation_response(result, fallback_project_id=project_id)
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO I2V] Start/end frame generation ",
                    defer_browser_error_notification=True,
                )
                if should_retry:
                    max_retries = self._resolve_generation_retry_budget(max_retries, e)
                    retry_attempt += 1
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
            retry_attempt += 1
        
        # All retries failed
        raise last_error

    async def generate_video_start_image(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        start_media_id: str,
        use_v2_model_config: bool = False,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
    ) -> dict:
        """Video from start frame only; returns task_id

        Args:
            at: Access Token
            project_id: project ID
            prompt: prompt text
            model_key: veo_3_1_i2v_s_fast_fl, etc.
            aspect_ratio: video aspect ratio
            start_media_id: start frame mediaId
            user_paygate_tier: user tier

        Returns:
            Same as generate_video_text
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoStartImage"

        # 403/reCAPTCHA retry logic - reCAPTCHA evaluation failed gets its own retry budget
        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        last_error = None
        retry_attempt = 0
        session_id = self._generate_session_id()
        batch_id = str(uuid.uuid4())
        video_context_warmed = False
        while retry_attempt < max_retries:
            # Get a fresh reCAPTCHA token on every retry - video uses VIDEO_GENERATION action
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO I2V] Start frame generation ",
                )
                if should_retry:
                    retry_attempt += 1
                    continue
                raise last_error
            if not video_context_warmed:
                await self._warmup_flow_video_frontend_context(
                    at=at,
                    project_id=project_id,
                    token_id=token_id,
                    session_id=session_id,
                    user_paygate_tier=user_paygate_tier,
                    prompt=prompt,
                    model_key=model_key,
                    aspect_ratio=aspect_ratio,
                )
                video_context_warmed = True
            client_context = {
                "recaptchaContext": {
                    "token": recaptcha_token,
                    "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                },
                "sessionId": session_id,
                "projectId": project_id,
                "tool": "PINHOLE",
                "userPaygateTier": user_paygate_tier
            }
            request_seed = random.randint(1, 99999)
            request_data = {
                "aspectRatio": aspect_ratio,
                "seed": request_seed,
                "textInput": self._build_video_text_input(prompt, use_v2_model_config=True),
                "videoModelKey": model_key,
                "startImage": {
                    "mediaId": start_media_id
                },
                # Note: no endImage field, start frame only
                "metadata": {}
            }
            json_data = {
                "mediaGenerationContext": self._build_video_media_generation_context(batch_id),
                "clientContext": client_context,
                "requests": [request_data],
                "useV2ModelConfig": True,
            }

            try:
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout(),
                    project_id=project_id,
                    token_id=token_id,
                    action="VIDEO_GENERATION",
                )
                return self._normalize_video_generation_response(result, fallback_project_id=project_id)
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO I2V] Start frame generation ",
                    defer_browser_error_notification=True,
                )
                if should_retry:
                    max_retries = self._resolve_generation_retry_budget(max_retries, e)
                    retry_attempt += 1
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
            retry_attempt += 1
        
        # All retries failed
        raise last_error

    # ========== Video Extend ==========

    async def generate_video_extend(
        self,
        at: str,
        project_id: str,
        prompt: str,
        model_key: str,
        aspect_ratio: str,
        video_media_id: str,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
    ) -> dict:
        """Extend a generated video by 7 seconds

        Args:
            at: Access Token
            project_id: project ID
            prompt: extend prompt
            model_key: veo_3_1_extend_portrait / veo_3_1_extend, etc.
            aspect_ratio: video aspect ratio
            video_media_id: source video mediaGenerationId
            user_paygate_tier: user tier

        Returns:
            Same as generate_video_text (operations list)
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoExtendVideo"

        # 403/reCAPTCHA retry logic - reCAPTCHA evaluation failed gets its own retry budget
        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        last_error = None
        retry_attempt = 0
        session_id = self._generate_session_id()
        workflow_id = str(uuid.uuid4())
        batch_id = str(uuid.uuid4())
        video_context_warmed = False
        while retry_attempt < max_retries:
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO EXTEND] Extend ",
                )
                if should_retry:
                    retry_attempt += 1
                    continue
                raise last_error
            if not video_context_warmed:
                await self._warmup_flow_video_frontend_context(
                    at=at,
                    project_id=project_id,
                    token_id=token_id,
                    session_id=session_id,
                    user_paygate_tier=user_paygate_tier,
                    prompt=prompt,
                    model_key=model_key,
                    aspect_ratio=aspect_ratio,
                )
                video_context_warmed = True

            request_seed = random.randint(1, 99999)
            json_data = {
                "clientContext": {
                    "recaptchaContext": {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                    },
                    "sessionId": session_id,
                    "projectId": project_id,
                    "tool": "PINHOLE",
                    "userPaygateTier": user_paygate_tier
                },
                "mediaGenerationContext": self._build_video_media_generation_context(batch_id),
                "requests": [{
                    "aspectRatio": aspect_ratio,
                    "seed": request_seed,
                    "textInput": {
                        "structuredPrompt": {
                            "parts": [{"text": prompt}]
                        }
                    },
                    "videoInput": {
                        "mediaId": video_media_id
                    },
                    "videoModelKey": model_key,
                    "metadata": {
                        "workflowId": workflow_id
                    }
                }],
                "useV2ModelConfig": True
            }

            # Debug: print request body
            import json as _json
            debug_logger.log_info(f"[VIDEO EXTEND] Request URL: {url}")
            debug_logger.log_info(f"[VIDEO EXTEND] Request JSON: {_json.dumps(json_data, indent=2, ensure_ascii=False)[:2000]}")

            try:
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout(),
                    project_id=project_id,
                    token_id=token_id,
                    action="VIDEO_GENERATION",
                )
                return self._normalize_video_generation_response(result, fallback_project_id=project_id)
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO EXTEND] Extend ",
                    defer_browser_error_notification=True,
                )
                if should_retry:
                    max_retries = self._resolve_generation_retry_budget(max_retries, e)
                    retry_attempt += 1
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
            retry_attempt += 1

        # All retries failed
        raise last_error

    # ========== Video Concatenation ==========

    async def run_concatenation(
        self,
        at: str,
        original_media_id: str,
        extend_media_id: str,
    ) -> dict:
        """
        Join videos with the Google runVideoFxConcatenation API
        
        Args:
            at: auth token
            original_media_id: original video mediaGenerationId (UUID)
            extend_media_id: extended video mediaGenerationId (UUID)
        
        Returns:
            dict with the operation name
        """
        url = f"{self.api_base_url}:runVideoFxConcatenation"
        
        json_data = {
            "inputVideos": [
                {
                    "mediaGenerationId": original_media_id,
                    "lengthNanos": 8000,
                    "startTimeOffset": "0s",
                    "endTimeOffset": "8s"
                },
                {
                    "mediaGenerationId": extend_media_id,
                    "lengthNanos": 8000,
                    "startTimeOffset": "1s",
                    "endTimeOffset": "8s"
                }
            ]
        }
        
        debug_logger.log_info(f"[CONCAT] Submitting concat job: original={original_media_id[:12]}..., extend={extend_media_id[:12]}...")
        
        result = await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_at=True,
            at_token=at
        )
        debug_logger.log_info(f"[CONCAT] Concat job submitted: {json.dumps(result, ensure_ascii=False)[:300]}")
        return result

    async def poll_concatenation_status(
        self,
        at: str,
        operation_name: str,
        timeout: int = 300,
        poll_interval: int = 3,
    ) -> dict:
        """
        Poll the concat job until it finishes or times out
        
        Args:
            at: auth token
            operation_name: concat job operation name
            timeout: timeout in seconds
            poll_interval: poll interval in seconds
        
        Returns:
            dict with outputUri and mediaGenerationId
        """
        url = f"{self.api_base_url}:runVideoFxCheckConcatenationStatus"
        json_data = {
            "operation": {
                "operation": {
                    "name": operation_name
                }
            }
        }
        
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            result = await self._make_request(
                method="POST",
                url=url,
                json_data=json_data,
                use_at=True,
                at_token=at,
                timeout=300,  # concat API returns base64 video (~14MB), needs longer timeout
            )
            
            status = result.get("status", "")
            output_uri = result.get("outputUri", "")
            encoded_video = result.get("encodedVideo", "")
            
            ev_len = len(encoded_video) if encoded_video else 0
            elapsed = int(time.time() - start_time)
            all_keys = list(result.keys())
            debug_logger.log_info(
                f"[CONCAT] Status: {status}, outputUri={'yes' if output_uri else 'no'}, "
                f"encodedVideo={ev_len} chars, elapsed={elapsed}s, keys={all_keys}"
            )
            
            # Check outputUri first
            if output_uri:
                debug_logger.log_info(f"[CONCAT] Concat done (outputUri): {output_uri[:120]}")
                return result
            
            # Google API returns encodedVideo (base64 MP4) instead of outputUri
            if encoded_video and "SUCCESSFUL" in status:
                try:
                    import os
                    video_bytes = base64.b64decode(encoded_video)
                    video_filename = f"concat_{uuid.uuid4().hex[:12]}.mp4"
                    
                    # Save to tmp/ (FastAPI serves it as static files at /tmp)
                    save_dir = "tmp"
                    os.makedirs(save_dir, exist_ok=True)
                    save_path = os.path.join(save_dir, video_filename)
                    
                    with open(save_path, "wb") as f:
                        f.write(video_bytes)
                    
                    # Build URL: FastAPI mounts /tmp -> /app/tmp/
                    serve_url = f"/tmp/{video_filename}"
                    debug_logger.log_info(f"[CONCAT] Concat done (encodedVideo): saved {len(video_bytes)} bytes -> {serve_url}")
                    
                    result["outputUri"] = serve_url
                    result["local_file"] = save_path
                    return result
                except Exception as e:
                    debug_logger.log_error(f"[CONCAT] Failed to decode encodedVideo: {e}")
                    raise Exception(f"Failed to decode stitched video: {e}")
            
            # SUCCESSFUL but neither outputUri nor encodedVideo
            if "SUCCESSFUL" in status:
                debug_logger.log_warning(f"[CONCAT] SUCCESSFUL but no outputUri/encodedVideo: {json.dumps(result, ensure_ascii=False)[:300]}")

            if "FAILED" in status or "ERROR" in status:
                debug_logger.log_error(f"[CONCAT] Failed: {status}, response: {json.dumps(result, ensure_ascii=False)[:300]}")
                raise Exception(f"Video stitching failed: {status}")
            
            await asyncio.sleep(poll_interval)
        
        debug_logger.log_error(f"[CONCAT] Timed out ({timeout}s), giving up on concat")
        raise Exception(f"Video stitching timed out ({timeout}s)")

    # ========== Video Upsampler ==========

    async def upsample_video(
        self,
        at: str,
        project_id: str,
        video_media_id: str,
        aspect_ratio: str,
        resolution: str,
        model_key: str,
        user_paygate_tier: str = "PAYGATE_TIER_ONE",
        token_id: Optional[int] = None,
        token_video_concurrency: Optional[int] = None,
    ) -> dict:
        """Upscale a video to 4K/1080P; returns task_id

        Args:
            at: Access Token
            project_id: project ID
            video_media_id: video mediaId
            aspect_ratio: video aspect ratio VIDEO_ASPECT_RATIO_PORTRAIT/LANDSCAPE
            resolution: VIDEO_RESOLUTION_4K or VIDEO_RESOLUTION_1080P
            model_key: veo_3_1_upsampler_4k or veo_3_1_upsampler_1080p

        Returns:
            Same as generate_video_text
        """
        url = f"{self.api_base_url}/video:batchAsyncGenerateVideoUpsampleVideo"

        # 403/reCAPTCHA retry logic - reCAPTCHA evaluation failed gets its own retry budget
        max_retries = self._resolve_generation_retry_budget(config.flow_max_retries)
        last_error = None
        retry_attempt = 0
        session_id = self._generate_session_id()
        batch_id = str(uuid.uuid4())
        scene_id = str(uuid.uuid4())
        video_context_warmed = False
        while retry_attempt < max_retries:
            launch_gate_acquired = False
            launch_ok, _, _ = await self._acquire_video_launch_gate(
                token_id=token_id,
                token_video_concurrency=token_video_concurrency,
            )
            if not launch_ok:
                last_error = Exception("Video launch queue wait timeout")
                raise last_error

            launch_gate_acquired = True
            try:
                recaptcha_token, browser_id = await self._get_recaptcha_token(
                    project_id,
                    action="VIDEO_GENERATION",
                    token_id=token_id
                )
            finally:
                if launch_gate_acquired:
                    await self._release_video_launch_gate(token_id)
            if not recaptcha_token:
                last_error = Exception("Failed to obtain reCAPTCHA token")
                should_retry = await self._handle_missing_recaptcha_token(
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO UPSAMPLE] Upscale ",
                )
                if should_retry:
                    retry_attempt += 1
                    continue
                raise last_error
            if not video_context_warmed:
                await self._warmup_flow_video_frontend_context(
                    at=at,
                    project_id=project_id,
                    token_id=token_id,
                    session_id=session_id,
                    user_paygate_tier=user_paygate_tier,
                    prompt="",
                    model_key=model_key,
                    aspect_ratio=aspect_ratio,
                )
                video_context_warmed = True

            request_seed = random.randint(1, 99999)
            json_data = {
                "mediaGenerationContext": self._build_video_media_generation_context(batch_id),
                "requests": [{
                    "aspectRatio": aspect_ratio,
                    "resolution": resolution,
                    "seed": request_seed,
                    "videoInput": {
                        "mediaId": video_media_id
                    },
                    "videoModelKey": model_key,
                    "metadata": {
                        "sceneId": scene_id
                    }
                }],
                "clientContext": {
                    "recaptchaContext": {
                        "token": recaptcha_token,
                        "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"
                    },
                    "sessionId": session_id,
                    "projectId": project_id,
                    "tool": "PINHOLE",
                    "userPaygateTier": user_paygate_tier,
                },
                "useV2ModelConfig": True,
            }

            try:
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_submit_timeout(),
                    project_id=project_id,
                    token_id=token_id,
                    action="VIDEO_GENERATION",
                )
                return self._normalize_video_generation_response(result, fallback_project_id=project_id)
            except Exception as e:
                last_error = e
                should_retry = await self._handle_retryable_generation_error(
                    error=e,
                    retry_attempt=retry_attempt,
                    max_retries=max_retries,
                    browser_id=browser_id,
                    project_id=project_id,
                    log_prefix="[VIDEO UPSAMPLE] Upscale ",
                    defer_browser_error_notification=True,
                )
                if should_retry:
                    max_retries = self._resolve_generation_retry_budget(max_retries, e)
                    retry_attempt += 1
                    continue
                raise
            finally:
                await self._notify_browser_captcha_request_finished(browser_id)
            retry_attempt += 1
        
        raise last_error

    # ========== Task polling (uses AT) ==========

    async def check_video_status(self, at: str, operations: List[Dict]) -> dict:
        """Check video generation status

        Args:
            at: Access Token
            operations: internal operation list; the upstream status check only uses the media refs taken from it

        Returns:
            {
                "operations": [{
                    "operation": {
                        "name": "task_id",
                        "metadata": {...}  # has video info when done
                    },
                    "status": "MEDIA_GENERATION_STATUS_SUCCESSFUL"
                }]
            }
        """
        url = f"{self.api_base_url}/video:batchCheckAsyncVideoGenerationStatus"

        media_refs = self._operations_to_media_refs(operations)
        if not media_refs:
            raise ValueError("Video status check has no media refs, cannot build the upstream query")

        json_data = {"media": media_refs}
        max_retries = config.flow_max_retries
        last_error: Optional[Exception] = None

        for retry_attempt in range(max_retries):
            try:
                result = await self._make_video_api_request(
                    url=url,
                    json_data=json_data,
                    at=at,
                    timeout=self._get_video_poll_timeout()
                )
                try:
                    media_preview = result.get("media") if isinstance(result, dict) else None
                    operations_preview = result.get("operations") if isinstance(result, dict) else None
                    preview_payload = {
                        "top_keys": list(result.keys()) if isinstance(result, dict) else [],
                        "media_count": len(media_preview) if isinstance(media_preview, list) else 0,
                        "operations_count": len(operations_preview) if isinstance(operations_preview, list) else 0,
                        "first_media": media_preview[0] if isinstance(media_preview, list) and media_preview else None,
                        "first_operation": operations_preview[0] if isinstance(operations_preview, list) and operations_preview else None,
                    }
                    print(
                        "[VIDEO POLL RAW] "
                        + json.dumps(
                            self._truncate_large_debug_value(preview_payload),
                            ensure_ascii=False,
                        )[:4000]
                    )
                except Exception:
                    pass
                return self._normalize_video_generation_response(result)
            except Exception as e:
                last_error = e
                retry_reason = self._get_retry_reason(str(e))
                if retry_reason and retry_attempt < max_retries - 1:
                    debug_logger.log_warning(
                        f"[VIDEO POLL] Status check hit {retry_reason}, retrying ({retry_attempt + 2}/{max_retries})..."
                    )
                    await asyncio.sleep(1)
                    continue
                raise

        if last_error is not None:
            raise last_error
        raise RuntimeError("Video status query failed")

    async def get_media_url_redirect(
        self,
        st: str,
        media_name: str,
    ) -> str:
        """Get the real video CDN URL from the labs.google trpc endpoint (added 2026-05).

        With the new schema, batchCheckAsyncVideoGenerationStatus no longer returns
        fifeUrl even when SUCCESSFUL; use the labs.google
        /fx/api/trpc/media.getMediaUrlRedirect endpoint (ST cookie auth) to get the
        302 redirect Location.

        Args:
            st: Session Token (__Secure-next-auth.session-token cookie value)
            media_name: media ID, i.e. media[].name from batchCheckAsync...

        Returns:
            The real video CDN URL (value of the 3xx Location header).

        Raises:
            ValueError: media_name or st is empty.
            RuntimeError: network error, no 3xx, or no Location header.
        """
        if not media_name:
            raise ValueError("get_media_url_redirect: media_name is empty")
        if not st:
            raise ValueError("get_media_url_redirect: missing ST token")

        url = (
            f"{self.labs_base_url}/trpc/media.getMediaUrlRedirect"
            f"?name={media_name}"
        )

        # Accept / Range headers copied from a real browser capture
        # (FINDINGS/T2V_04_FINDING_the_redirect_result.md); cannot use _make_request because it follows the 302.
        headers = {
            "accept": (
                "video/webm,video/ogg,video/*;q=0.9,application/ogg;q=0.7,"
                "audio/*;q=0.6,*/*;q=0.5"
            ),
            "accept-language": "en-US,en;q=0.9",
            "accept-encoding": "identity",
            "Range": "bytes=0-",
            "referer": f"{self.labs_base_url}/fx/tools/flow",
            "Sec-Fetch-Dest": "video",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Priority": "u=0",
            "pragma": "no-cache",
            "cache-control": "no-cache",
            "cookie": f"__Secure-next-auth.session-token={st}",
        }

        # Send via proxy (if any), reusing the proxy logic of _make_request.
        proxy_url = None
        try:
            if self.proxy_manager:
                if hasattr(self.proxy_manager, "get_request_proxy_url"):
                    proxy_url = await self.proxy_manager.get_request_proxy_url()
                else:
                    proxy_url = await self.proxy_manager.get_proxy_url()
        except Exception:
            proxy_url = None

        # Debug: log request
        if config.debug_enabled:
            debug_logger.log_request(
                method="GET",
                url=url,
                headers=headers,
                proxy=proxy_url
            )

        start_time = time.time()

        try:
            async with AsyncSession(trust_env=False) as session:
                response = await session.get(
                    url,
                    headers=headers,
                    allow_redirects=False,
                    timeout=30,
                    proxy=proxy_url,
                    impersonate="chrome124",
                )
        except Exception as e:
            debug_logger.log_error(f"[MEDIA REDIRECT] Request failed: media={media_name}, error={e}")
            raise RuntimeError(
                f"getMediaUrlRedirect request failed (media={media_name}): {e}"
            ) from e

        duration_ms = (time.time() - start_time) * 1000
        status_code = getattr(response, "status_code", 0)
        resp_headers = getattr(response, "headers", {}) or {}

        # Debug: log response
        if config.debug_enabled:
            debug_logger.log_response(
                status_code=status_code,
                headers=dict(resp_headers) if resp_headers else {},
                body=f"Location: {resp_headers.get('Location') or resp_headers.get('location', 'N/A')}",
                duration_ms=duration_ms
            )

        location = None
        try:
            location = resp_headers.get("Location") or resp_headers.get("location")
        except Exception:
            try:
                location = dict(resp_headers).get("Location")
            except Exception:
                location = None

        if status_code not in (301, 302, 303, 307, 308) or not location:
            debug_logger.log_error(
                f"[MEDIA REDIRECT] No redirect returned: status={status_code}, "
                f"media={media_name}, location={location}"
            )
            raise RuntimeError(
                f"getMediaUrlRedirect did not return a redirect "
                f"(status={status_code}, media={media_name})"
            )

        debug_logger.log_info(f"[MEDIA REDIRECT] Success: media={media_name}, location={str(location)[:200]}")
        return str(location)

    # ========== Media delete (uses ST) ==========

    async def delete_media(self, st: str, media_names: List[str]):
        """Delete media

        Args:
            st: Session Token
            media_names: list of media IDs
        """
        url = f"{self.labs_base_url}/trpc/media.deleteMedia"
        json_data = {
            "json": {
                "names": media_names
            }
        }

        await self._make_request(
            method="POST",
            url=url,
            json_data=json_data,
            use_st=True,
            st_token=st
        )

    async def get_media_url_redirect(
        self,
        st: str,
        media_name: str,
        media_url_type: str = "MEDIA_URL_TYPE_FULL_MEDIA",
    ) -> Optional[str]:
        """Get the real media URL via trpc media.getMediaUrlRedirect."""
        normalized_media_name = str(media_name or "").strip()
        if not normalized_media_name:
            return None

        url = (
            f"{self.labs_base_url}/trpc/media.getMediaUrlRedirect"
            f"?name={quote(normalized_media_name, safe='')}"
            f"&mediaUrlType={quote(str(media_url_type or 'MEDIA_URL_TYPE_FULL_MEDIA'), safe='')}"
        )

        proxy_url = None
        if self.proxy_manager:
            if hasattr(self.proxy_manager, "get_request_proxy_url"):
                proxy_url = await self.proxy_manager.get_request_proxy_url()
            else:
                proxy_url = await self.proxy_manager.get_proxy_url()

        headers = {
            "Cookie": f"__Secure-next-auth.session-token={st}",
            "User-Agent": self._generate_user_agent(st[:16] if st else None),
            "Accept": "*/*",
        }
        for key, value in self._default_client_headers.items():
            headers.setdefault(key, value)

        request_timeout = self._get_control_plane_timeout()
        start_time = time.time()
        try:
            async with AsyncSession(trust_env=False) as session:
                response = await session.get(
                    url,
                    headers=headers,
                    proxy=proxy_url,
                    timeout=request_timeout,
                    impersonate="chrome124",
                    allow_redirects=False,
                )

            duration_ms = (time.time() - start_time) * 1000
            if config.debug_enabled:
                debug_logger.log_response(
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    body=response.text,
                    duration_ms=duration_ms,
                )

            if 300 <= response.status_code < 400:
                location = response.headers.get("Location") or response.headers.get("location")
                if location:
                    return urljoin(url, location)

            final_url = str(getattr(response, "url", "") or "").strip()
            if response.status_code == 200 and final_url and final_url != url:
                return final_url

            raise RuntimeError(
                f"media.getMediaUrlRedirect returned unexpected status={response.status_code} "
                f"for media={normalized_media_name}"
            )
        except Exception as e:
            raise RuntimeError(
                f"Failed to get media redirect URL: media={normalized_media_name}, type={media_url_type}, error={e}"
            ) from e

    # ========== Helpers ==========

    async def _maybe_switch_to_server_mint(self, error: BaseException, log_prefix: str) -> None:
        # 2026-09-23: Google refused a token the worker extension minted (old
        # extension, or a new Google rule). The rest of THIS request mints on the
        # server, if the fallback can serve this account. Never for quota, prompt
        # refusals or other 403s: only a transport-classified reCAPTCHA rejection.
        if (
            self._is_recaptcha_rejection(error)
            and self._mint_source_ctx.get() == "extension"
            and self._mint_override_ctx.get() != "server"
            and await self._server_fallback_can_serve(self._current_token_id_ctx.get())
        ):
            self._mint_override_ctx.set("server")
            debug_logger.event(f"{log_prefix}extension token rejected by Google; remaining mints of this request go to the server fallback")

    async def _handle_retryable_generation_error(
        self,
        error: Exception,
        retry_attempt: int,
        max_retries: int,
        browser_id: Optional[Union[int, str]],
        project_id: str,
        log_prefix: str,
        defer_browser_error_notification: bool = False,
    ) -> bool:
        """Shared retry decision for generation, plus captcha self-heal notification."""
        error_str = str(error)
        await self._maybe_switch_to_server_mint(error, log_prefix)
        retry_reason = self._get_retry_reason(error_str)
        retry_delay = self._get_retry_delay_seconds(error_str, retry_attempt)

        effective_max_retries = self._resolve_generation_retry_budget(max_retries, error_str)
        is_terminal_attempt = retry_attempt >= effective_max_retries - 1
        should_notify_browser = (
            not defer_browser_error_notification
            or not retry_reason
            or is_terminal_attempt
        )

        if should_notify_browser:
            notify_reason = retry_reason or error_str[:120] or type(error).__name__
            await self._notify_browser_captcha_error(
                browser_id=browser_id,
                project_id=project_id,
                error_reason=notify_reason,
                error_message=error_str,
            )

        if not retry_reason:
            return False

        if is_terminal_attempt:
            debug_logger.log_warning(
                f"{log_prefix}hit {retry_reason}, max retries reached ({effective_max_retries}); request failed, closing and cleaning up."
            )
            return False

        debug_logger.log_warning(
            f"{log_prefix}hit {retry_reason}, getting a new captcha and retrying in {retry_delay}s ({retry_attempt + 2}/{effective_max_retries})..."
        )
        await asyncio.sleep(retry_delay)
        return True

    async def _handle_missing_recaptcha_token(
        self,
        retry_attempt: int,
        max_retries: int,
        browser_id: Optional[Union[int, str]],
        project_id: str,
        log_prefix: str,
    ) -> bool:
        token_error = Exception("Failed to obtain reCAPTCHA token")
        return await self._handle_retryable_generation_error(
            error=token_error,
            retry_attempt=retry_attempt,
            max_retries=max_retries,
            browser_id=browser_id,
            project_id=project_id,
            log_prefix=log_prefix,
        )

    def _get_retry_reason(self, error_str: str) -> Optional[str]:
        """Decide whether to retry; returns the reason text for logs"""
        error_lower = error_str.lower()
        if "error_no_slot_available_block" in error_lower:
            return "captcha solver blocked (no slot)"
        if "error_no_slot_available" in error_lower:
            return "captcha solver out of slots"
        if "403" in error_lower:
            return "403 error"
        if "429" in error_lower or "too many requests" in error_lower:
            return "429 rate limit"
        if self._is_retryable_network_error(error_str):
            return "network/TLS error"
        if "recaptcha evaluation failed" in error_lower:
            return "reCAPTCHA verification failed"
        if "recaptcha" in error_lower:
            return "reCAPTCHA error"
        if any(keyword in error_lower for keyword in [
            "http error 500",
            "public_error",
            "internal error",
            "reason=internal",
            "reason: internal",
            "\"reason\":\"internal\"",
            "server error",
            "upstream error",
        ]):
            return "500/internal error"
        return None

    def _get_retry_delay_seconds(self, error_str: str, retry_attempt: int) -> int:
        error_lower = str(error_str or "").lower()
        if "error_no_slot_available_block" in error_lower:
            return 20
        if "error_no_slot_available" in error_lower:
            index = max(0, min(retry_attempt, len(self.YESCAPTCHA_SLOT_BACKOFF_SECONDS) - 1))
            return self.YESCAPTCHA_SLOT_BACKOFF_SECONDS[index]
        if "recaptcha evaluation failed" in error_lower:
            return 2
        if "recaptcha" in error_lower:
            return 2
        return 1

    def _resolve_recaptcha_runtime_settings(
        self,
        method: str,
        action: str,
    ) -> Dict[str, Any]:
        normalized_action = str(action or "IMAGE_GENERATION").strip() or "IMAGE_GENERATION"
        website_key = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"

        if method == "yescaptcha":
            page_action = normalized_action
            task_type = config.yescaptcha_task_type
            min_score = get_yescaptcha_min_score(task_type)
        elif method == "capmonster":
            page_action = normalized_action
            task_type = "RecaptchaV3TaskProxyless"
            min_score = None
        elif method == "ezcaptcha":
            page_action = normalized_action
            task_type = "ReCaptchaV3TaskProxylessS9"
            min_score = None
        elif method == "capsolver":
            page_action = normalized_action
            task_type = "ReCaptchaV3EnterpriseTaskProxyLess"
            min_score = None
        else:
            page_action = normalized_action
            task_type = None
            min_score = None

        return {
            "website_key": website_key,
            "page_action": page_action,
            "task_type": task_type,
            "min_score": min_score,
        }

    def _merge_request_fingerprint(self, patch: Optional[Dict[str, Any]]):
        if not isinstance(patch, dict) or not patch:
            return
        current = self.get_request_fingerprint() or {}
        merged = dict(current)
        for key, value in patch.items():
            if value is None:
                continue
            if isinstance(value, str):
                normalized = value.strip()
                if not normalized:
                    continue
                merged[key] = normalized
            else:
                merged[key] = value
        self._set_request_fingerprint(merged if merged else None)

    async def _notify_browser_captcha_error(
        self,
        browser_id: Optional[Union[int, str]] = None,
        project_id: Optional[str] = None,
        error_reason: Optional[str] = None,
        error_message: Optional[str] = None,
    ):
        """Tell the browser captcha service to self-heal after a failure.
        
        Args:
            browser_id: browser ID (browser mode)
            project_id: project_id (personal mode)
            error_reason: classified error reason
            error_message: raw error text
        """
        if config.captcha_method == "browser":
            try:
                from .browser_captcha import BrowserCaptchaService
                service = await BrowserCaptchaService.get_instance(self.db)
                await service.report_error(
                    browser_id,
                    error_reason=error_reason or error_message or "upstream_error"
                )
            except Exception:
                pass
        elif config.captcha_method == "extension":
            try:
                from .browser_captcha_extension import ExtensionCaptchaService
                service = await ExtensionCaptchaService.get_instance()
                await service.report_flow_error(
                    project_id=project_id,
                    error_reason=error_reason or "",
                    error_message=error_message or "",
                )
            except Exception:
                pass
        elif config.captcha_method == "personal" and project_id:
            try:
                from .browser_captcha_personal import BrowserCaptchaService
                service = await BrowserCaptchaService.get_instance(self.db)
                await service.report_flow_error(
                    project_id=project_id,
                    error_reason=error_reason or "",
                    error_message=error_message or "",
                )
            except Exception:
                pass
        elif config.captcha_method == "remote_browser" and browser_id:
            try:
                session_id = quote(str(browser_id), safe="")
                await self._call_remote_browser_service(
                    method="POST",
                    path=f"/api/v1/sessions/{session_id}/error",
                    json_data={"error_reason": error_reason or error_message or "upstream_error"},
                    timeout_override=2,
                )
            except Exception as e:
                debug_logger.log_warning(f"[reCAPTCHA RemoteBrowser] Failed to report error: {e}")

    async def _notify_browser_captcha_request_finished(self, browser_id: Optional[Union[int, str]] = None):
        """Tell the headed browser the upstream image/video request is done, so its captcha browser can close."""
        if config.captcha_method == "browser":
            try:
                from .browser_captcha import BrowserCaptchaService
                service = await BrowserCaptchaService.get_instance(self.db)
                await service.report_request_finished(browser_id)
            except Exception:
                pass
        elif config.captcha_method == "remote_browser" and browser_id:
            try:
                session_id = quote(str(browser_id), safe="")
                await self._call_remote_browser_service(
                    method="POST",
                    path=f"/api/v1/sessions/{session_id}/finish",
                    json_data={"status": "success"},
                    timeout_override=2,
                )
            except Exception as e:
                debug_logger.log_warning(f"[reCAPTCHA RemoteBrowser] Failed to report finish: {e}")

    def _generate_session_id(self) -> str:
        """Build sessionId: ;timestamp"""
        return f";{int(time.time() * 1000)}"

    def _generate_scene_id(self) -> str:
        """Build sceneId: UUID"""
        return str(uuid.uuid4())

    def _get_remote_browser_service_config(self) -> tuple[str, str, int]:
        base_url = (config.remote_browser_base_url or "").strip().rstrip("/")
        api_key = (config.remote_browser_api_key or "").strip()
        timeout = max(5, int(config.remote_browser_timeout or 60))

        if not base_url:
            raise RuntimeError("remote_browser service URL is not configured")
        if not api_key:
            raise RuntimeError("remote_browser API Key is not configured")

        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            raise RuntimeError("remote_browser service URL is malformed")

        return base_url, api_key, timeout

    @staticmethod
    def _build_remote_browser_http_timeout(read_timeout: float) -> Any:
        read_value = max(3.0, float(read_timeout))
        write_value = min(10.0, max(3.0, read_value))
        if httpx is None:
            return read_value
        return httpx.Timeout(
            connect=2.5,
            read=read_value,
            write=write_value,
            pool=2.5,
        )

    @staticmethod
    def _parse_json_response_text(text: str) -> Optional[Any]:
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            return None

    @staticmethod
    async def _stdlib_json_http_request(
        method: str,
        url: str,
        headers: Dict[str, str],
        payload: Optional[Dict[str, Any]],
        timeout: int,
    ) -> tuple[int, Optional[Any], str]:
        req_headers = dict(headers or {})
        req_headers.setdefault("Accept", "application/json")
        request_method = (method or "GET").upper()
        request_data: Optional[bytes] = None

        if payload is not None:
            req_headers["Content-Type"] = "application/json; charset=utf-8"
            if request_method != "GET":
                request_data = json.dumps(payload).encode("utf-8")

        def do_request() -> tuple[int, str]:
            request = urllib.request.Request(
                url=url,
                data=request_data,
                headers=req_headers,
                method=request_method,
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with opener.open(request, timeout=max(1.0, float(timeout))) as response:
                    status_code = int(getattr(response, "status", 0) or response.getcode() or 0)
                    body = response.read()
                    charset = response.headers.get_content_charset() or "utf-8"
                    return status_code, body.decode(charset, errors="replace")
            except urllib.error.HTTPError as exc:
                body = exc.read()
                charset = exc.headers.get_content_charset() if exc.headers else None
                return int(getattr(exc, "code", 0) or 0), body.decode(charset or "utf-8", errors="replace")

        try:
            status_code, text = await asyncio.to_thread(do_request)
        except Exception as e:
            raise RuntimeError(f"remote_browser request failed: {e}") from e

        return status_code, FlowClient._parse_json_response_text(text), text

    @staticmethod
    async def _sync_json_http_request(
        method: str,
        url: str,
        headers: Dict[str, str],
        payload: Optional[Dict[str, Any]],
        timeout: int,
    ) -> tuple[int, Optional[Any], str]:
        req_headers = dict(headers or {})
        req_headers.setdefault("Accept", "application/json")
        request_method = (method or "GET").upper()
        request_kwargs: Dict[str, Any] = {
            "headers": req_headers,
            "timeout": FlowClient._build_remote_browser_http_timeout(timeout),
        }

        if payload is not None:
            req_headers["Content-Type"] = "application/json; charset=utf-8"
            if request_method != "GET":
                request_kwargs["json"] = payload

        if httpx is None:
            return await FlowClient._stdlib_json_http_request(
                method=method,
                url=url,
                headers=req_headers,
                payload=payload,
                timeout=timeout,
            )

        try:
            # The remote_browser control plane only needs reliable JSON, no fingerprint spoofing.
            # httpx avoids curl_cffi dropping the POST body in this environment.
            async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as session:
                response = await session.request(
                    method=request_method,
                    url=url,
                    **request_kwargs,
                )
        except Exception as e:
            raise RuntimeError(f"remote_browser request failed: {e}") from e

        status_code = int(getattr(response, "status_code", 0) or 0)
        text = response.text or ""
        parsed = FlowClient._parse_json_response_text(text)

        return status_code, parsed, text

    async def _call_remote_browser_service(
        self,
        method: str,
        path: str,
        json_data: Optional[Dict[str, Any]] = None,
        timeout_override: Optional[int] = None,
    ) -> Dict[str, Any]:
        base_url, api_key, timeout = self._get_remote_browser_service_config()
        url = f"{base_url}{path}"
        effective_timeout = max(5, int(timeout_override or timeout))

        status_code, payload, response_text = await self._sync_json_http_request(
            method=method,
            url=url,
            headers={"Authorization": f"Bearer {api_key}"},
            payload=json_data,
            timeout=effective_timeout,
        )

        if status_code >= 400:
            detail = ""
            if isinstance(payload, dict):
                detail = payload.get("detail") or payload.get("message") or str(payload)
            if not detail:
                detail = (response_text or "").strip() or f"HTTP {status_code}"
            raise RuntimeError(f"remote_browser request failed: {detail}")

        if not isinstance(payload, dict):
            raise RuntimeError("remote_browser returned an invalid format")

        return payload

    async def prefill_remote_browser_pool(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: Optional[int] = None,
        *,
        cooldown_seconds: float = 8.0,
    ) -> bool:
        """Ask the local remote_browser service to refill its pool early, so token wait happens sooner."""
        if config.captcha_method != "remote_browser":
            return False

        normalized_project = str(project_id or "").strip()
        normalized_action = str(action or "IMAGE_GENERATION").strip() or "IMAGE_GENERATION"
        if not normalized_project:
            return False

        cache_key = f"{normalized_project}|{normalized_action}|{int(token_id or 0)}"
        now_value = time.monotonic()
        last_sent = float(self._remote_browser_prefill_last_sent.get(cache_key, 0.0) or 0.0)
        if (now_value - last_sent) < max(0.5, float(cooldown_seconds)):
            return False

        try:
            await self._call_remote_browser_service(
                method="POST",
                path="/api/v1/prefill",
                json_data={
                    "project_id": normalized_project,
                    "action": normalized_action,
                    "token_id": token_id,
                },
                timeout_override=3,
            )
            self._remote_browser_prefill_last_sent[cache_key] = now_value
            return True
        except Exception as e:
            debug_logger.log_warning(f"[reCAPTCHA RemoteBrowser] prefill failed: {e}")
            return False

    async def prefill_remote_browser_for_tokens(self, tokens: List[Any], action: str = "IMAGE_GENERATION") -> int:
        if config.captcha_method != "remote_browser":
            return 0

        unique_projects: List[str] = []
        seen_projects = set()
        for token in tokens or []:
            project_id = str(getattr(token, "current_project_id", "") or "").strip()
            if not project_id or project_id in seen_projects:
                continue
            seen_projects.add(project_id)
            unique_projects.append(project_id)

        warmed = 0
        for project_id in unique_projects:
            if await self.prefill_remote_browser_pool(project_id, action=action):
                warmed += 1
        return warmed

    def _resolve_remote_browser_solve_timeout(self, action: str) -> int:
        base_timeout = max(5, int(config.remote_browser_timeout or 60))
        action_name = str(action or "").strip().upper()

        # This only fetches a reCAPTCHA token; it should not share the generation timeout of hundreds of seconds.
        target_timeout = 45 if action_name == "VIDEO_GENERATION" else 35
        return max(12, min(base_timeout, target_timeout))

    async def _server_fallback_can_serve(self, token_id: Optional[int]) -> bool:
        """Flag on, this image has Chromium, and the account has its own proxy."""
        if not config.captcha_server_fallback_enabled or not token_id or not self.db:
            return False
        try:
            from .flow_page_captcha import FlowPageCaptchaService
            service = await FlowPageCaptchaService.get_instance()
            if not service.is_available():
                return False
            token_row = await self.db.get_token(token_id)
            return bool((getattr(token_row, "redeem_proxy_url", None) or "").strip()) if token_row else False
        except Exception:
            return False

    async def _server_fallback_mint(self, action: str, token_id: Optional[int], why: str = "") -> Optional[str]:
        """Server-side reCAPTCHA mint for this account (see flow_page_captcha). Returns the
        token and binds the redeem fingerprint (account proxy + the server Chromium's UA)."""
        if not config.captcha_server_fallback_enabled or not token_id or not self.db:
            return None
        try:
            from .flow_page_captcha import FlowPageCaptchaService
            service = await FlowPageCaptchaService.get_instance()
            if not service.is_available():
                return None
            token_row = await self.db.get_token(token_id)
            proxy_url = (getattr(token_row, "redeem_proxy_url", None) or "").strip() if token_row else ""
            if not proxy_url:
                debug_logger.op_warning(f"[FALLBACK_MINT] token={token_id} skipped: account has no redeem proxy ({why[:80]})")
                return None
            result = await service.mint(action, proxy_url, token_id=token_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            debug_logger.op_warning(f"[FALLBACK_MINT] token={token_id} error {type(exc).__name__}: {str(exc)[:160]}")
            return None
        if not result or not result.get("token"):
            return None
        fingerprint: Dict[str, Any] = {"proxy_url": proxy_url}
        if result.get("user_agent"):
            fingerprint["user_agent"] = result["user_agent"]
        self._set_request_fingerprint(fingerprint)
        self._mint_source_ctx.set("server")
        debug_logger.event(
            f"[REDEEM_ALIGN] token={token_id} egress={mask_proxy_url(proxy_url)} ua_set={bool(result.get('user_agent'))} source=server"
        )
        return result["token"]

    async def _build_extension_redeem_fingerprint(
        self, token_id: Optional[int]
    ) -> Optional[Dict[str, Any]]:
        """Slice B: build the redeem fingerprint (residential proxy + real browser UA)
        for this account from what its worker extension reported. Returns None when the
        account has reported nothing yet, so the caller falls back to the global proxy
        (no regression). _make_request consumes fingerprint['proxy_url'] and ['user_agent'].
        """
        if not token_id or not self.db:
            return None
        try:
            token = await self.db.get_token(token_id)
        except Exception:
            return None
        if not token:
            return None
        fp: Dict[str, Any] = {}
        proxy = (getattr(token, "redeem_proxy_url", None) or "").strip()
        ua = (getattr(token, "browser_user_agent", None) or "").strip()
        if proxy:
            fp["proxy_url"] = proxy
        if ua:
            fp["user_agent"] = ua
        return fp or None

    async def _get_recaptcha_token(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        token_id: Optional[int] = None
    ) -> tuple[Optional[str], Optional[Union[int, str]]]:
        """Get a reCAPTCHA token - supports several captcha methods
        
        Args:
            project_id: project ID
            action: reCAPTCHA action type
                - IMAGE_GENERATION: image generation and 2K/4K image upscale (default)
                - VIDEO_GENERATION: video generation and video upscale
            token_id: current token id (browser mode uses it to read the per-token captcha proxy)
        
        Returns:
            (token, browser_id) tuple.
            - browser mode: browser_id is the local browser ID
            - remote_browser mode: browser_id is the remote session_id
            - other modes: browser_id is None
        """
        captcha_method = config.captcha_method
        debug_logger.log_info(f"[reCAPTCHA] Getting token: method={captcha_method}, project_id={project_id}, action={action}")

        if captcha_method == "extension":
            token = None
            extension_error: Optional[str] = None
            if token_id:
                self._current_token_id_ctx.set(token_id)
            if self._mint_override_ctx.get() != "server":
                try:
                    from .browser_captcha_extension import ExtensionCaptchaService
                    service = await ExtensionCaptchaService.get_instance(self.db)
                    # Video mints take longer in the browser (extension waits up to 60 s for
                    # VIDEO_GENERATION since ext 3.3.6); give the server side headroom.
                    extension_timeout = 75 if action == "VIDEO_GENERATION" else 25
                    token = await service.get_token(
                        project_id,
                        action,
                        timeout=extension_timeout,
                        token_id=token_id
                    )
                except Exception as e:
                    extension_error = str(e)
                    debug_logger.log_error(f"[reCAPTCHA Extension] Error: {extension_error}")
                if token:
                    # Slice B — align REDEEM with MINT: make the generate request exit the SAME
                    # residential IP + use the SAME browser UA that the extension used to mint the
                    # reCAPTCHA. If the account hasn't reported them, this is None and the redeem
                    # falls back to the global request proxy (WARP) exactly as before (no regression).
                    redeem_fp = await self._build_extension_redeem_fingerprint(token_id)
                    self._set_request_fingerprint(redeem_fp)
                    self._mint_source_ctx.set("extension")
                    if redeem_fp:
                        debug_logger.event(
                            f"[REDEEM_ALIGN] token={token_id} egress={mask_proxy_url(redeem_fp.get('proxy_url'))} "
                            f"ua_set={bool(redeem_fp.get('user_agent'))}"
                        )
                    return token, None
            # 2026-09-23 server-side fallback: the worker is offline, could not mint, or
            # its token was refused earlier in this request. Mint on flow.google.com in a
            # server Chromium that egresses through the account's own proxy; redeem must
            # then use THAT Chromium's UA (a mismatched UA is refused).
            fallback = await self._server_fallback_mint(action, token_id, why=extension_error or "extension mint failed")
            if fallback:
                return fallback, "server"
            self._set_request_fingerprint(None)
            return None, None

        # Built-in browser captcha (nodriver)
        if captcha_method == "personal":
            debug_logger.log_info(f"[reCAPTCHA] Using personal mode")
            try:
                from .browser_captcha_personal import BrowserCaptchaService
                debug_logger.log_info(f"[reCAPTCHA] Imported BrowserCaptchaService")
                service = await BrowserCaptchaService.get_instance(self.db)
                debug_logger.log_info(f"[reCAPTCHA] Got service instance, calling get_token")
                solve_bundle = None
                get_token_bundle = getattr(service, "get_token_bundle", None)
                if callable(get_token_bundle):
                    solve_bundle = await get_token_bundle(
                        project_id,
                        action,
                        token_id=token_id,
                    )
                    token = str((solve_bundle or {}).get("token") or "").strip() or None
                else:
                    get_token_with_metadata = getattr(service, "get_token_with_metadata", None)
                    if callable(get_token_with_metadata):
                        token, _slot_id, _cookie_source_token_id = await get_token_with_metadata(
                            project_id,
                            action,
                            token_id=token_id,
                        )
                    else:
                        token = await service.get_token(project_id, action, token_id=token_id)
                    solve_bundle = {
                        "token": token,
                        "fingerprint": service.get_last_fingerprint() if token else None,
                    } if token else None
                debug_logger.log_info(f"[reCAPTCHA] get_token returned: {token[:50] if token else None}...")
                fingerprint = (
                    solve_bundle.get("fingerprint")
                    if isinstance(solve_bundle, dict) and isinstance(solve_bundle.get("fingerprint"), dict)
                    else None
                )
                if isinstance(solve_bundle, dict) and token:
                    session_cookies = solve_bundle.get("session_cookies")
                    proxy_url = str(solve_bundle.get("proxy_url") or "").strip()
                    next_fingerprint = dict(fingerprint or {})
                    if isinstance(session_cookies, dict) and session_cookies:
                        next_fingerprint["session_cookies"] = dict(session_cookies)
                    if proxy_url and not str(next_fingerprint.get("proxy_url") or "").strip():
                        next_fingerprint["proxy_url"] = proxy_url
                    next_fingerprint["project_id"] = project_id
                    next_fingerprint.setdefault("origin", "https://labs.google")
                    next_fingerprint.setdefault("referer", self._build_flow_project_page_url(project_id))
                    fingerprint = next_fingerprint or None
                if token:
                    effective_ua = str((fingerprint or {}).get("user_agent") or "").strip()
                    effective_lang = str((fingerprint or {}).get("accept_language") or "").strip()
                    if not effective_ua:
                        debug_logger.log_warning(
                            "[reCAPTCHA Personal] Got a token but no browser fingerprint UA; "
                            "dropping this token and retrying so submit and captcha environments match"
                        )
                        self._set_request_fingerprint(None)
                        return None, None
                    debug_logger.log_info(
                        "[reCAPTCHA Personal] Using browser fingerprint: "
                        f"UA={effective_ua[:120]}, Accept-Language={effective_lang or '<empty>'}"
                    )
                self._set_request_fingerprint(fingerprint if token else None)
                return token, None
            except RuntimeError as e:
                # Catch clear errors from Docker or missing dependencies
                error_msg = str(e)
                debug_logger.log_error(f"[reCAPTCHA Personal] {error_msg}")
                print(f"[reCAPTCHA] ❌ Built-in browser captcha failed: {error_msg}")
                self._set_request_fingerprint(None)
                return None, None
            except ImportError as e:
                debug_logger.log_error(f"[reCAPTCHA Personal] Import failed: {str(e)}")
                print(f"[reCAPTCHA] ❌ nodriver is not installed, run: pip install nodriver")
                self._set_request_fingerprint(None)
                return None, None
            except Exception as e:
                debug_logger.log_error(f"[reCAPTCHA Personal] Error: {str(e)}")
                self._set_request_fingerprint(None)
                return None, None
        # Headed browser captcha (playwright)
        elif captcha_method == "browser":
            try:
                from .browser_captcha import BrowserCaptchaService
                service = await BrowserCaptchaService.get_instance(self.db)
                token, browser_id = await service.get_token(project_id, action, token_id=token_id)
                fingerprint = await service.get_fingerprint(browser_id) if token else None
                self._set_request_fingerprint(fingerprint if token else None)
                return token, browser_id
            except RuntimeError as e:
                # Catch clear errors from Docker or missing dependencies
                error_msg = str(e)
                debug_logger.log_error(f"[reCAPTCHA Browser] {error_msg}")
                print(f"[reCAPTCHA] ❌ Headed browser captcha failed: {error_msg}")
                self._set_request_fingerprint(None)
                return None, None
            except ImportError as e:
                debug_logger.log_error(f"[reCAPTCHA Browser] Import failed: {str(e)}")
                print(f"[reCAPTCHA] ❌ playwright is not installed, run: pip install playwright && python -m playwright install chromium")
                self._set_request_fingerprint(None)
                return None, None
            except Exception as e:
                debug_logger.log_error(f"[reCAPTCHA Browser] Error: {str(e)}")
                self._set_request_fingerprint(None)
                return None, None
        elif captcha_method == "remote_browser":
            try:
                solve_timeout = self._resolve_remote_browser_solve_timeout(action)
                payload = await self._call_remote_browser_service(
                    method="POST",
                    path="/api/v1/solve",
                    json_data={
                        "project_id": project_id,
                        "action": action,
                        "token_id": token_id,
                    },
                    timeout_override=solve_timeout,
                )
                token = payload.get("token")
                session_id = payload.get("session_id")
                fingerprint = payload.get("fingerprint") if isinstance(payload.get("fingerprint"), dict) else None
                self._set_request_fingerprint(fingerprint if token else None)
                if not token or not session_id:
                    raise RuntimeError(f"remote_browser response missing token/session_id: {payload}")
                return token, str(session_id)
            except Exception as e:
                debug_logger.log_error(f"[reCAPTCHA RemoteBrowser] Error: {str(e)}")
                self._set_request_fingerprint(None)
                return None, None
        # API captcha services
        elif captcha_method in ["yescaptcha", "capmonster", "ezcaptcha", "capsolver"]:
            proxy_url = None
            if self.proxy_manager:
                try:
                    proxy_url = await self.proxy_manager.get_request_proxy_url()
                except Exception as e:
                    debug_logger.log_warning(f"[reCAPTCHA] Failed to get proxy for API captcha: {e}")

            api_captcha_ua = self._generate_user_agent(str(token_id or project_id or captcha_method))
            self._set_request_fingerprint(
                self._build_fingerprint_from_user_agent(
                    api_captcha_ua,
                    accept_language=self._get_primary_accept_language(),
                    proxy_url=proxy_url,
                )
            )
            self._merge_request_fingerprint(
                {
                    "project_id": project_id,
                    "origin": "https://labs.google",
                    "referer": self._build_flow_project_page_url(project_id),
                }
            )

            api_result = await self._get_api_captcha_token(
                captcha_method,
                project_id,
                action,
                proxy_url=proxy_url,
                user_agent=api_captcha_ua,
            )
            if api_result is None:
                return None, None
            token, captcha_user_agent = api_result
            # Merge the solver's userAgent into the fingerprint so Flow API submits use the same UA.
            # Otherwise Google reCAPTCHA V3 flags the UA mismatch as UNUSUAL_ACTIVITY and returns
            # "reCAPTCHA evaluation failed". Other Client Hints (sec-ch-ua-platform, etc.)
            # are inferred from the UA by _make_request (Windows → "Windows", etc.).
            if captcha_user_agent:
                existing_fp = self._request_fingerprint_ctx.get()
                merged_fp = dict(existing_fp) if isinstance(existing_fp, dict) else {}
                merged_fp["user_agent"] = captcha_user_agent
                self._set_request_fingerprint(merged_fp)
                debug_logger.log_info(
                    f"[reCAPTCHA {captcha_method}] Injected captcha UA into request fingerprint: "
                    f"{captcha_user_agent[:80]}"
                )
            return token, None
        else:
            debug_logger.log_info(f"[reCAPTCHA] Unknown captcha method: {captcha_method}")
            self._set_request_fingerprint(None)
            return None, None

    async def _get_api_captcha_token(
        self,
        method: str,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        proxy_url: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Optional[tuple[str, str]]:
        """Generic API captcha service

        Args:
            method: captcha service type
            project_id: project ID
            action: reCAPTCHA action type (IMAGE_GENERATION or VIDEO_GENERATION)
            proxy_url: pre-resolved proxy URL, so captcha and submit use the same egress
            user_agent: UA passed to the solver, so solve and submit environments match

        Returns:
            (gRecaptchaResponse, userAgent) tuple, or None on failure.
            userAgent is returned so later Flow API requests reuse the solve UA
            and reCAPTCHA V3 does not flag a UA mismatch as UNUSUAL_ACTIVITY.
        """
        # Load config
        if method == "yescaptcha":
            client_key = config.yescaptcha_api_key
            base_url = config.yescaptcha_base_url
        elif method == "capmonster":
            client_key = config.capmonster_api_key
            base_url = config.capmonster_base_url
        elif method == "ezcaptcha":
            client_key = config.ezcaptcha_api_key
            base_url = config.ezcaptcha_base_url
        elif method == "capsolver":
            client_key = config.capsolver_api_key
            base_url = config.capsolver_base_url
        else:
            debug_logger.log_error(f"[reCAPTCHA] Unknown API method: {method}")
            return None

        if not client_key:
            debug_logger.log_info(f"[reCAPTCHA] {method} API key not configured, skipping")
            return None

        runtime_settings = self._resolve_recaptcha_runtime_settings(method, action)
        task_type = runtime_settings["task_type"]
        min_score = runtime_settings["min_score"]
        website_key = runtime_settings["website_key"]
        website_url = self._build_flow_project_page_url(project_id)
        page_action = runtime_settings["page_action"]

        try:
            # Load proxy config so captcha API calls also use the proxy
            # Note: curl_cffi uses the proxy param for SOCKS5 and proxies for HTTP
            proxy = None
            proxies = None
            resolved_proxy_url = str(proxy_url or "").strip()
            if not resolved_proxy_url and self.proxy_manager:
                try:
                    resolved_proxy_url = str(await self.proxy_manager.get_request_proxy_url() or "").strip()
                    if resolved_proxy_url:
                        if resolved_proxy_url.startswith("socks5://"):
                            # curl_cffi uses the proxy param for SOCKS5
                            proxy = resolved_proxy_url
                        else:
                            # HTTP/HTTPS proxies use the proxies dict
                            proxies = {"http": resolved_proxy_url, "https": resolved_proxy_url}
                except Exception as e:
                    debug_logger.log_warning(f"[reCAPTCHA {method}] Failed to get proxy: {e}")
            elif resolved_proxy_url:
                if resolved_proxy_url.startswith("socks5://"):
                    proxy = resolved_proxy_url
                else:
                    proxies = {"http": resolved_proxy_url, "https": resolved_proxy_url}
            
            async with AsyncSession() as session:
                create_url = f"{base_url}/createTask"
                create_data = {
                    "clientKey": client_key,
                    "task": {
                        "websiteURL": website_url,
                        "websiteKey": website_key,
                        "type": task_type,
                        "pageAction": page_action
                    }
                }
                effective_user_agent = str(
                    user_agent
                    or (self.get_request_fingerprint() or {}).get("user_agent")
                    or self._generate_user_agent(str(project_id or method))
                ).strip()
                if effective_user_agent:
                    create_data["task"]["userAgent"] = effective_user_agent
                if min_score is not None:
                    create_data["task"]["minScore"] = min_score

                task_id = None
                last_create_error = None
                max_create_attempts = len(self.YESCAPTCHA_SLOT_BACKOFF_SECONDS) if method == "yescaptcha" else 1
                for create_attempt in range(max_create_attempts):
                    if proxy:
                        result = await session.post(create_url, json=create_data, impersonate="chrome124", proxy=proxy)
                    else:
                        result = await session.post(create_url, json=create_data, impersonate="chrome124", proxies=proxies)

                    debug_logger.log_info(f"[reCAPTCHA {method}] createTask response status: {result.status_code}")
                    result_json = result.json()
                    task_id = result_json.get('taskId')

                    debug_logger.log_info(f"[reCAPTCHA {method}] created task_id: {task_id}, response: {result_json}")

                    if task_id:
                        break

                    error_code = str(result_json.get("errorCode") or "").strip()
                    error_desc = result_json.get('errorDescription', 'Unknown error')
                    last_create_error = result_json
                    if method == "yescaptcha" and error_code in {"ERROR_NO_SLOT_AVAILABLE", "ERROR_NO_SLOT_AVAILABLE_BLOCK"}:
                        delay = self._get_retry_delay_seconds(error_code, create_attempt)
                        if create_attempt < max_create_attempts - 1:
                            debug_logger.log_warning(
                                f"[reCAPTCHA {method}] createTask out of slots ({error_code}), retrying in {delay}s "
                                f"({create_attempt + 2}/{max_create_attempts})"
                            )
                            await asyncio.sleep(delay)
                            continue
                    debug_logger.log_error(f"[reCAPTCHA {method}] Failed to create task: {error_desc}")
                    return None

                if not task_id:
                    error_desc = (last_create_error or {}).get('errorDescription', 'Unknown error')
                    debug_logger.log_error(f"[reCAPTCHA {method}] Failed to create task: {error_desc}")
                    return None

                get_url = f"{base_url}/getTaskResult"
                for i in range(40):
                    get_data = {
                        "clientKey": client_key,
                        "taskId": task_id
                    }
                    # Use different params per proxy type
                    if proxy:
                        result = await session.post(get_url, json=get_data, impersonate="chrome124", proxy=proxy)
                    else:
                        result = await session.post(get_url, json=get_data, impersonate="chrome124", proxies=proxies)
                    result_json = result.json()

                    debug_logger.log_info(f"[reCAPTCHA {method}] polling #{i+1}: {result_json}")

                    status = result_json.get('status')
                    if status == 'ready':
                        solution = result_json.get('solution', {})
                        response = solution.get('gRecaptchaResponse')
                        if response:
                            self._merge_request_fingerprint(
                                self._build_fingerprint_from_user_agent(
                                    solution.get("userAgent"),
                                    accept_language=self._get_primary_accept_language(),
                                    proxy_url=resolved_proxy_url,
                                )
                            )
                            debug_logger.log_info(f"[reCAPTCHA {method}] Token obtained")
                            # Also return userAgent; the caller injects it into the fingerprint context
                            # so Flow API submits use the same UA as the solve.
                            user_agent = solution.get('userAgent')
                            return response, user_agent

                    await asyncio.sleep(3)

                debug_logger.log_error(f"[reCAPTCHA {method}] Timeout waiting for token")
                return None

        except Exception as e:
            debug_logger.log_error(f"[reCAPTCHA {method}] error: {str(e)}")
            return None


