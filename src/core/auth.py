"""Authentication module"""

import hmac

import bcrypt
from typing import Optional
from fastapi import Header, HTTPException, Query, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from .config import config

security = HTTPBearer()
optional_security = HTTPBearer(auto_error=False)

AUTH_NOT_CONFIGURED_DETAIL = "authentication not configured"


class AuthManager:
    """Authentication manager"""

    @staticmethod
    def resolve_principal(api_key: Optional[str]) -> Optional[str]:
        """Principal name for an API key (constant-time), or None if unknown."""
        return config.resolve_principal(api_key)

    @staticmethod
    def verify_api_key(api_key: str) -> bool:
        """Verify API key"""
        return config.resolve_principal(api_key) is not None

    @staticmethod
    def verify_admin(username: str, password: str) -> bool:
        """Verify admin credentials"""
        # Compare with current config (which may be from database or config file)
        username_ok = hmac.compare_digest(
            (username or "").encode("utf-8"), (config.admin_username or "").encode("utf-8")
        )
        password_ok = hmac.compare_digest(
            (password or "").encode("utf-8"), (config.admin_password or "").encode("utf-8")
        )
        return username_ok and password_ok

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash password"""
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    @staticmethod
    def verify_password(password: str, hashed: str) -> bool:
        """Verify password"""
        return bcrypt.checkpw(password.encode(), hashed.encode())


def authenticate_api_key(api_key: Optional[str]) -> str:
    """Resolve a presented key to its principal or raise the matching HTTP error.

    With no key configured at all the answer is 503, never 401: a fresh install
    must fail closed without ever hinting that some key would have worked.
    """
    if not config.auth_configured:
        raise HTTPException(status_code=503, detail=AUTH_NOT_CONFIGURED_DETAIL)
    principal = AuthManager.resolve_principal(api_key)
    if principal is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return principal


async def verify_api_key_header(credentials: HTTPAuthorizationCredentials = Security(security)) -> str:
    """Verify the Authorization header and return the caller's principal name."""
    return authenticate_api_key(credentials.credentials)


async def verify_api_key_flexible(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(optional_security),
    x_goog_api_key: Optional[str] = Header(None, alias="x-goog-api-key"),
    key: Optional[str] = Query(None),
) -> str:
    """Verify the API key from Authorization, x-goog-api-key, or ?key= and return the principal."""
    api_key = None

    if credentials is not None:
        api_key = credentials.credentials
    elif x_goog_api_key:
        api_key = x_goog_api_key
    elif key:
        api_key = key

    return authenticate_api_key(api_key)
