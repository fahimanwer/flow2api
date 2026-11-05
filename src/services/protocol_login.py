"""Google-cookie parsing and proxy-URL normalising shared by the push endpoint and the cookie sessions.

The Labs "Sign in with Google" replay (ProtocolLogin) that used to live here was deleted in stage 2 (2026-11):
Google retired labs.google/fx/api/auth on 2026-10-21. The module name is kept for its three import sites."""

import json
import re
from typing import Dict, Optional


GOOGLE_COOKIE_NAMES = ("SID", "HSID", "SSID", "APISID", "SAPISID")


def google_cookies_usable(raw: str) -> bool:
    """True when the export carries at least one of the classic Google login cookies
    (SID/HSID/SSID/APISID/SAPISID); the push endpoint's gate."""
    cookies = _parse_google_cookies(raw)
    return any(name in cookies for name in GOOGLE_COOKIE_NAMES)


def _parse_google_cookies(raw: str) -> Dict[str, str]:
    """Parse Google cookies from JSON export or name=value text."""
    text = (raw or "").strip()
    if not text:
        return {}

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        data = None

    if isinstance(data, list):
        result: Dict[str, str] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "").strip()
            if name and value:
                result[name] = value
        if result:
            return result

    if isinstance(data, dict):
        cookies_list = data.get("cookies")
        if isinstance(cookies_list, list):
            result: Dict[str, str] = {}
            for item in cookies_list:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                value = str(item.get("value") or "").strip()
                if name and value:
                    result[name] = value
            if result:
                return result

        result = {
            str(key).strip(): str(value).strip()
            for key, value in data.items()
            if isinstance(value, str) and str(key).strip() and value.strip()
        }
        if result:
            return result

    result: Dict[str, str] = {}
    for part in text.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip()
        if name and value:
            result[name] = value
    return result


def _normalize_proxy_url(proxy_url: Optional[str]) -> Optional[str]:
    raw = (proxy_url or "").strip()
    if not raw:
        return None

    st5_match = re.match(r"^st5\s+(.+)$", raw, re.IGNORECASE)
    if st5_match:
        rest = st5_match.group(1).strip()
        if "@" in rest:
            return f"socks5://{rest}"
        parts = rest.split(":")
        if len(parts) >= 4 and parts[1].isdigit():
            return f"socks5://{parts[2]}:{':'.join(parts[3:])}@{parts[0]}:{parts[1]}"
        return None

    if "://" not in raw:
        if "@" in raw:
            return f"http://{raw}"
        parts = raw.split(":")
        if len(parts) == 2 and parts[1].isdigit():
            return f"http://{parts[0]}:{parts[1]}"
        if len(parts) >= 4 and parts[1].isdigit():
            return f"http://{parts[2]}:{':'.join(parts[3:])}@{parts[0]}:{parts[1]}"
        return None

    if re.match(r"^(http|https|socks5h?|socks5)://", raw, re.IGNORECASE):
        if "@" in raw:
            return raw
        scheme, _, rest = raw.partition("://")
        parts = rest.split(":")
        if len(parts) == 2 and parts[1].isdigit():
            return raw
        if len(parts) >= 4 and parts[1].isdigit():
            return f"{scheme}://{parts[2]}:{':'.join(parts[3:])}@{parts[0]}:{parts[1]}"
    return None
