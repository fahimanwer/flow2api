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


def test_image_translation_refuses_characters_and_unknown_models():
    url = "https://aisandbox-pa.googleapis.com/v1/projects/p/flowMedia:batchGenerateImages"
    with pytest.raises(flow_rpc.CookieModeUnsupported):
        flow_rpc.translate_request("POST", url, _image_body(referenceEntities=[{"entityId": "e"}]))
    with pytest.raises(flow_rpc.CookieModeUnsupported):
        flow_rpc.translate_request("POST", url, _image_body(structuredPrompt={"parts": [{"entity": "x"}]}))
    with pytest.raises(flow_rpc.CookieModeUnsupported):
        flow_rpc.translate_request("POST", url, _image_body(imageModelName="IMAGEN_3"))


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
