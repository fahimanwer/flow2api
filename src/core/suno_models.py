"""Suno provider: job states, model registry and request validation.

Everything here is pure (no I/O) so it can be unit-tested without a Suno account.

Contract source: Suno's own web bundle, read 2026-09-16. See
``docs/suno-provider-plan.md`` for how each constant was derived. The upstream
project ``gcui-art/suno-api`` targets retired endpoints and is NOT the reference.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

# ---------------------------------------------------------------- job states

QUEUED = "queued"
SUBMITTING = "submitting"        # persisted BEFORE the generate call leaves us
SUBMITTED = "submitted"          # upstream clip ids known
FINALIZING = "finalizing"        # clips terminal; resolving a playable download
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
NEEDS_REVIEW = "needs_review"    # ambiguous submit: may or may not exist upstream
BLOCKED_CAPTCHA = "blocked_captcha"  # pre-submit gate; nothing was sent upstream

ALL_STATES: Tuple[str, ...] = (
    QUEUED, SUBMITTING, SUBMITTED, FINALIZING, SUCCEEDED,
    FAILED, CANCELLED, NEEDS_REVIEW, BLOCKED_CAPTCHA,
)

TERMINAL_STATES: FrozenSet[str] = frozenset({SUCCEEDED, FAILED, CANCELLED})

# States that hold an account dispatch slot.
#
# BLOCKED_CAPTCHA is deliberately NOT here. That state is reached by the
# pre-flight captcha check, i.e. before anything was sent upstream, so no
# account capacity is in use. Holding a slot there would let two challenged
# jobs permanently strand an account with two slots.
#
# NEEDS_REVIEW is here for the opposite reason: the request may well have been
# accepted upstream, so the slot stays reserved until that is settled.
ACTIVE_STATES: FrozenSet[str] = frozenset(
    {SUBMITTING, SUBMITTED, FINALIZING, NEEDS_REVIEW}
)

# States an operator may resolve by hand.
RESOLVABLE_STATES: FrozenSet[str] = frozenset({NEEDS_REVIEW, BLOCKED_CAPTCHA})

# States whose polling resumes after a backend restart.
RESUMABLE_STATES: FrozenSet[str] = frozenset({SUBMITTED, FINALIZING})

# States that still occupy the admission queue (bounded globally).
ADMITTED_STATES: FrozenSet[str] = frozenset(
    {QUEUED, BLOCKED_CAPTCHA, SUBMITTING, SUBMITTED, FINALIZING, NEEDS_REVIEW}
)

STATE_TRANSITIONS: Dict[str, FrozenSet[str]] = {
    QUEUED: frozenset({SUBMITTING, BLOCKED_CAPTCHA, CANCELLED, FAILED}),
    SUBMITTING: frozenset({SUBMITTED, FAILED, NEEDS_REVIEW}),
    SUBMITTED: frozenset({FINALIZING, SUCCEEDED, FAILED, NEEDS_REVIEW}),
    FINALIZING: frozenset({SUCCEEDED, FAILED, NEEDS_REVIEW}),
    NEEDS_REVIEW: frozenset({SUBMITTED, FINALIZING, SUCCEEDED, FAILED}),
    # Nothing was submitted, so the job can simply go back in line or be dropped.
    BLOCKED_CAPTCHA: frozenset({QUEUED, CANCELLED, FAILED}),
    SUCCEEDED: frozenset(),
    FAILED: frozenset(),
    CANCELLED: frozenset(),
}


def can_transition(current: str, target: str) -> bool:
    """True when ``current -> target`` is an allowed job transition."""
    return target in STATE_TRANSITIONS.get(current, frozenset())


# ------------------------------------------------------------ account status

ACCOUNT_READY = "ready"
ACCOUNT_NEEDS_LOGIN = "needs_login"   # cookie/session rejected; owner must re-import
ACCOUNT_COOLDOWN = "cooldown"         # transient upstream failure or no credits
ACCOUNT_DISABLED = "disabled"         # operator turned it off

ACCOUNT_STATUSES: Tuple[str, ...] = (
    ACCOUNT_READY, ACCOUNT_NEEDS_LOGIN, ACCOUNT_COOLDOWN, ACCOUNT_DISABLED,
)

# ------------------------------------------------------------------- models
# Suno's UI exposes version tiers; the wire field ``mv`` takes a codename.
# Mapping read from the bundle's ModelTier -> mv table (2026-09-16).

MODEL_PREFIX = "suno/"

# public id -> (mv sent upstream, human label)
MODEL_REGISTRY: Dict[str, Tuple[str, str]] = {
    "v3": ("chirp-v3-0", "Suno v3"),
    "v3.5": ("chirp-v3-5", "Suno v3.5"),
    "v4": ("chirp-v4", "Suno v4"),
    "v4.5": ("chirp-auk", "Suno v4.5"),
    "v4.5+": ("chirp-bluejay", "Suno v4.5+"),
    "v5": ("chirp-crow", "Suno v5"),
    "v5.5": ("chirp-fenix", "Suno v5.5"),
    "v6-mini": ("chirp-goose", "Suno v6 mini"),
    "v6": ("chirp-hawk", "Suno v6"),
}

DEFAULT_MODEL = "v5"

# Lyrics budget grows on newer models (MAX_CUSTOM_PROMPT_CHARS / _LONG / _LONGEST).
_LYRICS_LIMIT_DEFAULT = 1250
_LYRICS_LIMIT_LONG = 3000
_LYRICS_LIMIT_LONGEST = 5000
_LONG_LYRICS_MV = frozenset({"chirp-bluejay", "chirp-crow"})
_LONGEST_LYRICS_MV = frozenset({"chirp-fenix", "chirp-goose", "chirp-hawk"})

MAX_DESCRIPTION_CHARS = 2000      # MAX_DESCRIPTION_LENGTH
MAX_STYLE_CHARS = 1000            # MAX_STYLE_CHARS_LONG (newer models)
MAX_NEGATIVE_STYLE_CHARS = 1000   # MAX_NEGATIVE_STYLE_CHARS
MAX_TITLE_CHARS = 80              # MAX_TITLE_CHARS
MAX_IDEMPOTENCY_KEY_CHARS = 200

# Captcha versions returned by POST /api/c/check for ctype=generation.
CAPTCHA_VERSION_HCAPTCHA = 1
CAPTCHA_VERSION_TURNSTILE = 2
HCAPTCHA_SITE_KEY = "d65453de-3f1a-4aac-9366-a0f06e52b2ce"

CLIP_STATUS_COMPLETE = "complete"
CLIP_STATUS_STREAMING = "streaming"
CLIP_STATUS_ERROR = "error"

# A clip stops changing at ``complete`` or ``error``. ``streaming`` means Suno
# will play it already but is still generating, so it is NOT terminal: settling
# the job there would release capacity early and abandon the sibling clip.
CLIP_TERMINAL_STATUSES: FrozenSet[str] = frozenset({CLIP_STATUS_COMPLETE, CLIP_STATUS_ERROR})
# Early playback is a separate, weaker signal, surfaced to callers while running.
CLIP_PLAYABLE_STATUSES: FrozenSet[str] = frozenset({CLIP_STATUS_COMPLETE, CLIP_STATUS_STREAMING})

AUDIO_FORMATS: Tuple[str, ...] = ("mp3", "m4a")

_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@\-]{0,199}$")
_CLIP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$")


class SunoValidationError(ValueError):
    """Caller-visible validation failure. ``code`` maps to an HTTP 4xx body."""

    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


class SunoConflictError(SunoValidationError):
    """Idempotency or state conflict; rendered as HTTP 409."""


def lyrics_limit_for(mv: str) -> int:
    """Lyrics character budget for a given upstream model codename."""
    if mv in _LONGEST_LYRICS_MV:
        return _LYRICS_LIMIT_LONGEST
    if mv in _LONG_LYRICS_MV:
        return _LYRICS_LIMIT_LONG
    return _LYRICS_LIMIT_DEFAULT


def list_models() -> List[Dict[str, Any]]:
    """Catalogue for ``GET /v1/suno/models``.

    This is a static map of what Suno's web client can select. It is capability
    discovery, not a claim that the connected account may use every tier: plans
    gate the newer models, and an upstream rejection surfaces per job.
    """
    out: List[Dict[str, Any]] = []
    for public_id, (mv, label) in MODEL_REGISTRY.items():
        out.append({
            "id": f"{MODEL_PREFIX}{public_id}",
            "label": label,
            "mv": mv,
            "default": public_id == DEFAULT_MODEL,
            "max_lyrics_chars": lyrics_limit_for(mv),
        })
    return out


def resolve_model(model: Optional[str]) -> Tuple[str, str]:
    """Map a caller ``model`` to ``(public_id, mv)``.

    Accepts ``suno/v5``, ``v5`` or the raw codename ``chirp-crow``.
    """
    if model is None or not str(model).strip():
        public_id = DEFAULT_MODEL
        return public_id, MODEL_REGISTRY[public_id][0]

    raw = str(model).strip()
    candidate = raw[len(MODEL_PREFIX):] if raw.lower().startswith(MODEL_PREFIX) else raw

    if candidate in MODEL_REGISTRY:
        return candidate, MODEL_REGISTRY[candidate][0]

    lowered = candidate.lower()
    for public_id, (mv, _label) in MODEL_REGISTRY.items():
        if lowered == public_id.lower() or lowered == mv.lower():
            return public_id, mv

    raise SunoValidationError(
        "unknown_model",
        f"Unknown Suno model '{model}'. Call GET /v1/suno/models for the list.",
        model=model,
    )


def _clean_text(value: Any, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SunoValidationError("invalid_field", f"'{field}' must be a string.", field=field)
    # Suno rejects NULs; strip them rather than forwarding a poisoned body.
    return value.replace("\x00", "").strip()


def _bounded(value: str, limit: int, field: str) -> str:
    if len(value) > limit:
        raise SunoValidationError(
            "field_too_long",
            f"'{field}' is {len(value)} characters; the limit is {limit}.",
            field=field, limit=limit, length=len(value),
        )
    return value


def normalize_generation_request(body: Any) -> Dict[str, Any]:
    """Validate a caller request and return the normalized internal form.

    Two modes, mirroring Suno's own create page:

    * description mode (``custom`` false, the default) sends ``prompt`` as
      ``gpt_description_prompt`` and lets Suno write lyrics and style;
    * custom mode (``custom`` true) sends ``prompt`` as literal lyrics and
      requires style ``tags``; ``title`` is optional.
    """
    if not isinstance(body, dict):
        raise SunoValidationError("invalid_body", "Request body must be a JSON object.")

    unknown = set(body) - _ALLOWED_REQUEST_KEYS
    if unknown:
        raise SunoValidationError(
            "unknown_field",
            "Unsupported field(s): " + ", ".join(sorted(unknown)),
            fields=sorted(unknown),
        )

    custom = body.get("custom", False)
    if not isinstance(custom, bool):
        raise SunoValidationError("invalid_field", "'custom' must be a boolean.", field="custom")

    instrumental = body.get("make_instrumental", False)
    if not isinstance(instrumental, bool):
        raise SunoValidationError(
            "invalid_field", "'make_instrumental' must be a boolean.", field="make_instrumental"
        )

    public_model, mv = resolve_model(body.get("model"))

    prompt = _clean_text(body.get("prompt"), "prompt")
    title = _bounded(_clean_text(body.get("title"), "title"), MAX_TITLE_CHARS, "title")
    tags = _bounded(_clean_text(body.get("tags"), "tags"), MAX_STYLE_CHARS, "tags")
    negative_tags = _bounded(
        _clean_text(body.get("negative_tags"), "negative_tags"),
        MAX_NEGATIVE_STYLE_CHARS, "negative_tags",
    )

    if custom:
        # Lyrics may be empty for an instrumental, but then style has to carry it.
        _bounded(prompt, lyrics_limit_for(mv), "prompt")
        if not prompt and not tags:
            raise SunoValidationError(
                "missing_input",
                "Custom mode needs 'prompt' (lyrics) or 'tags' (style).",
            )
        if not prompt and not instrumental:
            raise SunoValidationError(
                "missing_input",
                "Custom mode without lyrics must set 'make_instrumental': true.",
            )
    else:
        _bounded(prompt, MAX_DESCRIPTION_CHARS, "prompt")
        if not prompt:
            raise SunoValidationError("missing_input", "'prompt' is required.")
        if title or tags or negative_tags:
            raise SunoValidationError(
                "invalid_field",
                "'title', 'tags' and 'negative_tags' need \"custom\": true.",
            )

    idempotency_key = _clean_text(body.get("idempotency_key"), "idempotency_key")
    if idempotency_key:
        _bounded(idempotency_key, MAX_IDEMPOTENCY_KEY_CHARS, "idempotency_key")
        if not _IDEMPOTENCY_KEY_RE.match(idempotency_key):
            raise SunoValidationError(
                "invalid_field",
                "'idempotency_key' may use letters, digits and . _ - : @ only.",
                field="idempotency_key",
            )

    account_id = body.get("account_id")
    if account_id is not None and not isinstance(account_id, int):
        raise SunoValidationError("invalid_field", "'account_id' must be an integer.", field="account_id")

    return {
        "model": public_model,
        "mv": mv,
        "custom": custom,
        "prompt": prompt,
        "title": title,
        "tags": tags,
        "negative_tags": negative_tags,
        "make_instrumental": instrumental,
        "idempotency_key": idempotency_key or None,
        "account_id": account_id,
    }


_ALLOWED_REQUEST_KEYS: FrozenSet[str] = frozenset({
    "model", "prompt", "custom", "title", "tags", "negative_tags",
    "make_instrumental", "idempotency_key", "account_id",
})


def build_generate_payload(request: Dict[str, Any], *, transaction_uuid: str,
                           captcha_token: Optional[str],
                           captcha_version: Optional[int]) -> Dict[str, Any]:
    """Build the ``POST /api/generate/v2-web/`` body.

    Field names and the endpoint come from Suno's live client. ``token`` is null
    when ``/api/c/check`` said no captcha was required, which is exactly what the
    web client sends in that case.
    """
    payload: Dict[str, Any] = {
        "token": captcha_token,
        "token_provider": captcha_version if captcha_token else None,
        "transaction_uuid": transaction_uuid,
        "mv": request["mv"],
        "prompt": "",
        "generation_type": "TEXT",
        "metadata": {
            "web_client_pathname": "/create",
            "create_version": CREATE_VERSION,
        },
    }

    if request["custom"]:
        payload["prompt"] = request["prompt"]
        payload["title"] = request["title"]
        payload["tags"] = request["tags"]
        payload["negative_tags"] = request["negative_tags"]
        payload["make_instrumental"] = request["make_instrumental"]
    else:
        payload["gpt_description_prompt"] = request["prompt"]
        if request["make_instrumental"]:
            payload["make_instrumental"] = True

    return payload


CREATE_VERSION = "1.5"


def new_transaction_uuid() -> str:
    """Client-generated id echoed on the generate call; our reconciliation handle."""
    return str(uuid.uuid4())


def validate_clip_id(clip_id: Any) -> str:
    if not isinstance(clip_id, str) or not _CLIP_ID_RE.match(clip_id):
        raise SunoValidationError("invalid_clip_id", "Malformed clip id.", clip_id=clip_id)
    return clip_id


def validate_audio_format(fmt: Any) -> str:
    value = (fmt or "mp3")
    if value not in AUDIO_FORMATS:
        raise SunoValidationError(
            "invalid_format",
            "Supported formats: " + ", ".join(AUDIO_FORMATS),
            format=value,
        )
    return value


def summarize_clip(clip: Any) -> Dict[str, Any]:
    """Project an upstream clip onto the fields we expose to callers.

    Deliberately narrow: no upstream URLs are surfaced, because ``audio_url`` is
    ``/api/forbidden`` for current clips and the ``media_urls`` entries are
    encrypted. Audio is served through our own authenticated endpoint instead.
    """
    if not isinstance(clip, dict):
        return {}
    metadata = clip.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "clip_id": clip.get("id"),
        "title": clip.get("title"),
        "status": clip.get("status"),
        "model_name": clip.get("model_name"),
        "image_url": clip.get("image_url"),
        "duration": metadata.get("duration"),
        "tags": metadata.get("tags"),
        "lyrics": metadata.get("prompt"),
        "error_message": metadata.get("error_message") or clip.get("error_message"),
    }


def clip_is_playable(clip: Any) -> bool:
    """Early-playback signal. Not a completion signal."""
    return isinstance(clip, dict) and clip.get("status") in CLIP_PLAYABLE_STATUSES


def clip_is_terminal(clip: Any) -> bool:
    return isinstance(clip, dict) and clip.get("status") in CLIP_TERMINAL_STATUSES


def clip_failed(clip: Any) -> bool:
    return isinstance(clip, dict) and clip.get("status") == CLIP_STATUS_ERROR


def evaluate_clips(clips_by_id: Dict[str, Any], expected_ids: List[str]) -> Dict[str, Any]:
    """Decide what a poll round means for the job as a whole.

    One generation yields an A/B pair, and the two clips finish independently.
    The job is only settled once **every** expected clip is terminal, so a job
    is never marked done while its sibling is still rendering, and one failed
    clip never fails a job whose other clip succeeded.

    Returns a summary with:
      ``all_terminal``  every expected clip reached complete/error
      ``complete_ids``  clips that finished successfully
      ``failed_ids``    clips that errored
      ``pending_ids``   clips not yet terminal (includes ``streaming``)
      ``playable_ids``  clips Suno will already play (early signal only)
      ``settle``        None while running, else ``succeeded``/``failed``
    """
    complete_ids: List[str] = []
    failed_ids: List[str] = []
    pending_ids: List[str] = []
    playable_ids: List[str] = []

    for clip_id in expected_ids:
        clip = clips_by_id.get(clip_id)
        if clip is None:
            # Never observed this round: unknown, not failed.
            pending_ids.append(clip_id)
            continue
        status = clip.get("status") if isinstance(clip, dict) else None
        if status == CLIP_STATUS_COMPLETE:
            complete_ids.append(clip_id)
        elif status == CLIP_STATUS_ERROR:
            failed_ids.append(clip_id)
        else:
            pending_ids.append(clip_id)
        if clip_is_playable(clip):
            playable_ids.append(clip_id)

    all_terminal = not pending_ids and bool(expected_ids)
    settle: Optional[str] = None
    if all_terminal:
        settle = SUCCEEDED if complete_ids else FAILED

    return {
        "all_terminal": all_terminal,
        "complete_ids": complete_ids,
        "failed_ids": failed_ids,
        "pending_ids": pending_ids,
        "playable_ids": playable_ids,
        "settle": settle,
    }
