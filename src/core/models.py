"""Data models for Flow2API"""

from pydantic import BaseModel, ConfigDict, Field
from typing import Optional, List, Union, Any, Literal
from datetime import datetime


class Token(BaseModel):
    """Token model for Flow2API"""

    id: Optional[int] = None

    # Auth info (core)
    st: str  # Session Token (__Secure-next-auth.session-token)
    at: Optional[str] = None  # Access Token (converted from ST)
    at_expires: Optional[datetime] = None  # AT expiry time

    # Basic info
    email: str
    name: Optional[str] = ""
    remark: Optional[str] = None
    is_active: bool = True
    created_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    use_count: int = 0

    # VideoFX-specific fields
    credits: int = 0  # Remaining credits
    user_paygate_tier: Optional[str] = None  # PAYGATE_TIER_ONE

    # Project management
    current_project_id: Optional[str] = None  # UUID of the project in use
    current_project_name: Optional[str] = None  # Project name

    # Feature switches
    image_enabled: bool = True
    video_enabled: bool = True

    # Concurrency limits
    image_concurrency: int = -1  # -1 means unlimited
    video_concurrency: int = -1  # -1 means unlimited

    # Captcha proxy (per token, overrides the global browser captcha proxy)
    captcha_proxy_url: Optional[str] = None
    extension_route_key: Optional[str] = None

    # Slice B — mint/redeem consistency. Reported by the worker extension so the
    # generate (redeem) request exits the SAME residential IP + uses the SAME browser
    # UA that minted the reCAPTCHA. When unset, redeem falls back to the global proxy.
    redeem_proxy_url: Optional[str] = None  # per-account residential proxy for the generate call
    browser_user_agent: Optional[str] = None  # the extension browser's real User-Agent

    # Two-pool routing: 'auto' (normal article images) or 'failed_image' (staff-driven
    # failed-image regeneration). Reported by the extension's "Failed-image mode" switch.
    pool_mode: Optional[str] = "auto"

    # Per-caller routing: when set, ONLY the named client (X-Flow-Client / X-Client
    # header) may generate with this account, and that client prefers it. Admin-set
    # (POST /api/tokens/{id}/reserved-client); the extension never writes it.
    reserved_client: Optional[str] = ""

    # worker-extension version this device last reported (on session push). Lets the
    # admin see which devices are still on an old build after an update ships.
    ext_version: Optional[str] = None

    # Protocol refresh of Session Token (upstream protocol-login: refresh ST via Google login
    # instead of a browser session cookie)
    protocol_mode: str = "session"  # session/protocol
    google_cookies: str = ""
    login_account: str = ""
    login_password: str = ""
    proxy_url: str = ""
    auto_refresh_enabled: bool = True
    refresh_interval_minutes: int = 120
    last_st_refresh_at: Optional[datetime] = None
    last_st_refresh_result: str = ""
    # Cookie sync (worker extension 3.7.0): when the Google login was last shared, and the
    # client sequence of that write (docs/cookie-sync.md).
    google_cookies_updated_at: Optional[datetime] = None
    google_cookies_seq: int = 0

    # 429 ban fields
    ban_reason: Optional[str] = None  # Ban reason: "429_rate_limit" or None
    banned_at: Optional[datetime] = None  # Ban time


class Project(BaseModel):
    """Project model for VideoFX"""

    id: Optional[int] = None
    project_id: str  # VideoFX project UUID
    token_id: int  # Linked token ID
    project_name: str  # Project name
    tool_name: str = "PINHOLE"  # Tool name, always PINHOLE
    is_active: bool = True
    created_at: Optional[datetime] = None


class TokenStats(BaseModel):
    """Token statistics"""

    token_id: int
    image_count: int = 0
    video_count: int = 0
    success_count: int = 0
    error_count: int = 0  # Historical total errors (never reset)
    last_success_at: Optional[datetime] = None
    last_error_at: Optional[datetime] = None
    # Today's stats
    today_image_count: int = 0
    today_video_count: int = 0
    today_error_count: int = 0
    today_date: Optional[str] = None
    # Consecutive error count (used for auto-disable)
    consecutive_error_count: int = 0


class Task(BaseModel):
    """Generation task"""

    id: Optional[int] = None
    task_id: str  # Operation name returned by Flow API
    token_id: int
    model: str
    prompt: str
    status: str  # processing, completed, failed
    progress: int = 0  # 0-100
    result_urls: Optional[List[str]] = None
    error_message: Optional[str] = None
    scene_id: Optional[str] = None  # Flow API sceneId
    created_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class RequestLog(BaseModel):
    """API request log"""

    id: Optional[int] = None
    token_id: Optional[int] = None
    operation: str
    request_body: Optional[str] = None
    response_body: Optional[str] = None
    status_code: int
    duration: float
    status_text: Optional[str] = None
    progress: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class AdminConfig(BaseModel):
    """Admin configuration"""

    id: int = 1
    username: str
    password: str
    api_key: str
    error_ban_threshold: int = 3  # Auto-disable token after N consecutive errors


class ProxyConfig(BaseModel):
    """Proxy configuration"""

    id: int = 1
    enabled: bool = False  # Request proxy switch
    proxy_url: Optional[str] = None  # Request proxy URL
    media_proxy_enabled: bool = False  # Image upload/download proxy switch
    media_proxy_url: Optional[str] = None  # Image upload/download proxy URL


class GenerationConfig(BaseModel):
    """Generation timeout configuration"""

    id: int = 1
    image_timeout: int = 300  # seconds
    video_timeout: int = 1500  # seconds
    max_retries: int = 3  # Max request retries
    remove_watermark: bool = True  # Remove the visible Gemini watermark from free/Pro account images


class CallLogicConfig(BaseModel):
    """Token selection call logic configuration"""

    id: int = 1
    call_mode: str = "default"
    polling_mode_enabled: bool = False
    tier_order: str = "save_ultra"  # save_ultra = images Pro→Free→Ultra, videos Ultra→Pro→Free; balanced = ignore tier
    updated_at: Optional[datetime] = None


class CacheConfig(BaseModel):
    """Cache configuration"""

    id: int = 1
    cache_enabled: bool = False
    cache_timeout: int = 7200  # seconds (2 hours), 0 means never expire
    cache_base_url: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class DebugConfig(BaseModel):
    """Debug configuration"""

    id: int = 1
    enabled: bool = False
    log_requests: bool = True
    log_responses: bool = True
    mask_token: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CaptchaConfig(BaseModel):
    """Captcha configuration"""

    id: int = 1
    captcha_method: str = "browser"  # yescaptcha/capmonster/ezcaptcha/capsolver/browser/personal/remote_browser
    yescaptcha_api_key: str = ""
    yescaptcha_base_url: str = "https://api.yescaptcha.com"
    yescaptcha_task_type: str = "RecaptchaV3TaskProxylessM1S9"
    capmonster_api_key: str = ""
    capmonster_base_url: str = "https://api.capmonster.cloud"
    ezcaptcha_api_key: str = ""
    ezcaptcha_base_url: str = "https://api.ez-captcha.com"
    capsolver_api_key: str = ""
    capsolver_base_url: str = "https://api.capsolver.com"
    remote_browser_base_url: str = ""
    remote_browser_api_key: str = ""
    remote_browser_timeout: int = 60
    website_key: str = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV"
    page_action: str = "IMAGE_GENERATION"
    browser_proxy_enabled: bool = False  # Whether browser captcha uses a proxy
    browser_proxy_url: Optional[str] = None  # Browser captcha proxy URL
    browser_count: int = 1  # Number of browser captcha instances
    personal_project_pool_size: int = 4  # Default project pool size per token (only affects project rotation)
    personal_max_resident_tabs: int = 5  # Max shared captcha tabs per built-in browser instance
    browser_personal_fresh_restart_every_n_solves: int = 10  # Clean and restart the browser after this many successful solves, 0 = disabled
    personal_idle_tab_ttl_seconds: int = 600  # Built-in browser tab idle timeout (seconds)
    server_fallback_enabled: bool = True  # server mints on flow.google.com when a worker cannot (2026-09-23)
    server_fallback_max_browsers: int = 3  # fallback Chromiums open at once (one per proxy)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class PluginConfig(BaseModel):
    """Plugin connection configuration"""

    id: int = 1
    connection_token: str = ""  # Plugin connection token
    auto_enable_on_update: bool = True  # Auto-enable the token when it is updated (on by default)
    # #2 residential proxy pool the extension fetches (JSON: {host,user,pass,ports:[...]})
    # so more IPs can be added from the admin UI without redistributing the extension.
    ext_proxy_pool: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class LogCleanupConfig(BaseModel):
    """Scheduled request_logs retention configuration"""

    id: int = 1
    enabled: bool = True
    retention_hours: int = 24
    interval_minutes: int = 60
    vacuum_after_cleanup: bool = False
    last_run_at: Optional[datetime] = None
    last_deleted_count: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class TokenRefreshConfig(BaseModel):
    """Protocol ST refresh configuration"""

    id: int = 1
    enabled: bool = True
    refresh_interval_minutes: int = 120
    updated_at: Optional[datetime] = None


# OpenAI Compatible Request Models
class CharacterInput(BaseModel):
    """A character: a named person or thing built from 1-3 photos. Write `@Name` in the
    prompt and Flow keeps that face/object consistent across images and videos."""

    name: str = Field(
        ...,
        description="1-40 characters: letters, digits, space, _ or -. Unique per request. Use it in the prompt as @Name.",
        examples=["Maya"],
    )
    images: List[str] = Field(
        ...,
        description="1-3 photos of the character, as data URLs (data:image/jpeg;base64,...) or http(s) URLs, up to 12 MB each.",
        examples=[["data:image/jpeg;base64,/9j/4AAQ...", "https://example.com/maya-side.jpg"]],
    )

    model_config = ConfigDict(json_schema_extra={
        "example": {"name": "Maya", "images": ["data:image/jpeg;base64,/9j/4AAQ...", "https://example.com/maya-side.jpg"]}
    })


class ChatMessage(BaseModel):
    """Chat message"""

    role: str
    content: Union[str, List[dict]]  # string or multimodal array


class ImageConfig(BaseModel):
    """Gemini imageConfig parameters"""

    aspectRatio: Optional[str] = None  # "16:9", "9:16", "1:1", "4:3", "3:4"
    imageSize: Optional[str] = None  # "2k", "4k"

    # Accept size/quality or snake_case fields that OpenAI/NewAPI-style upstreams may pass through
    model_config = ConfigDict(extra="allow")


class GenerationConfigParam(BaseModel):
    """Gemini generationConfig parameters (for model name resolution)"""

    responseModalities: Optional[List[str]] = None  # ["IMAGE", "TEXT"]
    imageConfig: Optional[ImageConfig] = None

    model_config = ConfigDict(extra="allow")


class GeminiInlineData(BaseModel):
    """Gemini inline binary data."""

    mimeType: str
    data: str


class GeminiFileData(BaseModel):
    """Gemini file reference."""

    fileUri: str
    mimeType: Optional[str] = None


class GeminiPart(BaseModel):
    """Gemini content part."""

    text: Optional[str] = None
    inlineData: Optional[GeminiInlineData] = None
    fileData: Optional[GeminiFileData] = None

    model_config = ConfigDict(extra="allow")


class GeminiContent(BaseModel):
    """Gemini content block."""

    role: Optional[Literal["user", "model"]] = None
    parts: List[GeminiPart]


class GeminiGenerateContentRequest(BaseModel):
    """Gemini official generateContent request."""

    contents: List[GeminiContent]
    generationConfig: Optional[GenerationConfigParam] = None
    systemInstruction: Optional[GeminiContent] = None
    characters: Optional[List[CharacterInput]] = Field(
        default=None,
        description="Flow Characters (same as on /v1/chat/completions): name + 1-3 photos each, referenced as @Name in the prompt.",
    )

    model_config = ConfigDict(extra="allow")


class ChatCompletionRequest(BaseModel):
    """Chat completion request (OpenAI compatible + Gemini extension)"""

    model: str
    messages: Optional[List[ChatMessage]] = None
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    # Flow2API specific parameters
    image: Optional[str] = None  # Base64 encoded image (deprecated, use messages)
    video: Optional[str] = None  # Base64 encoded video (deprecated)
    # Gemini extension parameters (from extra_body or top-level)
    generationConfig: Optional[GenerationConfigParam] = None
    contents: Optional[List[Any]] = None  # Gemini native contents
    characters: Optional[List[CharacterInput]] = Field(
        default=None,
        description=(
            "Flow Characters: people/objects that must look the same across generations. Give each a name and 1-3 "
            "photos, then write @Name in the prompt (e.g. \"@Maya sits in a sunny cafe\"). Images: up to 10 characters. "
            "Videos: up to 3, only on ingredients models (omni-r2v, omni, omni-flash, veo-r2v, veo-r2v-lite). "
            "Character photos are NOT reference images; you may still add image_url parts as ordinary references."
        ),
    )

    model_config = ConfigDict(extra="allow")  # Allow extra fields like extra_body passthrough
