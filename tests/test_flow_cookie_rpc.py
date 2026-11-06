"""Cookie-session transport (2026-10-23): jar placement, batchexecute parsing, REST->RPC translation."""
import json

import pytest

from src.services import flow_cookie_session as ck
from src.services import flow_rpc


def test_jar_places_accounts_cookies_on_accounts_host_and_rest_on_google_com():
    raw = json.dumps([
        {"name": "SID", "value": "a"}, {"name": "__Secure-1PSID", "value": "b"},
        {"name": "LSID", "value": "c"}, {"name": "__Host-GAPS", "value": "d"},
        {"name": "ACCOUNT_CHOOSER", "value": "e"},
    ])
    placed = {(c.name, c.domain) for c in ck.build_cookie_jar(raw)}
    assert ("SID", ".google.com") in placed
    assert ("__Secure-1PSID", ".google.com") in placed
    assert ("LSID", "accounts.google.com") in placed
    assert ("__Host-GAPS", "accounts.google.com") in placed
    assert ("ACCOUNT_CHOOSER", "accounts.google.com") in placed


def test_jar_keeps_explicit_domains_and_drops_non_google():
    raw = json.dumps([
        {"name": "OSID", "value": "x", "domain": "flow.google.com"},
        {"name": "SID", "value": "y", "domain": ".google.com"},
        {"name": "evil", "value": "z", "domain": ".example.com"},
    ])
    placed = {(c.name, c.domain) for c in ck.build_cookie_jar(raw)}
    assert placed == {("OSID", "flow.google.com"), ("SID", ".google.com")}


def test_has_login_cookies():
    assert ck.has_login_cookies(json.dumps([{"name": "SID", "value": "1"}]))
    assert not ck.has_login_cookies(json.dumps([{"name": "NID", "value": "1"}]))
    assert not ck.has_login_cookies("")


def test_email_comes_only_from_oPEP7c():
    page = 'x "oPEP7c":"Someone@Gmail.com" y other@gmail.com'
    assert ck._find_email(page) == "someone@gmail.com"
    # Any other email on the page proves nothing (Codex 2026-10-23).
    assert ck._find_email("a@gmail.com b@gmail.com") == ""


def test_payload_and_errors():
    ok = ck.parse_batchexecute_frames(')]}\'\n\n150\n[["wrb.fr","nzlxg","[25050,2,3,3,null,25050,[1,2]]",null,null,null,"generic"]]\n')
    assert ck.extract_rpc_payload(ok, "nzlxg")[0] == 25050
    with pytest.raises(ck.FlowCookieAuthError):
        ck.extract_rpc_payload([[["er", None, None, None, None, 401]]], "nzlxg")
    refused = [[["wrb.fr", "ogiZ0b", None, None, None, [3, None, [["PUBLIC_ERROR_UNUSUAL_ACTIVITY"]]]]]]
    with pytest.raises(ck.FlowRpcError) as err:
        ck.extract_rpc_payload(refused, "ogiZ0b")
    assert err.value.code == "PUBLIC_ERROR_UNUSUAL_ACTIVITY" and err.value.status_code == 403


def test_credits_never_guess_tier():
    out = flow_rpc._normalize_credits([25050, 2, 3, 3, None, 25050, [1, 2]])
    assert out["credits"] == 25050 and "userPaygateTier" not in out


def _image_body(**request_overrides):
    request = {
        "clientContext": {"recaptchaContext": {"token": "TOK"}, "projectId": "p"},
        "seed": 42, "imageModelName": "BELUGA", "imageAspectRatio": "IMAGE_ASPECT_RATIO_PORTRAIT",
        "structuredPrompt": {"parts": [{"text": "a cat"}]},
        "imageInputs": [{"name": "11111111-1111-1111-1111-111111111111", "imageInputType": "IMAGE_INPUT_TYPE_REFERENCE"}],
    }
    request.update(request_overrides)
    return {"clientContext": {"recaptchaContext": {"token": "TOK"}, "projectId": "p", "sessionId": ";1"},
            "requests": [request]}


def test_image_translation_matches_upstream_layout():
    pid = "22222222-2222-2222-2222-222222222222"
    call = flow_rpc.translate_request(
        "POST", f"https://aisandbox-pa.googleapis.com/v1/projects/{pid}/flowMedia:batchGenerateImages", _image_body())
    assert call.rpc_id == "ogiZ0b" and call.source_path == f"/project/{pid}"
    arg = call.argument
    context = [None, 22, None, None, None, pid, None, None, None, None, ["TOK", 1]]
    assert arg[0] is None and arg[2] == 1 and arg[3] == context
    req = arg[1][0]
    assert req[2] == [["11111111-1111-1111-1111-111111111111", None, None, None, 1]]
    assert req[3] == 42 and req[4] == 2 and req[5] == "BELUGA" and req[7] == context
    assert req[8] == [[["a cat"]]]
    # Real Flow web app (HAR 2026-10-23): its own batch id, not the request's session id.
    assert len(arg[4]) == 1 and flow_rpc.UUID_RE.fullmatch(arg[4][0]) and arg[4][0] != req[12]


def test_image_translation_refuses_unknown_parts_and_models():
    url = "https://aisandbox-pa.googleapis.com/v1/projects/p/flowMedia:batchGenerateImages"
    with pytest.raises(flow_rpc.CookieModeUnsupported):
        flow_rpc.translate_request("POST", url, _image_body(structuredPrompt={"parts": [{"entity": "x"}]}))
    with pytest.raises(flow_rpc.CookieModeUnsupported):
        flow_rpc.translate_request("POST", url, _image_body(imageModelName="IMAGEN_3"))


CHAR = "cdc65f26-97bf-45a4-9802-56fdb72e6776"
PROJ = "ac31097d-1391-4fcd-a6ad-f6aea1d14043"


def test_image_with_a_character_matches_the_flow_web_app():
    # HAR of the real Flow app, 2026-11-04: the character goes in request slot 10 as [[id]], the prompt stays text.
    body = _image_body(referenceEntities=[{"entityId": CHAR}], imageInputs=[],
                       structuredPrompt={"parts": [{"reference": {"entity": {"entityId": CHAR, "handle": "Bikey"}}},
                                                   {"text": " parked next to a green door, photo"}]})
    call = flow_rpc.translate_request(
        "POST", f"https://aisandbox-pa.googleapis.com/v1/projects/{PROJ}/flowMedia:batchGenerateImages", body)
    req = call.argument[1][0]
    assert req[10] == [[CHAR]] and req[8] == [[["Bikey parked next to a green door, photo"]]] and req[2] is None


def test_character_create_and_photo_slot_match_the_flow_web_app():
    create = flow_rpc.translate_request("POST", "https://aisandbox-pa.googleapis.com/v1/flow/entities", {
        "entity": {"projectId": PROJ, "entityInfo": {"entityType": "CHARACTER", "displayName": "Untitled character",
                                                     "characterInfo": {}}}})
    assert create.rpc_id == "C4BZMd"
    assert create.argument == [[PROJ, None, None, [1, "Untitled character", []]]]
    out = create.normalize([[PROJ, CHAR, None, [1, "Untitled character", []], None, None, [1, 2], [1, 2]]])
    assert out["entity"]["entityId"] == CHAR
    media = "814e7827-f3dd-45e3-8122-5cae073375a1"
    slot = flow_rpc.translate_request("POST", "https://aisandbox-pa.googleapis.com/v1/flow:copyProjectMedia", {
        "mediaId": media, "destinationProjectId": PROJ,
        "destinationMediaContext": {"entityContext": {"entityId": CHAR, "characterSlot": {"imageReferenceIndex": 0}}}})
    assert slot.rpc_id == "Sc7aEb"
    assert slot.argument[:9] == [media, None, None, PROJ, None, None, None, [None, None, [CHAR, [0]]], None]
    with pytest.raises(RuntimeError):
        slot.normalize(None)


def test_image_response_normalizes_to_rest_shape():
    payload = [[["m1", None, ["https://flow-content.google/image/abc?sig=1"]]]]
    out = flow_rpc.normalize_image_response(payload)
    assert out["media"][0]["name"] == "m1"
    assert out["media"][0]["image"]["generatedImage"]["fifeUrl"].startswith("https://flow-content.google/image/")


def test_upload_and_upsample_translation():
    pid = "33333333-3333-3333-3333-333333333333"
    up = flow_rpc.translate_request("POST", "https://aisandbox-pa.googleapis.com/v1/flow/uploadImage", {
        "clientContext": {"projectId": pid, "recaptchaContext": {"token": "T"}},
        "imageBytes": "QUJD", "mimeType": "image/png", "fileName": "f.png"})
    assert up.rpc_id == "maseQ" and up.argument[1] == "QUJD" and up.argument[2] == "image/png"
    assert up.argument[0][10] == ["T", 1]
    media = "44444444-4444-4444-4444-444444444444"
    assert up.normalize([f"https://flow-content.google/image/{media}?x"])["media"]["name"] == media
    sp = flow_rpc.translate_request("POST", "https://aisandbox-pa.googleapis.com/v1/flow/upsampleImage", {
        "mediaId": media, "targetResolution": "UPSAMPLE_IMAGE_RESOLUTION_4K",
        "clientContext": {"projectId": pid, "recaptchaContext": {"token": "T"}}})
    assert sp.rpc_id == "SPrCad" and sp.argument[:2] == [media, 2]


def test_unmapped_calls_fail_loudly():
    with pytest.raises(flow_rpc.CookieModeUnsupported):
        flow_rpc.translate_request("POST", "https://aisandbox-pa.googleapis.com/v1/flow/entities", {})


def _session(email, fp="f", proxy="p"):
    return ck.FlowCookieSession(handle=ck.FlowCookieSessions.new_handle(email), email=email, proxy=proxy,
                                jar=ck.CookieJar(), source_fingerprint=fp)


def test_each_session_has_its_own_handle_and_drop_is_exact():
    reg = ck.FlowCookieSessions()
    old, new = _session("a@x.com"), _session("a@x.com", fp="g")
    assert old.handle != new.handle and ck.is_session_handle(old.handle)
    reg.put(old)
    reg.put(new)
    reg.alias("FLOWCK.legacy.1", old)
    reg.drop_session(old)  # a failure of the OLD session must not remove the newer one
    assert reg.get(old.handle) is None and reg.get("FLOWCK.legacy.1") is None
    assert reg.get(new.handle) is new
    assert reg.find("A@x.com", "g", "p") is new and reg.find("a@x.com", "f", "p") is None
    reg.drop_email("a@x.com")
    assert reg.get(new.handle) is None


def test_registry_cap_counts_aliases():
    reg = ck.FlowCookieSessions()
    for i in range(ck.MAX_SESSIONS + 50):
        session = _session(f"u{i}@x.com")
        reg.put(session)
        reg.alias(f"FLOWCK.alias.{i}", session)
    assert len(reg._by_handle) <= ck.MAX_SESSIONS + 1


def test_empty_credits_answer_is_an_error_not_zero():
    with pytest.raises(RuntimeError):
        flow_rpc._normalize_credits(None)


def test_signed_out_flow_page_is_a_login_verdict_but_a_changed_page_is_not():
    import asyncio
    from unittest.mock import patch

    class Resp:
        def __init__(self, text, status=200):
            self.text, self.status_code, self.headers = text, status, {}

    class Http:
        def __init__(self, resp):
            self.resp, self.cookies = resp, type("C", (), {"jar": ck.CookieJar()})()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, *a, **k):
            return self.resp

    def run(text, status=200):
        session = ck.FlowCookieSession(handle="h", email="", proxy="http://p", jar=ck.CookieJar(), source_fingerprint="f")
        with patch.object(ck, "AsyncSession", lambda **kw: Http(Resp(text, status))):
            asyncio.run(ck.load_bootstrap(session))
        return session

    app = 'boq_labs-ai-sandbox-frontend_20261021.10 "FdrFJe":"-123"'
    assert run(app + ' "oPEP7c":"a@x.com"').email == "a@x.com"
    with pytest.raises(ck.FlowCookieAuthError):
        run(app)  # signed-out Flow app (stale device-bound cookies)
    with pytest.raises(RuntimeError) as err:
        run("<html>new page</html>")
    assert not isinstance(err.value, ck.FlowCookieAuthError)
    with pytest.raises(RuntimeError) as err:
        run("busy", status=503)
    assert not isinstance(err.value, ck.FlowCookieAuthError)


def test_reuse_never_crosses_rows_with_the_same_email():
    reg = ck.FlowCookieSessions()
    bound = _session("a@x.com")
    bound.token_id = 17
    reg.put(bound)
    assert reg.find("a@x.com", "f", "p", token_id=17) is bound
    assert reg.find("a@x.com", "f", "p", token_id=18) is None


def test_reply_cookies_are_kept_but_never_overwrite_a_newer_value():
    def jar(**values):
        j = ck.CookieJar()
        for name, value in values.items():
            j.set_cookie(ck._cookie(name, value, "flow.google.com"))
        return j

    shared = jar(OSID="v1", NID="n1")
    before = ck._jar_values(shared)
    # This reply rotated OSID and NID; meanwhile another reply already rotated NID to n2.
    shared.set_cookie(ck._cookie("NID", "n2", "flow.google.com"))
    ck._merge_changed(shared, before, jar(OSID="v2", NID="n-old-reply"))
    values = {c.name: c.value for c in shared}
    assert values == {"OSID": "v2", "NID": "n2"}


def test_transient_flow_errors_carry_the_marker():
    err = ck.FlowTransientError("Flow bootstrap answered HTTP 503")
    assert ck.TRANSIENT_MARKER in str(err) and not isinstance(err, ck.FlowCookieAuthError)


def test_session_built_before_a_cookie_clear_never_serves():
    import time as _t
    reg = ck.FlowCookieSessions()
    in_flight = _session("a@x.com")          # an ON push started building this...
    _t.sleep(0.01)
    reg.drop_email("a@x.com")                 # ...then the OFF landed
    reg.put(in_flight)                        # the ON push finishes afterwards
    assert reg.get(in_flight.handle) is None
    reg.alias("FLOWCK.db.handle", in_flight)
    assert reg.get("FLOWCK.db.handle") is None
    _t.sleep(0.01)
    fresh = _session("a@x.com")               # a later ON works again
    reg.put(fresh)
    assert reg.get(fresh.handle) is fresh


def test_network_failures_become_transient_and_keep_their_text():
    import asyncio

    async def boom():
        raise OSError("curl: (56) Recv failure: Connection reset by peer")

    with pytest.raises(ck.FlowTransientError) as err:
        asyncio.run(ck._network(boom()))
    assert "curl: (56)" in str(err.value) and ck.TRANSIENT_MARKER in str(err.value)


def test_google_interstitial_is_transient_but_off_google_is_auth():
    import asyncio
    from unittest.mock import patch

    class Resp:
        def __init__(self, status, location=""):
            self.status_code, self.text, self.headers = status, "", {"location": location}

    class Http:
        def __init__(self, location):
            self.location, self.cookies = location, type("C", (), {"jar": ck.CookieJar()})()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, *a, **k):
            return Resp(302, self.location)

    def run(location):
        session = ck.FlowCookieSession(handle="h", email="", proxy="http://p", jar=ck.CookieJar(), source_fingerprint="f")
        with patch.object(ck, "AsyncSession", lambda **kw: Http(location)):
            asyncio.run(ck.load_bootstrap(session))

    with pytest.raises(ck.FlowTransientError):
        run("https://www.google.com/sorry/index?continue=x")
    with pytest.raises(ck.FlowCookieAuthError):
        run("https://evil.example.com/")


def test_envelope_er_frames_are_transient_and_401_is_auth():
    with pytest.raises(ck.FlowRpcError) as err:
        ck.extract_rpc_payload([[["er", None, None, None, None, 400]]], "ogiZ0b")
    assert err.value.transient and ck.TRANSIENT_MARKER in str(err.value.code)
    with pytest.raises(ck.FlowCookieAuthError):
        ck.extract_rpc_payload([[["er", None, None, None, None, 401]]], "ogiZ0b")


def test_xsrf_frame_does_not_resubmit_an_answered_rpc():
    import asyncio
    from unittest.mock import patch

    posts = []
    answered = '[["xsrf","NEW"],["wrb.fr","ogiZ0b","[1]",null,null,null,"generic"]]'

    class Resp:
        status_code = 200
        def __init__(self, text):
            self.text = text

    class Http:
        cookies = type("C", (), {"jar": ck.CookieJar()})()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, *a, **k):
            posts.append(1)
            return Resp(answered)

    session = ck.FlowCookieSession(handle="h", email="a@x.com", proxy="http://p", jar=ck.CookieJar(), source_fingerprint="f",
                                   bl="boq_labs-ai-sandbox-frontend_1", fsid="1", bootstrap_at=__import__("time").time())
    with patch.object(ck, "AsyncSession", lambda **kw: Http()):
        assert asyncio.run(ck.call_rpc(session, "ogiZ0b", [])) == [1]
    assert len(posts) == 1


def test_fleet_watch_diagnosis():
    from src.services.fleet_watch import FleetWatch
    assert FleetWatch.diagnose(total=20, ok=0, active=5)
    assert FleetWatch.diagnose(total=0, ok=0, active=0)
    assert FleetWatch.diagnose(total=20, ok=1, active=5) is None
    assert FleetWatch.diagnose(total=2, ok=0, active=5) is None  # too few requests to judge


def test_server_mint_routing_breaker_opens_and_probes():
    from src.services import flow_page_captcha as fpc
    svc = fpc.FlowPageCaptchaService.__new__(fpc.FlowPageCaptchaService)
    assert svc.routing_healthy()
    svc._consecutive_failures = fpc.ROUTING_BREAKER_FAILURES
    svc._breaker_probe_at = __import__("time").monotonic() + 100
    assert not svc.routing_healthy()
    svc._breaker_probe_at = 0.0
    assert svc.routing_healthy()       # probe window open...
    assert svc.routing_healthy()       # ...and filtering candidates does not use it up


def test_fleet_watch_needs_a_success_to_report_recovery():
    import asyncio
    from src.services.fleet_watch import FleetWatch

    pages = []
    watch = FleetWatch(db=None)
    numbers = iter([(20, 0, 5, 20, 0), (0, 0, 5, 20, 0), (3, 1, 5, 23, 1)])

    async def fake_numbers():
        return next(numbers)

    async def fake_page(kind, text):
        pages.append(kind)

    watch._numbers, watch._page = fake_numbers, fake_page
    asyncio.run(watch.check_once())   # 20 finished, none OK -> down
    asyncio.run(watch.check_once())   # quiet: stays down, no "recovered"
    asyncio.run(watch.check_once())   # a real success -> recovered
    assert pages == ["fleet_down", "fleet_ok"]


def test_fleet_watch_degraded_pages_and_needs_70_percent_to_recover():
    import asyncio
    from src.services.fleet_watch import FleetWatch

    assert FleetWatch.diagnose(total=10, ok=3, active=5, hour_total=40, hour_ok=10)[0] == "fleet_degraded"
    assert FleetWatch.diagnose(total=10, ok=6, active=5, hour_total=40, hour_ok=24) is None    # 60 %: fine
    assert FleetWatch.diagnose(total=10, ok=3, active=5, hour_total=20, hour_ok=2) is None     # too few to judge

    pages = []
    watch = FleetWatch(db=None)
    numbers = iter([
        (10, 3, 5, 40, 10),   # 25 % of the hour -> degraded page
        (10, 6, 5, 40, 24),   # 60 %: below 70 %, stays degraded, no new page
        (2, 1, 5, 12, 6),     # too quiet to judge: stays degraded, silently
        (10, 0, 5, 40, 20),   # 0 of 10 in 15 min -> down page right away
        (10, 6, 5, 40, 24),   # some success again; 60 % is still below 70 % after a degraded spell -> degraded page
        (10, 9, 5, 40, 30),   # 75 % -> recovered
    ])

    async def fake_numbers():
        return next(numbers)

    async def fake_page(kind, text):
        pages.append(kind)

    watch._numbers, watch._page = fake_numbers, fake_page
    for _ in range(6):
        asyncio.run(watch.check_once())
    assert pages == ["fleet_degraded", "fleet_down", "fleet_degraded", "fleet_ok"]


def test_fleet_watch_restart_does_not_page_an_open_problem_again():
    import asyncio
    from datetime import datetime, timedelta, timezone
    from src.services.fleet_watch import FleetWatch

    pages = []
    watch = FleetWatch(db=None)
    opened = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()

    async def rows():
        return [("fleet_degraded", "flow2api degraded: only 5 of 40", opened)]

    numbers = iter([(10, 1, 5, 40, 5), (10, 6, 5, 40, 24), (10, 9, 5, 40, 30)])

    async def fake_numbers():
        return next(numbers)

    async def fake_page(kind, text):
        pages.append(kind)

    watch._open_problem_rows, watch._numbers, watch._page = rows, fake_numbers, fake_page
    asyncio.run(watch.restore_open_problem())
    for _ in range(3):
        asyncio.run(watch.check_once())   # still degraded: silent; 60 %: still below 70 %, silent; 75 %: recovered
    assert pages == ["fleet_ok"]


def test_signed_in_page_without_account_field_is_transient():
    import asyncio
    from unittest.mock import patch

    class Resp:
        status_code, headers = 200, {}
        text = 'boq_labs-ai-sandbox-frontend_1 "FdrFJe":"-1" "SNlM0e":"tok"'

    class Http:
        cookies = type("C", (), {"jar": ck.CookieJar()})()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, *a, **k):
            return Resp()

    session = ck.FlowCookieSession(handle="h", email="", proxy="http://p", jar=ck.CookieJar(), source_fingerprint="f")
    with patch.object(ck, "AsyncSession", lambda **kw: Http()):
        with pytest.raises(ck.FlowTransientError):
            asyncio.run(ck.load_bootstrap(session))
def test_image_aspect_codes_match_what_google_makes():
    # Measured live 2026-11-04: 1 -> 1024x1024, 2 -> 768x1376, 3 -> 1376x768, 4 -> 896x1200, 5 -> 1200x896.
    assert flow_rpc.IMAGE_ASPECT["IMAGE_ASPECT_RATIO_SQUARE"] == 1
    assert flow_rpc.IMAGE_ASPECT["IMAGE_ASPECT_RATIO_PORTRAIT"] == 2
    assert flow_rpc.IMAGE_ASPECT["IMAGE_ASPECT_RATIO_LANDSCAPE"] == 3
    assert flow_rpc.IMAGE_ASPECT["IMAGE_ASPECT_RATIO_PORTRAIT_THREE_FOUR"] == 4
    assert flow_rpc.IMAGE_ASPECT["IMAGE_ASPECT_RATIO_LANDSCAPE_FOUR_THREE"] == 5


def test_protocol_healer_stamps_a_failed_cookie_recheck():
    # 2026-11-04: a failed re-check returned before stamping last_st_refresh_at, so the healer
    # retried every disabled account every 60 s instead of once per refresh interval.
    import asyncio
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock
    from src.services.token_manager import TokenManager

    tm = TokenManager.__new__(TokenManager)
    row = SimpleNamespace(id=7, auto_refresh_enabled=True, is_active=False, ban_reason="auto_st_expired",
                          protocol_mode="protocol", google_cookies='[{"name":"SID","value":"x"}]')
    tm.db = MagicMock()
    tm.db.get_token = AsyncMock(return_value=row)
    tm.db.update_token = AsyncMock()
    tm._try_protocol_refresh_st = AsyncMock(return_value=None)
    now = datetime.now(timezone.utc)
    asyncio.run(tm._refresh_protocol_token(row, now))
    kwargs = tm.db.update_token.await_args.kwargs
    assert kwargs["last_st_refresh_at"] == now


def test_a_dropped_handle_remembers_its_row_only():
    # 2026-11-04: requests in flight when a push replaced the handle failed with "unknown cookie session".
    # The fallback follows the ROW's current handle (guarded promotion), so it records the row, nothing else.
    reg = ck.FlowCookieSessions()
    bound, unbound = _session("a@x.com"), _session("b@x.com")
    bound.token_id = 17
    reg.put(bound)
    reg.put(unbound)
    reg.alias("FLOWCK.db.old", bound)
    reg.drop_session(bound)
    reg.drop_session(unbound)
    assert reg.retired_token_id(bound.handle) == 17 and reg.retired_token_id("FLOWCK.db.old") == 17
    assert reg.retired_token_id(unbound.handle) is None


def test_stale_handle_follows_the_rows_current_promoted_handle():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from src.services.flow_client import FlowClient, FlowAPIError

    client = FlowClient.__new__(FlowClient)
    client._cookie_sessions = ck.FlowCookieSessions()
    import contextvars
    client._cookie_token_ctx = contextvars.ContextVar("t", default=None)
    old = _session("a@x.com"); old.token_id = 17
    new = _session("a@x.com", fp="g"); new.token_id = 17
    client._cookie_sessions.put(old)
    client._cookie_sessions.put(new)
    client._cookie_sessions.drop_session(old)
    client.db = SimpleNamespace(get_token_by_at=AsyncMock(return_value=None),
                                get_token=AsyncMock(return_value=SimpleNamespace(id=17, at=new.handle, email="a@x.com")),
                                count_tokens_by_email=AsyncMock(return_value=1))
    assert asyncio.run(client._cookie_session_for_handle(old.handle)) is new
    client.db.count_tokens_by_email = AsyncMock(return_value=2)          # two rows share the email: no guessing
    with pytest.raises(FlowAPIError):
        asyncio.run(client._cookie_session_for_handle(old.handle))
    client.db.count_tokens_by_email = AsyncMock(return_value=1)
    client.db.get_token = AsyncMock(return_value=SimpleNamespace(id=17, at=old.handle, email="a@x.com"))  # not re-pushed yet
    with pytest.raises(FlowAPIError):
        asyncio.run(client._cookie_session_for_handle(old.handle))


def _non_handle_401():
    """Stage 2 (2026-11): the REST transport is gone, so an `at` that is not a FLOWCK handle is a local 401.
    Both AsyncSession imports fail if entered: a real REST request would otherwise reach Google, and
    Google's own 401 would pass the status check by accident (plan r2, A3)."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch
    from src.services.flow_client import FlowAPIError, FlowClient

    client = FlowClient(proxy_manager=None)
    client._cookie_session_for_handle = AsyncMock(side_effect=AssertionError("session lookup"))
    rest = MagicMock(side_effect=AssertionError("network"))
    cookie = MagicMock(side_effect=AssertionError("network"))
    with patch("src.services.flow_client.AsyncSession", rest), \
            patch("src.services.flow_cookie_session.AsyncSession", cookie):
        with pytest.raises(FlowAPIError) as err:
            asyncio.run(client._make_request(method="GET", url=client.api_base_url + "/credits",
                                             use_at=True, at_token="ya29.legacy"))
    rest.assert_not_called()
    cookie.assert_not_called()
    client._cookie_session_for_handle.assert_not_called()
    return client, err.value


def test_non_handle_at_is_a_local_401_without_any_network_call():
    _, error = _non_handle_401()
    assert error.status_code == 401 and error.reason == "UNAUTHENTICATED"


def test_the_local_401_starts_the_normal_refresh():
    from src.services.token_manager import TokenManager

    client, error = _non_handle_401()
    tm = TokenManager.__new__(TokenManager)
    tm.flow_client = client
    assert tm._classify_refresh_error(error) == "st_expired"
