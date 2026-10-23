"""REST -> flow.google.com RPC translation for cookie sessions (2026-10-23).

Our FlowClient still builds the aisandbox-pa REST bodies it always did (retries, captcha, routing and
response parsing stay untouched). When the account runs on a cookie session, `_make_request` hands
the REST call here: `translate_request` returns the batchexecute RPC that does the same job plus a
`normalize` function that turns the RPC payload back into the REST-shaped dict our code parses.

RPC ids and argument layouts come from upstream TheSmallHanCat/flow2api (src/services/flow_frontend.py
and flow_current.py, 2026-09-27 .. 2026-10-23), which captured them from the live Flow web app.
Videos live in flow_rpc_video.py.
"""

import base64
import random
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
IMAGE_MODELS = frozenset({"BELUGA", "HARBOR_SEAL", "GEM_PIX_2"})
IMAGE_ASPECT = {
    "IMAGE_ASPECT_RATIO_SQUARE": 1,
    "IMAGE_ASPECT_RATIO_PORTRAIT": 2,
    "IMAGE_ASPECT_RATIO_16_9": 3,
    "IMAGE_ASPECT_RATIO_LANDSCAPE": 3,
    "IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE": 4,
    "IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR": 5,
}


class CookieModeUnsupported(Exception):
    """A REST call that has no flow.google.com RPC mapping yet (fails loudly, never silently)."""


@dataclass
class RpcCall:
    rpc_id: str
    argument: Any
    source_path: str
    normalize: Callable[[Any], Dict[str, Any]]
    timeout_floor: int = 0


def project_context(project_id: str, recaptcha_token: Optional[str]) -> List[Any]:
    return [None, 22, None, None, None, project_id, None, None, None, None,
            [recaptcha_token, 1] if recaptcha_token else None]


def source_path_for(project_id: Optional[str]) -> str:
    return f"/project/{project_id}" if project_id else "/projects"


def find_content_url(value: Any, kind: str) -> str:
    if isinstance(value, str):
        return value if value.startswith("https://") and f"/{kind}/" in value else ""
    if isinstance(value, list):
        for item in value:
            url = find_content_url(item, kind)
            if url:
                return url
    elif isinstance(value, dict):
        for item in value.values():
            url = find_content_url(item, kind)
            if url:
                return url
    return ""


def find_uuid(value: Any, excluded: Optional[set] = None) -> str:
    excluded = {item.lower() for item in (excluded or set())}
    if isinstance(value, str):
        for candidate in UUID_RE.findall(value):
            if candidate.lower() not in excluded:
                return candidate
        return ""
    if isinstance(value, list):
        for item in value:
            found = find_uuid(item, excluded)
            if found:
                return found
    elif isinstance(value, dict):
        for item in value.values():
            found = find_uuid(item, excluded)
            if found:
                return found
    return ""


def find_encoded_media(value: Any) -> str:
    if isinstance(value, str):
        if value.startswith("https://"):
            return ""
        if len(value) >= 512 and re.fullmatch(r"[A-Za-z0-9+/=_-]+", value):
            return value
        return ""
    if isinstance(value, list):
        for item in value:
            found = find_encoded_media(item)
            if found:
                return found
    elif isinstance(value, dict):
        for item in value.values():
            found = find_encoded_media(item)
            if found:
                return found
    return ""


def media_name(value: str) -> str:
    text = str(value or "").strip().rstrip("/")
    return text.rsplit("/", 1)[-1] if "/" in text else text


def _recaptcha_token(body: Dict[str, Any]) -> Optional[str]:
    context = (body or {}).get("clientContext") or {}
    token = ((context.get("recaptchaContext") or {}).get("token") or "").strip()
    if not token:
        for request in (body or {}).get("requests") or []:
            inner = (request or {}).get("clientContext") or {}
            token = ((inner.get("recaptchaContext") or {}).get("token") or "").strip()
            if token:
                break
    return token or None


# ---------- credits ----------

def _normalize_credits(payload: Any) -> Dict[str, Any]:
    values = payload if isinstance(payload, list) else []
    credits = next((int(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)), 0)
    # The tier is NOT returned on purpose: Flow's numeric tier fields are not mapped yet (seen on an
    # Ultra account: [25050, 2, 3, 3, null, 25050, [ts]]), and a guessed tier would re-route accounts.
    # Callers write the tier only when the key is present, so the stored tier stays.
    return {"credits": credits, "frontendRpc": "nzlxg", "frontendRaw": values[:6]}


# ---------- images ----------

def _prompt_text(request: Dict[str, Any]) -> str:
    parts = ((request.get("structuredPrompt") or {}).get("parts")) or []
    texts = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if set(part) - {"text"}:
            raise CookieModeUnsupported(
                "Flow Characters (@Name) are not available on the new Flow API yet; send the prompt without characters"
            )
        texts.append(str(part.get("text") or ""))
    if texts:
        return "".join(texts)
    return str(request.get("prompt") or (request.get("textInput") or {}).get("prompt") or "")


def build_image_argument(project_id: str, body: Dict[str, Any]) -> tuple:
    requests = body.get("requests") or []
    if len(requests) != 1:
        raise CookieModeUnsupported(f"image batch of {len(requests)} requests (expected 1)")
    request = requests[0] or {}
    if request.get("referenceEntities"):
        raise CookieModeUnsupported("Flow Characters are not available on the new Flow API yet")
    model = str(request.get("imageModelName") or "").strip()
    if model == "NARWHAL":
        model = "BELUGA"
    if model not in IMAGE_MODELS:
        raise CookieModeUnsupported(f"image model {model or '?'} has no Flow web mapping")
    aspect_key = str(request.get("imageAspectRatio") or "").strip().upper()
    if aspect_key not in IMAGE_ASPECT:
        raise CookieModeUnsupported(f"image aspect ratio {aspect_key or '?'} has no Flow web mapping")
    refs = []
    for item in request.get("imageInputs") or []:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("mediaId") or "").strip()
            if name:
                refs.append([media_name(name), None, None, None, 1])
    token = _recaptcha_token(body)
    context = project_context(project_id, token)
    session_id = str(uuid.uuid4()).upper()
    seed = request.get("seed")
    seed = int(seed) if isinstance(seed, (int, float)) and seed > 0 else random.randint(1, 2147483647)
    rpc_request = [
        None, None, refs or None, seed, IMAGE_ASPECT[aspect_key], model, None, context,
        [[[_prompt_text(request)]]], None, None, None, session_id, str(uuid.uuid4()).upper(),
    ]
    return [None, [rpc_request], 1, context, [session_id]], session_id


def normalize_image_response(payload: Any) -> Dict[str, Any]:
    items = payload[0] if isinstance(payload, list) and payload and isinstance(payload[0], list) else []
    media = []
    for item in items:
        if not isinstance(item, list) or not item:
            continue
        media_id = str(item[0] or "").strip()
        url = find_content_url(item, "image")
        if media_id and url:
            media.append({"name": media_id, "image": {"generatedImage": {"fifeUrl": url}}})
    if not media:
        raise RuntimeError("Flow image RPC returned no downloadable media")
    return {"media": media, "frontendRpc": "ogiZ0b"}


# ---------- upload ----------

def build_upload_argument(body: Dict[str, Any]) -> tuple:
    context = body.get("clientContext") or {}
    project_id = str(context.get("projectId") or "").strip()
    if not project_id:
        raise CookieModeUnsupported("image upload without a project id")
    image_b64 = str(body.get("imageBytes") or "")
    if not image_b64:
        raise CookieModeUnsupported("image upload without bytes")
    mime = str(body.get("mimeType") or "image/jpeg")
    filename = str(body.get("fileName") or f"flow2api_{uuid.uuid4().hex[:12]}.{'png' if 'png' in mime else 'jpg'}")
    argument = [
        project_context(project_id, _recaptcha_token(body)), image_b64, mime, 1,
        None, None, None, None, filename, None, str(uuid.uuid4()).upper(), str(uuid.uuid4()).upper(),
    ]
    return argument, project_id


def make_upload_normalizer(project_id: str) -> Callable[[Any], Dict[str, Any]]:
    def normalize(payload: Any) -> Dict[str, Any]:
        url = find_content_url(payload, "image")
        media_id = ""
        if url:
            match = re.search(r"/image/([0-9a-f-]{36})", url, re.IGNORECASE)
            media_id = match.group(1) if match else ""
        media_id = media_id or find_uuid(payload, excluded={project_id})
        if not media_id:
            raise RuntimeError("Flow upload RPC returned no media id")
        return {"media": {"name": media_id}, "mediaGenerationId": {"mediaGenerationId": media_id}, "frontendRpc": "maseQ"}
    return normalize


# ---------- upsample ----------

def build_upsample_argument(body: Dict[str, Any]) -> tuple:
    context = body.get("clientContext") or {}
    project_id = str(context.get("projectId") or "").strip()
    if not project_id:
        raise CookieModeUnsupported("image upsample without a project id")
    resolution = 2 if "4K" in str(body.get("targetResolution") or "").upper() else 1
    argument = [media_name(str(body.get("mediaId") or "")), resolution, project_context(project_id, _recaptcha_token(body))]
    return argument, project_id


def normalize_upsample_response(payload: Any) -> Dict[str, Any]:
    encoded = find_encoded_media(payload)
    if encoded:
        return {"encodedImage": encoded, "frontendRpc": "SPrCad"}
    url = find_content_url(payload, "image")
    if url:
        # FlowClient downloads it and fills encodedImage (the caller expects base64).
        return {"encodedImage": "", "fifeUrl": url, "frontendRpc": "SPrCad"}
    raise RuntimeError("Flow upsample RPC returned no image")


# ---------- project ----------

def build_create_project_argument(title: str) -> List[Any]:
    return ["projects/*", [None, [str(title or "flow2api")[:80]]], [None, 22]]


def extract_project_id(payload: Any) -> str:
    if isinstance(payload, str) and UUID_RE.fullmatch(payload):
        return payload
    if isinstance(payload, list):
        for item in payload:
            found = extract_project_id(item)
            if found:
                return found
    return ""


# ---------- dispatcher ----------

def translate_request(method: str, url: str, body: Optional[Dict[str, Any]]) -> RpcCall:
    """Map one aisandbox-pa REST call to its Flow web RPC, or raise CookieModeUnsupported."""
    path = urlparse(url).path
    body = body or {}
    if method.upper() == "GET" and path.endswith("/v1/credits"):
        return RpcCall("nzlxg", [], "/projects", _normalize_credits)
    match = re.search(r"/v1/projects/([^/]+)/flowMedia:batchGenerateImages$", path)
    if match:
        project_id = match.group(1)
        argument, _ = build_image_argument(project_id, body)
        return RpcCall("ogiZ0b", argument, source_path_for(project_id), normalize_image_response, timeout_floor=90)
    if path.endswith("/v1/flow/uploadImage"):
        argument, project_id = build_upload_argument(body)
        return RpcCall("maseQ", argument, source_path_for(project_id), make_upload_normalizer(project_id), timeout_floor=90)
    if path.endswith("/v1/flow/upsampleImage"):
        argument, project_id = build_upsample_argument(body)
        return RpcCall("SPrCad", argument, source_path_for(project_id), normalize_upsample_response, timeout_floor=90)
    if "/v1/video:" in path or path.endswith("/v1/video") or ":runVideoFx" in path:
        try:
            from .flow_rpc_video import translate_video_request
        except ImportError:
            translate_video_request = None
        if translate_video_request is not None:
            translated = translate_video_request(path, body)
            if translated:
                rpc_id, argument, project_id, normalize = translated
                return RpcCall(rpc_id, argument, source_path_for(project_id), normalize, timeout_floor=90)
        raise CookieModeUnsupported(f"video call {path} is not mapped to the new Flow API yet")
    raise CookieModeUnsupported(f"{method.upper()} {path} has no new Flow API mapping")


def decode_b64_ok(value: str) -> bool:
    try:
        base64.b64decode(value[:64] + "=" * (-len(value[:64]) % 4))
        return True
    except Exception:
        return False
