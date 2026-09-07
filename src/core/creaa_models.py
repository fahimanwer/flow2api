"""Pure helpers for the browser-backed Creaa provider.

No I/O, no FastAPI, no database. Shared by ``services.creaa_bridge`` (the durable
broker) and ``api.creaa`` (the REST/WebSocket surface):

* job state names, which states hold an account's single generation slot, and
  which worker-reported transitions are legal;
* request normalisation/validation for image and video generations (global
  shape rules only - model-specific limits are the worker's preflight job);
* canonical request hashing for Idempotency-Key comparisons.

Nothing here knows Creaa website URLs, cookies or upstream routes.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

# --------------------------------------------------------------------------- states

QUEUED = "queued"
CLAIMED = "claimed"          # persisted BEFORE the execute message leaves the server
SUBMITTING = "submitting"    # worker announced submit intent; ack gates the website click
SUBMITTED = "submitted"      # provider task id known
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
NEEDS_REVIEW = "needs_review"  # uncertain: may or may not exist upstream
NEEDS_LOGIN = "needs_login"    # worker lost its session while this job was in flight

ALL_STATES = (
    QUEUED, CLAIMED, SUBMITTING, SUBMITTED, RUNNING,
    SUCCEEDED, FAILED, CANCELLED, NEEDS_REVIEW, NEEDS_LOGIN,
)
TERMINAL_STATES = frozenset({SUCCEEDED, FAILED, CANCELLED})
# States that occupy the account's single generation slot. needs_review / needs_login
# deliberately hold the slot: releasing them would let a second generation start
# while a first one may still be running upstream.
ACTIVE_STATES = frozenset({CLAIMED, SUBMITTING, SUBMITTED, RUNNING, NEEDS_REVIEW, NEEDS_LOGIN})
# States an operator may settle through POST /v1/creaa/jobs/{id}/resolve.
RESOLVABLE_STATES = frozenset({NEEDS_REVIEW, NEEDS_LOGIN})
# States whose tracking may be resumed on reconnect - only when a provider task id exists.
RESUMABLE_STATES = frozenset({SUBMITTED, RUNNING, NEEDS_LOGIN})
# States a worker may report in a job_event message.
EVENT_STATES = frozenset({SUBMITTING, SUBMITTED, RUNNING, SUCCEEDED, FAILED, NEEDS_REVIEW, NEEDS_LOGIN})
# States that require a provider task id to be known once entered.
STATES_REQUIRING_PROVIDER_ID = frozenset({SUBMITTED, RUNNING, SUCCEEDED})

# Legal worker-driven transitions (current job state -> allowed event states).
# Terminal states are absent on purpose: they are immutable.
# A late report for the SAME attempt from needs_review/needs_login is the evidence
# that settles the uncertainty, so those rows may still move forward - never back.
EVENT_TRANSITIONS: Dict[str, frozenset] = {
    CLAIMED: frozenset({SUBMITTING, FAILED, NEEDS_LOGIN}),
    SUBMITTING: frozenset({SUBMITTED, FAILED, NEEDS_REVIEW, NEEDS_LOGIN}),
    SUBMITTED: frozenset({RUNNING, SUCCEEDED, FAILED, NEEDS_REVIEW, NEEDS_LOGIN}),
    RUNNING: frozenset({RUNNING, SUCCEEDED, FAILED, NEEDS_REVIEW, NEEDS_LOGIN}),
    NEEDS_REVIEW: frozenset({SUBMITTED, RUNNING, SUCCEEDED, FAILED}),
    NEEDS_LOGIN: frozenset({SUBMITTED, RUNNING, SUCCEEDED, FAILED}),
}


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES


def event_transition_allowed(current_state: str, event_state: str) -> bool:
    return event_state in EVENT_TRANSITIONS.get(current_state, frozenset())


# --------------------------------------------------------------------------- limits

MEDIA_TYPES = ("image", "video")
BILLING_POLICIES = ("unlimited_only", "allow_credits")
ASPECT_RATIOS = ("auto", "1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "21:9", "9:21", "4:5", "5:4", "2:1", "1:4", "4:1", "1:8", "8:1")
IMAGE_SIZES = ("512px", "1K", "2K", "4K")
QUALITIES = ("auto", "low", "medium", "high")
RESOLUTIONS = ("480p", "720p", "1080p", "2k", "4k")

MAX_PROMPT_CHARS = 20000
MAX_REFERENCES = 50
MAX_REFERENCE_URL_CHARS = 2048
MAX_REFERENCE_DATA_CHARS = 8_000_000   # ~6 MB decoded; the backend never fetches or decodes it
MAX_DURATION_SECONDS = 60
MAX_CREDITS = 100_000
MAX_CATALOG_ENTRIES = 200
MAX_IDEMPOTENCY_KEY_CHARS = 200

MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\-]{0,149}$")
OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@\-]{0,127}$")
DATA_URL_PREFIX_RE = re.compile(r"^data:image/[a-zA-Z0-9.+\-]+;base64,", re.IGNORECASE)

_MODEL_PREFIX = "creaa/"
_ALLOWED_REQUEST_KEYS = frozenset({
    "model", "prompt", "account_id", "n",
    "aspect_ratio", "image_size", "quality",
    "duration", "resolution",
    "references", "billing_policy", "max_credits",
})
_IMAGE_ONLY_KEYS = ("image_size", "quality")
_VIDEO_ONLY_KEYS = ("duration", "resolution")


class CreaaValidationError(ValueError):
    """Client-side request problem. ``status`` maps straight to the HTTP code."""

    def __init__(self, code: str, message: str, status: int = 400, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra

    def as_detail(self) -> Dict[str, Any]:
        detail: Dict[str, Any] = {"code": self.code, "message": self.message}
        detail.update(self.extra)
        return detail


# --------------------------------------------------------------------------- ids / time

def new_job_id() -> str:
    return "cj_" + uuid.uuid4().hex[:20]


def new_attempt_id() -> str:
    return "at_" + uuid.uuid4().hex[:16]


def iso_utc(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds")


# --------------------------------------------------------------------------- normalisation

def normalize_model_id(model: Any) -> str:
    """Strip one leading ``creaa/`` prefix and validate the remaining id."""
    if not isinstance(model, str) or not model.strip():
        raise CreaaValidationError("model_required", "model is required")
    value = model.strip()
    if value.lower().startswith(_MODEL_PREFIX):
        value = value[len(_MODEL_PREFIX):]
    if value.lower().startswith(_MODEL_PREFIX) or not MODEL_ID_RE.fullmatch(value) or "//" in value or ".." in value:
        raise CreaaValidationError("invalid_model", "model id contains unsupported characters or is too long")
    return value


def validate_opaque_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CreaaValidationError(f"{field}_required", f"{field} is required")
    value = value.strip()
    if not OPAQUE_ID_RE.match(value):
        raise CreaaValidationError(f"invalid_{field}", f"{field} contains unsupported characters or is too long")
    return value


def _enum(value: Any, allowed: Iterable[str], field: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CreaaValidationError(f"invalid_{field}", f"{field} must be a string")
    allowed_list = list(allowed)
    lookup = {item.lower(): item for item in allowed_list}
    chosen = lookup.get(value.strip().lower())
    if chosen is None:
        raise CreaaValidationError(
            f"invalid_{field}", f"{field} must be one of {', '.join(allowed_list)}", allowed=allowed_list
        )
    return chosen


def _normalize_reference(item: Any, index: int) -> Dict[str, str]:
    if isinstance(item, dict):
        if "url" in item and "data" in item:
            raise CreaaValidationError("invalid_reference", f"references[{index}] must carry either url or data, not both")
        raw = item.get("url") if "url" in item else item.get("data")
        kind = "url" if "url" in item else ("data" if "data" in item else None)
        if kind is None:
            raise CreaaValidationError("invalid_reference", f"references[{index}] needs a url or data field")
    elif isinstance(item, str):
        raw = item
        kind = "data" if item[:5].lower() == "data:" else "url"
    else:
        raise CreaaValidationError("invalid_reference", f"references[{index}] must be a string or object")
    if not isinstance(raw, str) or not raw.strip():
        raise CreaaValidationError("invalid_reference", f"references[{index}] is empty")
    raw = raw.strip()
    if kind == "url":
        if len(raw) > MAX_REFERENCE_URL_CHARS:
            raise CreaaValidationError("invalid_reference", f"references[{index}] url is longer than {MAX_REFERENCE_URL_CHARS} characters")
        if not (raw.startswith("https://") or raw.startswith("http://")):
            raise CreaaValidationError("invalid_reference", f"references[{index}] url must start with http:// or https://")
        return {"type": "url", "url": raw}
    if len(raw) > MAX_REFERENCE_DATA_CHARS:
        raise CreaaValidationError("invalid_reference", f"references[{index}] data exceeds {MAX_REFERENCE_DATA_CHARS} characters")
    if not DATA_URL_PREFIX_RE.match(raw):
        raise CreaaValidationError("invalid_reference", f"references[{index}] data must be a base64 data:image/* URL")
    return {"type": "data", "data": raw}


def normalize_generation_request(media_type: str, body: Any) -> Dict[str, Any]:
    """Validate a caller body and return the canonical request dict.

    Returned keys: model, prompt, account_id (may be None), aspect_ratio, image_size,
    quality, duration, resolution, references, billing_policy, max_credits.
    Raises ``CreaaValidationError`` on any problem. Never touches the network.
    """
    if media_type not in MEDIA_TYPES:
        raise CreaaValidationError("invalid_media_type", f"media_type must be one of {', '.join(MEDIA_TYPES)}")
    if not isinstance(body, dict):
        raise CreaaValidationError("invalid_body", "request body must be a JSON object")

    unknown = sorted(set(body) - _ALLOWED_REQUEST_KEYS)
    if unknown:
        raise CreaaValidationError("unknown_field", f"unsupported field(s): {', '.join(unknown)}", fields=unknown)

    model = normalize_model_id(body.get("model"))

    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise CreaaValidationError("prompt_required", "prompt is required")
    prompt = prompt.strip()
    if len(prompt) > MAX_PROMPT_CHARS:
        raise CreaaValidationError("prompt_too_long", f"prompt exceeds {MAX_PROMPT_CHARS} characters")

    n = body.get("n", 1)
    if n is None:
        n = 1
    if isinstance(n, bool) or not isinstance(n, int) or n != 1:
        raise CreaaValidationError("unsupported_n", "only n=1 is supported")

    account_id = body.get("account_id")
    if account_id is not None:
        account_id = validate_opaque_id(account_id, "account_id")

    for key in (_VIDEO_ONLY_KEYS if media_type == "image" else _IMAGE_ONLY_KEYS):
        if body.get(key) is not None:
            raise CreaaValidationError("field_not_applicable", f"{key} is not applicable to {media_type} generations", field=key)

    aspect_ratio = _enum(body.get("aspect_ratio"), ASPECT_RATIOS, "aspect_ratio")
    image_size = _enum(body.get("image_size"), IMAGE_SIZES, "image_size")
    quality = _enum(body.get("quality"), QUALITIES, "quality")
    raw_resolution = body.get("resolution")
    if isinstance(raw_resolution, str) and re.fullmatch(r"[1-9][0-9]{1,4}x[1-9][0-9]{1,4}", raw_resolution.strip().lower()):
        resolution = raw_resolution.strip().lower()
    else:
        resolution = _enum(raw_resolution, RESOLUTIONS, "resolution")

    duration = body.get("duration")
    if duration is not None:
        if isinstance(duration, bool) or not isinstance(duration, int) or duration < 1 or duration > MAX_DURATION_SECONDS:
            raise CreaaValidationError("invalid_duration", f"duration must be an integer between 1 and {MAX_DURATION_SECONDS} seconds")

    references_raw = body.get("references") or []
    if not isinstance(references_raw, list):
        raise CreaaValidationError("invalid_reference", "references must be a list")
    if len(references_raw) > MAX_REFERENCES:
        raise CreaaValidationError("too_many_references", f"at most {MAX_REFERENCES} references are allowed")
    references = [_normalize_reference(item, i) for i, item in enumerate(references_raw)]
    if sum(len(ref.get("data", "")) for ref in references) > 12_000_000:
        raise CreaaValidationError("references_too_large", "combined inline references exceed 12 MB; use uploaded HTTPS URLs")

    billing_policy = _enum(body.get("billing_policy"), BILLING_POLICIES, "billing_policy") or "unlimited_only"
    max_credits = body.get("max_credits")
    if billing_policy == "allow_credits":
        if isinstance(max_credits, bool) or not isinstance(max_credits, int) or max_credits <= 0:
            raise CreaaValidationError("max_credits_required", "allow_credits requires max_credits to be an integer greater than 0")
        if max_credits > MAX_CREDITS:
            raise CreaaValidationError("max_credits_too_large", f"max_credits may not exceed {MAX_CREDITS}")
    else:
        if max_credits not in (None, 0):
            raise CreaaValidationError("max_credits_not_allowed", "max_credits is only meaningful with billing_policy=allow_credits")
        max_credits = None

    return {
        "model": model,
        "prompt": prompt,
        "account_id": account_id,
        "aspect_ratio": aspect_ratio,
        "image_size": image_size,
        "quality": quality,
        "duration": duration,
        "resolution": resolution,
        "references": references,
        "billing_policy": billing_policy,
        "max_credits": max_credits,
    }


def canonical_request_hash(media_type: str, normalized: Dict[str, Any]) -> str:
    """Stable hash of a normalised request, used for Idempotency-Key comparisons."""
    payload = {"media_type": media_type, **normalized}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def wire_request(normalized: Dict[str, Any]) -> Dict[str, Any]:
    """The request object sent to the worker inside execute/resume messages."""
    return {key: value for key, value in normalized.items() if key != "account_id"}


def public_request(normalized: Dict[str, Any]) -> Dict[str, Any]:
    """Caller-facing request view: references are summarised, never echoed in full."""
    view = wire_request(normalized)
    view["references"] = [
        {"type": ref.get("type"), "length": len(ref.get("url") or ref.get("data") or "")}
        for ref in normalized.get("references") or []
    ]
    return view


def normalize_catalog(models: Any) -> List[Dict[str, Any]]:
    """Validate a worker-advertised model list. Unknown extra keys are kept as-is."""
    if models is None:
        return []
    if not isinstance(models, list):
        raise CreaaValidationError("invalid_catalog", "models must be a list")
    if len(models) > MAX_CATALOG_ENTRIES:
        raise CreaaValidationError("invalid_catalog", f"models may not exceed {MAX_CATALOG_ENTRIES} entries")
    catalog: List[Dict[str, Any]] = []
    seen = set()
    for index, entry in enumerate(models):
        if not isinstance(entry, dict):
            raise CreaaValidationError("invalid_catalog", f"models[{index}] must be an object")
        model_id = normalize_model_id(entry.get("id"))
        media_type = entry.get("media_type")
        if media_type not in MEDIA_TYPES:
            raise CreaaValidationError("invalid_catalog", f"models[{index}].media_type must be image or video")
        key = (model_id, media_type)
        if key in seen:
            continue
        seen.add(key)
        item = dict(entry)
        item["id"] = model_id
        item["media_type"] = media_type
        label = entry.get("label")
        item["label"] = str(label)[:200] if label is not None else model_id
        try:
            json.dumps(item)
        except (TypeError, ValueError):
            raise CreaaValidationError("invalid_catalog", f"models[{index}] is not JSON serialisable")
        catalog.append(item)
    return catalog


def catalog_supports(catalog: Iterable[Dict[str, Any]], model: str, media_type: str) -> bool:
    return any(entry.get("id") == model and entry.get("media_type") == media_type for entry in catalog)
