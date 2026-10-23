"""REST -> flow.google.com batchexecute translation for the VIDEO endpoints (2026-10-23).

Google retired the aisandbox-pa REST + Bearer path. Our FlowClient still builds the REST bodies it always
did; `translate_video_request` turns one of those bodies into the batchexecute RPC the Flow web app sends,
and hands back a `normalize` function that turns the decoded RPC payload into the REST-shaped dict our
code already parses (flow_client._normalize_video_generation_response, generation_handler._poll_video_result).

RPC ids and argument layouts are ported from upstream TheSmallHanCat/flow2api commit 551cca5
(src/services/flow_frontend.py `_build_frontend_video_generation_argument`, `_normalize_frontend_video_*`;
src/services/flow_current.py `_current_generate_video`, `check_video_status`, `get_media`).

    REST path (after the host)                         RPC
    /v1/video:batchAsyncGenerateVideoText              YhhmEf
    /v1/video:batchAsyncGenerateVideoReferenceImages   MZZa6b   (Veo r2v and Omni abra_r2v_*)
    /v1/video:batchAsyncGenerateVideoStartAndEndImage  nprQif
    /v1/video:batchAsyncGenerateVideoStartImage        eb1hJf
    /v1/video:batchAsyncGenerateVideoExtendVideo       fZytfe
    /v1/video:batchAsyncGenerateVideoUpsampleVideo     p0UkFb
    /v1/video:batchCheckAsyncVideoGenerationStatus     jwpduf   (+ as29s follow-ups, see video_status_followups)
    /v1:runVideoFxConcatenation, ...CheckConcatenation NotImplementedError (no frontend RPC exists upstream)

Ids, as the current Flow frontend uses them (upstream tests/test_video_asset_resolution.py):
- A submit returns records [operation_id, project_id, media_name, ...]. jwpduf (status) and as29s (media
  lookup) are keyed by operation_id; as29s answers code=[5] for media_name.
- Our status body only carries `media[].name`, built by flow_client._operations_to_media_refs from
  `mediaName or name or operation.name`. So the submit result deliberately has NO `mediaName` key: the name
  that reaches the status call is the operation id. The Flow media_name is kept as `flowMediaName`.
- In the status result `mediaName` = `mediaGenerationId` = operation id, because our handler's
  _resolve_video_asset uses `mediaName` as the lookup key (REST-era invariant: media name == generation id).

Errors: NotImplementedError = the body asks for something the RPC has no slot for (fail loudly rather than
make a different video). ValueError = the body is malformed (missing project, token, model, media id).

Standard library only; no imports from our other modules (flow_rpc imports this file lazily).
"""

import random
import re
import uuid
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Tuple

STATUS_RPC = "jwpduf"
MEDIA_LOOKUP_RPC = "as29s"

STATUS_ACTIVE = "MEDIA_GENERATION_STATUS_ACTIVE"
STATUS_SUCCESSFUL = "MEDIA_GENERATION_STATUS_SUCCESSFUL"
STATUS_FAILED = "MEDIA_GENERATION_STATUS_FAILED"

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)

# endpoint name after "/video:" -> builder mode of upstream's _build_frontend_video_generation_argument
_GENERATION_MODES = {
    "batchAsyncGenerateVideoText": "text",
    "batchAsyncGenerateVideoReferenceImages": "references",
    "batchAsyncGenerateVideoStartAndEndImage": "start_end",
    "batchAsyncGenerateVideoStartImage": "start",
    "batchAsyncGenerateVideoExtendVideo": "extend",
    "batchAsyncGenerateVideoUpsampleVideo": "upsample",
}
_STATUS_ENDPOINT = "batchCheckAsyncVideoGenerationStatus"
_CONCAT_ENDPOINTS = ("runVideoFxConcatenation", "runVideoFxCheckConcatenationStatus")

# Modes whose upstream request has a resolution slot (index 11 / 9 / 7 / 6).
_MODES_WITH_RESOLUTION = {"references", "start", "text", "upsample"}

_ERROR_TEXT = {
    "PUBLIC_ERROR_PROMINENT_PEOPLE_FILTER_FAILED": "the prompt or reference media hit Google's real-person filter",
    "PUBLIC_ERROR_UNSAFE_GENERATION": "the prompt or reference media hit Google's content safety filter",
}

# The status body carries only names + project ids, so the aspect ratio and model key the submit used would
# be lost before the upscale step (handler passes video_info.aspectRatio to upsample_video). Bounded memory
# of what each submit used, filled by the submit normalizer, read by the status normalizer.
_OPERATION_INFO: "OrderedDict[str, Dict[str, str]]" = OrderedDict()
_OPERATION_INFO_LIMIT = 4096


# --------------------------------------------------------------------------- upstream helpers (ported)


def _project_context(project_id: str, recaptcha_token: str) -> List[Any]:
    return [None, 22, None, None, None, project_id, None, None, None, None, [recaptcha_token, 1]]


def _map_aspect_ratio(aspect_ratio: Any) -> int:
    # Upstream maps anything that is not PORTRAIT/SQUARE to 2 (landscape). Stricter here: an unknown or
    # missing ratio would silently become a landscape video.
    normalized = str(aspect_ratio or "").strip().upper()
    if normalized.startswith("VIDEO_ASPECT_RATIO_"):
        if "PORTRAIT" in normalized:
            return 1
        if "SQUARE" in normalized:
            return 0
        if "LANDSCAPE" in normalized:
            return 2
    raise NotImplementedError(f"Flow video RPC: unsupported video aspect ratio {aspect_ratio!r}")


def _prompt_value(prompt: str) -> List[Any]:
    return [[[str(prompt or "")]]]


def _generation_metadata(session_id: str) -> List[Any]:
    return [None, None, None, None, session_id, str(uuid.uuid4()).upper()]


def _media_reference(media_id: str) -> List[Any]:
    return [None, str(media_id or "").strip()]


def _video_resolution(resolution: Optional[str]) -> Optional[int]:
    normalized = str(resolution or "").strip().upper()
    if not normalized:
        return None
    if "4K" in normalized:
        return 4
    if "1080" in normalized:
        return 3
    if "720" in normalized:
        return 2
    if "360" in normalized:
        return 1
    return None


def _find_flow_content_url(value: Any, media_kind: str) -> str:
    if isinstance(value, str):
        return value if value.startswith("https://") and f"/{media_kind}/" in value else ""
    if isinstance(value, list):
        for item in value:
            url = _find_flow_content_url(item, media_kind)
            if url:
                return url
    elif isinstance(value, dict):
        for item in value.values():
            url = _find_flow_content_url(item, media_kind)
            if url:
                return url
    return ""


def _is_uuid(value: Any) -> bool:
    return bool(isinstance(value, str) and _UUID_RE.fullmatch(value))


def _extract_error_code(value: Any) -> str:
    if isinstance(value, str):
        return value if value.startswith(("PUBLIC_ERROR_", "ERROR_")) else ""
    if isinstance(value, (list, tuple)):
        for item in value:
            code = _extract_error_code(item)
            if code:
                return code
    return ""


def _extract_video_records(payload: Any, project_id: Optional[str] = None) -> List[List[Any]]:
    """Every [uuid, project_id, uuid, ...] list in the payload, first occurrence per operation id."""
    records: List[List[Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, list):
            if (
                len(value) >= 3
                and _is_uuid(value[0])
                and (project_id is None or str(value[1] or "") == project_id)
                and _is_uuid(value[2])
            ):
                records.append(value)
                return
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)

    walk(payload)
    unique: List[List[Any]] = []
    seen = set()
    for record in records:
        key = str(record[0]).lower()
        if key not in seen:
            seen.add(key)
            unique.append(record)
    return unique


def _build_video_argument(
    *,
    project_id: str,
    prompt: str,
    model_key: str,
    aspect_ratio: str,
    recaptcha_token: str,
    session_id: str,
    mode: str,
    reference_media_ids: Optional[List[str]] = None,
    start_media_id: Optional[str] = None,
    end_media_id: Optional[str] = None,
    video_media_id: Optional[str] = None,
    resolution: Optional[str] = None,
    seed: Optional[int] = None,
) -> Tuple[str, List[Any]]:
    """Upstream _build_frontend_video_generation_argument, verbatim except: `output_spec` is always None
    (upstream never fills it either), and upsample takes our REST seed instead of a fresh random one."""
    output_spec = None
    prompt_message = [None, None, _prompt_value(prompt)]
    aspect_value = _map_aspect_ratio(aspect_ratio)
    metadata = _generation_metadata(session_id)
    resolution_value = _video_resolution(resolution)

    if mode == "references":
        references = [
            _media_reference(media_id)
            for media_id in (reference_media_ids or [])
            if str(media_id or "").strip()
        ]
        rpc_id = "MZZa6b"
        request = [
            prompt_message, references, model_key, aspect_value, None, metadata,
            None, None, output_spec, None, None, resolution_value,
        ]
    elif mode == "start_end":
        rpc_id = "nprQif"
        request = [
            prompt_message, model_key, aspect_value, None,
            _media_reference(start_media_id or ""), _media_reference(end_media_id or ""),
            metadata, None, output_spec,
        ]
    elif mode == "start":
        rpc_id = "eb1hJf"
        request = [
            prompt_message, model_key, aspect_value, None, _media_reference(start_media_id or ""),
            metadata, None, None, output_spec, resolution_value,
        ]
    elif mode == "extend":
        rpc_id = "fZytfe"
        request = [
            [None, str(video_media_id or "").strip()], prompt_message, model_key, aspect_value,
            None, metadata, None, output_spec,
        ]
    elif mode == "upsample":
        rpc_id = "p0UkFb"
        request = [
            [None, str(video_media_id or "").strip()], None, aspect_value,
            seed if isinstance(seed, int) and not isinstance(seed, bool) and seed > 0
            else random.randint(1, 2147483647),
            metadata, None, resolution_value, output_spec,
        ]
        if model_key:
            request.extend([None] * (31 - len(request)))
            request.append(model_key)
    elif mode == "text":
        rpc_id = "YhhmEf"
        request = [
            prompt_message, model_key, aspect_value, None, metadata, None, output_spec, resolution_value,
        ]
    else:  # pragma: no cover - modes come from _GENERATION_MODES only
        raise NotImplementedError(f"Flow video RPC: unknown mode {mode!r}")
    return rpc_id, [
        [request],
        _project_context(project_id, recaptcha_token),
        [str(uuid.uuid4()).upper(), 2],
    ]


# --------------------------------------------------------------------------- REST body readers


def _endpoint(path: str) -> Optional[str]:
    clean = str(path or "").split("?", 1)[0].split("#", 1)[0].rstrip("/")
    match = re.search(r"/video:([A-Za-z]+)$", clean)
    if match:
        return match.group(1)
    match = re.search(r":(runVideoFx[A-Za-z]+)$", clean)
    if match:
        return match.group(1)
    return None


def _require_str(value: Any, field: str) -> str:
    text = str(value or "").strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    if not text:
        raise ValueError(f"Flow video RPC: REST body has no {field}")
    return text


def _client_context(body: Dict[str, Any]) -> Tuple[str, str]:
    # Dropped (no slot in the RPC client context): sessionId (";<ms>" labs format; upstream sends a fresh
    # uppercase UUID per request instead), tool, userPaygateTier, recaptchaContext.applicationType.
    context = body.get("clientContext") if isinstance(body.get("clientContext"), dict) else {}
    project_id = _require_str(context.get("projectId"), "clientContext.projectId")
    recaptcha = context.get("recaptchaContext") if isinstance(context.get("recaptchaContext"), dict) else {}
    token = _require_str(recaptcha.get("token"), "clientContext.recaptchaContext.token")
    return project_id, token


def _single_request(body: Dict[str, Any]) -> Dict[str, Any]:
    requests = body.get("requests")
    if not isinstance(requests, list) or not requests:
        raise ValueError("Flow video RPC: REST body has no requests[]")
    if len(requests) != 1:
        # Upstream always submits exactly one request; a batch would need N records mapped back to N
        # requests, which nothing has verified.
        raise NotImplementedError(f"Flow video RPC: {len(requests)} requests in one call (only 1 is supported)")
    if not isinstance(requests[0], dict):
        raise ValueError("Flow video RPC: requests[0] is not an object")
    return requests[0]


def _prompt_text(text_input: Any) -> str:
    """textInput.structuredPrompt.parts[{text}] -> one string. Character parts cannot be sent."""
    if text_input is None:
        return ""
    if not isinstance(text_input, dict):
        raise ValueError("Flow video RPC: textInput is not an object")
    structured = text_input.get("structuredPrompt")
    if isinstance(structured, dict):
        parts = structured.get("parts") or []
        if not isinstance(parts, list):
            raise ValueError("Flow video RPC: structuredPrompt.parts is not a list")
        texts: List[str] = []
        for part in parts:
            if not isinstance(part, dict):
                raise ValueError("Flow video RPC: structuredPrompt part is not an object")
            if set(part) - {"text"}:
                raise NotImplementedError(
                    "Flow video RPC: prompt parts other than text (Flow characters / @Name entity "
                    "references) have no slot in the current frontend video RPC"
                )
            texts.append(str(part.get("text") or ""))
        return "".join(texts)
    if "prompt" in text_input:
        return str(text_input.get("prompt") or "")
    return ""


def _media_id(value: Any, field: str) -> str:
    if not isinstance(value, dict):
        raise ValueError(f"Flow video RPC: REST body has no {field}")
    return _require_str(value.get("mediaId") or value.get("name"), f"{field}.mediaId")


def _output_resolution(request: Dict[str, Any], mode: str) -> Optional[str]:
    """outputSpec.resolution (omni-flash 720P) -> the builder's resolution slot, where the mode has one."""
    output_spec = request.get("outputSpec")
    if output_spec in (None, {}):
        return None
    if not isinstance(output_spec, dict) or set(output_spec) - {"resolution"}:
        raise NotImplementedError(f"Flow video RPC: outputSpec {output_spec!r} has no RPC mapping")
    resolution = output_spec.get("resolution")
    if mode not in _MODES_WITH_RESOLUTION:
        raise NotImplementedError(f"Flow video RPC: mode {mode} has no output resolution slot")
    if _video_resolution(resolution) is None:
        raise NotImplementedError(f"Flow video RPC: unsupported output resolution {resolution!r}")
    return str(resolution)


# --------------------------------------------------------------------------- operation memory


def _remember_operation(operation_id: str, info: Dict[str, str]) -> None:
    key = operation_id.lower()
    _OPERATION_INFO[key] = info
    _OPERATION_INFO.move_to_end(key)
    while len(_OPERATION_INFO) > _OPERATION_INFO_LIMIT:
        _OPERATION_INFO.popitem(last=False)


def _recall_operation(operation_id: str) -> Dict[str, str]:
    return dict(_OPERATION_INFO.get(str(operation_id or "").lower()) or {})


def _plain_name(name: Any) -> str:
    normalized = str(name or "").strip().rstrip("/")
    return normalized.rsplit("/", 1)[-1] if "/" in normalized else normalized


# --------------------------------------------------------------------------- generation (submit)


def _translate_generation(body: Dict[str, Any], mode: str):
    project_id, token = _client_context(body)
    request = _single_request(body)
    model_key = _require_str(request.get("videoModelKey"), "requests[0].videoModelKey")
    aspect_ratio = request.get("aspectRatio")
    _map_aspect_ratio(aspect_ratio)  # fail before building anything

    if request.get("referenceEntities"):
        raise NotImplementedError(
            "Flow video RPC: referenceEntities (Flow characters) have no slot in the current frontend "
            "video RPC (upstream create_flow_entity is retired too)"
        )

    # Dropped everywhere (no slot in upstream's request): body.mediaGenerationContext (batchId,
    # audioFailurePreference=BLOCK_SILENCED_VIDEOS), body.useV2ModelConfig, requests[0].metadata
    # (extend workflowId, upsample sceneId), requests[0].seed (except upsample, which has a seed slot).
    kwargs: Dict[str, Any] = {}
    if mode == "references":
        references = request.get("referenceImages")
        if not isinstance(references, list) or not references:
            raise NotImplementedError("Flow video RPC: reference video without referenceImages")
        media_ids: List[str] = []
        for index, item in enumerate(references):
            if not isinstance(item, dict):
                raise ValueError(f"Flow video RPC: referenceImages[{index}] is not an object")
            usage = str(item.get("imageUsageType") or "IMAGE_USAGE_TYPE_ASSET").strip().upper()
            if usage != "IMAGE_USAGE_TYPE_ASSET":
                # Upstream's reference envelope is [None, media_id] with no usage type; only the ASSET
                # meaning (what our handler sends) is known to match it.
                raise NotImplementedError(f"Flow video RPC: imageUsageType {usage} has no RPC mapping")
            media_ids.append(_media_id(item, f"referenceImages[{index}]"))
        kwargs["reference_media_ids"] = media_ids
    elif mode == "start_end":
        kwargs["start_media_id"] = _media_id(request.get("startImage"), "startImage")
        kwargs["end_media_id"] = _media_id(request.get("endImage"), "endImage")
    elif mode == "start":
        if request.get("endImage"):
            raise ValueError("Flow video RPC: start-image body carries an endImage")
        kwargs["start_media_id"] = _media_id(request.get("startImage"), "startImage")
    elif mode in ("extend", "upsample"):
        kwargs["video_media_id"] = _media_id(request.get("videoInput"), "videoInput")

    if mode == "upsample":
        resolution = request.get("resolution")
        if _video_resolution(resolution) is None:
            raise NotImplementedError(f"Flow video RPC: upsample resolution {resolution!r} has no RPC value")
        kwargs["resolution"] = str(resolution)
        kwargs["seed"] = request.get("seed")
        prompt = ""
    else:
        prompt = _prompt_text(request.get("textInput"))
        kwargs["resolution"] = _output_resolution(request, mode)

    session_id = str(uuid.uuid4()).upper()
    rpc_id, argument = _build_video_argument(
        project_id=project_id,
        prompt=prompt,
        model_key=model_key,
        aspect_ratio=str(aspect_ratio),
        recaptcha_token=token,
        session_id=session_id,
        mode=mode,
        **kwargs,
    )
    return rpc_id, argument, project_id, _submission_normalizer(project_id, str(aspect_ratio), model_key, rpc_id)


def _submission_normalizer(
    project_id: str, aspect_ratio: str, model_key: str, rpc_id: str
) -> Callable[[Any], Dict[str, Any]]:
    def normalize(payload: Any) -> Dict[str, Any]:
        records = _extract_video_records(payload, project_id)
        operations: List[Dict[str, Any]] = []
        for record in records:
            operation_id = str(record[0])
            flow_media_name = str(record[2]) if _is_uuid(record[2]) else operation_id
            _remember_operation(
                operation_id,
                {
                    "aspectRatio": aspect_ratio,
                    "modelKey": model_key,
                    "projectId": project_id,
                    "flowMediaName": flow_media_name,
                },
            )
            # No "mediaName" on purpose: see the module docstring (status must be queried by operation id).
            operations.append(
                {
                    "operation": {"name": operation_id},
                    "name": operation_id,
                    "flowMediaName": flow_media_name,
                    "projectId": project_id,
                    "status": STATUS_ACTIVE,
                    "aspectRatio": aspect_ratio,
                    "modelKey": model_key,
                    "frontendRpc": rpc_id,
                }
            )
        if operations:
            return {"operations": operations, "projectId": project_id, "frontendRpc": rpc_id}
        if _find_flow_content_url(payload, "video"):
            # Upstream returns {"direct_media": True, "video_url": ...} here; our handler only knows how to
            # poll operations and would answer "Failed to create generation task".
            raise NotImplementedError(
                f"Flow video RPC {rpc_id} returned a finished video URL without an operation id"
            )
        error_code = _extract_error_code(payload)
        raise RuntimeError(
            f"Flow frontend video RPC returned no generation operations: rpc={rpc_id}"
            + (f", error={error_code}" if error_code else "")
        )

    return normalize


# --------------------------------------------------------------------------- status (poll)


def _translate_status(body: Dict[str, Any]):
    media = body.get("media")
    if not isinstance(media, list) or not media:
        raise ValueError("Flow video RPC: status body has no media[]")
    prior: List[Dict[str, Any]] = []
    operation_ids: List[str] = []
    for index, item in enumerate(media):
        if not isinstance(item, dict):
            raise ValueError(f"Flow video RPC: media[{index}] is not an object")
        operation_id = _plain_name(item.get("name"))
        if not operation_id:
            raise ValueError(f"Flow video RPC: media[{index}] has no name")
        if not _is_uuid(operation_id):
            raise NotImplementedError(
                f"Flow video RPC: status name {operation_id!r} is not a Flow operation id (uuid)"
            )
        if operation_id.lower() in {op.lower() for op in operation_ids}:
            continue
        operation_ids.append(operation_id)
        info = _recall_operation(operation_id)
        project_id = str(item.get("projectId") or info.get("projectId") or "").strip()
        operation: Dict[str, Any] = {
            "operation": {"name": operation_id},
            "name": operation_id,
            "mediaName": operation_id,
            "projectId": project_id,
            "status": STATUS_ACTIVE,
        }
        if info.get("flowMediaName"):
            operation["flowMediaName"] = info["flowMediaName"]
        if info.get("aspectRatio"):
            operation["aspectRatio"] = info["aspectRatio"]
        if info.get("modelKey"):
            operation["modelKey"] = info["modelKey"]
        prior.append(operation)
    project_id = str(prior[0].get("projectId") or "").strip() or None
    argument = [None, None, [[operation_id] for operation_id in operation_ids]]

    def normalize(payload: Any) -> Dict[str, Any]:
        return _normalize_status(payload, prior)

    return STATUS_RPC, argument, project_id, normalize


def _error_body(error_code: str) -> Dict[str, str]:
    reason = _ERROR_TEXT.get(error_code)
    message = f"Google refused the video ({error_code})" + (f": {reason}" if reason else "")
    return {"code": error_code, "message": message}


def _normalize_status(payload: Any, prior: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Upstream _normalize_frontend_video_status, shaped for our handler (see module docstring)."""
    by_id = {str(op["name"]).lower(): op for op in prior}
    normalized: List[Dict[str, Any]] = []
    # Only records for the operations we asked about: never report another job's video as ours.
    for record in _extract_video_records(payload):
        operation_id = str(record[0])
        previous = by_id.get(operation_id.lower())
        if previous is None:
            continue
        operation_id = str(previous["name"])
        video_url = _find_flow_content_url(record, "video")
        error_code = _extract_error_code(record)
        status = STATUS_SUCCESSFUL if video_url else (STATUS_FAILED if error_code else STATUS_ACTIVE)
        flow_media_name = (
            str(record[2]) if _is_uuid(record[2]) else str(previous.get("flowMediaName") or operation_id)
        )
        video_info: Dict[str, Any] = {
            "mediaName": operation_id,
            "mediaGenerationId": operation_id,
            "flowMediaName": flow_media_name,
            "aspectRatio": previous.get("aspectRatio"),
            "model": previous.get("modelKey"),
        }
        if video_url:
            video_info["fifeUrl"] = video_url
        operation_body: Dict[str, Any] = {"name": operation_id, "metadata": {"video": video_info}}
        if error_code:
            operation_body["error"] = _error_body(error_code)
        normalized.append(
            {
                "operation": operation_body,
                "name": operation_id,
                "mediaName": operation_id,
                "flowMediaName": flow_media_name,
                "projectId": str(record[1]) if record[1] is not None else previous.get("projectId", ""),
                "status": status,
                "progress": 100 if video_url else 45,
                "frontendRpc": STATUS_RPC,
            }
        )
    if not normalized:
        error_code = _extract_error_code(payload)
        for previous in prior:
            operation = {
                **previous,
                "operation": dict(previous["operation"]),
                "progress": 45,
                "frontendRpc": STATUS_RPC,
            }
            if error_code:
                operation["operation"]["error"] = _error_body(error_code)
                operation["status"] = STATUS_FAILED
            normalized.append(operation)
    return {"operations": normalized, "frontendRpc": STATUS_RPC}


def video_status_followups(status_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """as29s media lookups for operations jwpduf did not report as finished.

    Upstream check_video_status does this after every jwpduf poll: jwpduf does not always carry the signed
    video URL, and without the lookup a finished video stays ACTIVE until our 4-minute stuck cutoff.
    Run each item with the same cookie transport:
        payload = rpc(item["rpc_id"], item["argument"], source_path=item["source_path"])
        item["apply"](payload)   # mutates status_result in place; True when it found the video URL
    A failed lookup is not fatal (upstream logs a warning and moves on).
    """
    followups: List[Dict[str, Any]] = []
    for operation in (status_result or {}).get("operations") or []:
        if not isinstance(operation, dict) or "SUCCESSFUL" in str(operation.get("status") or ""):
            continue
        body = operation.get("operation") if isinstance(operation.get("operation"), dict) else {}
        operation_id = _plain_name(operation.get("name") or body.get("name"))
        if not operation_id:
            continue
        project_id = str(operation.get("projectId") or "").strip()
        page_media = str(operation.get("flowMediaName") or operation_id)
        source_path = f"/project/{project_id}/edit/{page_media}" if project_id else "/projects"

        def apply(payload: Any, operation: Dict[str, Any] = operation) -> bool:
            video_url = _find_flow_content_url(payload, "video")
            if not video_url:
                return False
            operation_body = dict(operation.get("operation") or {})
            metadata = dict(operation_body.get("metadata") or {})
            video_info = dict(metadata.get("video") or {}) if isinstance(metadata.get("video"), dict) else {}
            video_info["fifeUrl"] = video_url
            video_info.setdefault("mediaName", operation.get("mediaName") or operation.get("name"))
            video_info.setdefault("mediaGenerationId", operation.get("name"))
            metadata["video"] = video_info
            operation_body["metadata"] = metadata
            operation["operation"] = operation_body
            operation["status"] = STATUS_SUCCESSFUL
            operation["progress"] = 100
            return True

        followups.append(
            {
                "rpc_id": MEDIA_LOOKUP_RPC,
                "argument": [operation_id],
                "project_id": project_id or None,
                "source_path": source_path,
                "apply": apply,
            }
        )
    return followups


# --------------------------------------------------------------------------- entry point


def translate_video_request(
    path: str, body: dict
) -> Optional[Tuple[str, list, Optional[str], Callable[[Any], dict]]]:
    """path = URL path after the host, e.g. '/v1/video:batchAsyncGenerateVideoText'.
    body = the exact REST JSON dict our flow_client builds (includes clientContext.recaptchaContext.token,
    clientContext.projectId, requests[...]).
    Returns (rpc_id, argument, project_id, normalize) or None if the path is not a video path.
    normalize(payload) turns the decoded batchexecute payload (json.loads of the wrb.fr inner string) into
    the REST-shaped dict our existing code expects from that REST endpoint."""
    endpoint = _endpoint(path)
    if endpoint is None:
        return None
    if endpoint in _CONCAT_ENDPOINTS:
        raise NotImplementedError(
            f"Flow video RPC: {endpoint} has no current Flow frontend RPC (upstream run_concatenation is "
            "disabled too); the extend clip is returned unstitched"
        )
    if not isinstance(body, dict):
        raise ValueError("Flow video RPC: REST body is not a JSON object")
    if endpoint == _STATUS_ENDPOINT:
        return _translate_status(body)
    mode = _GENERATION_MODES.get(endpoint)
    if mode is None:
        raise NotImplementedError(f"Flow video RPC: video:{endpoint} has no RPC mapping")
    return _translate_generation(body, mode)
