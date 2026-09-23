"""Admin API routes"""
import asyncio
import importlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from fastapi import APIRouter, Depends, HTTPException, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import secrets
import time
import re
import urllib.error
import urllib.request
from urllib.parse import urlparse
from curl_cffi.requests import AsyncSession
from ..core.auth import AuthManager
from ..core.database import Database
from ..core.config import config, get_yescaptcha_min_score, normalize_yescaptcha_task_type
from ..core.models import Token
from ..core.client_policy import (
    DEFAULT_CLIENT,
    TIER_RULES,
    client_block_reason,
    client_policy_store,
    normalize_client,
    normalize_rule,
)
from ..core.browser_runtime_status import (
    fail_runtime_prepare,
    finish_runtime_prepare,
    get_runtime_status,
    progress_runtime_prepare,
    start_runtime_prepare,
)
from ..core.monitoring import build_public_health_snapshot
from ..core.logger import debug_logger, mask_proxy_url
from ..services.token_manager import TokenManager
from ..services.protocol_login import _parse_google_cookies, google_cookies_usable
from ..services.proxy_manager import ProxyManager
from ..services.concurrency_manager import ConcurrencyManager

try:
    import httpx
except ImportError:
    httpx = None

router = APIRouter()

# Dependency injection
token_manager: TokenManager = None
proxy_manager: ProxyManager = None
db: Database = None
concurrency_manager: Optional[ConcurrencyManager] = None
captcha_runtime_prepare_tasks: Dict[str, asyncio.Task] = {}

# Store active admin session tokens (in production, use Redis or database)
active_admin_tokens = set()
ADMIN_SESSION_COOKIE_NAME = "admin_session"
# Persistent, not a browser-session cookie. Without max_age the cookie died when
# Chrome closed while the bearer copy in localStorage lived on, and /login (bearer
# says logged in → go to /manage) and /manage (cookie says not → back to /login)
# bounced forever. See _ensure_admin_page_session in main.py for the loop breaker.
ADMIN_SESSION_COOKIE_MAX_AGE = 7 * 24 * 3600
SUPPORTED_API_CAPTCHA_METHODS = {"yescaptcha", "capmonster", "ezcaptcha", "capsolver"}


def _mask_token(token: Optional[str]) -> str:
    if not token:
        return ""
    if len(token) <= 24:
        return token
    return f"{token[:18]}...{token[-8:]}"


def _truncate_text(text: Any, limit: int = 240) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return f"{value[:limit - 3]}..."


def _extract_error_summary(payload: Any) -> str:
    """Extract a human-readable error summary from a response body."""
    if payload is None:
        return ""

    if isinstance(payload, str):
        raw = payload.strip()
        if not raw:
            return ""
        try:
            return _extract_error_summary(json.loads(raw))
        except Exception:
            return _truncate_text(raw)

    if isinstance(payload, dict):
        for key in ("error_summary", "error_message", "detail", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return _truncate_text(value)

        error_value = payload.get("error")
        if isinstance(error_value, dict):
            for key in ("message", "detail", "reason", "code"):
                value = error_value.get(key)
                if isinstance(value, str) and value.strip():
                    return _truncate_text(value)
        elif isinstance(error_value, str) and error_value.strip():
            return _truncate_text(error_value)

        for nested_key in ("response", "data"):
            nested = payload.get(nested_key)
            if isinstance(nested, (dict, list, str)):
                summary = _extract_error_summary(nested)
                if summary:
                    return summary

        return ""

    if isinstance(payload, list):
        for item in payload:
            summary = _extract_error_summary(item)
            if summary:
                return summary
        return ""

    return _truncate_text(payload)


def _guess_client_hints_from_user_agent(user_agent: str) -> Dict[str, str]:
    """Fill in common sec-ch-* headers based on the UA."""
    ua = (user_agent or "").strip()
    if not ua:
        return {}

    headers: Dict[str, str] = {}
    major_match = re.search(r"(?:Chrome|Chromium|Edg|EdgA|EdgiOS)/(\d+)", ua)
    is_mobile = any(token in ua for token in ("Android", "iPhone", "iPad", "Mobile"))
    headers["sec-ch-ua-mobile"] = "?1" if is_mobile else "?0"

    if "Windows" in ua:
        headers["sec-ch-ua-platform"] = '"Windows"'
    elif "Macintosh" in ua or "Mac OS X" in ua:
        headers["sec-ch-ua-platform"] = '"macOS"'
    elif "Android" in ua:
        headers["sec-ch-ua-platform"] = '"Android"'
    elif "iPhone" in ua or "iPad" in ua:
        headers["sec-ch-ua-platform"] = '"iOS"'
    elif "Linux" in ua:
        headers["sec-ch-ua-platform"] = '"Linux"'

    if major_match:
        major = major_match.group(1)
        if "Edg/" in ua:
            headers["sec-ch-ua"] = (
                f'"Not:A-Brand";v="99", "Microsoft Edge";v="{major}", "Chromium";v="{major}"'
            )
        else:
            headers["sec-ch-ua"] = (
                f'"Not:A-Brand";v="99", "Google Chrome";v="{major}", "Chromium";v="{major}"'
            )

    return headers


def _validate_browser_proxy_url_local(proxy_url: str) -> tuple[bool, Optional[str]]:
    """Accepts one proxy URL or a comma / newline / semicolon separated list (the
    browser pool rotates through a list; the validator used to reject the very
    list the settings page had stored, so nothing on that page could be saved)."""
    if not proxy_url:
        return True, None
    candidates = [p.strip() for p in re.split(r"[,\n;]+", proxy_url) if p.strip()]
    for candidate in candidates:
        normalized = candidate
        if not re.match(r"^(http|https|socks5h?|socks5)://", normalized):
            normalized = f"http://{normalized}"
        if not re.match(r"^(socks5h?|socks5|http|https)://(?:([^:]+):([^@]+)@)?([^:]+):(\d+)$", normalized):
            return False, f"Invalid proxy format: {candidate[:60]}"
    return True, None


def _normalize_runtime_method(method: Optional[str]) -> str:
    normalized = (method or "").strip().lower()
    if normalized not in {"browser", "personal"}:
        raise HTTPException(status_code=400, detail="Invalid runtime method")
    return normalized


async def _prepare_captcha_runtime(method: str):
    runtime_method = _normalize_runtime_method(method)
    try:
        if runtime_method == "browser":
            start_runtime_prepare(
                runtime_method,
                "Started preparing the headed-browser captcha runtime; install progress will show automatically.",
            )
        else:
            start_runtime_prepare(
                runtime_method,
                "Started preparing the built-in browser captcha runtime; install progress will show automatically.",
            )

        if runtime_method == "browser":
            module = await asyncio.to_thread(importlib.import_module, "src.services.browser_captcha")
            service_cls = getattr(module, "BrowserCaptchaService")
            service = await service_cls.get_instance(db)
            if hasattr(service, "reload_browser_count"):
                await service.reload_browser_count()
            if hasattr(service, "warmup_browser_slots"):
                await service.warmup_browser_slots()
            finish_runtime_prepare(runtime_method, "Chromium browser runtime is ready; headed-browser captcha can be used now.")
            return

        module = await asyncio.to_thread(importlib.import_module, "src.services.browser_captcha_personal")
        service_cls = getattr(module, "BrowserCaptchaService")
        service = await service_cls.get_instance(db)
        await service.reload_config()
        finish_runtime_prepare(runtime_method, "Built-in browser runtime is ready; personal captcha can be used now.")
    except HTTPException:
        raise
    except Exception as e:
        fail_runtime_prepare(runtime_method, f"Browser runtime preparation failed: {type(e).__name__}: {e}")
    finally:
        captcha_runtime_prepare_tasks.pop(runtime_method, None)


def _schedule_captcha_runtime_prepare(method: str) -> bool:
    runtime_method = _normalize_runtime_method(method)
    task = captcha_runtime_prepare_tasks.get(runtime_method)
    if task and not task.done():
        progress_runtime_prepare(runtime_method, "Browser runtime preparation is still in progress, please wait...")
        return False

    captcha_runtime_prepare_tasks[runtime_method] = asyncio.create_task(
        _prepare_captcha_runtime(runtime_method)
    )
    return True


def _guess_impersonate_from_user_agent(user_agent: str) -> str:
    """Pick a usable curl_cffi browser fingerprint version from the UA."""
    ua = (user_agent or "").strip()
    major_match = re.search(r"(?:Chrome|Chromium|Edg|EdgA|EdgiOS)/(\d+)", ua)
    if not major_match:
        return "chrome120"

    try:
        major = int(major_match.group(1))
    except Exception:
        return "chrome120"

    if major >= 124:
        return "chrome124"
    if major >= 120:
        return "chrome120"
    return "chrome120"


def _build_proxy_map(proxy_url: str) -> Optional[Dict[str, str]]:
    normalized = (proxy_url or "").strip()
    if not normalized:
        return None
    return {"http": normalized, "https": normalized}


def _normalize_http_base_url(base_url: str) -> str:
    normalized = (base_url or "").strip().rstrip("/")
    if not normalized:
        raise RuntimeError("Remote captcha service URL is not configured")

    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("Invalid remote captcha service URL; must be http(s)://host[:port]")

    return normalized


def _get_remote_browser_client_config() -> tuple[str, str, int]:
    base_url = _normalize_http_base_url(config.remote_browser_base_url)
    api_key = (config.remote_browser_api_key or "").strip()
    if not api_key:
        raise RuntimeError("Remote captcha service API key is not configured")
    timeout = max(5, int(config.remote_browser_timeout or 60))
    return base_url, api_key, timeout


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


def _parse_json_response_text(text: str) -> Optional[Any]:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


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
        raise RuntimeError(f"Remote captcha service request failed: {e}") from e

    return status_code, _parse_json_response_text(text), text


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
        "timeout": _build_remote_browser_http_timeout(timeout),
    }

    if payload is not None:
        req_headers["Content-Type"] = "application/json; charset=utf-8"
        if request_method != "GET":
            request_kwargs["json"] = payload

    if httpx is None:
        return await _stdlib_json_http_request(
            method=method,
            url=url,
            headers=req_headers,
            payload=payload,
            timeout=timeout,
        )

    try:
        # The remote_browser control plane is a service-to-service JSON API. Use httpx so that
        # curl_cffi on Windows + impersonate does not drop the POST body (FastAPI would reject it as missing).
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as session:
            response = await session.request(
                method=request_method,
                url=url,
                **request_kwargs,
            )
    except Exception as e:
        raise RuntimeError(f"Remote captcha service request failed: {e}") from e

    status_code = int(getattr(response, "status_code", 0) or 0)
    text = response.text or ""
    parsed = _parse_json_response_text(text)

    return status_code, parsed, text


async def _resolve_score_test_verify_proxy(
    captcha_method: str,
    browser_proxy_enabled: bool,
    browser_proxy_url: str
) -> tuple[Optional[Dict[str, str]], bool, str, str]:
    """
    Pick the proxy for the score-test verify request, preferring the browser captcha proxy.
    Returns: (proxies, used, source, proxy_url)
    """
    # Browser captcha modes prefer browser_proxy so the egress matches the one that got the token
    if captcha_method in {"browser", "personal"} and browser_proxy_enabled and browser_proxy_url:
        proxy_map = _build_proxy_map(browser_proxy_url)
        if proxy_map:
            return proxy_map, True, "captcha_browser_proxy", browser_proxy_url

    # Fall back to the request proxy config
    try:
        if proxy_manager:
            proxy_cfg = await proxy_manager.get_proxy_config()
            if proxy_cfg and proxy_cfg.enabled and proxy_cfg.proxy_url:
                proxy_map = _build_proxy_map(proxy_cfg.proxy_url)
                if proxy_map:
                    return proxy_map, True, "request_proxy", proxy_cfg.proxy_url
    except Exception:
        pass

    return None, False, "none", ""


async def _solve_recaptcha_with_api_service(
    method: str,
    website_url: str,
    website_key: str,
    action: str,
    enterprise: bool = False
) -> Optional[str]:
    """Get a token from the currently configured third-party captcha service."""
    if method == "yescaptcha":
        client_key = config.yescaptcha_api_key
        base_url = config.yescaptcha_base_url
        task_type = config.yescaptcha_task_type
        min_score = get_yescaptcha_min_score(task_type)
    elif method == "capmonster":
        client_key = config.capmonster_api_key
        base_url = config.capmonster_base_url
        task_type = "RecaptchaV3TaskProxyless"
        min_score = None
    elif method == "ezcaptcha":
        client_key = config.ezcaptcha_api_key
        base_url = config.ezcaptcha_base_url
        task_type = "ReCaptchaV3TaskProxylessS9"
        min_score = None
    elif method == "capsolver":
        client_key = config.capsolver_api_key
        base_url = config.capsolver_base_url
        task_type = "ReCaptchaV3EnterpriseTaskProxyLess" if enterprise else "ReCaptchaV3TaskProxyLess"
        min_score = None
    else:
        raise RuntimeError(f"Unsupported captcha method: {method}")

    if not client_key:
        raise RuntimeError(f"{method} API key is not configured")

    task: Dict[str, Any] = {
        "websiteURL": website_url,
        "websiteKey": website_key,
        "type": task_type,
        "pageAction": action,
    }
    if min_score is not None:
        task["minScore"] = min_score

    if enterprise and method == "capsolver":
        task["isEnterprise"] = True

    create_url = f"{base_url.rstrip('/')}/createTask"
    get_url = f"{base_url.rstrip('/')}/getTaskResult"

    # Get proxy config
    proxies = None
    try:
        if proxy_manager:
            proxy_cfg = await proxy_manager.get_proxy_config()
            if proxy_cfg and proxy_cfg.enabled and proxy_cfg.proxy_url:
                proxies = {"http": proxy_cfg.proxy_url, "https": proxy_cfg.proxy_url}
    except Exception:
        pass

    async with AsyncSession() as session:
        create_resp = await session.post(
            create_url,
            json={"clientKey": client_key, "task": task},
            impersonate="chrome120",
            timeout=30,
            proxies=proxies
        )
        create_json = create_resp.json()
        task_id = create_json.get("taskId")

        if not task_id:
            error_desc = create_json.get("errorDescription") or create_json.get("errorMessage") or str(create_json)
            raise RuntimeError(f"{method} createTask failed: {error_desc}")

        for _ in range(40):
            poll_resp = await session.post(
                get_url,
                json={"clientKey": client_key, "taskId": task_id},
                impersonate="chrome120",
                timeout=30,
                proxies=proxies
            )
            poll_json = poll_resp.json()
            if poll_json.get("status") == "ready":
                solution = poll_json.get("solution", {}) or {}
                token = solution.get("gRecaptchaResponse") or solution.get("token")
                if token:
                    return token
                raise RuntimeError(f"{method} result is missing the token: {poll_json}")

            if poll_json.get("errorId") not in (None, 0):
                error_desc = poll_json.get("errorDescription") or poll_json.get("errorMessage") or str(poll_json)
                raise RuntimeError(f"{method} getTaskResult failed: {error_desc}")

            await asyncio.sleep(3)

    raise RuntimeError(f"{method} timed out getting token")


async def _score_test_with_remote_browser_service(
    website_url: str,
    website_key: str,
    verify_url: str,
    action: str,
    enterprise: bool = False,
) -> Dict[str, Any]:
    """Call the remote headed captcha service to solve in-page and verify the score."""
    base_url, api_key, timeout = _get_remote_browser_client_config()
    endpoint = f"{base_url}/api/v1/custom-score"
    request_payload = {
        "website_url": website_url,
        "website_key": website_key,
        "verify_url": verify_url,
        "action": action,
        "enterprise": enterprise,
    }

    status_code, response_payload, response_text = await _sync_json_http_request(
        method="POST",
        url=endpoint,
        headers={"Authorization": f"Bearer {api_key}"},
        payload=request_payload,
        timeout=timeout,
    )

    if status_code >= 400:
        detail = ""
        if isinstance(response_payload, dict):
            detail = response_payload.get("detail") or response_payload.get("message") or str(response_payload)
        if not detail:
            detail = (response_text or "").strip()
        raise RuntimeError(f"Remote captcha service request failed (HTTP {status_code}): {detail or 'unknown error'}")

    if not isinstance(response_payload, dict):
        raise RuntimeError("Remote captcha service returned an invalid format")
    return response_payload


def set_dependencies(tm: TokenManager, pm: ProxyManager, database: Database, cm: Optional[ConcurrencyManager] = None):
    """Set service instances"""
    global token_manager, proxy_manager, db, concurrency_manager
    token_manager = tm
    proxy_manager = pm
    db = database
    concurrency_manager = cm


# ========== Request Models ==========

class LoginRequest(BaseModel):
    username: str
    password: str


class AddTokenRequest(BaseModel):
    st: str
    project_id: Optional[str] = None  # Optional user-supplied project_id
    project_name: Optional[str] = None
    remark: Optional[str] = None
    captcha_proxy_url: Optional[str] = None
    extension_route_key: Optional[str] = None
    image_enabled: bool = True
    video_enabled: bool = True
    image_concurrency: int = -1
    video_concurrency: int = -1
    protocol_mode: str = "session"
    google_cookies: Optional[str] = None
    login_account: Optional[str] = None
    login_password: Optional[str] = None
    proxy_url: Optional[str] = None
    auto_refresh_enabled: bool = True
    refresh_interval_minutes: int = 120


class UpdateTokenRequest(BaseModel):
    st: str  # Session Token (required, used to refresh AT)
    project_id: Optional[str] = None  # Optional user-supplied project_id
    project_name: Optional[str] = None
    remark: Optional[str] = None
    captcha_proxy_url: Optional[str] = None
    extension_route_key: Optional[str] = None
    image_enabled: Optional[bool] = None
    video_enabled: Optional[bool] = None
    image_concurrency: Optional[int] = None
    video_concurrency: Optional[int] = None
    protocol_mode: Optional[str] = None
    google_cookies: Optional[str] = None
    login_account: Optional[str] = None
    login_password: Optional[str] = None
    proxy_url: Optional[str] = None
    auto_refresh_enabled: Optional[bool] = None
    refresh_interval_minutes: Optional[int] = None


class ProxyConfigRequest(BaseModel):
    proxy_enabled: bool
    proxy_url: Optional[str] = None
    media_proxy_enabled: Optional[bool] = None
    media_proxy_url: Optional[str] = None


class ProxyTestRequest(BaseModel):
    proxy_url: str
    test_url: Optional[str] = "https://labs.google/"
    timeout_seconds: Optional[int] = 15


class CaptchaScoreTestRequest(BaseModel):
    website_url: Optional[str] = "https://antcpt.com/score_detector/"
    website_key: Optional[str] = "6LcR_okUAAAAAPYrPe-HK_0RULO1aZM15ENyM-Mf"
    action: Optional[str] = "homepage"
    verify_url: Optional[str] = "https://antcpt.com/score_detector/verify.php"
    enterprise: Optional[bool] = False


class ClientPolicyRequest(BaseModel):
    client: str
    image_tier: str = "any"
    video_tier: str = "any"
    note: Optional[str] = ""


class GenerationConfigRequest(BaseModel):
    image_timeout: Optional[int] = None
    video_timeout: Optional[int] = None
    max_retries: Optional[int] = None
    remove_watermark: Optional[bool] = None


class CallLogicConfigRequest(BaseModel):
    call_mode: Optional[str] = None
    tier_order: Optional[str] = None


class ChangePasswordRequest(BaseModel):
    username: Optional[str] = None
    old_password: str
    new_password: str


class UpdateAPIKeyRequest(BaseModel):
    new_api_key: str


class UpdateDebugConfigRequest(BaseModel):
    enabled: bool


class UpdateAdminConfigRequest(BaseModel):
    error_ban_threshold: int


class ST2ATRequest(BaseModel):
    """ST-to-AT request"""
    st: str


class ImportTokenItem(BaseModel):
    """Import token item"""
    email: Optional[str] = None
    access_token: Optional[str] = None
    session_token: Optional[str] = None
    is_active: bool = True
    captcha_proxy_url: Optional[str] = None
    extension_route_key: Optional[str] = None
    image_enabled: bool = True
    video_enabled: bool = True
    image_concurrency: int = -1
    video_concurrency: int = -1
    # Protocol-login fields: None = "not in the import file" — update_token then
    # SKIPS them, so re-importing an old backup can't reset a token's protocol
    # mode/refresh settings to defaults. (add_token normalizes None to defaults.)
    protocol_mode: Optional[str] = None
    google_cookies: Optional[str] = None
    login_account: Optional[str] = None
    login_password: Optional[str] = None
    proxy_url: Optional[str] = None
    auto_refresh_enabled: Optional[bool] = None
    refresh_interval_minutes: Optional[int] = None


class ImportTokensRequest(BaseModel):
    """Import tokens request"""
    tokens: List[ImportTokenItem]


class TokenRefreshConfigRequest(BaseModel):
    enabled: Optional[bool] = None
    refresh_interval_minutes: Optional[int] = None


# ========== Auth Middleware ==========

async def verify_admin_token(request: Request, authorization: str = Header(None)):
    """Verify admin session token (NOT API key)"""
    header_token = ""
    if authorization and authorization.startswith("Bearer "):
        header_token = authorization[7:].strip()

    cookie_token = get_admin_token_from_cookie(request) or ""

    if header_token and header_token in active_admin_tokens:
        return header_token

    if cookie_token and cookie_token in active_admin_tokens:
        return cookie_token

    if header_token or cookie_token:
        raise HTTPException(status_code=401, detail="Invalid or expired admin token")

    raise HTTPException(status_code=401, detail="Missing authorization")


def get_admin_token_from_cookie(request: Request) -> Optional[str]:
    token = str(request.cookies.get(ADMIN_SESSION_COOKIE_NAME) or "").strip()
    return token or None


def is_admin_session_token_valid(token: Optional[str]) -> bool:
    normalized = str(token or "").strip()
    return bool(normalized) and normalized in active_admin_tokens


# ========== Auth Endpoints ==========

@router.post("/api/admin/login")
async def admin_login(request: LoginRequest, response: Response):
    """Admin login - returns session token (NOT API key)"""
    admin_config = await db.get_admin_config()

    if not AuthManager.verify_admin(request.username, request.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Generate independent session token
    session_token = f"admin-{secrets.token_urlsafe(32)}"

    # Store in active tokens
    active_admin_tokens.add(session_token)

    response.set_cookie(
        key=ADMIN_SESSION_COOKIE_NAME,
        value=session_token,
        max_age=ADMIN_SESSION_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )

    return {
        "success": True,
        "token": session_token,  # Session token (NOT API key)
        "username": admin_config.username
    }


@router.post("/api/admin/logout")
async def admin_logout(response: Response, token: str = Depends(verify_admin_token)):
    """Admin logout - invalidate session token"""
    active_admin_tokens.discard(token)
    response.delete_cookie(ADMIN_SESSION_COOKIE_NAME, path="/")
    return {"success": True, "message": "Logged out"}


@router.post("/api/admin/change-password")
async def change_password(
    request: ChangePasswordRequest,
    token: str = Depends(verify_admin_token)
):
    """Change admin password"""
    admin_config = await db.get_admin_config()

    # Verify old password
    if not AuthManager.verify_admin(admin_config.username, request.old_password):
        raise HTTPException(status_code=400, detail="Old password is incorrect")

    # Update password and username in database
    update_params = {"password": request.new_password}
    if request.username:
        update_params["username"] = request.username

    await db.update_admin_config(**update_params)

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()

    # 🔑 Invalidate all admin session tokens (force re-login for security)
    active_admin_tokens.clear()

    return {"success": True, "message": "Password changed, please log in again"}


# ========== Token Management ==========

@router.get("/api/tokens")
async def get_tokens(token: str = Depends(verify_admin_token)):
    """Get all tokens with statistics"""
    token_rows = await db.get_all_tokens_with_stats()
    to_iso = lambda value: value.isoformat() if hasattr(value, "isoformat") else value
    now = datetime.now(timezone.utc)
    skip_reasons = await _compute_skip_reasons(token_rows, now)

    def normalize_dt(value):
        if not value:
            return None
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except Exception:
                return None
        if getattr(value, "tzinfo", None) is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    return [{
        "id": row.get("id"),
        "st": row.get("st"),  # Session Token for editing
        "at": row.get("at"),  # Access Token for editing (converted from ST)
        "at_expires": to_iso(row.get("at_expires")) if row.get("at_expires") else None,  # AT expiry time
        "at_expired": bool(normalize_dt(row.get("at_expires")) and normalize_dt(row.get("at_expires")) <= now),
        "at_expiring_within_1h": bool(
            normalize_dt(row.get("at_expires"))
            and normalize_dt(row.get("at_expires")) > now
            and (normalize_dt(row.get("at_expires")) - now).total_seconds() < 3600
        ),
        "token": row.get("at"),  # Frontend compatibility: token.token access
        "email": row.get("email"),
        "name": row.get("name"),
        "remark": row.get("remark"),
        "is_active": bool(row.get("is_active")),
        "created_at": to_iso(row.get("created_at")) if row.get("created_at") else None,
        "last_used_at": to_iso(row.get("last_used_at")) if row.get("last_used_at") else None,
        "use_count": row.get("use_count"),
        "credits": row.get("credits"),  # Credits balance
        "user_paygate_tier": row.get("user_paygate_tier"),
        "current_project_id": row.get("current_project_id"),  # Project ID
        "current_project_name": row.get("current_project_name"),  # Project name
        "captcha_proxy_url": row.get("captcha_proxy_url") or "",
        "extension_route_key": row.get("extension_route_key") or "",
        "pool_mode": row.get("pool_mode") or "auto",
        "reserved_client": normalize_client(row.get("reserved_client") or ""),
        "ext_version": row.get("ext_version") or "",
        "protocol_mode": row.get("protocol_mode") or "session",
        # Cookie sync (docs/cookie-sync.md): the VALUES never leave the server; the
        # admin page only learns whether a Google login is on file and how old it is.
        "google_cookies": "",
        "google_cookies_set": bool((row.get("google_cookies") or "").strip()),
        "google_cookies_updated_at": to_iso(row.get("google_cookies_updated_at")) if row.get("google_cookies_updated_at") else None,
        "login_account": row.get("login_account") or "",
        "login_password": "",
        "proxy_url": row.get("proxy_url") or "",
        "auto_refresh_enabled": bool(row.get("auto_refresh_enabled", True)),
        "refresh_interval_minutes": row.get("refresh_interval_minutes") or 120,
        "last_st_refresh_at": to_iso(row.get("last_st_refresh_at")) if row.get("last_st_refresh_at") else None,
        "last_st_refresh_result": row.get("last_st_refresh_result") or "",
        "image_enabled": bool(row.get("image_enabled")),
        "video_enabled": bool(row.get("video_enabled")),
        "image_concurrency": row.get("image_concurrency"),
        "video_concurrency": row.get("video_concurrency"),
        "image_count": row.get("image_count", 0),
        "video_count": row.get("video_count", 0),
        "error_count": row.get("error_count", 0),
        "today_error_count": row.get("today_error_count", 0),
        "consecutive_error_count": row.get("consecutive_error_count", 0),
        "last_error_at": to_iso(row.get("last_error_at")) if row.get("last_error_at") else None,
        "ban_reason": row.get("ban_reason"),
        "banned_at": to_iso(row.get("banned_at")) if row.get("banned_at") else None,
        # Why the load balancer would skip this token RIGHT NOW for EVERY model
        # ("" = eligible): reCAPTCHA cooldown, health cooldown, no browser online for
        # its route key, expired access token. The balancer only logs its filter
        # reasons at debug level, so "active but never used" was undiagnosable.
        "skip_reason": skip_reasons.get(row.get("id"), {}).get("blocking", ""),
        # Model families whose daily quota is exhausted on this account. Per-model:
        # the token still serves every other family, so this is NOT a skip reason.
        "quota_exhausted": skip_reasons.get(row.get("id"), {}).get("quota", []),
    } for row in token_rows]  # Return a plain array for frontend compatibility


_QUOTA_FAMILIES = ("gemini-3.0-pro-image", "gemini-3.1-flash-image", "nano-banana-2-lite")


async def _compute_skip_reasons(token_rows, now) -> Dict[int, Dict[str, Any]]:
    """Mirror LoadBalancer.select_token's filters for every ACTIVE token, read-only.

    Returns {token_id: {"blocking": "reason; reason", "quota": [family, ...]}}.
    Blocking reasons take the token out of the pool for every model; quota is
    per family and only removes it for that family.
    """
    reasons: Dict[int, Dict[str, Any]] = {}
    if token_manager is None:
        return reasons
    try:
        await token_manager._ensure_quota_loaded()
    except Exception:
        pass
    service = None
    if config.captcha_method == "extension":
        try:
            from ..services.browser_captcha_extension import ExtensionCaptchaService
            service = await ExtensionCaptchaService.get_instance(db)
        except Exception:
            service = None
    for row in token_rows:
        tid = row.get("id")
        if not row.get("is_active"):
            continue
        parts: List[str] = []
        exhausted: List[str] = []
        try:
            cd = token_manager._recaptcha_cd.get(tid)
            if cd and now < cd[0]:
                mins = int((cd[0] - now).total_seconds() // 60) + 1
                parts.append(f"recaptcha cooldown {mins}m (strike {cd[1]})")
            if token_manager.is_health_cooldown(tid):
                parts.append(token_manager.health_cooldown_reason(tid) or "health cooldown")
            reserved = normalize_client(row.get("reserved_client") or "")
            if reserved:
                # Same rule as client_policy.client_block_reason: everyone else skips it.
                parts.append(f"reserved for {reserved} (only that client may use it)")
            exhausted = [f for f in _QUOTA_FAMILIES if token_manager.is_model_quota_exhausted(tid, f)]
            if service is not None:
                ok, route_key = await service.has_connection_for_token(tid)
                if not ok:
                    parts.append("no browser online for route key" if route_key else "no route key / no browser")
            at_exp = row.get("at_expires")
            if isinstance(at_exp, str):
                try:
                    at_exp = datetime.fromisoformat(at_exp.replace("Z", "+00:00"))
                except Exception:
                    at_exp = None
            if at_exp is not None and getattr(at_exp, "tzinfo", None) is None:
                at_exp = at_exp.replace(tzinfo=timezone.utc)
            if not row.get("at") or (at_exp is not None and at_exp <= now):
                parts.append("access token expired")
        except Exception as e:  # never let diagnostics break the token list
            parts.append(f"(diag error: {e})")
        reasons[tid] = {"blocking": "; ".join(parts), "quota": exhausted}
    return reasons


@router.post("/api/tokens")
async def add_token(
    request: AddTokenRequest,
    token: str = Depends(verify_admin_token)
):
    """Add a new token"""
    try:
        new_token = await token_manager.add_token(
            st=request.st,
            project_id=request.project_id,  # User may specify project_id
            project_name=request.project_name,
            remark=request.remark,
            captcha_proxy_url=request.captcha_proxy_url.strip() if request.captcha_proxy_url is not None else None,
            extension_route_key=request.extension_route_key.strip() if request.extension_route_key is not None else None,
            image_enabled=request.image_enabled,
            video_enabled=request.video_enabled,
            image_concurrency=request.image_concurrency,
            video_concurrency=request.video_concurrency,
            protocol_mode=request.protocol_mode,
            google_cookies=request.google_cookies,
            login_account=request.login_account,
            login_password=request.login_password,
            proxy_url=request.proxy_url,
            auto_refresh_enabled=request.auto_refresh_enabled,
            refresh_interval_minutes=request.refresh_interval_minutes
        )

        # Hot-reload concurrency limits so no restart is needed
        if concurrency_manager:
            await concurrency_manager.reset_token(
                new_token.id,
                image_concurrency=new_token.image_concurrency,
                video_concurrency=new_token.video_concurrency
            )

        return {
            "success": True,
            "message": "Token added",
            "token": {
                "id": new_token.id,
                "email": new_token.email,
                "credits": new_token.credits,
                "project_id": new_token.current_project_id,
                "project_name": new_token.current_project_name
            }
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to add token: {str(e)}")


@router.put("/api/tokens/{token_id}")
async def update_token(
    token_id: int,
    request: UpdateTokenRequest,
    token: str = Depends(verify_admin_token)
):
    """Update token - auto-refresh AT from ST"""
    try:
        # Convert ST to AT first
        result = await token_manager.flow_client.st_to_at(request.st)
        at = result["access_token"]
        expires = result.get("expires")

        # Parse expiry time
        from datetime import datetime
        at_expires = None
        if expires:
            try:
                at_expires = datetime.fromisoformat(expires.replace('Z', '+00:00'))
            except:
                pass

        # Update token (AT, ST, AT expiry, project_id and project_name)
        await token_manager.update_token(
            token_id=token_id,
            st=request.st,
            at=at,
            at_expires=at_expires,  # Update AT expiry time
            project_id=request.project_id,
            project_name=request.project_name,
            remark=request.remark,
            captcha_proxy_url=request.captcha_proxy_url.strip() if request.captcha_proxy_url is not None else None,
            extension_route_key=request.extension_route_key.strip() if request.extension_route_key is not None else None,
            image_enabled=request.image_enabled,
            video_enabled=request.video_enabled,
            image_concurrency=request.image_concurrency,
            video_concurrency=request.video_concurrency,
            protocol_mode=request.protocol_mode,
            google_cookies=request.google_cookies,
            login_account=request.login_account,
            login_password=request.login_password,
            proxy_url=request.proxy_url,
            auto_refresh_enabled=request.auto_refresh_enabled,
            refresh_interval_minutes=request.refresh_interval_minutes
        )

        # Hot-reload concurrency limits so admin changes apply immediately
        if concurrency_manager:
            updated_token = await token_manager.get_token(token_id)
            if updated_token:
                await concurrency_manager.reset_token(
                    token_id,
                    image_concurrency=updated_token.image_concurrency,
                    video_concurrency=updated_token.video_concurrency
                )

        return {"success": True, "message": "Token updated"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/tokens/{token_id}")
async def delete_token(
    token_id: int,
    token: str = Depends(verify_admin_token)
):
    """Delete token"""
    try:
        await token_manager.delete_token(token_id)
        if concurrency_manager:
            await concurrency_manager.remove_token(token_id)
        return {"success": True, "message": "Token deleted"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/tokens/{token_id}/enable")
async def enable_token(
    token_id: int,
    token: str = Depends(verify_admin_token)
):
    """Enable token"""
    await token_manager.enable_token(token_id)
    return {"success": True, "message": "Token enabled"}


@router.post("/api/tokens/{token_id}/disable")
async def disable_token(
    token_id: int,
    token: str = Depends(verify_admin_token)
):
    """Disable token"""
    await token_manager.disable_token(token_id)
    return {"success": True, "message": "Token disabled"}


@router.post("/api/tokens/{token_id}/refresh-credits")
async def refresh_credits(
    token_id: int,
    token: str = Depends(verify_admin_token)
):
    """Refresh token credits"""
    try:
        credits = await token_manager.refresh_credits(token_id)
        return {
            "success": True,
            "message": "Credits refreshed",
            "credits": credits
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to refresh credits: {str(e)}")


@router.post("/api/tokens/{token_id}/refresh-at")
async def refresh_at(
    token_id: int,
    token: str = Depends(verify_admin_token)
):
    """Manually refresh a token's AT (converted from ST)
    
    If AT refresh fails in personal mode, it automatically tries to refresh the ST via the browser
    """
    from ..core.logger import debug_logger
    from ..core.config import config

    debug_logger.log_info(f"[API] Manual AT refresh request: token_id={token_id}, captcha_method={config.captcha_method}")

    # Reason-specific guidance. Manual refresh NEVER disables the token (the
    # disable decision lives only in the automatic pool path), so the operator
    # can safely retry / re-supply credentials without losing the row.
    reason_messages = {
        "st_expired": (
            "Session Token expired or invalid — supply a fresh ST "
            "(re-login in the worker browser or paste a new ST). "
            "This fails in any captcha mode; the token was not disabled."
        ),
        "network": (
            "Network or proxy error reaching Google Labs while refreshing — "
            "check proxy/connection and retry. The token was not disabled."
        ),
        "st_refresh_unavailable": (
            "The backend cannot auto-refresh the ST in this mode — supply a fresh "
            "ST or trigger the extension push. The token was not disabled."
        ),
    }

    try:
        # Call token_manager's internal refresh (manual path: never disables the token)
        outcome = await token_manager._refresh_at(token_id, escalate=False)  # manual: never strike/disable

        if outcome.success:
            # Get the updated token info
            updated_token = await token_manager.get_token(token_id)

            debug_logger.log_info(f"[API] AT refreshed: token_id={token_id}")

            return {
                "success": True,
                "message": "AT refreshed successfully",
                "token": {
                    "id": updated_token.id,
                    "email": updated_token.email,
                    "at_expires": updated_token.at_expires.isoformat() if updated_token.at_expires else None
                }
            }

        debug_logger.log_error(f"[API] AT refresh failed: token_id={token_id}, reason={outcome.reason}")

        detail = reason_messages.get(
            outcome.reason,
            "AT refresh failed (unknown reason) — check server logs.",
        )
        # Expected application-level failure: respond 200 with success=false so the
        # admin UI shows the guidance toast (it renders d.detail).
        return {"success": False, "reason": outcome.reason, "detail": detail}
    except Exception as e:
        debug_logger.log_error(f"[API] AT refresh error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"AT refresh failed: {str(e)}")


@router.post("/api/tokens/{token_id}/route-key")
async def set_token_route_key(
    token_id: int,
    request: dict,
    token: str = Depends(verify_admin_token)
):
    """Bind/unbind a token to a browser extension Route Key (metadata only, no valid ST needed).

    Used to target on-demand session refresh. Empty means unbind (NULL). Deliberately not via PUT /api/tokens/{id}
    (that path forces st_to_at, so rows with an expired ST could not be bound).
    """
    from ..core.logger import debug_logger

    target = await token_manager.get_token(token_id)
    if not target:
        raise HTTPException(status_code=404, detail="Token not found")

    value = (request.get("extension_route_key") or "").strip()
    # Write via the DB layer directly so we can clear to NULL: token_manager.update_token
    # treats extension_route_key=None as "leave unchanged", whereas db.update_token writes
    # whatever kwarg we pass (incl. NULL). value or None => "" unbinds to NULL.
    await db.update_token(token_id, extension_route_key=(value or None))
    updated = await token_manager.get_token(token_id)
    debug_logger.log_info(f"[API] Set route_key: token_id={token_id}, route_key={value or '(cleared)'}")
    return {
        "success": True,
        "message": "Route key updated",
        "token": {
            "id": updated.id,
            "email": updated.email,
            "extension_route_key": updated.extension_route_key or "",
        },
    }


@router.post("/api/tokens/{token_id}/refresh-session")
async def refresh_session_via_extension(
    token_id: int,
    token: str = Depends(verify_admin_token)
):
    """On-demand Google Labs session refresh: tell the worker browser bound to this account to read live cookies and push a new ST.

    Works only while that browser is still logged in; a logged-out/expired session cannot be recovered by any command
    (a human must log in again in that browser). The result is reported honestly via status, never a false "refreshed".
    """
    from ..core.logger import debug_logger
    from ..services.browser_captcha_extension import ExtensionCaptchaService

    target = await token_manager.get_token(token_id)
    if not target:
        raise HTTPException(status_code=404, detail="Token not found")

    def _tok(t):
        return {
            "id": t.id,
            "email": t.email,
            "extension_route_key": t.extension_route_key or "",
            "at_expires": t.at_expires.isoformat() if t.at_expires else None,
        }

    # Manual action: NEVER disables the token (mirrors refresh-at). Every expected
    # failure returns HTTP 200 + success:false so the UI shows the guidance toast.
    reason_messages = {
        "not_bound": (
            "Not bound: set this token's Route Key AND the same Route Key in that browser's "
            "extension first (an unbound refresh could hit the wrong account). Token not disabled."
        ),
        "no_browser": (
            "No online extension matches this token's Route Key — open/connect that browser. "
            "Token not disabled."
        ),
        "logged_out": (
            "Target browser is logged OUT of Google Labs — a human must re-login there, then retry. "
            "No command can refresh a logged-out session. Token not disabled."
        ),
        "busy": (
            "Busy minting; the tab was not reloaded mid-mint — retry shortly."
        ),
        "timeout": (
            "Timed out waiting for the browser (busy, or its service worker slept) — retry. Token not disabled."
        ),
        "network": (
            "Network/proxy or server error pushing the new ST — check and retry. Token not disabled."
        ),
        "not_configured": (
            "The worker extension is missing its Server URL / Connection Token."
        ),
        "account_mismatch": (
            "The browser is logged into a DIFFERENT Google account than this token — refused to "
            "write (would corrupt the wrong account). Use the correct account's browser. Token not disabled."
        ),
        "unknown": (
            "Session refresh failed (unknown reason) — check server logs."
        ),
    }

    # NOTE: no longer refuse an empty route_key here. request_session_refresh now
    # safely fans out over the shared browser pool (email-guarded push), so the button
    # works whether or not a per-account Route Key is bound. A bound Route Key still
    # targets exactly one browser.
    try:
        service = await ExtensionCaptchaService.get_instance(db=db)
        result = await service.request_session_refresh(token_id, timeout=30)
        status = (result or {}).get("status")

        if status == "refreshed":
            updated = await token_manager.get_token(token_id)
            debug_logger.log_info(f"[API] Session refreshed via extension: token_id={token_id}")
            return {
                "success": True,
                "message": "Session refreshed successfully",
                "token": _tok(updated),
            }

        debug_logger.log_error(f"[API] Session refresh failed: token_id={token_id}, status={status}")
        return {
            "success": False,
            "reason": status or "unknown",
            "detail": reason_messages.get(status, reason_messages["unknown"]),
            "token": _tok(target),
        }
    except Exception as e:
        debug_logger.log_error(f"[API] Session refresh error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Session refresh failed: {str(e)}")


def _sanitize_reported_proxy(value) -> Optional[str]:
    """Validate a proxy URL reported by the worker extension. Returns the normalized
    string, or None to mean 'not reported / invalid — leave the stored value unchanged'.
    Accepts only scheme://[creds@]host:port to avoid persisting garbage or SSRF-y values.
    """
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or len(raw) > 500:
        return None
    if not re.match(r"^(https?|socks5h?|socks4)://", raw, re.IGNORECASE):
        return None
    host_part = raw.split("://", 1)[1].rsplit("@", 1)[-1]
    if ":" not in host_part or not host_part.rsplit(":", 1)[-1].split("/")[0].isdigit():
        return None
    return raw


def _sanitize_reported_ua(value) -> Optional[str]:
    """Validate a browser User-Agent reported by the extension. None = leave unchanged."""
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or len(raw) > 500:
        return None
    if not any(tok in raw for tok in ("Mozilla", "Chrome", "Safari", "AppleWebKit")):
        return None
    return raw


async def _probe_egress_ip(label: str, proxy_url: Optional[str], timeout: float = 10.0) -> dict:
    """Fetch this server's PUBLIC egress IP as seen through `proxy_url` (or direct).

    Read-only diagnostic: shows which IP each leg of the pipeline exits from, so a
    residential-mint vs datacenter-redeem mismatch is visible at a glance. Proxy
    credentials are masked in the response.
    """
    entry = {"label": label, "egress": mask_proxy_url(proxy_url), "ip": None, "error": None}
    try:
        async with AsyncSession(trust_env=False) as session:
            resp = await session.get(
                "https://api.ipify.org?format=json",
                proxy=proxy_url,
                timeout=timeout,
                impersonate="chrome124",
            )
            if resp.status_code == 200:
                try:
                    entry["ip"] = resp.json().get("ip")
                except Exception:
                    entry["ip"] = (resp.text or "").strip()[:64]
            else:
                entry["error"] = f"HTTP {resp.status_code}"
    except Exception as e:
        entry["error"] = str(e)[:200]
    return entry


@router.get("/api/admin/ip-debug")
async def ip_debug(token: str = Depends(verify_admin_token)):
    """Show the public egress IP for every server-side leg (direct / request proxy /
    media proxy / each captcha-browser proxy). Use it to confirm whether the image
    request (redeem) exits the SAME IP that mints the reCAPTCHA.

    NOTE: in `extension` captcha mode the reСAPTCHA is minted inside the operator's
    Chrome extension, which egresses from ITS OWN proxy — the server cannot observe
    that IP here; the extension must report it (see the worker extension). This probes
    only the server-controlled legs.
    """
    probes: list = []
    request_proxy = None
    media_proxy = None
    if proxy_manager:
        try:
            request_proxy = await proxy_manager.get_request_proxy_url()
            media_proxy = await proxy_manager.get_media_proxy_url()
        except Exception:
            pass

    tasks = [
        _probe_egress_ip("direct (no proxy)", None),
        _probe_egress_ip("request_proxy (redeem/generate)", request_proxy),
        _probe_egress_ip("media_proxy (image up/download)", media_proxy),
    ]

    # Server-side captcha-browser proxies (used by built-in browser/personal modes,
    # NOT the extension). Comma-separated list in captcha_config.browser_proxy_url.
    try:
        cap = await db.get_captcha_config() if db else None
        raw = (getattr(cap, "browser_proxy_url", "") or "").strip() if cap else ""
        for i, part in enumerate([p.strip() for p in raw.split(",") if p.strip()]):
            tasks.append(_probe_egress_ip(f"captcha_browser_proxy[{i}]", part))
    except Exception:
        pass

    probes = await asyncio.gather(*tasks)

    # Per-account redeem alignment (Slice B): what residential proxy/UA each account
    # reported, which the generate call now redeems through. Creds masked.
    accounts = []
    try:
        for t in (await db.get_all_tokens() if db else []):
            accounts.append({
                "id": t.id,
                "email": t.email,
                "is_active": t.is_active,
                "redeem_proxy": mask_proxy_url(getattr(t, "redeem_proxy_url", None)),
                "redeem_ua_set": bool(getattr(t, "browser_user_agent", None)),
            })
    except Exception:
        pass

    method = getattr(config, "captcha_method", None)
    return {
        "success": True,
        "captcha_method": method,
        "probes": probes,
        "accounts": accounts,
        "note": (
            "Compare 'request_proxy' (where the image request exits) against the reCAPTCHA "
            "MINT IP. In 'extension' mode the mint IP is the extension's own egress and is not "
            "visible here — it must be reported by the worker extension."
        ),
    }


@router.post("/api/tokens/st2at")
async def st_to_at(
    request: ST2ATRequest,
    token: str = Depends(verify_admin_token)
):
    """Convert Session Token to Access Token (convert only, not saved to the database)"""
    try:
        result = await token_manager.flow_client.st_to_at(request.st)
        return {
            "success": True,
            "message": "ST converted to AT successfully",
            "access_token": result["access_token"],
            "email": result.get("user", {}).get("email"),
            "expires": result.get("expires")
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/tokens/import")
async def import_tokens(
    request: ImportTokensRequest,
    token: str = Depends(verify_admin_token)
):
    """Bulk import tokens"""
    from datetime import datetime, timezone

    added = 0
    updated = 0
    errors = []
    # Match legacy behavior: in created_at DESC order, the newest row for the same email wins
    existing_by_email = {}
    for existing_token in await token_manager.get_all_tokens():
        if existing_token.email and existing_token.email not in existing_by_email:
            existing_by_email[existing_token.email] = existing_token

    for idx, item in enumerate(request.tokens):
        try:
            st = item.session_token

            if not st:
                errors.append(f"Item {idx+1}: missing session_token")
                continue

            # Convert ST to AT to get user info
            try:
                result = await token_manager.flow_client.st_to_at(st)
                at = result["access_token"]
                email = result.get("user", {}).get("email")
                expires = result.get("expires")

                if not email:
                    errors.append(f"Item {idx+1}: could not get email")
                    continue

                # Parse expiry time
                at_expires = None
                is_expired = False
                if expires:
                    try:
                        at_expires = datetime.fromisoformat(expires.replace('Z', '+00:00'))
                        # Check whether it has expired
                        now = datetime.now(timezone.utc)
                        is_expired = at_expires <= now
                    except:
                        pass

                # Check by email whether it already exists
                existing = existing_by_email.get(email)

                if existing:
                    # Update existing token
                    await token_manager.update_token(
                        token_id=existing.id,
                        st=st,
                        at=at,
                        at_expires=at_expires,
                        captcha_proxy_url=item.captcha_proxy_url.strip() if item.captcha_proxy_url is not None else None,
                        extension_route_key=item.extension_route_key.strip() if item.extension_route_key is not None else None,
                        image_enabled=item.image_enabled,
                        video_enabled=item.video_enabled,
                        image_concurrency=item.image_concurrency,
                        video_concurrency=item.video_concurrency,
                        protocol_mode=item.protocol_mode,
                        google_cookies=item.google_cookies,
                        login_account=item.login_account,
                        login_password=item.login_password,
                        proxy_url=item.proxy_url,
                        auto_refresh_enabled=item.auto_refresh_enabled,
                        refresh_interval_minutes=item.refresh_interval_minutes
                    )
                    # Disable if expired
                    if is_expired:
                        await token_manager.disable_token(existing.id)
                        existing.is_active = False
                    existing.st = st
                    existing.at = at
                    existing.at_expires = at_expires
                    existing.captcha_proxy_url = item.captcha_proxy_url
                    existing.extension_route_key = item.extension_route_key
                    existing.image_enabled = item.image_enabled
                    existing.video_enabled = item.video_enabled
                    existing.image_concurrency = item.image_concurrency
                    existing.video_concurrency = item.video_concurrency
                    existing.protocol_mode = item.protocol_mode
                    existing.google_cookies = item.google_cookies or ""
                    existing.login_account = item.login_account or ""
                    existing.login_password = item.login_password or ""
                    existing.proxy_url = item.proxy_url or ""
                    existing.auto_refresh_enabled = item.auto_refresh_enabled
                    existing.refresh_interval_minutes = item.refresh_interval_minutes
                    updated += 1
                else:
                    # Add new token
                    new_token = await token_manager.add_token(
                        st=st,
                        captcha_proxy_url=item.captcha_proxy_url.strip() if item.captcha_proxy_url is not None else None,
                        extension_route_key=item.extension_route_key.strip() if item.extension_route_key is not None else None,
                        image_enabled=item.image_enabled,
                        video_enabled=item.video_enabled,
                        image_concurrency=item.image_concurrency,
                        video_concurrency=item.video_concurrency,
                        protocol_mode=item.protocol_mode or "session",
                        google_cookies=item.google_cookies,
                        login_account=item.login_account,
                        login_password=item.login_password,
                        proxy_url=item.proxy_url,
                        # Import fields are None when absent from the file — for a brand-new
                        # token coalesce to the normal add-time defaults.
                        auto_refresh_enabled=True if item.auto_refresh_enabled is None else item.auto_refresh_enabled,
                        refresh_interval_minutes=item.refresh_interval_minutes or 120
                    )
                    # Disable if expired
                    if is_expired:
                        await token_manager.disable_token(new_token.id)
                        new_token.is_active = False
                    existing_by_email[email] = new_token
                    added += 1

            except Exception as e:
                errors.append(f"Item {idx+1}: {str(e)}")

        except Exception as e:
            errors.append(f"Item {idx+1}: {str(e)}")

    return {
        "success": True,
        "added": added,
        "updated": updated,
        "errors": errors if errors else None,
        "message": f"Import done: {added} added, {updated} updated" + (f", {len(errors)} failed" if errors else "")
    }


# ========== Config Management ==========

@router.get("/api/config/proxy")
async def get_proxy_config(token: str = Depends(verify_admin_token)):
    """Get proxy configuration"""
    config = await proxy_manager.get_proxy_config()
    return {
        "success": True,
        "config": {
            "enabled": config.enabled,
            "proxy_url": config.proxy_url,
            "media_proxy_enabled": config.media_proxy_enabled,
            "media_proxy_url": config.media_proxy_url
        }
    }


@router.get("/api/proxy/config")
async def get_proxy_config_alias(token: str = Depends(verify_admin_token)):
    """Get proxy configuration (alias for frontend compatibility)"""
    config = await proxy_manager.get_proxy_config()
    return {
        "proxy_enabled": config.enabled,  # Frontend expects proxy_enabled
        "proxy_url": config.proxy_url,
        "media_proxy_enabled": config.media_proxy_enabled,
        "media_proxy_url": config.media_proxy_url
    }


@router.post("/api/proxy/config")
async def update_proxy_config_alias(
    request: ProxyConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update proxy configuration (alias for frontend compatibility)"""
    try:
        await proxy_manager.update_proxy_config(
            enabled=request.proxy_enabled,
            proxy_url=request.proxy_url,
            media_proxy_enabled=request.media_proxy_enabled,
            media_proxy_url=request.media_proxy_url
        )
    except ValueError as e:
        return {"success": False, "message": str(e)}
    return {"success": True, "message": "Proxy config updated"}


@router.post("/api/config/proxy")
async def update_proxy_config(
    request: ProxyConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update proxy configuration"""
    try:
        await proxy_manager.update_proxy_config(
            enabled=request.proxy_enabled,
            proxy_url=request.proxy_url,
            media_proxy_enabled=request.media_proxy_enabled,
            media_proxy_url=request.media_proxy_url
        )
    except ValueError as e:
        return {"success": False, "message": str(e)}
    return {"success": True, "message": "Proxy config updated"}


@router.post("/api/proxy/test")
async def test_proxy_connectivity(
    request: ProxyTestRequest,
    token: str = Depends(verify_admin_token)
):
    """Test whether the proxy can reach the target site (default https://labs.google/)"""
    proxy_input = (request.proxy_url or "").strip()
    test_url = (request.test_url or "https://labs.google/").strip()
    timeout_seconds = int(request.timeout_seconds or 15)
    timeout_seconds = max(5, min(timeout_seconds, 60))

    if not proxy_input:
        return {
            "success": False,
            "message": "Proxy URL is empty",
            "test_url": test_url
        }

    try:
        proxy_url = proxy_manager.normalize_proxy_url(proxy_input)
    except ValueError as e:
        return {
            "success": False,
            "message": str(e),
            "test_url": test_url
        }

    start_time = time.time()
    try:
        proxies = {"http": proxy_url, "https": proxy_url}
        async with AsyncSession() as session:
            resp = await session.get(
                test_url,
                proxies=proxies,
                timeout=timeout_seconds,
                impersonate="chrome120",
                allow_redirects=True,
                verify=False
            )

        elapsed_ms = int((time.time() - start_time) * 1000)
        status_code = resp.status_code
        final_url = str(resp.url)
        ok = 200 <= status_code < 400

        return {
            "success": ok,
            "message": "Proxy works" if ok else f"Proxy connected, but target returned status {status_code}",
            "test_url": test_url,
            "final_url": final_url,
            "status_code": status_code,
            "elapsed_ms": elapsed_ms
        }
    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        return {
            "success": False,
            "message": f"Proxy test failed: {str(e)}",
            "test_url": test_url,
            "elapsed_ms": elapsed_ms
        }


@router.get("/api/config/generation")
async def get_generation_config(token: str = Depends(verify_admin_token)):
    """Get generation timeout configuration"""
    config = await db.get_generation_config()
    return {
        "success": True,
        "config": {
            "image_timeout": config.image_timeout,
            "video_timeout": config.video_timeout,
            "max_retries": config.max_retries,
            "remove_watermark": config.remove_watermark,
        }
    }


@router.post("/api/config/generation")
async def update_generation_config(
    request: GenerationConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update generation timeout configuration"""
    await db.update_generation_config(
        image_timeout=request.image_timeout,
        video_timeout=request.video_timeout,
        max_retries=request.max_retries,
        remove_watermark=request.remove_watermark,
    )

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()

    return {"success": True, "message": "Generation config updated"}


# ---------------- Per-caller routing (client_policies + tokens.reserved_client) ----------------
async def _client_policy_view() -> List[Dict[str, Any]]:
    """Policies plus, per policy, how many ACTIVE accounts pass its rule right now."""
    tokens = await token_manager.get_active_tokens() if token_manager else []
    out = []
    for policy in client_policy_store.all():
        eligible = {}
        for media, flag in (("image", "image_enabled"), ("video", "video_enabled")):
            eligible[media] = sum(
                1 for t in tokens
                if getattr(t, flag, True) and client_block_reason(t, policy.client, media) is None
            )
        row = policy.as_dict()
        row["eligible"] = eligible
        row["reserved_accounts"] = [
            t.email for t in tokens if normalize_client(getattr(t, "reserved_client", "") or "") == policy.client
        ]
        out.append(row)
    return out


@router.get("/api/client-policies")
async def get_client_policies(token: str = Depends(verify_admin_token)):
    return {"success": True, "policies": await _client_policy_view(), "rules": list(TIER_RULES)}


@router.post("/api/client-policies")
async def upsert_client_policy(request: ClientPolicyRequest, token: str = Depends(verify_admin_token)):
    client = normalize_client(request.client)
    if not client:
        raise HTTPException(status_code=400, detail="client must be a-z 0-9 . _ - (max 40 chars)")
    image_tier, video_tier = normalize_rule(request.image_tier), normalize_rule(request.video_tier)
    if (request.image_tier or "any").strip().lower() not in TIER_RULES or (request.video_tier or "any").strip().lower() not in TIER_RULES:
        raise HTTPException(status_code=400, detail=f"tiers must be one of {', '.join(TIER_RULES)}")
    await db.upsert_client_policy(client, image_tier, video_tier, (request.note or "").strip()[:200])
    await client_policy_store.load(db)  # hot reload, same idea as reload_config_to_memory
    from ..core.logger import debug_logger
    debug_logger.op_warning(f"[CLIENT_POLICY] {client}: image={image_tier} video={video_tier}")
    return {"success": True, "policies": await _client_policy_view()}


@router.delete("/api/client-policies/{client}")
async def delete_client_policy(client: str, token: str = Depends(verify_admin_token)):
    client = normalize_client(client)
    if not client or client == DEFAULT_CLIENT:
        raise HTTPException(status_code=400, detail="the default policy cannot be deleted")
    await db.delete_client_policy(client)
    await client_policy_store.load(db)
    return {"success": True, "policies": await _client_policy_view()}


@router.post("/api/tokens/{token_id}/reserved-client")
async def set_token_reserved_client(token_id: int, request: dict, token: str = Depends(verify_admin_token)):
    """Reserve this account for ONE client (metadata only, no ST needed — same reason as
    /route-key: PUT /api/tokens/{id} forces st_to_at). Blank = shared again."""
    from ..core.logger import debug_logger
    target = await token_manager.get_token(token_id)
    if not target:
        raise HTTPException(status_code=404, detail="Token not found")
    raw = (request.get("reserved_client") or "").strip()
    value = normalize_client(raw)
    if raw and not value:
        raise HTTPException(status_code=400, detail="client must be a-z 0-9 . _ - (max 40 chars)")
    await db.update_token(token_id, reserved_client=value)
    debug_logger.op_warning(f"[CLIENT_POLICY] token={token_id} ({target.email}) reserved_client={value or '(cleared)'}")
    return {"success": True, "token": {"id": token_id, "email": target.email, "reserved_client": value}}


@router.get("/api/call-logic/config")
async def get_call_logic_config(token: str = Depends(verify_admin_token)):
    """Get token call logic configuration."""
    config_obj = await db.get_call_logic_config()
    call_mode = getattr(config_obj, "call_mode", None)
    if call_mode not in ("default", "polling"):
        call_mode = "polling" if getattr(config_obj, "polling_mode_enabled", False) else "default"
    return {
        "success": True,
        "config": {
            "call_mode": call_mode,
            "polling_mode_enabled": call_mode == "polling",
            "tier_order": config_obj.tier_order,
        }
    }


@router.post("/api/call-logic/config")
async def update_call_logic_config(
    request: CallLogicConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update token call logic configuration."""
    if request.call_mode is not None and request.call_mode not in ("default", "polling"):
        raise HTTPException(status_code=400, detail="Invalid call_mode")
    if request.tier_order is not None and request.tier_order not in ("save_ultra", "balanced"):
        raise HTTPException(status_code=400, detail="Invalid tier_order")
    if request.call_mode is None and request.tier_order is None:
        raise HTTPException(status_code=400, detail="Nothing to update")

    await db.update_call_logic_config(call_mode=request.call_mode, tier_order=request.tier_order)
    await db.reload_config_to_memory()
    saved = await db.get_call_logic_config()

    return {
        "success": True,
        "message": "Token rotation mode saved",
        "config": {
            "call_mode": saved.call_mode,
            "polling_mode_enabled": saved.call_mode == "polling",
            "tier_order": saved.tier_order,
        }
    }


# ========== System Info ==========

@router.get("/api/system/info")
async def get_system_info(token: str = Depends(verify_admin_token)):
    """Get system information"""
    stats = await db.get_system_info_stats()

    return {
        "success": True,
        "info": {
            "total_tokens": stats["total_tokens"],
            "active_tokens": stats["active_tokens"],
            "total_credits": stats["total_credits"],
            "version": "1.0.0"
        }
    }


# ========== Additional Routes for Frontend Compatibility ==========

@router.post("/api/login")
async def login(request: LoginRequest, response: Response):
    """Login endpoint (alias for /api/admin/login)"""
    return await admin_login(request, response)


@router.post("/api/logout")
async def logout(response: Response, token: str = Depends(verify_admin_token)):
    """Logout endpoint (alias for /api/admin/logout)"""
    return await admin_logout(response, token)


@router.get("/health")
async def health_check():
    """Public health check endpoint - no auth required"""
    try:
        return await build_public_health_snapshot(db)
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"backend_running": True, "database_available": False,
                     "has_active_tokens": False},
        )


@router.get("/api/stats")
async def get_stats(token: str = Depends(verify_admin_token)):
    """Get statistics for dashboard"""
    return await db.get_dashboard_stats()


@router.get("/api/logs")
async def get_logs(
    limit: int = 100,
    token: str = Depends(verify_admin_token)
):
    """Get lightweight request logs for list view"""
    limit = max(1, min(limit, 100))
    logs = await db.get_logs(limit=limit, include_payload=False)

    result = []
    for log in logs:
        raw_status_code = log.get("status_code")
        try:
            status_code = int(raw_status_code) if raw_status_code is not None else None
        except (TypeError, ValueError):
            status_code = None
        result.append({
            "id": log.get("id"),
            "token_id": log.get("token_id"),
            "token_email": log.get("token_email"),
            "token_username": log.get("token_username"),
            "operation": log.get("operation"),
            "status_code": status_code if status_code is not None else raw_status_code,
            "duration": log.get("duration"),
            "status_text": log.get("status_text") or "",
            "progress": log.get("progress") or 0,
            "created_at": log.get("created_at"),
            "updated_at": log.get("updated_at"),
            "error_summary": _extract_error_summary(log.get("response_body_excerpt")) if status_code is not None and status_code >= 400 else "",
        })
    return result


@router.get("/api/logs/{log_id}")
async def get_log_detail(
    log_id: int,
    token: str = Depends(verify_admin_token)
):
    """Get single request log detail (payload loaded on demand)"""
    log = await db.get_log_detail(log_id)
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")

    error_summary = _extract_error_summary(log.get("response_body"))

    return {
        "id": log.get("id"),
        "token_id": log.get("token_id"),
        "token_email": log.get("token_email"),
        "token_username": log.get("token_username"),
        "operation": log.get("operation"),
        "status_code": log.get("status_code"),
        "duration": log.get("duration"),
        "status_text": log.get("status_text") or "",
        "progress": log.get("progress") or 0,
        "created_at": log.get("created_at"),
        "updated_at": log.get("updated_at"),
        "error_summary": error_summary,
        "request_body": log.get("request_body"),
        "response_body": log.get("response_body")
    }


@router.delete("/api/logs")
async def clear_logs(token: str = Depends(verify_admin_token)):
    """Clear all logs"""
    try:
        await db.clear_all_logs()
        return {"success": True, "message": "All logs cleared"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/admin/config")
async def get_admin_config(token: str = Depends(verify_admin_token)):
    """Get admin configuration"""
    admin_config = await db.get_admin_config()

    return {
        "admin_username": admin_config.username,
        "api_key": admin_config.api_key,
        "error_ban_threshold": admin_config.error_ban_threshold,
        "debug_enabled": config.debug_enabled  # Return actual debug status
    }


@router.post("/api/admin/config")
async def update_admin_config(
    request: UpdateAdminConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update admin configuration (error_ban_threshold)"""
    # Update error_ban_threshold in database
    await db.update_admin_config(error_ban_threshold=request.error_ban_threshold)

    return {"success": True, "message": "Config updated"}


@router.post("/api/admin/password")
async def update_admin_password(
    request: ChangePasswordRequest,
    token: str = Depends(verify_admin_token)
):
    """Update admin password"""
    return await change_password(request, token)


@router.post("/api/admin/apikey")
async def update_api_key(
    request: UpdateAPIKeyRequest,
    token: str = Depends(verify_admin_token)
):
    """Update API key (for external API calls, NOT for admin login)"""
    # Update API key in database
    await db.update_admin_config(api_key=request.new_api_key)

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()

    return {"success": True, "message": "API key updated"}


@router.post("/api/admin/debug")
async def update_debug_config(
    request: UpdateDebugConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update debug configuration"""
    try:
        # Update in-memory config only (not database)
        # This ensures debug mode is automatically disabled on restart
        config.set_debug_enabled(request.enabled)

        status = "enabled" if request.enabled else "disabled"
        return {"success": True, "message": f"Debug mode {status}", "enabled": request.enabled}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update debug config: {str(e)}")


@router.get("/api/generation/timeout")
async def get_generation_timeout(token: str = Depends(verify_admin_token)):
    """Get generation timeout configuration"""
    return await get_generation_config(token)


@router.post("/api/generation/timeout")
async def update_generation_timeout(
    request: GenerationConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update generation timeout configuration"""
    await db.update_generation_config(
        image_timeout=request.image_timeout,
        video_timeout=request.video_timeout,
        max_retries=request.max_retries,
        remove_watermark=request.remove_watermark,
    )

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()

    return {"success": True, "message": "Generation config updated"}


# ========== AT Auto Refresh Config ==========

@router.get("/api/token-refresh/config")
async def get_token_refresh_config(token: str = Depends(verify_admin_token)):
    """Get AT/protocol refresh configuration."""
    refresh_config = await db.get_token_refresh_config()
    return {
        "success": True,
        "config": {
            "at_auto_refresh_enabled": True,
            "protocol_refresh_enabled": refresh_config.enabled,
            "refresh_interval_minutes": refresh_config.refresh_interval_minutes,
        }
    }


@router.post("/api/token-refresh/enabled")
async def update_token_refresh_enabled(
    request: Optional[dict] = None,
    token: str = Depends(verify_admin_token)
):
    """Legacy console endpoint — compatibility NO-OP (pre-merge semantics).

    The retained console's "Auto-refresh AT" toggle posts {"enabled": bool} here;
    AT refresh is always on, so that must stay a no-op. Upstream repurposed this
    route to flip the hidden protocol-ST refresher, which would let the legacy
    toggle silently change an unrelated background feature. Changing protocol
    refresh now requires the explicit {"protocol": true} flag (or use
    /api/token-refresh/config)."""
    body = request or {}
    if body.get("protocol") is True and body.get("enabled") is not None:
        await db.update_token_refresh_config(enabled=bool(body.get("enabled")))
        return {"success": True, "message": "Protocol ST refresh config updated"}
    return {
        "success": True,
        "message": "Flow2API AT auto-refresh is always on and cannot be turned off"
    }


@router.post("/api/token-refresh/config")
async def update_token_refresh_config(
    request: TokenRefreshConfigRequest,
    token: str = Depends(verify_admin_token)
):
    refresh_config = await db.update_token_refresh_config(
        enabled=request.enabled,
        refresh_interval_minutes=request.refresh_interval_minutes,
    )
    return {
        "success": True,
        "config": {
            "at_auto_refresh_enabled": True,
            "protocol_refresh_enabled": refresh_config.enabled,
            "refresh_interval_minutes": refresh_config.refresh_interval_minutes,
        }
    }


async def _sync_runtime_cache_config():
    from . import routes
    if routes.generation_handler and routes.generation_handler.file_cache:
        file_cache = routes.generation_handler.file_cache
        file_cache.set_timeout(config.cache_timeout)
        await file_cache.refresh_cleanup_task()

# ========== Cache Configuration Endpoints ==========

@router.get("/api/cache/config")
async def get_cache_config(token: str = Depends(verify_admin_token)):
    """Get cache configuration"""
    cache_config = await db.get_cache_config()

    # Calculate effective base URL
    effective_base_url = cache_config.cache_base_url if cache_config.cache_base_url else "http://127.0.0.1:8000"

    return {
        "success": True,
        "config": {
            "enabled": cache_config.cache_enabled,
            "timeout": cache_config.cache_timeout,
            "base_url": cache_config.cache_base_url or "",
            "effective_base_url": effective_base_url
        }
    }


@router.post("/api/cache/enabled")
async def update_cache_enabled(
    request: dict,
    token: str = Depends(verify_admin_token)
):
    """Update cache enabled status"""
    enabled = request.get("enabled", False)
    await db.update_cache_config(enabled=enabled)

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()
    await _sync_runtime_cache_config()

    return {"success": True, "message": f"Cache {'enabled' if enabled else 'disabled'}"}


@router.post("/api/cache/config")
async def update_cache_config_full(
    request: dict,
    token: str = Depends(verify_admin_token)
):
    """Update complete cache configuration"""
    enabled = request.get("enabled")
    timeout = request.get("timeout")
    base_url = request.get("base_url")

    if timeout is not None:
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Cache timeout must be an integer")
        if timeout < 0:
            raise HTTPException(status_code=400, detail="Cache timeout cannot be less than 0")

    await db.update_cache_config(enabled=enabled, timeout=timeout, base_url=base_url)

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()
    await _sync_runtime_cache_config()

    return {"success": True, "message": "Cache config updated"}


@router.post("/api/cache/base-url")
async def update_cache_base_url(
    request: dict,
    token: str = Depends(verify_admin_token)
):
    """Update cache base URL"""
    base_url = request.get("base_url", "")
    await db.update_cache_config(base_url=base_url)

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()
    await _sync_runtime_cache_config()

    return {"success": True, "message": "Cache base URL updated"}


@router.post("/api/captcha/config")
async def update_captcha_config(
    request: dict,
    token: str = Depends(verify_admin_token)
):
    """Update captcha configuration"""
    captcha_method = request.get("captcha_method")
    yescaptcha_api_key = request.get("yescaptcha_api_key")
    yescaptcha_base_url = request.get("yescaptcha_base_url")
    yescaptcha_task_type = normalize_yescaptcha_task_type(request.get("yescaptcha_task_type"))
    capmonster_api_key = request.get("capmonster_api_key")
    capmonster_base_url = request.get("capmonster_base_url")
    ezcaptcha_api_key = request.get("ezcaptcha_api_key")
    ezcaptcha_base_url = request.get("ezcaptcha_base_url")
    capsolver_api_key = request.get("capsolver_api_key")
    capsolver_base_url = request.get("capsolver_base_url")
    remote_browser_base_url = request.get("remote_browser_base_url")
    remote_browser_api_key = request.get("remote_browser_api_key")
    remote_browser_timeout = request.get("remote_browser_timeout", 60)
    browser_proxy_enabled = request.get("browser_proxy_enabled", False)
    browser_proxy_url = request.get("browser_proxy_url", "")
    browser_count = request.get("browser_count", 1)
    personal_project_pool_size = request.get("personal_project_pool_size")
    personal_max_resident_tabs = request.get("personal_max_resident_tabs")
    browser_personal_fresh_restart_every_n_solves = request.get(
        "browser_personal_fresh_restart_every_n_solves",
        10,
    )
    personal_idle_tab_ttl_seconds = request.get("personal_idle_tab_ttl_seconds")
    server_fallback_enabled = request.get("server_fallback_enabled")
    server_fallback_max_browsers = request.get("server_fallback_max_browsers")
    if server_fallback_max_browsers is not None:
        try:
            server_fallback_max_browsers = max(1, min(6, int(server_fallback_max_browsers)))
        except Exception:
            return {"success": False, "message": "Server fallback browsers must be a number from 1 to 6"}

    # Validate browser proxy URL format
    if browser_proxy_enabled and browser_proxy_url:
        is_valid, error_msg = _validate_browser_proxy_url_local(browser_proxy_url)
        if not is_valid:
            return {"success": False, "message": error_msg}

    if remote_browser_base_url:
        try:
            remote_browser_base_url = _normalize_http_base_url(remote_browser_base_url)
        except RuntimeError as e:
            return {"success": False, "message": str(e)}

    try:
        remote_browser_timeout = max(5, int(remote_browser_timeout or 60))
    except Exception:
        return {"success": False, "message": "Remote captcha timeout must be a whole number of seconds"}
    try:
        browser_count = max(1, min(20, int(browser_count or 1)))
    except Exception:
        return {"success": False, "message": "Browser instance count must be an integer"}
    try:
        browser_personal_fresh_restart_every_n_solves = max(
            0,
            int(browser_personal_fresh_restart_every_n_solves if browser_personal_fresh_restart_every_n_solves is not None else 10),
        )
    except Exception:
        return {"success": False, "message": "Restart-after-solves count must be an integer (0 = disabled)"}

    if captcha_method == "remote_browser":
        if not (remote_browser_base_url or "").strip():
            return {"success": False, "message": "remote_browser mode needs the remote captcha service URL"}
        if not (remote_browser_api_key or "").strip():
            return {"success": False, "message": "remote_browser mode needs the remote captcha service API key"}

    await db.update_captcha_config(
        captcha_method=captcha_method,
        yescaptcha_api_key=yescaptcha_api_key,
        yescaptcha_base_url=yescaptcha_base_url,
        yescaptcha_task_type=yescaptcha_task_type,
        capmonster_api_key=capmonster_api_key,
        capmonster_base_url=capmonster_base_url,
        ezcaptcha_api_key=ezcaptcha_api_key,
        ezcaptcha_base_url=ezcaptcha_base_url,
        capsolver_api_key=capsolver_api_key,
        capsolver_base_url=capsolver_base_url,
        remote_browser_base_url=remote_browser_base_url,
        remote_browser_api_key=remote_browser_api_key,
        remote_browser_timeout=remote_browser_timeout,
        browser_proxy_enabled=browser_proxy_enabled,
        browser_proxy_url=browser_proxy_url if browser_proxy_enabled else None,
        browser_count=browser_count,
        personal_project_pool_size=personal_project_pool_size,
        personal_max_resident_tabs=personal_max_resident_tabs,
        browser_personal_fresh_restart_every_n_solves=browser_personal_fresh_restart_every_n_solves,
        personal_idle_tab_ttl_seconds=personal_idle_tab_ttl_seconds,
        server_fallback_enabled=(bool(server_fallback_enabled) if server_fallback_enabled is not None else None),
        server_fallback_max_browsers=server_fallback_max_browsers,
    )

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()
    try:
        # Disabled or a lower cap: idle fallback browsers go now, busy ones at their next sweep.
        from ..services.flow_page_captcha import FlowPageCaptchaService
        await (await FlowPageCaptchaService.get_instance()).sweep_idle(
            ttl_seconds=0 if not config.captcha_server_fallback_enabled else 10**9
        )
    except Exception:
        pass

    runtime_prepare_started = False
    runtime_prepare_message = ""
    runtime_status_method = None

    if captcha_method in {"browser", "personal"}:
        runtime_status_method = captcha_method
        runtime_prepare_started = _schedule_captcha_runtime_prepare(captcha_method)
        if captcha_method == "browser":
            runtime_prepare_message = (
                "Started preparing the headed-browser captcha runtime; install progress will show automatically."
            )
        else:
            runtime_prepare_message = (
                "Started preparing the built-in browser captcha runtime; install progress will show automatically."
            )

    return {
        "success": True,
        "message": "Captcha config updated",
        "runtime_prepare_started": runtime_prepare_started,
        "runtime_prepare_message": runtime_prepare_message,
        "runtime_status_method": runtime_status_method,
    }


@router.get("/api/captcha/runtime-status")
async def get_captcha_runtime_status(
    method: str = "browser",
    token: str = Depends(verify_admin_token)
):
    """Get background browser runtime preparation status."""
    if (method or "").strip().lower() == "extension":
        # Extension mode has no runtime to prepare; report the server fallback pool instead.
        from ..services.flow_page_captcha import FlowPageCaptchaService
        service = await FlowPageCaptchaService.get_instance()
        status = {"state": "idle", "active": False, "message": "", "error": "", "method": "extension", "task_running": False}
        status["server_fallback"] = service.status()
        return status
    runtime_method = _normalize_runtime_method(method)
    task = captcha_runtime_prepare_tasks.get(runtime_method)
    status = get_runtime_status(runtime_method)
    status["method"] = runtime_method
    status["task_running"] = bool(task and not task.done())
    return status


@router.get("/api/captcha/config")
async def get_captcha_config(token: str = Depends(verify_admin_token)):
    """Get captcha configuration"""
    captcha_config = await db.get_captcha_config()
    return {
        "captcha_method": captcha_config.captcha_method,
        "yescaptcha_api_key": captcha_config.yescaptcha_api_key,
        "yescaptcha_base_url": captcha_config.yescaptcha_base_url,
        "yescaptcha_task_type": captcha_config.yescaptcha_task_type,
        "capmonster_api_key": captcha_config.capmonster_api_key,
        "capmonster_base_url": captcha_config.capmonster_base_url,
        "ezcaptcha_api_key": captcha_config.ezcaptcha_api_key,
        "ezcaptcha_base_url": captcha_config.ezcaptcha_base_url,
        "capsolver_api_key": captcha_config.capsolver_api_key,
        "capsolver_base_url": captcha_config.capsolver_base_url,
        "remote_browser_base_url": captcha_config.remote_browser_base_url,
        "remote_browser_api_key": captcha_config.remote_browser_api_key,
        "remote_browser_timeout": captcha_config.remote_browser_timeout,
        "browser_proxy_enabled": captcha_config.browser_proxy_enabled,
        "browser_proxy_url": captcha_config.browser_proxy_url or "",
        "browser_count": captcha_config.browser_count,
        "personal_project_pool_size": captcha_config.personal_project_pool_size,
        "personal_max_resident_tabs": captcha_config.personal_max_resident_tabs,
        "browser_personal_fresh_restart_every_n_solves": captcha_config.browser_personal_fresh_restart_every_n_solves,
        "personal_idle_tab_ttl_seconds": captcha_config.personal_idle_tab_ttl_seconds,
        "server_fallback_enabled": bool(captcha_config.server_fallback_enabled),
        "server_fallback_max_browsers": captcha_config.server_fallback_max_browsers,
    }


@router.post("/api/captcha/score-test")
async def test_captcha_score(
    _request: Optional[CaptchaScoreTestRequest] = None,
    _token: str = Depends(verify_admin_token)
):
    """Get a token with the current captcha method and submit it to antcpt to check the score."""
    req = _request or CaptchaScoreTestRequest()
    website_url = (req.website_url or "https://antcpt.com/score_detector/").strip()
    website_key = (req.website_key or "6LcR_okUAAAAAPYrPe-HK_0RULO1aZM15ENyM-Mf").strip()
    action = (req.action or "homepage").strip()
    verify_url = (req.verify_url or "https://antcpt.com/score_detector/verify.php").strip()
    enterprise = bool(req.enterprise)

    started_at = time.time()
    captcha_config = await db.get_captcha_config()
    captcha_method = (captcha_config.captcha_method or config.captcha_method or "").strip().lower()
    browser_proxy_enabled = bool(captcha_config.browser_proxy_enabled)
    browser_proxy_url = captcha_config.browser_proxy_url or ""

    token_value: Optional[str] = None
    fingerprint: Optional[Dict[str, Any]] = None
    token_elapsed_ms = 0
    verify_elapsed_ms = 0
    verify_http_status = None
    verify_result: Dict[str, Any] = {}
    verify_headers: Dict[str, str] = {}
    verify_proxy_used = False
    verify_proxy_source = "none"
    verify_proxy_url = ""
    verify_impersonate = "chrome120"
    page_verify_only = captcha_method in {"browser", "personal", "remote_browser"}
    verify_mode = "browser_page" if page_verify_only else "server_post"

    try:
        token_start = time.time()
        if captcha_method == "browser":
            from ..services.browser_captcha import BrowserCaptchaService
            service = await BrowserCaptchaService.get_instance(db)
            score_payload, browser_id = await service.get_custom_score(
                website_url=website_url,
                website_key=website_key,
                verify_url=verify_url,
                action=action,
                enterprise=enterprise
            )
            if isinstance(score_payload, dict):
                token_value = score_payload.get("token")
                verify_elapsed_ms = int(score_payload.get("verify_elapsed_ms") or 0)
                verify_http_status = score_payload.get("verify_http_status")
                verify_result = score_payload.get("verify_result") if isinstance(score_payload.get("verify_result"), dict) else {}
                verify_mode = score_payload.get("verify_mode") or "browser_page"
                score_token_elapsed = score_payload.get("token_elapsed_ms")
                if isinstance(score_token_elapsed, (int, float)):
                    token_elapsed_ms = int(score_token_elapsed)
            if token_value:
                fingerprint = await service.get_fingerprint(browser_id)
                verify_proxy_used = bool(browser_proxy_enabled and browser_proxy_url)
                verify_proxy_source = "captcha_browser_proxy" if verify_proxy_used else "browser_direct"
                verify_proxy_url = browser_proxy_url if verify_proxy_used else ""
        elif captcha_method == "personal":
            from ..services.browser_captcha_personal import BrowserCaptchaService
            service = await BrowserCaptchaService.get_instance(db)
            score_payload = await service.get_custom_score(
                website_url=website_url,
                website_key=website_key,
                verify_url=verify_url,
                action=action,
                enterprise=enterprise
            )
            if isinstance(score_payload, dict):
                token_value = score_payload.get("token")
                verify_elapsed_ms = int(score_payload.get("verify_elapsed_ms") or 0)
                verify_http_status = score_payload.get("verify_http_status")
                verify_result = score_payload.get("verify_result") if isinstance(score_payload.get("verify_result"), dict) else {}
                verify_mode = score_payload.get("verify_mode") or "browser_page"
                score_token_elapsed = score_payload.get("token_elapsed_ms")
                if isinstance(score_token_elapsed, (int, float)):
                    token_elapsed_ms = int(score_token_elapsed)
            if token_value:
                fingerprint = service.get_last_fingerprint()
                verify_proxy_used = bool(browser_proxy_enabled and browser_proxy_url)
                verify_proxy_source = "captcha_browser_proxy" if verify_proxy_used else "browser_direct"
                verify_proxy_url = browser_proxy_url if verify_proxy_used else ""
        elif captcha_method == "remote_browser":
            score_payload = await _score_test_with_remote_browser_service(
                website_url=website_url,
                website_key=website_key,
                verify_url=verify_url,
                action=action,
                enterprise=enterprise,
            )
            if isinstance(score_payload, dict):
                if score_payload.get("success") is False:
                    raise RuntimeError(score_payload.get("message") or "Remote captcha score test failed")
                token_value = score_payload.get("token")
                verify_elapsed_ms = int(score_payload.get("verify_elapsed_ms") or 0)
                verify_http_status = score_payload.get("verify_http_status")
                verify_result = score_payload.get("verify_result") if isinstance(score_payload.get("verify_result"), dict) else {}
                verify_mode = score_payload.get("verify_mode") or "remote_browser_page"
                score_token_elapsed = score_payload.get("token_elapsed_ms")
                if isinstance(score_token_elapsed, (int, float)):
                    token_elapsed_ms = int(score_token_elapsed)
                fingerprint = score_payload.get("fingerprint") if isinstance(score_payload.get("fingerprint"), dict) else None
        elif captcha_method in SUPPORTED_API_CAPTCHA_METHODS:
            if captcha_method == "capsolver" and "antcpt.com" in website_url:
                # CapSolver specifically blocks antcpt.com. Test against labs.google to verify API key config.
                token_value = await _solve_recaptcha_with_api_service(
                    method=captcha_method,
                    website_url="https://labs.google/",
                    website_key="6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV",
                    action="IMAGE_GENERATION",
                    enterprise=True
                )
                if token_value:
                    if token_elapsed_ms <= 0:
                        token_elapsed_ms = int((time.time() - token_start) * 1000)
                    return {
                        "success": True,
                        "message": "CapSolver does not support antcpt. Connectivity tested successfully against Google Labs",
                        "captcha_method": captcha_method,
                        "website_url": "https://labs.google/",
                        "website_key": "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV",
                        "action": "IMAGE_GENERATION",
                        "verify_url": "",
                        "enterprise": True,
                        "token_acquired": True,
                        "token_preview": _mask_token(token_value),
                        "token_elapsed_ms": token_elapsed_ms,
                        "verify_elapsed_ms": 0,
                        "verify_http_status": 200,
                        "score": 0.9,
                        "verify_result": {"success": True, "message": "Score check skipped"},
                        "verify_request_meta": {},
                        "browser_proxy_enabled": browser_proxy_enabled,
                        "browser_proxy_url": browser_proxy_url if browser_proxy_enabled else "",
                        "fingerprint": fingerprint,
                        "elapsed_ms": int((time.time() - started_at) * 1000)
                    }
            else:
                token_value = await _solve_recaptcha_with_api_service(
                    method=captcha_method,
                    website_url=website_url,
                    website_key=website_key,
                    action=action,
                    enterprise=enterprise
                )
        else:
            return {
                "success": False,
                "message": f"Current captcha method does not support score tests: {captcha_method}",
                "captcha_method": captcha_method,
                "website_url": website_url,
                "website_key": website_key,
                "action": action,
                "verify_url": verify_url,
                "enterprise": enterprise,
                "token_acquired": False,
                "elapsed_ms": int((time.time() - started_at) * 1000)
            }
        if token_elapsed_ms <= 0:
            token_elapsed_ms = int((time.time() - token_start) * 1000)

        # Remote headed captcha custom-score may verify inside the page and, in some
        # implementations, not return the token; fall back to verify_result locally.
        if captcha_method == "remote_browser" and not token_value and isinstance(verify_result, dict):
            if verify_result.get("success") is True:
                token_value = verify_result.get("token") or verify_result.get("gRecaptchaResponse") or "__verified_by_remote__"

        if not token_value:
            return {
                "success": False,
                "message": "No reCAPTCHA token obtained",
                "captcha_method": captcha_method,
                "website_url": website_url,
                "website_key": website_key,
                "action": action,
                "verify_url": verify_url,
                "enterprise": enterprise,
                "token_acquired": False,
                "token_elapsed_ms": token_elapsed_ms,
                "browser_proxy_enabled": browser_proxy_enabled,
                "browser_proxy_url": browser_proxy_url if browser_proxy_enabled else "",
                "fingerprint": fingerprint,
                "elapsed_ms": int((time.time() - started_at) * 1000)
            }

        if verify_mode == "server_post" and not page_verify_only:
            verify_start = time.time()
            verify_headers = {
                "accept": "application/json, text/javascript, */*; q=0.01",
                "content-type": "application/json",
                "origin": "https://antcpt.com",
                "referer": website_url,
                "x-requested-with": "XMLHttpRequest",
            }
            if isinstance(fingerprint, dict):
                ua = (fingerprint.get("user_agent") or "").strip()
                lang = (fingerprint.get("accept_language") or "").strip()
                sec_ch_ua = (fingerprint.get("sec_ch_ua") or "").strip()
                sec_ch_ua_mobile = (fingerprint.get("sec_ch_ua_mobile") or "").strip()
                sec_ch_ua_platform = (fingerprint.get("sec_ch_ua_platform") or "").strip()

                if ua:
                    verify_headers["user-agent"] = ua
                if lang:
                    verify_headers["accept-language"] = lang if "," in lang else f"{lang},zh;q=0.9"
                if sec_ch_ua:
                    verify_headers["sec-ch-ua"] = sec_ch_ua
                if sec_ch_ua_mobile:
                    verify_headers["sec-ch-ua-mobile"] = sec_ch_ua_mobile
                if sec_ch_ua_platform:
                    verify_headers["sec-ch-ua-platform"] = sec_ch_ua_platform

            if verify_headers.get("user-agent"):
                for header_name, header_value in _guess_client_hints_from_user_agent(
                    verify_headers.get("user-agent", "")
                ).items():
                    if header_value and not verify_headers.get(header_name):
                        verify_headers[header_name] = header_value
                verify_impersonate = _guess_impersonate_from_user_agent(verify_headers.get("user-agent", ""))

            verify_proxies, verify_proxy_used, verify_proxy_source, verify_proxy_url = (
                await _resolve_score_test_verify_proxy(
                    captcha_method=captcha_method,
                    browser_proxy_enabled=browser_proxy_enabled,
                    browser_proxy_url=browser_proxy_url
                )
            )

            async with AsyncSession() as session:
                verify_resp = await session.post(
                    verify_url,
                    json={"g-recaptcha-response": token_value},
                    headers=verify_headers,
                    proxies=verify_proxies,
                    impersonate=verify_impersonate,
                    timeout=30
                )
            verify_elapsed_ms = int((time.time() - verify_start) * 1000)
            verify_http_status = verify_resp.status_code

            try:
                verify_result = verify_resp.json()
            except Exception:
                verify_result = {"raw": verify_resp.text}
        else:
            verify_headers = {
                "origin": "https://antcpt.com",
                "referer": website_url,
                "x-requested-with": "XMLHttpRequest",
            }
            if isinstance(fingerprint, dict):
                verify_headers.update({
                    "user-agent": fingerprint.get("user_agent", ""),
                    "accept-language": fingerprint.get("accept_language", ""),
                    "sec-ch-ua": fingerprint.get("sec_ch_ua", ""),
                    "sec-ch-ua-mobile": fingerprint.get("sec_ch_ua_mobile", ""),
                    "sec-ch-ua-platform": fingerprint.get("sec_ch_ua_platform", ""),
                })

        verify_success = bool(verify_result.get("success")) if isinstance(verify_result, dict) else False
        score_value = verify_result.get("score") if isinstance(verify_result, dict) else None

        return {
            "success": verify_success,
            "message": "Score check passed" if verify_success else "Score check failed",
            "captcha_method": captcha_method,
            "website_url": website_url,
            "website_key": website_key,
            "action": action,
            "verify_url": verify_url,
            "enterprise": enterprise,
            "token_acquired": True,
            "token_preview": _mask_token(token_value),
            "token_elapsed_ms": token_elapsed_ms,
            "verify_elapsed_ms": verify_elapsed_ms,
            "verify_http_status": verify_http_status,
            "score": score_value,
            "verify_result": verify_result,
            "verify_request_meta": {
                "mode": verify_mode,
                "proxy_used": verify_proxy_used,
                "user_agent": verify_headers.get("user-agent", ""),
                "accept_language": verify_headers.get("accept-language", ""),
                "sec_ch_ua": verify_headers.get("sec-ch-ua", ""),
                "sec_ch_ua_mobile": verify_headers.get("sec-ch-ua-mobile", ""),
                "sec_ch_ua_platform": verify_headers.get("sec-ch-ua-platform", ""),
                "origin": verify_headers.get("origin", ""),
                "referer": verify_headers.get("referer", ""),
                "x_requested_with": verify_headers.get("x-requested-with", ""),
                "proxy_source": verify_proxy_source,
                "proxy_url": verify_proxy_url,
                "impersonate": verify_impersonate,
            },
            "browser_proxy_enabled": browser_proxy_enabled,
            "browser_proxy_url": browser_proxy_url if browser_proxy_enabled else "",
            "fingerprint": fingerprint,
            "elapsed_ms": int((time.time() - started_at) * 1000)
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Score test failed: {str(e)}",
            "captcha_method": captcha_method,
            "website_url": website_url,
            "website_key": website_key,
            "action": action,
            "verify_url": verify_url,
            "enterprise": enterprise,
            "token_acquired": bool(token_value),
            "token_preview": _mask_token(token_value),
            "token_elapsed_ms": token_elapsed_ms,
            "verify_elapsed_ms": verify_elapsed_ms,
            "verify_http_status": verify_http_status,
            "verify_result": verify_result,
            "verify_request_meta": {
                "mode": verify_mode,
                "proxy_used": verify_proxy_used,
                "user_agent": verify_headers.get("user-agent", ""),
                "accept_language": verify_headers.get("accept-language", ""),
                "sec_ch_ua": verify_headers.get("sec-ch-ua", ""),
                "sec_ch_ua_mobile": verify_headers.get("sec-ch-ua-mobile", ""),
                "sec_ch_ua_platform": verify_headers.get("sec-ch-ua-platform", ""),
                "origin": verify_headers.get("origin", ""),
                "referer": verify_headers.get("referer", ""),
                "x_requested_with": verify_headers.get("x-requested-with", ""),
                "proxy_source": verify_proxy_source,
                "proxy_url": verify_proxy_url,
                "impersonate": verify_impersonate,
            },
            "browser_proxy_enabled": browser_proxy_enabled,
            "browser_proxy_url": browser_proxy_url if browser_proxy_enabled else "",
            "fingerprint": fingerprint,
            "elapsed_ms": int((time.time() - started_at) * 1000)
        }


# ========== Plugin Configuration Endpoints ==========

async def _verify_plugin_connection_token(authorization: Optional[str]) -> None:
    plugin_config = await db.get_plugin_config()
    provided_token = None
    if authorization:
        if authorization.startswith("Bearer "):
            provided_token = authorization[7:]
        else:
            provided_token = authorization
    if not plugin_config.connection_token or provided_token != plugin_config.connection_token:
        raise HTTPException(status_code=401, detail="Invalid connection token")


@router.get("/api/plugin/config")
async def get_plugin_config(request: Request, token: str = Depends(verify_admin_token)):
    """Get plugin configuration"""
    plugin_config = await db.get_plugin_config()

    # Get the actual domain and port from the request
    # This allows the connection URL to reflect the user's actual access path
    host_header = request.headers.get("host", "")

    # Generate connection URL based on actual request
    if host_header:
        # Use the actual domain/IP and port from the request
        connection_url = f"http://{host_header}/api/plugin/update-token"
    else:
        # Fallback to config-based URL
        from ..core.config import config
        server_host = config.server_host
        server_port = config.server_port

        if server_host == "0.0.0.0":
            connection_url = f"http://127.0.0.1:{server_port}/api/plugin/update-token"
        else:
            connection_url = f"http://{server_host}:{server_port}/api/plugin/update-token"

    return {
        "success": True,
        "config": {
            "connection_token": plugin_config.connection_token,
            "connection_url": connection_url,
            "auto_enable_on_update": plugin_config.auto_enable_on_update,
            "ext_proxy_pool": _parse_ext_proxy_pool(plugin_config.ext_proxy_pool),
        }
    }


def _parse_ext_proxy_pool(raw: Optional[str]) -> Optional[dict]:
    """Parse the stored residential proxy pool JSON ({host,user,pass,ports:[...]}); None if unset/invalid."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("ports"):
            return data
    except Exception:
        pass
    return None


@router.post("/api/plugin/config")
async def update_plugin_config(
    request: dict,
    token: str = Depends(verify_admin_token)
):
    """Update plugin configuration"""
    connection_token = request.get("connection_token", "")
    auto_enable_on_update = request.get("auto_enable_on_update", True)  # On by default

    # Generate random token if empty
    if not connection_token:
        connection_token = secrets.token_urlsafe(32)

    # #2 residential proxy pool: accept a dict {host,user,pass,ports} or a JSON string.
    # Absent => leave unchanged. Ports may be pasted as "8006, 8007" or a list.
    ext_proxy_pool = None
    if "ext_proxy_pool" in request:
        _pp = request.get("ext_proxy_pool")
        if isinstance(_pp, dict):
            ports = _pp.get("ports")
            if isinstance(ports, str):
                ports = [int(p) for p in re.findall(r"\d+", ports)]
            _pp = {
                "host": (_pp.get("host") or "").strip(),
                "user": (_pp.get("user") or "").strip(),
                "pass": (_pp.get("pass") or ""),
                "ports": sorted(set(int(p) for p in (ports or []))),
            }
            ext_proxy_pool = json.dumps(_pp) if _pp["ports"] and _pp["host"] else ""
        elif isinstance(_pp, str):
            ext_proxy_pool = _pp.strip()

    await db.update_plugin_config(
        connection_token=connection_token,
        auto_enable_on_update=auto_enable_on_update,
        ext_proxy_pool=ext_proxy_pool,
    )

    return {
        "success": True,
        "message": "Plugin config updated",
        "connection_token": connection_token,
        "auto_enable_on_update": auto_enable_on_update,
        "ext_proxy_pool": _parse_ext_proxy_pool(ext_proxy_pool),
    }


@router.get("/api/plugin/proxy-pool")
async def plugin_proxy_pool(
    route_key: Optional[str] = None, authorization: Optional[str] = Header(None)
):
    """The worker extension fetches its residential proxy pool from here (connection-token
    authed, like /update-token). Returns {host,user,pass,ports:[...]} or {} if unset, so
    new IPs added in the admin UI are picked up WITHOUT redistributing the extension.

    When the device sends its route_key, we also COORDINATE its IP: `assigned_port` is a
    least-loaded pick, so each account gets its own IP while ports are free and they spread
    evenly once accounts exceed IPs. The extension mints+redeems through that port."""
    plugin_config = await db.get_plugin_config()
    provided = authorization[7:] if (authorization or "").startswith("Bearer ") else (authorization or "")
    if not plugin_config.connection_token or provided != plugin_config.connection_token:
        raise HTTPException(status_code=401, detail="Invalid connection token")
    pool = _parse_ext_proxy_pool(plugin_config.ext_proxy_pool)
    assigned_port = None
    if pool and route_key:
        try:
            assigned_port = await db.assign_device_port((route_key or "").strip(), pool.get("ports") or [])
        except Exception as e:
            debug_logger.op_warning(f"[PROXY_POOL] could not assign port for route_key: {e}")
    return {"success": True, "pool": pool or {}, "assigned_port": assigned_port}


@router.post("/api/plugin/update-token")
async def plugin_update_token(request: dict, authorization: Optional[str] = Header(None)):
    """Receive token update from Chrome extension (no admin auth required, uses connection_token)"""
    await _verify_plugin_connection_token(authorization)
    plugin_config = await db.get_plugin_config()

    # Extract session token from request
    session_token = request.get("session_token")
    if session_token is not None and not isinstance(session_token, str):
        raise HTTPException(status_code=400, detail="session_token must be a string")
    session_token = (session_token or "").strip()

    # Cookie sync (worker 3.7.0, docs/cookie-sync.md): the Google login cookies of the
    # worker's Chrome profile. None = not sent (older worker: leave stored state alone);
    # "" = the switch is OFF (delete the server copy); JSON list = store/replace.
    google_cookies = _parse_cookie_sync_field(request.get("google_cookies"))
    cookie_seq = _parse_cookie_sync_seq(request.get("cookie_sync_seq"))

    if not session_token and not google_cookies:
        raise HTTPException(status_code=400, detail="Missing session_token")

    # Optional explicit attribution for admin-triggered on-demand refresh. When
    # absent (autonomous extension pushes), we fall back to email matching below.
    token_id_override = request.get("token_id")
    if token_id_override is not None:
        try:
            token_id_override = int(token_id_override)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid token_id")

    # Slice B: the extension reports the residential proxy it minted through + its real
    # browser UA, so the server can redeem the generate call from the SAME IP/UA. Stored
    # per-account; only accepted if they look sane (defensive — never trust blindly).
    reported_proxy_url = _sanitize_reported_proxy(request.get("proxy_url"))
    reported_user_agent = _sanitize_reported_ua(request.get("user_agent"))
    # #1 per-account routing: the device's stable route key. Binding the account's token
    # to it makes captcha minting for the account route back to THIS device (same IP as
    # the redeem). Only auto-bind an empty or previously-auto ('auto-…') key — never
    # clobber an explicit admin-set Route Key.
    reported_route_key = (request.get("route_key") or "").strip() or None
    # Two-pool routing: the extension's "Failed-image mode" switch. 'failed_image' reserves
    # this account for staff-driven failed-image regeneration (excluded from the auto pool).
    _pm = request.get("pool_mode")
    reported_pool_mode = _pm if _pm in ("auto", "failed_image") else None
    # Version visibility: the extension reports its own manifest version so the admin can
    # see which devices are still on an old build after an update ships.
    _ev = request.get("ext_version")
    reported_ext_version = str(_ev).strip()[:20] if isinstance(_ev, str) and _ev.strip() else None
    # 3.5.2+: the project the worker sees open on flow.google.com (a UUID). Flow no longer lets the
    # server create one through Labs for a fresh account, so a new token is registered with this one.
    _pid = request.get("project_id")
    reported_project_id = _pid.strip().lower() if isinstance(_pid, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", _pid.strip()) else None
    _pname = request.get("project_name")
    reported_project_name = str(_pname).strip()[:80] if isinstance(_pname, str) and _pname.strip() else None

    # Cookie login helper: derive a fresh Labs session from the pushed Google cookies,
    # through the worker's reported residential proxy (else the bound row's stored one).
    bound_row = await db.get_token(token_id_override) if token_id_override is not None else None
    if token_id_override is not None and not bound_row:
        raise HTTPException(status_code=404, detail=f"Token {token_id_override} not found")
    derived_from_cookies = False

    async def _derive_st_from_cookies() -> str:
        nonlocal derived_from_cookies
        proxy = reported_proxy_url or (token_manager.cookie_login_proxy(bound_row) if bound_row else None)
        login = await token_manager.cookie_login(
            bound_row or SimpleNamespace(id="new", email=None, login_account=None),
            google_cookies=google_cookies, proxy=proxy,
        )
        if login.get("success") and login.get("session_token"):
            derived_from_cookies = True
            return str(login["session_token"]).strip()
        reason = login.get("reason") or "rejected"
        if reason in ("network", "timeout"):
            raise HTTPException(status_code=503, detail=f"Google login via cookies failed ({reason}); retry")
        raise HTTPException(status_code=400, detail=f"Google cookies could not log in ({reason}): {login.get('error')}")

    # Step 1: Convert ST to AT to get user info (including email). A dead pushed ST is
    # replaced by one derived from the cookies when the worker sent them.
    async def _validate(st_value: str):
        result = await token_manager.flow_client.st_to_at(st_value)
        at = result["access_token"]
        expires = result.get("expires")
        email = (result.get("user", {}) or {}).get("email", "")
        if not email:
            raise ValueError("Failed to get email from session token")
        from datetime import datetime
        at_expires = None
        if expires:
            try:
                at_expires = datetime.fromisoformat(expires.replace('Z', '+00:00'))
            except Exception:
                pass
        return result, at, at_expires, email

    try:
        if not session_token:
            session_token = await _derive_st_from_cookies()
            result, at, at_expires, email = await _validate(session_token)
        else:
            try:
                result, at, at_expires, email = await _validate(session_token)
            except HTTPException:
                raise
            except Exception as direct_error:
                if not google_cookies:
                    raise
                debug_logger.log_info(f"[COOKIE_SYNC] pushed session token is dead ({str(direct_error)[:80]}); trying the cookies")
                session_token = await _derive_st_from_cookies()
                result, at, at_expires, email = await _validate(session_token)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid session token: {str(e)}")

    # Step 2: Resolve which token row this update applies to.
    # On-demand refresh threads an explicit token_id so the ST is attributed to the
    # CORRECT row even when emails are duplicated, guarded by an email match so a
    # wrong/spoofed token_id can never overwrite a different account's ST. This runs
    # OUTSIDE the st_to_at try/except above, so the 409 is surfaced (not swallowed
    # into a 400). Autonomous pushes (no token_id) keep the original email path.
    if bound_row is not None:
        existing_token = bound_row
        if (existing_token.email or "").strip().lower() != (email or "").strip().lower():
            raise HTTPException(
                status_code=409,
                detail=f"account mismatch: cookie is {email}, token {existing_token.id} is {existing_token.email}",
            )
    else:
        existing_token = await db.get_token_by_email(email)

    # Legacy extensions (< 3.3.5) ignore `action` and clear their login state on any 2xx,
    # so they must keep receiving the old "updated" action; only newer builds understand
    # relogin_required (persisted grant_expired state + red badge + notification).
    ext_supports_relogin = _ext_version_at_least(reported_ext_version, (3, 3, 5))

    if existing_token:
        # Update existing token
        try:
            # VALIDATE-THEN-PROMOTE: the ST/AT pair is stored only if the API accepts the
            # access token. A session whose embedded AT Google stopped renewing (cookie
            # alive, API 401 = at_stale) must never overwrite working credentials nor
            # re-enable the account — that loop is what kept 30 accounts flapping hourly.
            outcome = await token_manager.validate_and_promote(
                existing_token.id, session_token, source="plugin_push"
            )
            if not outcome.success and outcome.reason in ("st_expired", "at_stale") and google_cookies and not derived_from_cookies:
                # The pushed session is dead, or Google stopped renewing its grant: a fresh
                # login from the cookies is exactly what "sign out / sign in" would do.
                bound_row = existing_token
                session_token = await _derive_st_from_cookies()
                outcome = await token_manager.validate_and_promote(
                    existing_token.id, session_token, source="plugin_push_cookies"
                )
            if not outcome.success and outcome.reason == "st_expired":
                raise HTTPException(status_code=400, detail="Invalid session token (expired)")
            if not outcome.success and outcome.reason == "account_mismatch":
                raise HTTPException(status_code=409, detail=f"account mismatch: session is not {existing_token.email}")
            if not outcome.success and outcome.reason not in ("at_stale",):
                # Transport/unknown failure talking to Google: nothing stored; let the
                # device retry next cycle (non-400 ⇒ extension treats it as network).
                raise HTTPException(status_code=503, detail=f"Could not verify session with Google ({outcome.reason}); retry")
            # Only a VERIFIED credential (API accepted the AT) may re-enable or signal the
            # device 'healthy'. A transport-blip promotion (success but unverified) is
            # stored, but treated as unknown for activation purposes.
            credential_ok = outcome.success and outcome.verified
            promoted_unverified = outcome.success and not outcome.verified

            # Cookie sync: store / clear the Google login now that the push proved the
            # account (email). Independent of the AT outcome: an at_stale row needs its
            # cookie backup most. Stale (older-seq) writes are ignored.
            cookie_sync = await _apply_cookie_sync(existing_token, google_cookies, cookie_seq, email)
            cookie_sync["derived_from_cookies"] = derived_from_cookies
            # NOTE: request["proxy_url"] is the worker extension's residential REDEEM
            # proxy (persisted as redeem_proxy_url below) — it must never overwrite the
            # protocol-login proxy_url field.
            await token_manager.update_token(
                token_id=existing_token.id,
                auto_refresh_enabled=request.get("auto_refresh_enabled"),
                refresh_interval_minutes=request.get("refresh_interval_minutes"),
            )

            # Slice B: persist the reported residential proxy + real browser UA so the
            # generate (redeem) request aligns with the reCAPTCHA mint. Written via the DB
            # layer directly (token_manager.update_token has an explicit signature).
            _redeem_updates = {}
            if reported_proxy_url is not None:
                _redeem_updates["redeem_proxy_url"] = reported_proxy_url
            if reported_user_agent is not None:
                _redeem_updates["browser_user_agent"] = reported_user_agent
            if reported_route_key:
                _cur_rk = (existing_token.extension_route_key or "").strip()
                if not _cur_rk or _cur_rk.startswith("auto-"):
                    _redeem_updates["extension_route_key"] = reported_route_key
            if reported_pool_mode is not None:
                _redeem_updates["pool_mode"] = reported_pool_mode
            if reported_ext_version is not None:
                _redeem_updates["ext_version"] = reported_ext_version
            if _redeem_updates:
                await db.update_token(existing_token.id, **_redeem_updates)
                debug_logger.event(
                    f"[REDEEM_REPORT] token={existing_token.id} "
                    f"proxy={mask_proxy_url(reported_proxy_url)} ua_set={reported_user_agent is not None} "
                    f"route_key={_redeem_updates.get('extension_route_key', '(kept)')}"
                )

            if promoted_unverified:
                # Stored, but Google's API was unreachable for verification: no state
                # changes that require proof (no re-enable, no 'healthy' signal).
                return {
                    "success": True,
                    "message": f"Token updated for {email} (verification deferred — Google API unreachable)",
                    "action": "updated",
                    "token_id": existing_token.id,
                    "cookie_sync": cookie_sync,
                    "credential_verified": False,
                }

            if not credential_ok:
                # at_stale: cookie alive, access token dead. Credentials untouched, no
                # re-enable. Tell a capable device to show "sign out / sign in"; older
                # builds just see a normal update (they'd clear login state on any 2xx).
                debug_logger.op_warning(
                    f"[TOKEN] push for token={existing_token.id} ({email}) carries a DEAD access token "
                    f"(ext={reported_ext_version or '?'}) — not promoted, not enabled; device re-login needed"
                )
                return {
                    "success": True,
                    "message": f"Google is no longer renewing this account's access token — sign out of Google Labs and back in, then Reconnect ({email})",
                    "action": "relogin_required" if ext_supports_relogin else "updated",
                    "token_id": existing_token.id,
                    "cookie_sync": cookie_sync,
                    "credential_verified": False,
                }

            # Auto-recover a disabled account — ONLY now that the pushed credential is
            # VERIFIED. A verified login proves an auth-class ban (auto_st_expired /
            # auto_at_stale) is healed; it proves nothing about auto_error / 429 bans, so
            # those are never lifted by a push. A MANUAL disable (ban_reason NULL) is
            # revived only when the admin opted into auto_enable_on_update.
            ban = existing_token.ban_reason or ""
            auth_disabled = ban in token_manager.AUTH_DISABLE_REASONS
            manual_disabled = not ban
            if not existing_token.is_active and (auth_disabled or (manual_disabled and plugin_config.auto_enable_on_update)):
                await token_manager.enable_token(existing_token.id)
                debug_logger.event(
                    f"[TOKEN] auto-re-enabled token={existing_token.id} ({email}) "
                    f"was={existing_token.ban_reason or 'manual'} (credential verified)"
                )
                return {
                    "success": True,
                    "message": f"Token updated and auto-enabled for {email}",
                    "action": "updated",
                    "auto_enabled": True,
                    "token_id": existing_token.id,
                    "cookie_sync": cookie_sync,
                    "credential_verified": True,
                }

            return {
                "success": True,
                "message": f"Token updated for {email}",
                "action": "updated",
                "token_id": existing_token.id,
                "cookie_sync": cookie_sync,
                "credential_verified": True,
            }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to update token: {str(e)}")
    else:
        # Add new token — verify the access token FIRST so a dead grant never gets an
        # active window (add_token swallows get_credits failures and would otherwise
        # insert an active row and create projects with a dead AT).
        dead_grant = False
        probe_verified = False
        try:
            await token_manager.flow_client.get_credits(at)
            probe_verified = True
        except Exception as probe_err:
            if token_manager._is_auth_error(probe_err):
                dead_grant = True
            # transport blip: proceed as before (AT likely fine) but report UNVERIFIED
        try:
            new_token = await token_manager.add_token(
                st=session_token,
                project_id=reported_project_id,
                project_name=reported_project_name,
                remark="Added by Chrome Extension",
                is_active=not dead_grant,
                ban_reason=("auto_at_stale" if dead_grant else None),
                # The AT we probed above is the one stored (no second, unprobed mint).
                session_result=result,
                protocol_mode=("protocol" if google_cookies else "session"),
                google_cookies=(google_cookies or ""),
                login_account=(email if google_cookies else ""),
                # NOTE: request["proxy_url"] is the worker's residential REDEEM proxy
                # (persisted as redeem_proxy_url below), NOT the protocol-login proxy.
                auto_refresh_enabled=request.get("auto_refresh_enabled", True),
                refresh_interval_minutes=request.get("refresh_interval_minutes", 120),
            )

            # Slice B + #1: persist the reported residential proxy, real browser UA, and
            # bind the account to this device via its route key.
            _redeem_updates = {}
            if reported_proxy_url is not None:
                _redeem_updates["redeem_proxy_url"] = reported_proxy_url
            if reported_user_agent is not None:
                _redeem_updates["browser_user_agent"] = reported_user_agent
            if reported_route_key:
                _redeem_updates["extension_route_key"] = reported_route_key
            if reported_pool_mode is not None:
                _redeem_updates["pool_mode"] = reported_pool_mode
            if reported_ext_version is not None:
                _redeem_updates["ext_version"] = reported_ext_version
            if google_cookies:
                _redeem_updates["google_cookies_updated_at"] = datetime.now(timezone.utc)
                _redeem_updates["google_cookies_seq"] = cookie_seq
            if _redeem_updates:
                await db.update_token(new_token.id, **_redeem_updates)
            cookie_sync = {
                "stored": bool(google_cookies), "cleared": False, "stale": False,
                "cookies": (len(_parse_google_cookies(google_cookies)) if google_cookies else 0),
                "derived_from_cookies": derived_from_cookies,
            }
            if google_cookies:
                debug_logger.event(f"[COOKIE_SYNC] token={new_token.id} ({new_token.email}) Google login stored with the new account ({cookie_sync['cookies']} cookies)")

            if dead_grant:
                debug_logger.op_warning(
                    f"[TOKEN] new token={new_token.id} ({new_token.email}) has a DEAD access token "
                    f"— created INACTIVE (auto_at_stale); device re-login needed"
                )
                return {
                    "success": True,
                    "message": f"Account added but Google is not renewing its access token — sign out of Google Labs and back in, then Reconnect ({new_token.email})",
                    "action": "relogin_required" if ext_supports_relogin else "added",
                    "token_id": new_token.id,
                    "cookie_sync": cookie_sync,
                    "credential_verified": False,
                }

            return {
                "success": True,
                "message": f"Token added for {new_token.email}" + ("" if probe_verified else " (verification deferred — Google API unreachable)"),
                "action": "added",
                "token_id": new_token.id,
                "cookie_sync": cookie_sync,
                "credential_verified": probe_verified,
            }
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to add token: {str(e)}")


COOKIE_SYNC_MAX_BYTES = 256 * 1024


def _parse_cookie_sync_field(value) -> Optional[str]:
    """`google_cookies` from a worker push: None = not sent, "" = clear, else a usable
    export (JSON list / dict / name=value text with at least one Google login cookie)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail="google_cookies must be a string")
    text = value.strip()
    if not text:
        return ""
    if len(text) > COOKIE_SYNC_MAX_BYTES:
        raise HTTPException(status_code=413, detail="google_cookies is too large")
    if not google_cookies_usable(text):
        raise HTTPException(status_code=400, detail="google_cookies unusable: no Google login cookie (SID/HSID/SSID/APISID/SAPISID)")
    return text


def _parse_cookie_sync_seq(value) -> int:
    """Client-side write sequence (the worker's Date.now() when it built the push). A
    write with a lower sequence than the stored one is stale and ignored, so an ON push
    that was still in flight can never undo a later OFF."""
    try:
        seq = int(value or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="cookie_sync_seq must be an integer")
    return max(0, seq)


async def _apply_cookie_sync(token_row, google_cookies: Optional[str], cookie_seq: int, email: str) -> dict:
    """Store (non-empty) or clear ("") the Google login for a token row. Returns the
    `cookie_sync` object the worker shows in its popup. Never logs a cookie value."""
    out = {"stored": False, "cleared": False, "stale": False, "cookies": 0}
    if google_cookies is None:
        return out
    latest = await db.get_token(token_row.id) or token_row
    stored_seq = int(getattr(latest, "google_cookies_seq", 0) or 0)
    if cookie_seq and stored_seq and cookie_seq < stored_seq:
        out["stale"] = True
        debug_logger.log_info(f"[COOKIE_SYNC] token={token_row.id} stale write ignored (seq {cookie_seq} < {stored_seq})")
        return out
    now = datetime.now(timezone.utc)
    if google_cookies == "":
        await db.update_token(
            token_row.id, google_cookies="", protocol_mode="session", login_account="",
            google_cookies_updated_at=now, google_cookies_seq=cookie_seq,
        )
        out["cleared"] = True
        if (getattr(latest, "google_cookies", "") or "").strip():
            debug_logger.event(f"[COOKIE_SYNC] token={token_row.id} ({email}) Google login deleted (switch OFF)")
        return out
    count = len(_parse_google_cookies(google_cookies))
    await db.update_token(
        token_row.id, google_cookies=google_cookies, protocol_mode="protocol", login_account=(email or "").strip(),
        google_cookies_updated_at=now, google_cookies_seq=cookie_seq,
    )
    out["stored"] = True
    out["cookies"] = count
    debug_logger.event(f"[COOKIE_SYNC] token={token_row.id} ({email}) Google login stored ({count} cookies)")
    return out


@router.post("/api/plugin/cookie-sync")
async def plugin_cookie_sync(request: dict, authorization: Optional[str] = Header(None)):
    """Cookie-sync control that needs NO Google round-trip: the worker's OFF switch must
    delete the server copy even when its Labs login is broken or Google is down.
    Body: {"action": "clear", "token_id": <bound id>, "cookie_sync_seq": <int>}."""
    await _verify_plugin_connection_token(authorization)
    action = str(request.get("action") or "").strip().lower()
    if action != "clear":
        raise HTTPException(status_code=400, detail="action must be 'clear'")
    try:
        token_id = int(request.get("token_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="token_id required (the worker learns it from its session push)")
    row = await db.get_token(token_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Token {token_id} not found")
    cookie_sync = await _apply_cookie_sync(row, "", _parse_cookie_sync_seq(request.get("cookie_sync_seq")), row.email or "")
    return {"success": True, "token_id": token_id, "cookie_sync": cookie_sync}


def _ext_version_at_least(version: Optional[str], minimum: tuple) -> bool:
    """Dotted-numeric compare of a reported extension version ("3.3.5") against `minimum`.
    Unknown/unparseable ⇒ False (treat as legacy)."""
    if not version:
        return False
    try:
        parts = tuple(int(p) for p in str(version).strip().split(".")[:3])
    except Exception:
        return False
    parts = parts + (0,) * (3 - len(parts))
    return parts >= tuple(minimum)


@router.post("/api/plugin/check-tokens")
async def plugin_check_tokens(request: Optional[dict] = None, authorization: Optional[str] = Header(None)):
    """Return token status for external syncers using the plugin connection token."""
    await _verify_plugin_connection_token(authorization)

    request = request or {}
    requested_emails = request.get("emails") if isinstance(request, dict) else None
    email_filter = set()
    if isinstance(requested_emails, list):
        email_filter = {
            str(email or "").strip().lower()
            for email in requested_emails
            if str(email or "").strip()
        }

    rows = await db.get_all_tokens_with_stats()
    tokens = []
    for row in rows:
        email = str(row.get("email") or "").strip()
        if email_filter and email.lower() not in email_filter:
            continue
        token_obj = None
        try:
            token_obj = Token(**row)
        except Exception:
            token_obj = None
        needs_refresh = token_manager.needs_at_refresh(token_obj) if token_obj else True
        tokens.append({
            "id": row.get("id"),
            "email": email,
            "is_active": bool(row.get("is_active")),
            "needs_refresh": needs_refresh,
            "at_expires": row.get("at_expires").isoformat() if hasattr(row.get("at_expires"), "isoformat") else row.get("at_expires"),
            "last_used_at": row.get("last_used_at").isoformat() if hasattr(row.get("last_used_at"), "isoformat") else row.get("last_used_at"),
            "protocol_mode": row.get("protocol_mode") or "session",
            "auto_refresh_enabled": bool(row.get("auto_refresh_enabled", True)),
            "refresh_interval_minutes": row.get("refresh_interval_minutes") or 120,
            "last_st_refresh_at": (
                row.get("last_st_refresh_at").isoformat()
                if hasattr(row.get("last_st_refresh_at"), "isoformat")
                else row.get("last_st_refresh_at")
            ),
            "last_st_refresh_result": row.get("last_st_refresh_result") or "",
            "credits": row.get("credits", 0),
        })

    return {"success": True, "tokens": tokens}
