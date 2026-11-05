"""Configuration management for Flow2API"""
import tomli
from pathlib import Path
from typing import Dict, Any, Optional


class Config:
    """Application configuration"""

    def __init__(self):
        self._config = self._load_config()
        self._admin_username: Optional[str] = None
        self._admin_password: Optional[str] = None

    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from setting.toml, falling back to the example file."""
        config_dir = Path(__file__).parent.parent.parent / "config"
        config_path = config_dir / "setting.toml"
        fallback_path = config_dir / "setting_example.toml"

        if config_path.exists() and not config_path.is_file():
            print(
                f"[Config] {config_path} is not a regular file, "
                f"falling back to {fallback_path.name}. Check the Docker mount or local config path."
            )
            config_path = fallback_path
        elif not config_path.exists():
            config_path = fallback_path

        if not config_path.is_file():
            raise FileNotFoundError(
                f"Config file missing or unreadable: {config_path}. "
                f"Make sure the config directory has a usable setting.toml or setting_example.toml"
            )

        with open(config_path, "rb") as f:
            return tomli.load(f)

    def reload_config(self):
        """Reload configuration from file"""
        self._config = self._load_config()

    def get_raw_config(self) -> Dict[str, Any]:
        """Get raw configuration dictionary"""
        return self._config

    @property
    def admin_username(self) -> str:
        # If admin_username is set from database, use it; otherwise fall back to config file
        if self._admin_username is not None:
            return self._admin_username
        return self._config["global"]["admin_username"]

    @admin_username.setter
    def admin_username(self, value: str):
        self._admin_username = value
        self._config["global"]["admin_username"] = value

    def set_admin_username_from_db(self, username: str):
        """Set admin username from database"""
        self._admin_username = username

    # Flow2API specific properties
    @property
    def flow_api_base_url(self) -> str:
        """Google AI Sandbox API base URL for generation"""
        return self._config["flow"]["api_base_url"]

    @property
    def flow_timeout(self) -> int:
        timeout = self._config.get("flow", {}).get("timeout", 120)
        try:
            return max(5, int(timeout))
        except Exception:
            return 120

    @property
    def flow_max_retries(self) -> int:
        retries = self._config.get("flow", {}).get("max_retries", 3)
        try:
            return max(1, int(retries))
        except Exception:
            return 3

    def set_flow_max_retries(self, retries: int):
        """Set flow max retries"""
        if "flow" not in self._config:
            self._config["flow"] = {}
        try:
            normalized = max(1, int(retries))
        except Exception:
            normalized = 3
        self._config["flow"]["max_retries"] = normalized

    @property
    def flow_image_request_timeout(self) -> int:
        """Timeout (seconds) for a single image generation HTTP request."""
        default_timeout = min(self.flow_timeout, 40)
        timeout = self._config.get("flow", {}).get(
            "image_request_timeout",
            default_timeout
        )
        try:
            return max(5, int(timeout))
        except Exception:
            return self.flow_timeout

    @property
    def flow_image_timeout_retry_count(self) -> int:
        """Quick retries for image generation on a network timeout."""
        retry_count = self._config.get("flow", {}).get("image_timeout_retry_count", 1)
        try:
            return max(0, min(3, int(retry_count)))
        except Exception:
            return 1

    @property
    def flow_image_timeout_retry_delay(self) -> float:
        """Seconds to wait before retrying image generation after a network timeout."""
        delay = self._config.get("flow", {}).get("image_timeout_retry_delay", 0.8)
        try:
            return max(0.0, min(5.0, float(delay)))
        except Exception:
            return 0.8

    @property
    def flow_image_timeout_use_media_proxy_fallback(self) -> bool:
        """Whether to retry via the media proxy on a network timeout."""
        return bool(
            self._config.get("flow", {}).get(
                "image_timeout_use_media_proxy_fallback",
                True
            )
        )

    @property
    def flow_image_prefer_media_proxy(self) -> bool:
        """Whether image generation prefers the media proxy path."""
        return bool(
            self._config.get("flow", {}).get(
                "image_prefer_media_proxy",
                False
            )
        )

    @property
    def poll_interval(self) -> float:
        return self._config["flow"]["poll_interval"]

    @property
    def max_poll_attempts(self) -> int:
        return self._config["flow"]["max_poll_attempts"]

    @property
    def server_host(self) -> str:
        return self._config["server"]["host"]

    @property
    def server_port(self) -> int:
        return self._config["server"]["port"]

    @property
    def debug_enabled(self) -> bool:
        return self._config.get("debug", {}).get("enabled", False)

    @property
    def debug_log_requests(self) -> bool:
        return self._config.get("debug", {}).get("log_requests", True)

    @property
    def debug_log_responses(self) -> bool:
        return self._config.get("debug", {}).get("log_responses", True)

    @property
    def debug_mask_token(self) -> bool:
        return self._config.get("debug", {}).get("mask_token", True)

    # Mutable properties for runtime updates
    @property
    def api_key(self) -> str:
        return self._config["global"]["api_key"]

    @api_key.setter
    def api_key(self, value: str):
        self._config["global"]["api_key"] = value

    @property
    def admin_password(self) -> str:
        # If admin_password is set from database, use it; otherwise fall back to config file
        if self._admin_password is not None:
            return self._admin_password
        return self._config["global"]["admin_password"]

    @admin_password.setter
    def admin_password(self, value: str):
        self._admin_password = value
        self._config["global"]["admin_password"] = value

    def set_admin_password_from_db(self, password: str):
        """Set admin password from database"""
        self._admin_password = password

    def set_debug_enabled(self, enabled: bool):
        """Set debug mode enabled/disabled"""
        if "debug" not in self._config:
            self._config["debug"] = {}
        self._config["debug"]["enabled"] = enabled

    @property
    def image_timeout(self) -> int:
        """Get image generation timeout in seconds"""
        return self._config.get("generation", {}).get("image_timeout", 300)

    def set_image_timeout(self, timeout: int):
        """Set image generation timeout in seconds"""
        if "generation" not in self._config:
            self._config["generation"] = {}
        self._config["generation"]["image_timeout"] = timeout

    @property
    def video_timeout(self) -> int:
        """Get video generation timeout in seconds"""
        return self._config.get("generation", {}).get("video_timeout", 1500)

    def set_video_timeout(self, timeout: int):
        """Set video generation timeout in seconds"""
        if "generation" not in self._config:
            self._config["generation"] = {}
        self._config["generation"]["video_timeout"] = timeout

    @property
    def remove_watermark(self) -> bool:
        """Remove the visible Gemini watermark from free/Pro images (default on)"""
        return bool(self._config.get("generation", {}).get("remove_watermark", True))

    def set_remove_watermark(self, enabled: bool):
        """Set whether the visible Gemini watermark is removed"""
        if "generation" not in self._config:
            self._config["generation"] = {}
        self._config["generation"]["remove_watermark"] = bool(enabled)

    @property
    def polling_mode_enabled(self) -> bool:
        """Get polling mode enabled status."""
        return self.call_logic_mode == "polling"

    @property
    def call_logic_mode(self) -> str:
        """Get call logic mode (default or polling)."""
        call_logic = self._config.get("call_logic", {})
        mode = call_logic.get("call_mode")
        if mode in ("default", "polling"):
            return mode
        if call_logic.get("polling_mode_enabled", False):
            return "polling"
        return "default"

    def set_polling_mode_enabled(self, enabled: bool):
        """Set polling mode enabled/disabled."""
        self.set_call_logic_mode("polling" if enabled else "default")

    TIER_ORDER_MODES = ("save_ultra", "balanced")

    @property
    def tier_order(self) -> str:
        """Account order: save_ultra (Pro→Free→Ultra for images, Ultra→Pro→Free for videos) or balanced."""
        mode = self._config.get("call_logic", {}).get("tier_order")
        return mode if mode in self.TIER_ORDER_MODES else "save_ultra"

    def set_tier_order(self, mode: str):
        """Set the account order used by the token picker."""
        if "call_logic" not in self._config:
            self._config["call_logic"] = {}
        self._config["call_logic"]["tier_order"] = mode if mode in self.TIER_ORDER_MODES else "save_ultra"

    def set_call_logic_mode(self, mode: str):
        """Set call logic mode (default or polling)."""
        normalized = "polling" if mode == "polling" else "default"
        if "call_logic" not in self._config:
            self._config["call_logic"] = {}
        self._config["call_logic"]["call_mode"] = normalized
        self._config["call_logic"]["polling_mode_enabled"] = normalized == "polling"

    @property
    def upsample_timeout(self) -> int:
        """Get upsample (4K/2K) timeout in seconds"""
        return self._config.get("generation", {}).get("upsample_timeout", 300)

    def set_upsample_timeout(self, timeout: int):
        """Set upsample (4K/2K) timeout in seconds"""
        if "generation" not in self._config:
            self._config["generation"] = {}
        self._config["generation"]["upsample_timeout"] = timeout

    # Cache configuration
    @property
    def cache_enabled(self) -> bool:
        """Get cache enabled status"""
        return self._config.get("cache", {}).get("enabled", False)

    def set_cache_enabled(self, enabled: bool):
        """Set cache enabled status"""
        if "cache" not in self._config:
            self._config["cache"] = {}
        self._config["cache"]["enabled"] = enabled

    @property
    def cache_timeout(self) -> int:
        """Get cache timeout in seconds"""
        return self._config.get("cache", {}).get("timeout", 7200)

    def set_cache_timeout(self, timeout: int):
        """Set cache timeout in seconds"""
        if "cache" not in self._config:
            self._config["cache"] = {}
        self._config["cache"]["timeout"] = timeout

    @property
    def cache_base_url(self) -> str:
        """Get cache base URL"""
        return self._config.get("cache", {}).get("base_url", "")

    def set_cache_base_url(self, base_url: str):
        """Set cache base URL"""
        if "cache" not in self._config:
            self._config["cache"] = {}
        self._config["cache"]["base_url"] = base_url

    # Captcha configuration
    @property
    def captcha_method(self) -> str:
        """Get captcha method"""
        return self._config.get("captcha", {}).get("captcha_method", "extension")

    def set_captcha_method(self, method: str):
        """Set captcha method"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["captcha_method"] = method

    @property
    def captcha_server_fallback_enabled(self) -> bool:
        """Server-side reCAPTCHA fallback (flow_page_captcha) when a worker cannot deliver a token."""
        return bool(self._config.get("captcha", {}).get("server_fallback_enabled", True))

    def set_captcha_server_fallback_enabled(self, enabled: bool):
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["server_fallback_enabled"] = bool(enabled)

    @property
    def captcha_server_fallback_max_browsers(self) -> int:
        """How many fallback Chromiums (one per proxy) may be open at once, 1..6."""
        value = self._config.get("captcha", {}).get("server_fallback_max_browsers", 3)
        try:
            return max(1, min(6, int(value)))
        except Exception:
            return 3

    def set_captcha_server_fallback_max_browsers(self, value: int):
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        try:
            self._config["captcha"]["server_fallback_max_browsers"] = max(1, min(6, int(value)))
        except Exception:
            self._config["captcha"]["server_fallback_max_browsers"] = 3

    @property
    def browser_captcha_generation_retries(self) -> int:
        """Total retries allowed when generation fails reCAPTCHA evaluation."""
        # 2 (was 6): a reCAPTCHA evaluation failure is a risk signal on the account;
        # retrying six times makes it look worse and burned ~55 s per failed image.
        # Adapted from Gurumigun/flow2api 61e2d013.
        value = self._config.get("captcha", {}).get("browser_captcha_generation_retries", 2)
        try:
            return max(1, min(20, int(value)))
        except Exception:
            return 2

    @property
    def extension_route_min_interval_seconds(self) -> float:
        """Minimum spacing between two reCAPTCHA mints on the SAME worker browser.
        One Chrome profile = one Google account; a burst of parallel mints opens
        several hidden Flow tabs at once and produces "Frame with ID 0 was removed"
        / "labs tab did not reach Flow URL". Adapted from Gurumigun/flow2api."""
        value = self._config.get("captcha", {}).get("extension_route_min_interval_seconds", 3.0)
        try:
            return max(0.0, min(30.0, float(value)))
        except Exception:
            return 3.0

    @property
    def extension_route_failure_pause_seconds(self) -> float:
        """After a mint fails on a browser, how long queued/new mints for that route
        fail fast (matches TokenManager.MINT_FAILURE_PAUSE_SECONDS)."""
        value = self._config.get("captcha", {}).get("extension_route_failure_pause_seconds", 90.0)
        try:
            return max(5.0, min(3600.0, float(value)))
        except Exception:
            return 90.0

    @property
    def extension_global_min_interval_seconds(self) -> float:
        """Minimum spacing between mints across ALL worker browsers (smooths bursts)."""
        value = self._config.get("captcha", {}).get("extension_global_min_interval_seconds", 1.0)
        try:
            return max(0.0, min(10.0, float(value)))
        except Exception:
            return 1.0

    @property
    def personal_project_pool_size(self) -> int:
        """Default project pool size per token; only affects project rotation."""
        value = self._config.get("captcha", {}).get("personal_project_pool_size", 4)
        try:
            return max(1, min(50, int(value)))
        except Exception:
            return 4

    def set_personal_project_pool_size(self, value: int):
        """Set the default project pool size per token; only affects project rotation"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["personal_project_pool_size"] = max(1, min(50, int(value)))


# Global config instance
config = Config()
