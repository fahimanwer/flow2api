"""Omni 1.2 per account (2026-11-06): an account gets Omni 1.2 keys only when its own Flow model list has them."""
import asyncio
import types

from src.services.generation_handler import MODEL_CONFIG, omni_latest_config

OMNI_12_KEYS = frozenset({
    f"macaron_cookie_{mode}_{d}s" for mode in ("t2v", "i2v", "i2v_fl", "r2v") for d in (4, 6, 8, 10)
})
OMNI_11_ONLY = frozenset({f"abra_{mode}_{d}s" for mode in ("t2v", "i2v", "r2v") for d in (4, 6, 8, 10)})


def test_account_with_omni_12_gets_every_12_key():
    cfg = omni_latest_config(dict(MODEL_CONFIG["omni-flash_6s_portrait"]), OMNI_12_KEYS | OMNI_11_ONLY)
    assert cfg["model_key"] == "macaron_cookie_t2v_6s"
    assert cfg["first_frame_model_key"] == "macaron_cookie_i2v_6s"
    assert cfg["start_end_model_key"] == "macaron_cookie_i2v_fl_6s"   # first+last has its own key in 1.2
    assert cfg["reference_model_key"] == "macaron_cookie_r2v_6s"
    assert "output_resolution" not in cfg                              # 720p = the key itself, no slot
    assert cfg["omni_version"] == "1.2"


def test_account_without_omni_12_keeps_11_unchanged():
    base = dict(MODEL_CONFIG["omni_8s_portrait"])
    assert omni_latest_config(base, OMNI_11_ONLY) is base
    assert omni_latest_config(base, frozenset()) is base              # model list unreadable -> unchanged


def test_partial_rollout_keeps_11():
    base = dict(MODEL_CONFIG["omni_r2v_8s"])
    partial = OMNI_11_ONLY | {"macaron_cookie_t2v_8s"}                 # 1.2 text but not 1.2 ingredients
    assert omni_latest_config(base, partial) is base


def test_model_config_itself_is_not_changed():
    before = dict(MODEL_CONFIG["omni_4s"])
    omni_latest_config(MODEL_CONFIG["omni_4s"], OMNI_12_KEYS)
    assert MODEL_CONFIG["omni_4s"] == before


def test_model_list_is_cached_and_failures_retry():
    from src.services.flow_client import FlowClient

    client = FlowClient.__new__(FlowClient)
    client._video_keys_cache = {}
    calls = []

    async def session_for(handle):
        return types.SimpleNamespace(email="x@example.com", proxy="http://p")

    async def rpc(session, rpc_id, argument, source_path, timeout, proxy_url=None, user_agent=None):
        calls.append((rpc_id, argument))
        return [[None, None, [[1, [None, None, "x", "macaron_cookie_t2v_8s"]], "abra_t2v_8s"]]]

    client._cookie_session_for_handle, client._cookie_rpc_call = session_for, rpc
    token = types.SimpleNamespace(id=7, at="FLOWCK.a.b", current_project_id="p1")
    keys = asyncio.run(client.account_video_model_keys(token))
    assert {"macaron_cookie_t2v_8s", "abra_t2v_8s"} <= keys
    asyncio.run(client.account_video_model_keys(token))
    assert calls == [("HTrJv", [])]                                     # second call served from the cache

    async def broken(*a, **k):
        raise RuntimeError("signed out")

    client._video_keys_cache, client._cookie_rpc_call = {}, broken
    assert asyncio.run(client.account_video_model_keys(token)) == frozenset()
    assert client._video_keys_cache[7][0] - __import__("time").monotonic() <= FlowClient.VIDEO_KEYS_RETRY_SECONDS


def test_omni_12_cost_estimate():
    from src.services.generation_handler import _estimate_video_credit_cost_for_log
    cfg = omni_latest_config(dict(MODEL_CONFIG["omni_6s"]), OMNI_12_KEYS)
    assert _estimate_video_credit_cost_for_log(cfg["model_key"], cfg) == 18
    assert _estimate_video_credit_cost_for_log("omni_6s", MODEL_CONFIG["omni_6s"]) == 10   # 1.1 unchanged
