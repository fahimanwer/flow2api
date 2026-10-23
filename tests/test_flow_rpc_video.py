"""REST video bodies -> flow.google.com batchexecute (src/services/flow_rpc_video.py), 2026-10-23.

Ground truth: upstream TheSmallHanCat/flow2api commit 551cca5. The builder tests compare our argument with
upstream's own `_build_frontend_video_generation_argument`, loaded from git (skipped when that commit is
not in the local clone), and with literal layouts ported from upstream's tests.
"""

import ast
import json
import random
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services import flow_rpc_video as frv
from src.services.flow_rpc_video import translate_video_request, video_status_followups

REPO = Path(__file__).resolve().parents[1]
UPSTREAM_COMMIT = "551cca5"

PROJECT = "22222222-2222-2222-2222-222222222222"
OPERATION = "11111111-1111-1111-1111-111111111111"
MEDIA = "33333333-3333-3333-3333-333333333333"
OTHER_PROJECT = "44444444-4444-4444-4444-444444444444"
TOKEN = "recaptcha-token"
SEED = 4242
VIDEO_URL = f"https://flow-content.google/video/{OPERATION}?token=abc"

TEXT = "/v1/video:batchAsyncGenerateVideoText"
REFS = "/v1/video:batchAsyncGenerateVideoReferenceImages"
START_END = "/v1/video:batchAsyncGenerateVideoStartAndEndImage"
START = "/v1/video:batchAsyncGenerateVideoStartImage"
EXTEND = "/v1/video:batchAsyncGenerateVideoExtendVideo"
UPSAMPLE = "/v1/video:batchAsyncGenerateVideoUpsampleVideo"
STATUS = "/v1/video:batchCheckAsyncVideoGenerationStatus"


def U(n: int) -> str:
    return str(uuid.UUID(int=n)).upper()


def _uuid_seq():
    return iter(uuid.UUID(int=n) for n in range(1, 100))


@pytest.fixture(autouse=True)
def _clean_operation_memory():
    frv._OPERATION_INFO.clear()
    yield
    frv._OPERATION_INFO.clear()


# --------------------------------------------------------------------------- REST bodies as flow_client builds them


def _client_context() -> Dict[str, Any]:
    return {
        "recaptchaContext": {"token": TOKEN, "applicationType": "RECAPTCHA_APPLICATION_TYPE_WEB"},
        "sessionId": ";1761200000000",
        "projectId": PROJECT,
        "tool": "PINHOLE",
        "userPaygateTier": "PAYGATE_TIER_TWO",
    }


def _body(request: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "mediaGenerationContext": {"batchId": str(uuid.uuid4()), "audioFailurePreference": "BLOCK_SILENCED_VIDEOS"},
        "clientContext": _client_context(),
        "requests": [request],
        "useV2ModelConfig": True,
    }


def _text_input(prompt: str) -> Dict[str, Any]:
    return {"structuredPrompt": {"parts": [{"text": prompt}]}}


def text_body(prompt="猫猫", model="veo_3_1_t2v_lite", aspect="VIDEO_ASPECT_RATIO_LANDSCAPE", output_resolution=None):
    request = {"aspectRatio": aspect, "seed": SEED, "textInput": _text_input(prompt), "videoModelKey": model, "metadata": {}}
    if output_resolution:
        request["outputSpec"] = {"resolution": output_resolution}
    return _body(request)


def refs_body(media_ids=("reference-media",), prompt="参考图视频", model="abra_r2v_10s",
              aspect="VIDEO_ASPECT_RATIO_LANDSCAPE", **extra):
    return _body({
        "aspectRatio": aspect, "seed": SEED, "textInput": _text_input(prompt), "videoModelKey": model,
        "referenceImages": [{"imageUsageType": "IMAGE_USAGE_TYPE_ASSET", "mediaId": m} for m in media_ids],
        "metadata": {}, **extra,
    })


def start_end_body(prompt="变身猫猫", model="veo_3_1_interpolation_lite", aspect="VIDEO_ASPECT_RATIO_PORTRAIT"):
    return _body({
        "aspectRatio": aspect, "seed": SEED, "textInput": _text_input(prompt), "videoModelKey": model,
        "startImage": {"mediaId": "start-media"}, "endImage": {"mediaId": "end-media"}, "metadata": {},
    })


def start_body(prompt="首帧", model="abra_i2v_8s", aspect="VIDEO_ASPECT_RATIO_PORTRAIT"):
    return _body({
        "aspectRatio": aspect, "seed": SEED, "textInput": _text_input(prompt), "videoModelKey": model,
        "startImage": {"mediaId": "start-media"}, "metadata": {},
    })


def extend_body(prompt="继续", model="veo_3_1_extend", aspect="VIDEO_ASPECT_RATIO_LANDSCAPE"):
    body = _body({
        "aspectRatio": aspect, "seed": SEED, "textInput": _text_input(prompt), "videoInput": {"mediaId": MEDIA},
        "videoModelKey": model, "metadata": {"workflowId": str(uuid.uuid4())},
    })
    return body


def upsample_body(resolution="VIDEO_RESOLUTION_4K", model="veo_3_1_upsampler_4k", aspect="VIDEO_ASPECT_RATIO_PORTRAIT"):
    return _body({
        "aspectRatio": aspect, "resolution": resolution, "seed": SEED, "videoInput": {"mediaId": MEDIA},
        "videoModelKey": model, "metadata": {"sceneId": str(uuid.uuid4())},
    })


def status_body(names=(OPERATION,), project=PROJECT):
    return {"media": [{"name": n, "projectId": project} for n in names]}


def ours(path, body):
    with patch("uuid.uuid4", side_effect=_uuid_seq()):
        return translate_video_request(path, body)


# --------------------------------------------------------------------------- upstream builder oracle (from git)

_UPSTREAM_METHODS = {
    "_frontend_project_context", "_map_frontend_video_aspect_ratio", "_frontend_prompt",
    "_frontend_generation_metadata", "_frontend_media_reference", "_frontend_video_resolution",
    "_build_frontend_video_generation_argument", "_find_flow_content_url", "_is_uuid",
    "_extract_frontend_rpc_error_code", "_extract_frontend_video_records",
    "_normalize_frontend_video_submission", "_normalize_frontend_video_status",
}


def _load_upstream():
    try:
        source = subprocess.run(
            ["git", "show", f"{UPSTREAM_COMMIT}:src/services/flow_frontend.py"],
            cwd=REPO, capture_output=True, text=True, check=True, timeout=30,
        ).stdout
    except Exception:
        return None
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "FlowFrontendMixin")
    cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in _UPSTREAM_METHODS]
    cls.bases = []
    namespace = {"json": json, "random": random, "re": re, "uuid": uuid,
                 "Any": Any, "Dict": Dict, "List": List, "Optional": Optional}
    exec(compile(ast.Module(body=[cls], type_ignores=[]), "upstream_flow_frontend", "exec"), namespace)
    return namespace["FlowFrontendMixin"]()


UPSTREAM = _load_upstream()
needs_upstream = pytest.mark.skipif(UPSTREAM is None, reason=f"upstream commit {UPSTREAM_COMMIT} not in this clone")


def upstream_argument(**kwargs):
    """What upstream's _current_generate_video sends: session_id = fresh uuid, then the builder."""
    with patch("uuid.uuid4", side_effect=_uuid_seq()), patch("random.randint", return_value=SEED):
        session_id = str(uuid.uuid4()).upper()
        return UPSTREAM._build_frontend_video_generation_argument(
            project_id=PROJECT, recaptcha_token=TOKEN, session_id=session_id, **kwargs
        )


CONTEXT = [None, 22, None, None, None, PROJECT, None, None, None, None, [TOKEN, 1]]
META = [None, None, None, None, U(1), U(2)]
TAIL = [U(3), 2]


# --------------------------------------------------------------------------- generation: argument layout


def test_text_matches_upstream_layout():
    rpc_id, argument, project_id, _ = ours(TEXT, text_body())
    assert (rpc_id, project_id) == ("YhhmEf", PROJECT)
    request = argument[0][0]
    # upstream tests/test_veo_lite_support.py::test_generate_video_text_uses_v2_payload_for_lite
    assert request[0] == [None, None, [[["猫猫"]]]]
    assert request[1] == "veo_3_1_t2v_lite"
    assert request[2] == 2
    assert argument[2][0]
    assert argument == [[[[None, None, [[["猫猫"]]]], "veo_3_1_t2v_lite", 2, None, META, None, None, None]], CONTEXT, TAIL]


@needs_upstream
@pytest.mark.parametrize(
    "path,body,kwargs",
    [
        (TEXT, text_body(), dict(mode="text", prompt="猫猫", model_key="veo_3_1_t2v_lite",
                                 aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE")),
        (TEXT, text_body(model="abra_t2v_8s", aspect="VIDEO_ASPECT_RATIO_PORTRAIT", output_resolution="VIDEO_RESOLUTION_720P"),
         dict(mode="text", prompt="猫猫", model_key="abra_t2v_8s", aspect_ratio="VIDEO_ASPECT_RATIO_PORTRAIT",
              resolution="VIDEO_RESOLUTION_720P")),
        (REFS, refs_body(media_ids=("ref-a", "ref-b")),
         dict(mode="references", prompt="参考图视频", model_key="abra_r2v_10s",
              aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE", reference_media_ids=["ref-a", "ref-b"])),
        (START_END, start_end_body(),
         dict(mode="start_end", prompt="变身猫猫", model_key="veo_3_1_interpolation_lite",
              aspect_ratio="VIDEO_ASPECT_RATIO_PORTRAIT", start_media_id="start-media", end_media_id="end-media")),
        (START, start_body(),
         dict(mode="start", prompt="首帧", model_key="abra_i2v_8s", aspect_ratio="VIDEO_ASPECT_RATIO_PORTRAIT",
              start_media_id="start-media")),
        (EXTEND, extend_body(),
         dict(mode="extend", prompt="继续", model_key="veo_3_1_extend", aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
              video_media_id=MEDIA)),
        (UPSAMPLE, upsample_body(),
         dict(mode="upsample", prompt="", model_key="veo_3_1_upsampler_4k", aspect_ratio="VIDEO_ASPECT_RATIO_PORTRAIT",
              video_media_id=MEDIA, resolution="VIDEO_RESOLUTION_4K")),
        (UPSAMPLE, upsample_body(resolution="VIDEO_RESOLUTION_1080P", model="veo_3_1_upsampler_1080p"),
         dict(mode="upsample", prompt="", model_key="veo_3_1_upsampler_1080p",
              aspect_ratio="VIDEO_ASPECT_RATIO_PORTRAIT", video_media_id=MEDIA, resolution="VIDEO_RESOLUTION_1080P")),
    ],
    ids=["text", "text-720p", "references", "start_end", "start", "extend", "upsample-4k", "upsample-1080p"],
)
def test_argument_equals_upstream_builder(path, body, kwargs):
    rpc_id, argument, project_id, _ = ours(path, body)
    assert (rpc_id, argument) == upstream_argument(**kwargs)
    assert project_id == PROJECT


def test_references_layout():
    rpc_id, argument, _, _ = ours(REFS, refs_body())
    request = argument[0][0]
    # upstream test_reference_video_uses_current_prompt_and_media_envelopes
    assert rpc_id == "MZZa6b"
    assert request[0] == [None, None, [[["参考图视频"]]]]
    assert request[1] == [[None, "reference-media"]]
    assert request[2:6] == ["abra_r2v_10s", 2, None, META]
    assert len(request) == 12 and request[11] is None


def test_start_end_layout():
    rpc_id, argument, _, _ = ours(START_END, start_end_body())
    request = argument[0][0]
    # upstream test_generate_video_start_end_uses_v2_payload_for_interpolation_lite
    assert rpc_id == "nprQif"
    assert request[0] == [None, None, [[["变身猫猫"]]]]
    assert request[1] == "veo_3_1_interpolation_lite"
    assert request[2] == 1
    assert request[4][1] == "start-media"
    assert request[5][1] == "end-media"


def test_start_extend_upsample_layouts():
    rpc_id, argument, _, _ = ours(START, start_body())
    assert rpc_id == "eb1hJf"
    assert argument[0][0] == [[None, None, [[["首帧"]]]], "abra_i2v_8s", 1, None, [None, "start-media"], META,
                              None, None, None, None]

    rpc_id, argument, _, _ = ours(EXTEND, extend_body())
    assert rpc_id == "fZytfe"
    assert argument[0][0] == [[None, MEDIA], [None, None, [[["继续"]]]], "veo_3_1_extend", 2, None, META, None, None]

    rpc_id, argument, _, _ = ours(UPSAMPLE, upsample_body())
    request = argument[0][0]
    assert rpc_id == "p0UkFb"
    assert request[:8] == [[None, MEDIA], None, 1, SEED, META, None, 4, None]
    assert request[8:31] == [None] * 23
    assert request[31] == "veo_3_1_upsampler_4k" and len(request) == 32
    assert argument[1] == CONTEXT


def test_omni_720p_output_spec_fills_text_resolution_slot():
    _, argument, _, _ = ours(TEXT, text_body(model="abra_t2v_8s", output_resolution="VIDEO_RESOLUTION_720P"))
    assert argument[0][0][7] == 2


def test_prompt_parts_are_joined():
    body = text_body()
    body["requests"][0]["textInput"] = {"structuredPrompt": {"parts": [{"text": "a cat "}, {"text": "on a roof"}]}}
    _, argument, _, _ = ours(TEXT, body)
    assert argument[0][0][0] == [None, None, [[["a cat on a roof"]]]]


# --------------------------------------------------------------------------- generation: loud refusals


def test_characters_are_refused_loudly():
    with pytest.raises(NotImplementedError, match="characters"):
        ours(REFS, refs_body(referenceEntities=[{"entityId": "e1"}]))
    body = refs_body()
    body["requests"][0]["textInput"] = {"structuredPrompt": {"parts": [
        {"text": "show "}, {"reference": {"entity": {"entityId": "e1", "handle": "Nia"}}}]}}
    with pytest.raises(NotImplementedError, match="characters"):
        ours(REFS, body)


@pytest.mark.parametrize(
    "path,mutate,error",
    [
        (TEXT, lambda b: b["requests"][0].update(aspectRatio="VIDEO_ASPECT_RATIO_WIDE"), NotImplementedError),
        (TEXT, lambda b: b["requests"][0].update(aspectRatio=None), NotImplementedError),
        (TEXT, lambda b: b["requests"].append(dict(b["requests"][0])), NotImplementedError),
        (TEXT, lambda b: b["requests"][0].update(outputSpec={"resolution": "VIDEO_RESOLUTION_8K"}), NotImplementedError),
        (TEXT, lambda b: b["requests"][0].update(outputSpec={"resolution": "VIDEO_RESOLUTION_720P", "fps": 60}),
         NotImplementedError),
        (START_END, lambda b: b["requests"][0].update(outputSpec={"resolution": "VIDEO_RESOLUTION_720P"}),
         NotImplementedError),
        (REFS, lambda b: b["requests"][0].update(referenceImages=[]), NotImplementedError),
        (REFS, lambda b: b["requests"][0]["referenceImages"][0].update(imageUsageType="IMAGE_USAGE_TYPE_STYLE"),
         NotImplementedError),
        (UPSAMPLE, lambda b: b["requests"][0].pop("resolution"), NotImplementedError),
        (TEXT, lambda b: b["clientContext"]["recaptchaContext"].pop("token"), ValueError),
        (TEXT, lambda b: b["clientContext"].pop("projectId"), ValueError),
        (TEXT, lambda b: b["requests"][0].pop("videoModelKey"), ValueError),
        (START, lambda b: b["requests"][0].pop("startImage"), ValueError),
        (EXTEND, lambda b: b["requests"][0].update(videoInput={}), ValueError),
    ],
)
def test_unmappable_bodies_raise(path, mutate, error):
    body = {TEXT: text_body, REFS: refs_body, START_END: start_end_body, START: start_body,
            EXTEND: extend_body, UPSAMPLE: upsample_body}[path]()
    mutate(body)
    with pytest.raises(error):
        ours(path, body)


def test_path_routing():
    assert translate_video_request("/v1/credits", {}) is None
    assert translate_video_request("/v1/projects/p/flowMedia:batchGenerateImages", {}) is None
    with pytest.raises(NotImplementedError, match="runVideoFxConcatenation"):
        translate_video_request("/v1:runVideoFxConcatenation", {"inputVideos": []})
    with pytest.raises(NotImplementedError):
        translate_video_request("/v1:runVideoFxCheckConcatenationStatus", {})
    with pytest.raises(NotImplementedError):
        translate_video_request("/v1/video:batchAsyncGenerateVideoSomethingNew", text_body())
    rpc_id, *_ = ours(TEXT + "?alt=json", text_body())
    assert rpc_id == "YhhmEf"


# --------------------------------------------------------------------------- submit normalize


def test_submit_normalize_matches_upstream_and_omits_media_name():
    _, _, _, normalize = ours(TEXT, text_body())
    result = normalize([[OPERATION, PROJECT, MEDIA]])
    operation = result["operations"][0]
    # upstream test_generate_video_text_normalizes_media_only_create_response
    assert operation["operation"]["name"] == OPERATION
    assert operation["projectId"] == PROJECT
    assert operation["status"] == "MEDIA_GENERATION_STATUS_ACTIVE"
    assert "mediaName" not in operation and operation["flowMediaName"] == MEDIA


@needs_upstream
def test_submit_normalize_agrees_with_upstream_on_ids():
    _, _, _, normalize = ours(TEXT, text_body())
    payload = [[OPERATION, PROJECT, MEDIA], [MEDIA, OTHER_PROJECT, OPERATION]]
    mine = normalize(payload)["operations"]
    theirs = UPSTREAM._normalize_frontend_video_submission(
        payload, project_id=PROJECT, aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE", model_key="veo_3_1_t2v_lite"
    )["operations"]
    assert [(o["name"], o["projectId"], o["status"]) for o in mine] == [
        (o["name"], o["projectId"], o["status"]) for o in theirs
    ]
    assert [o["flowMediaName"] for o in mine] == [o["mediaName"] for o in theirs]


def test_submit_normalize_failures_are_loud():
    _, _, _, normalize = ours(TEXT, text_body())
    with pytest.raises(RuntimeError, match="no generation operations"):
        normalize([])
    with pytest.raises(RuntimeError, match="PUBLIC_ERROR_UNSAFE_GENERATION"):
        normalize([None, [3, "PUBLIC_ERROR_UNSAFE_GENERATION"]])
    with pytest.raises(NotImplementedError, match="without an operation id"):
        normalize([["not-a-uuid", f"https://flow-content.google/video/{OPERATION}"]])
    with pytest.raises(RuntimeError):  # a record for another project is not ours
        normalize([[OPERATION, OTHER_PROJECT, MEDIA]])


# --------------------------------------------------------------------------- status


def test_status_translation():
    rpc_id, argument, project_id, _ = ours(STATUS, status_body(names=(OPERATION, OPERATION.upper(), MEDIA)))
    # upstream test_check_video_status_uses_media_payload_and_normalizes_response
    assert rpc_id == "jwpduf"
    assert argument == [None, None, [[OPERATION], [MEDIA]]]
    assert project_id == PROJECT


def test_status_success_fixture():
    _, _, _, normalize = ours(STATUS, status_body())
    url = f"https://flow-content.google/video/{MEDIA}?token=abc"
    operation = normalize([OPERATION, PROJECT, MEDIA, url])["operations"][0]
    assert operation["operation"]["name"] == OPERATION
    assert operation["status"] == "MEDIA_GENERATION_STATUS_SUCCESSFUL"
    video = operation["operation"]["metadata"]["video"]
    assert video["fifeUrl"] == url
    assert video["mediaGenerationId"] == OPERATION
    assert operation["mediaName"] == OPERATION and video["mediaName"] == OPERATION
    assert operation["flowMediaName"] == MEDIA


@needs_upstream
def test_status_agrees_with_upstream_on_status_and_url():
    _, _, _, normalize = ours(STATUS, status_body())
    prior = [{"operation": {"name": OPERATION}, "name": OPERATION, "mediaName": MEDIA, "projectId": PROJECT}]
    for payload in (
        [OPERATION, PROJECT, MEDIA, VIDEO_URL],
        [OPERATION, PROJECT, MEDIA, None, [4, [3, "PUBLIC_ERROR_PROMINENT_PEOPLE_FILTER_FAILED"]]],
        [OPERATION, PROJECT, MEDIA],
    ):
        mine = normalize(payload)["operations"][0]
        theirs = UPSTREAM._normalize_frontend_video_status(payload, prior)["operations"][0]
        assert mine["status"] == theirs["status"]
        assert mine["operation"]["name"] == theirs["operation"]["name"]
        assert mine["operation"]["metadata"]["video"].get("fifeUrl") == theirs["operation"]["metadata"]["video"].get("fifeUrl")
        assert mine["operation"].get("error", {}).get("code") == theirs["operation"].get("error", {}).get("code")


def test_status_terminal_error_fixture():
    _, _, _, normalize = ours(STATUS, status_body())
    payload = [OPERATION, PROJECT, MEDIA, None, [4, [3, "PUBLIC_ERROR_PROMINENT_PEOPLE_FILTER_FAILED"]]]
    operation = normalize(payload)["operations"][0]
    # upstream test_check_video_status_surfaces_terminal_frontend_error (message is English here)
    assert operation["status"] == "MEDIA_GENERATION_STATUS_FAILED"
    assert operation["operation"]["error"]["code"] == "PUBLIC_ERROR_PROMINENT_PEOPLE_FILTER_FAILED"
    assert "real-person filter" in operation["operation"]["error"]["message"]


def test_status_without_records():
    _, _, _, normalize = ours(STATUS, status_body())
    pending = normalize([])["operations"][0]
    assert pending["status"] == "MEDIA_GENERATION_STATUS_ACTIVE"  # never None: handler calls status.startswith
    assert pending["operation"]["name"] == OPERATION

    failed = normalize([None, [3, "PUBLIC_ERROR_UNSAFE_GENERATION"]])["operations"][0]
    assert failed["status"] == "MEDIA_GENERATION_STATUS_FAILED"
    assert failed["operation"]["error"]["code"] == "PUBLIC_ERROR_UNSAFE_GENERATION"
    # token_manager's prompt-rejection marker still matches, so the account takes no strike
    assert "unsafe_generation" in failed["operation"]["error"]["message"].lower()


def test_status_ignores_records_for_other_operations():
    _, _, _, normalize = ours(STATUS, status_body())
    other = "55555555-5555-5555-5555-555555555555"
    operation = normalize([[other, PROJECT, MEDIA, VIDEO_URL]])["operations"][0]
    assert operation["name"] == OPERATION
    assert operation["status"] == "MEDIA_GENERATION_STATUS_ACTIVE"


def test_status_rejects_non_flow_names():
    with pytest.raises(NotImplementedError):
        ours(STATUS, status_body(names=("CAUSJDlkZjQ0",)))
    with pytest.raises(ValueError):
        ours(STATUS, {"media": []})


# --------------------------------------------------------------------------- round trip through our own code


def _flow_client():
    from src.services.flow_client import FlowClient
    return FlowClient(proxy_manager=None)


def test_round_trip_submit_to_status_uses_operation_id_and_keeps_aspect_ratio():
    client = _flow_client()
    _, _, _, submit_normalize = ours(TEXT, text_body(model="veo_3_1_t2v_fast_portrait", aspect="VIDEO_ASPECT_RATIO_PORTRAIT"))
    submitted = client._normalize_video_generation_response(
        submit_normalize([[OPERATION, PROJECT, MEDIA]]), fallback_project_id=PROJECT
    )
    operations = submitted["operations"]
    assert operations[0]["operation"]["name"] == OPERATION  # handler's task_id

    # exactly what FlowClient.check_video_status sends
    body = {"media": client._operations_to_media_refs(operations)}
    assert body == {"media": [{"name": OPERATION, "projectId": PROJECT}]}
    rpc_id, argument, project_id, status_normalize = ours(STATUS, body)
    assert (rpc_id, argument, project_id) == ("jwpduf", [None, None, [[OPERATION]]], PROJECT)

    checked = client._normalize_video_generation_response(status_normalize([OPERATION, PROJECT, MEDIA, VIDEO_URL]))
    video = checked["operations"][0]["operation"]["metadata"]["video"]
    assert checked["operations"][0]["status"] == "MEDIA_GENERATION_STATUS_SUCCESSFUL"
    assert video["aspectRatio"] == "VIDEO_ASPECT_RATIO_PORTRAIT"  # feeds upsample_video(aspect_ratio=...)
    assert video["model"] == "veo_3_1_t2v_fast_portrait"


@pytest.mark.asyncio
async def test_handler_resolves_the_status_result_by_operation_id():
    from src.services.generation_handler import GenerationHandler

    _, _, _, normalize = ours(STATUS, status_body())
    operation = normalize([OPERATION, PROJECT, MEDIA, VIDEO_URL])["operations"][0]
    handler = GenerationHandler.__new__(GenerationHandler)
    handler.flow_client = MagicMock()
    handler.flow_client.get_media_url_redirect = AsyncMock(return_value=VIDEO_URL)

    token = MagicMock()
    token.at = "FLOWCK.abc.def"
    resolved = await handler._resolve_video_asset(token, operation)

    # Cookie session: the signed link from the status RPC is used as is; the retired Labs redirect is not called.
    handler.flow_client.get_media_url_redirect.assert_not_awaited()
    assert resolved["video_url"] == VIDEO_URL
    assert resolved["video_media_id"] == OPERATION


@pytest.mark.asyncio
async def test_handler_without_a_link_keeps_the_labs_redirect_for_legacy_tokens():
    from src.services.generation_handler import GenerationHandler

    operation = {"name": OPERATION, "operation": {"name": OPERATION, "metadata": {"video": {}}}}
    handler = GenerationHandler.__new__(GenerationHandler)
    handler.flow_client = MagicMock()
    handler.flow_client.get_media_url_redirect = AsyncMock(return_value=VIDEO_URL)
    token = MagicMock()
    token.at = "ya29.legacy"
    token.st = "st"

    resolved = await handler._resolve_video_asset(token, operation)

    # The lookup key our handler sends is the operation id (as29s answers code=[5] for the media name).
    assert handler.flow_client.get_media_url_redirect.await_args.args[1] == OPERATION
    assert resolved["video_url"] == VIDEO_URL


# --------------------------------------------------------------------------- as29s follow-ups


def test_followups_for_unfinished_operations():
    ours(TEXT, text_body())[3]([[OPERATION, PROJECT, MEDIA]])  # submit remembers the Flow media name
    _, _, _, normalize = ours(STATUS, status_body())
    result = normalize([OPERATION, PROJECT, MEDIA])
    followups = video_status_followups(result)
    assert len(followups) == 1
    item = followups[0]
    # upstream get_media: argument [operation_id], page /project/<pid>/edit/<media name>
    assert item["rpc_id"] == "as29s"
    assert item["argument"] == [OPERATION]
    assert item["project_id"] == PROJECT
    assert item["source_path"] == f"/project/{PROJECT}/edit/{MEDIA}"

    assert item["apply"]([OPERATION, PROJECT, None]) is False
    assert result["operations"][0]["status"] == "MEDIA_GENERATION_STATUS_ACTIVE"
    assert item["apply"]([OPERATION, PROJECT, [VIDEO_URL]]) is True
    operation = result["operations"][0]
    assert operation["status"] == "MEDIA_GENERATION_STATUS_SUCCESSFUL"
    assert operation["operation"]["metadata"]["video"]["fifeUrl"] == VIDEO_URL


def test_no_followups_when_finished():
    _, _, _, normalize = ours(STATUS, status_body())
    assert video_status_followups(normalize([OPERATION, PROJECT, MEDIA, VIDEO_URL])) == []
