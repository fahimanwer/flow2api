"""Sealed secrets for Ultra browsers (account passwords), AES-256-GCM.

The key is `ULTRA_VAULT_KEY` (base64 of 32 bytes) in flow2api's environment only — the host agent never
holds it. A sealed value is `v1:<base64(nonce12 + ciphertext + tag)>`; the browser name is bound in as
associated data, so a sealed password copied onto another browser row does not open. Nothing here logs a
value. Without a key the feature that needs it refuses with a clear message (VaultUnavailable); the rest of
flow2api starts normally.
"""
from __future__ import annotations

import base64
import binascii
import os
from typing import Optional

PREFIX = "v1:"


class VaultUnavailable(RuntimeError):
    """No usable ULTRA_VAULT_KEY (or the cryptography package is missing)."""


def _load_key(raw: Optional[str] = None) -> bytes:
    raw = (os.environ.get("ULTRA_VAULT_KEY", "") if raw is None else raw).strip()
    if not raw:
        raise VaultUnavailable("ULTRA_VAULT_KEY is not set: passwords cannot be stored (set a base64 32-byte key)")
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise VaultUnavailable("ULTRA_VAULT_KEY is not valid base64")
    if len(key) != 32:
        raise VaultUnavailable(f"ULTRA_VAULT_KEY must decode to 32 bytes (got {len(key)})")
    return key


def _aesgcm(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as e:  # pragma: no cover - requirements.txt pins it
        raise VaultUnavailable(f"the cryptography package is missing: {e}")
    return AESGCM(key)


def available(raw_key: Optional[str] = None) -> bool:
    try:
        _aesgcm(_load_key(raw_key))
        return True
    except VaultUnavailable:
        return False


def seal(plaintext: str, context: str, raw_key: Optional[str] = None) -> str:
    if not isinstance(plaintext, str) or not plaintext:
        raise ValueError("nothing to seal")
    nonce = os.urandom(12)
    ct = _aesgcm(_load_key(raw_key)).encrypt(nonce, plaintext.encode("utf-8"), context.encode("utf-8"))
    return PREFIX + base64.b64encode(nonce + ct).decode("ascii")


def open_sealed(sealed: str, context: str, raw_key: Optional[str] = None) -> str:
    if not isinstance(sealed, str) or not sealed.startswith(PREFIX):
        raise ValueError("not a sealed value")
    blob = base64.b64decode(sealed[len(PREFIX):])
    if len(blob) < 12 + 16:
        raise ValueError("sealed value is too short")
    try:
        pt = _aesgcm(_load_key(raw_key)).decrypt(blob[:12], blob[12:], context.encode("utf-8"))
    except VaultUnavailable:
        raise
    except Exception:
        # wrong key, wrong context or tampered: never say which, never echo the value
        raise ValueError("sealed value does not open with this key/context")
    return pt.decode("utf-8")
