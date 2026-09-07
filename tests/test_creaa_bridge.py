"""Creaa browser-bridge broker: durability, serialisation, event validation, REST/WS surface."""
import json
import time
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import creaa
from src.core import creaa_models as m
from src.core.config import config
from src.core.creaa_models import CreaaValidationError, normalize_generation_request
from src.services.creaa_bridge import CreaaBridge

IMAGE_MODELS = [{"id": "gpt-image-2", "media_type": "image", "label": "GPT Image 2"}]
VIDEO_MODELS = [{"id": "seedance-2.5", "media_type": "video", "label": "Seedance 2.5"}]


class FakeSocket:
    """Minimal stand-in for a Starlette WebSocket (accept/send_text/close)."""

    def __init__(self, fail_send=False):
        self.sent = []
        self.closed = None
        self.accepted = False
        self.fail_send = fail_send

    async def accept(self):
        self.accepted = True

    async def send_text(self, text):
        if self.fail_send:
            raise RuntimeError("socket closed")
        self.sent.append(json.loads(text))

    async def close(self, code=1000):
        self.closed = code

    def of_type(self, message_type):
        return [msg for msg in self.sent if msg.get("type") == message_type]


async def make_bridge(tmp_path, **kwargs):
    kwargs.setdefault("housekeeping_interval", 0)
    bridge = CreaaBridge(tmp_path / "creaa.db", **kwargs)
    await bridge.start()
    return bridge


async def register(bridge, device_id, account_id, models=IMAGE_MODELS, ws=None, logged_in=True):
    ws = ws or FakeSocket()
    await bridge.connect(ws)
    await bridge.handle_message(ws, json.dumps({
        "type": "register", "device_id": device_id, "account_id": account_id,
        "account_label": account_id, "models": models, "capabilities": {"logged_in": logged_in},
    }))
    return ws


async def send_event(bridge, ws, job_id, attempt_id, state, **extra):
    await bridge.handle_message(ws, json.dumps({
        "type": "job_event", "job_id": job_id, "attempt_id": attempt_id, "state": state, **extra,
    }))
    return ws.sent[-1]


def image_body(**overrides):
    body = {"model": "creaa/gpt-image-2", "prompt": "a cat on a bicycle"}
    body.update(overrides)
    return body


async def submit_and_dispatch(bridge, ws, **overrides):
    job, created = await bridge.submit("image", image_body(**overrides))
    assert created
    await bridge.dispatch_now()
    job = bridge.get_job(job["id"])
    return job


# --------------------------------------------------------------------------- validation helpers

def test_request_normalisation_rules():
    ok = normalize_generation_request("image", {"model": "creaa/gpt-image-2", "prompt": " hi ", "aspect_ratio": "16:9"})
    assert ok["model"] == "gpt-image-2" and ok["prompt"] == "hi" and ok["billing_policy"] == "unlimited_only"
    with pytest.raises(CreaaValidationError) as excinfo:  # prefix is stripped once only
        normalize_generation_request("image", {"model": "creaa/creaa/x", "prompt": "p"})
    assert excinfo.value.code == "invalid_model"

    for bad, code in [
        ({"model": "gpt-image-2"}, "prompt_required"),
        ({"model": "gpt-image-2", "prompt": "p", "n": 2}, "unsupported_n"),
        ({"model": "gpt-image-2", "prompt": "p", "bogus": 1}, "unknown_field"),
        ({"model": "gpt-image-2", "prompt": "p", "billing_policy": "allow_credits"}, "max_credits_required"),
        ({"model": "gpt-image-2", "prompt": "p", "max_credits": 5}, "max_credits_not_allowed"),
        ({"model": "gpt-image-2", "prompt": "p", "duration": 5}, "field_not_applicable"),
        ({"model": "gpt-image-2", "prompt": "p", "aspect_ratio": "5:7"}, "invalid_aspect_ratio"),
        ({"model": "gpt-image-2", "prompt": "p", "references": ["ftp://x/y.png"]}, "invalid_reference"),
        ({"model": "gpt-image-2", "prompt": "p", "references": ["a"] * 51}, "too_many_references"),
        ({"model": "gpt-image-2", "prompt": "x" * 20001}, "prompt_too_long"),
    ]:
        with pytest.raises(CreaaValidationError) as excinfo:
            normalize_generation_request("image", bad)
        assert excinfo.value.code == code, bad

    video = normalize_generation_request("video", {
        "model": "seedance-2.5", "prompt": "p", "duration": 6, "resolution": "720p",
        "references": ["https://example.com/ref.png", {"data": "data:image/png;base64,AAAA"}],
        "billing_policy": "allow_credits", "max_credits": 20,
    })
    assert video["references"] == [
        {"type": "url", "url": "https://example.com/ref.png"},
        {"type": "data", "data": "data:image/png;base64,AAAA"},
    ]
    assert m.public_request(video)["references"] == [{"type": "url", "length": 27}, {"type": "data", "length": 26}]
    assert "account_id" not in m.wire_request(video)


# --------------------------------------------------------------------------- dispatch + durability

@pytest.mark.asyncio
async def test_claim_is_persisted_before_execute_is_sent(tmp_path):
    bridge = await make_bridge(tmp_path)
    ws = await register(bridge, "dev-1", "acct-a")
    job = await submit_and_dispatch(bridge, ws)
    execute = ws.of_type("execute")
    assert job["state"] == m.CLAIMED and job["device_id"] == "dev-1" and job["attempt_id"]
    assert len(execute) == 1
    assert execute[0]["job"]["id"] == job["id"]
    assert execute[0]["job"]["attempt_id"] == job["attempt_id"]
    assert execute[0]["job"]["provider_task_id"] is None
    assert execute[0]["job"]["request"]["model"] == "gpt-image-2"
    assert "account_id" not in execute[0]["job"]["request"]
    await bridge.close()


@pytest.mark.asyncio
async def test_restart_submitting_without_provider_id_becomes_needs_review_and_holds_slot(tmp_path):
    bridge = await make_bridge(tmp_path)
    ws = await register(bridge, "dev-1", "acct-a")
    job = await submit_and_dispatch(bridge, ws)
    ack = await send_event(bridge, ws, job["id"], job["attempt_id"], m.SUBMITTING)
    assert ack["accepted"] is True and ack["state"] == m.SUBMITTING
    assert bridge.get_job(job["id"])["state"] == m.SUBMITTING
    await bridge.close()

    restarted = await make_bridge(tmp_path)
    recovered = restarted.get_job(job["id"])
    assert recovered["state"] == m.NEEDS_REVIEW
    assert recovered["error"]["code"] == "interrupted_before_provider_id"

    ws2 = await register(restarted, "dev-1", "acct-a")
    assert ws2.of_type("execute") == [] and ws2.of_type("resume") == []
    ack = ws2.of_type("register_ack")[0]
    assert ack["pending_attempts"] == [{"job_id": job["id"], "attempt_id": job["attempt_id"], "state": m.NEEDS_REVIEW}]

    # Slot stays held: a new job for the same account is queued, never executed.
    second, _ = await restarted.submit("image", image_body(account_id="acct-a"))
    await restarted.dispatch_now()
    assert restarted.get_job(second["id"])["state"] == m.QUEUED
    assert ws2.of_type("execute") == []
    await restarted.close()


@pytest.mark.asyncio
async def test_restart_with_provider_id_resumes_same_job_and_attempt(tmp_path):
    bridge = await make_bridge(tmp_path)
    ws = await register(bridge, "dev-1", "acct-a")
    job = await submit_and_dispatch(bridge, ws)
    await send_event(bridge, ws, job["id"], job["attempt_id"], m.SUBMITTING)
    ack = await send_event(bridge, ws, job["id"], job["attempt_id"], m.SUBMITTED, provider_task_id="task-123")
    assert ack["accepted"] is True
    await bridge.close()

    restarted = await make_bridge(tmp_path)
    assert restarted.get_job(job["id"])["state"] == m.SUBMITTED
    ws2 = await register(restarted, "dev-1", "acct-a")
    resumes = ws2.of_type("resume")
    assert ws2.of_type("execute") == []
    assert len(resumes) == 1
    assert resumes[0]["job"]["id"] == job["id"]
    assert resumes[0]["job"]["attempt_id"] == job["attempt_id"]
    assert resumes[0]["job"]["provider_task_id"] == "task-123"
    # Same attempt keeps reporting after the restart.
    ack = await send_event(restarted, ws2, job["id"], job["attempt_id"], m.SUCCEEDED,
                           result={"urls": ["https://cdn.example/out.png"], "media_type": "image"})
    assert ack["accepted"] is True
    assert restarted.get_job(job["id"])["result"]["urls"] == ["https://cdn.example/out.png"]
    await restarted.close()


@pytest.mark.asyncio
async def test_restart_claimed_without_submit_intent_is_requeued(tmp_path):
    bridge = await make_bridge(tmp_path)
    ws = await register(bridge, "dev-1", "acct-a")
    job = await submit_and_dispatch(bridge, ws)
    first_attempt = job["attempt_id"]
    await bridge.close()

    restarted = await make_bridge(tmp_path)
    assert restarted.get_job(job["id"])["state"] == m.QUEUED
    ws2 = await register(restarted, "dev-1", "acct-a")
    await restarted.dispatch_now()
    execute = ws2.of_type("execute")
    assert len(execute) == 1 and execute[0]["job"]["id"] == job["id"]
    assert execute[0]["job"]["attempt_id"] != first_attempt
    # A late event for the stale attempt is refused.
    ack = await send_event(restarted, ws2, job["id"], first_attempt, m.SUBMITTING)
    assert ack["accepted"] is False and ack["error"] == "attempt_mismatch"
    await restarted.close()


@pytest.mark.asyncio
async def test_idempotency_key_replays_same_job_and_conflicts_on_changed_payload(tmp_path):
    bridge = await make_bridge(tmp_path)
    await register(bridge, "dev-1", "acct-a")
    first, created = await bridge.submit("image", image_body(), idempotency_key="key-1")
    again, created_again = await bridge.submit("image", image_body(), idempotency_key="key-1")
    assert created and not created_again and again["id"] == first["id"]
    with pytest.raises(CreaaValidationError) as excinfo:
        await bridge.submit("image", image_body(prompt="different"), idempotency_key="key-1")
    assert excinfo.value.status == 409 and excinfo.value.code == "idempotency_payload_mismatch"
    assert len(bridge.list_jobs()) == 1
    await bridge.close()


@pytest.mark.asyncio
async def test_one_active_generation_per_account_and_independent_accounts_run_concurrently(tmp_path):
    bridge = await make_bridge(tmp_path)
    ws_a = await register(bridge, "dev-a", "acct-a")
    ws_b = await register(bridge, "dev-b", "acct-b")
    a1, _ = await bridge.submit("image", image_body(account_id="acct-a"))
    a2, _ = await bridge.submit("image", image_body(account_id="acct-a"))
    b1, _ = await bridge.submit("image", image_body(account_id="acct-b"))
    await bridge.dispatch_now()
    assert [x["job"]["id"] for x in ws_a.of_type("execute")] == [a1["id"]]
    assert [x["job"]["id"] for x in ws_b.of_type("execute")] == [b1["id"]]
    assert bridge.get_job(a2["id"])["state"] == m.QUEUED

    a1_job = bridge.get_job(a1["id"])
    await send_event(bridge, ws_a, a1["id"], a1_job["attempt_id"], m.SUBMITTING)
    await send_event(bridge, ws_a, a1["id"], a1_job["attempt_id"], m.SUBMITTED, provider_task_id="t-a1")
    await bridge.dispatch_now()
    assert bridge.get_job(a2["id"])["state"] == m.QUEUED  # still serialised
    await send_event(bridge, ws_a, a1["id"], a1_job["attempt_id"], m.SUCCEEDED, result={"urls": ["https://x/1.png"]})
    await bridge.dispatch_now()
    assert [x["job"]["id"] for x in ws_a.of_type("execute")] == [a1["id"], a2["id"]]
    await bridge.close()


@pytest.mark.asyncio
async def test_disconnect_requeues_claimed_and_marks_submitting_needs_review(tmp_path):
    bridge = await make_bridge(tmp_path)
    ws = await register(bridge, "dev-1", "acct-a")
    claimed = await submit_and_dispatch(bridge, ws)
    await bridge.disconnect(ws)
    assert bridge.get_job(claimed["id"])["state"] == m.QUEUED
    assert bridge.get_job(claimed["id"])["device_id"] is None

    ws = await register(bridge, "dev-1", "acct-a")
    await bridge.dispatch_now()
    job = bridge.get_job(claimed["id"])
    assert job["state"] == m.CLAIMED
    await send_event(bridge, ws, job["id"], job["attempt_id"], m.SUBMITTING)
    await bridge.disconnect(ws)
    after = bridge.get_job(job["id"])
    assert after["state"] == m.NEEDS_REVIEW
    assert after["error"]["code"] == "worker_disconnected_before_provider_id"

    # Account looks offline now and its slot is still held.
    accounts = {a["account_id"]: a for a in bridge.list_accounts()}
    assert accounts["acct-a"]["online"] is False and accounts["acct-a"]["active_job"]["id"] == job["id"]
    await bridge.close()


@pytest.mark.asyncio
async def test_events_from_wrong_device_account_or_attempt_are_rejected(tmp_path):
    bridge = await make_bridge(tmp_path)
    ws_a = await register(bridge, "dev-a", "acct-a")
    ws_b = await register(bridge, "dev-b", "acct-b")
    ws_a2 = await register(bridge, "dev-a2", "acct-a")  # second device, same account
    job, _ = await bridge.submit("image", image_body(account_id="acct-a"))
    await bridge.dispatch_now()
    job = bridge.get_job(job["id"])
    assert job["device_id"] == "dev-a2"  # newest registration for the account wins dispatch

    ack = await send_event(bridge, ws_b, job["id"], job["attempt_id"], m.SUBMITTING)
    assert ack["accepted"] is False and ack["error"] == "account_mismatch"
    ack = await send_event(bridge, ws_a, job["id"], job["attempt_id"], m.SUBMITTING)
    assert ack["accepted"] is False and ack["error"] == "device_mismatch"
    ack = await send_event(bridge, ws_a2, job["id"], "at_bogus", m.SUBMITTING)
    assert ack["accepted"] is False and ack["error"] == "attempt_mismatch"
    ack = await send_event(bridge, ws_a2, "cj_missing", job["attempt_id"], m.SUBMITTING)
    assert ack["accepted"] is False and ack["error"] == "unknown_job"
    unregistered = FakeSocket()
    await bridge.connect(unregistered)
    ack = await send_event(bridge, unregistered, job["id"], job["attempt_id"], m.SUBMITTING)
    assert ack["accepted"] is False and ack["error"] == "not_registered"
    assert bridge.get_job(job["id"])["state"] == m.CLAIMED
    await bridge.close()


@pytest.mark.asyncio
async def test_event_order_provider_id_rules_and_terminal_immutability(tmp_path):
    bridge = await make_bridge(tmp_path)
    ws = await register(bridge, "dev-1", "acct-a")
    job = await submit_and_dispatch(bridge, ws)
    jid, att = job["id"], job["attempt_id"]

    ack = await send_event(bridge, ws, jid, att, m.SUBMITTED, provider_task_id="t-1")
    assert ack["accepted"] is False and ack["error"] == "invalid_transition"  # must announce submitting first
    assert (await send_event(bridge, ws, jid, att, m.SUBMITTING))["accepted"] is True
    ack = await send_event(bridge, ws, jid, att, m.SUBMITTED)
    assert ack["accepted"] is False and ack["error"] == "provider_task_id_required"
    assert (await send_event(bridge, ws, jid, att, m.SUBMITTED, provider_task_id="t-1"))["accepted"] is True
    ack = await send_event(bridge, ws, jid, att, m.RUNNING, provider_task_id="t-OTHER")
    assert ack["accepted"] is False and ack["error"] == "provider_task_id_mismatch"
    assert (await send_event(bridge, ws, jid, att, m.RUNNING, progress=40))["accepted"] is True
    assert bridge.get_job(jid)["progress"] == 40.0
    ack = await send_event(bridge, ws, jid, att, m.SUBMITTING)
    assert ack["accepted"] is False and ack["error"] == "invalid_transition"  # regression
    ack = await send_event(bridge, ws, jid, att, m.SUCCEEDED, result={"urls": []})
    assert ack["accepted"] is False and ack["error"] == "result_urls_required"
    assert (await send_event(bridge, ws, jid, att, m.SUCCEEDED, result={"urls": ["https://x/a.png"], "media_type": "image"}))["accepted"] is True
    final = bridge.get_job(jid)
    assert final["state"] == m.SUCCEEDED and final["finished_at"] and final["provider_task_id"] == "t-1"

    ack = await send_event(bridge, ws, jid, att, m.FAILED, error={"code": "late"})
    assert ack["accepted"] is False and ack["error"] == "terminal_state"
    assert bridge.get_job(jid)["state"] == m.SUCCEEDED
    await bridge.close()


@pytest.mark.asyncio
async def test_cancel_only_while_queued(tmp_path):
    bridge = await make_bridge(tmp_path)
    ws = await register(bridge, "dev-1", "acct-a")
    active = await submit_and_dispatch(bridge, ws)
    queued, _ = await bridge.submit("image", image_body(account_id="acct-a"))
    cancelled = await bridge.cancel(queued["id"])
    assert cancelled["state"] == m.CANCELLED
    assert (await bridge.cancel(queued["id"]))["state"] == m.CANCELLED  # idempotent
    with pytest.raises(CreaaValidationError) as excinfo:
        await bridge.cancel(active["id"])
    assert excinfo.value.status == 409 and excinfo.value.code == "cancel_unsupported_state"
    with pytest.raises(CreaaValidationError) as excinfo:
        await bridge.cancel("cj_nope")
    assert excinfo.value.status == 404
    await bridge.close()


@pytest.mark.asyncio
async def test_resolve_requires_explicit_acknowledgement(tmp_path):
    bridge = await make_bridge(tmp_path)
    ws = await register(bridge, "dev-1", "acct-a")
    job = await submit_and_dispatch(bridge, ws)
    with pytest.raises(CreaaValidationError) as excinfo:
        await bridge.resolve(job["id"], "fail", confirm_no_upstream_work=True)
    assert excinfo.value.status == 409 and excinfo.value.code == "not_resolvable"

    await send_event(bridge, ws, job["id"], job["attempt_id"], m.SUBMITTING)
    await bridge.disconnect(ws)
    assert bridge.get_job(job["id"])["state"] == m.NEEDS_REVIEW

    with pytest.raises(CreaaValidationError) as excinfo:
        await bridge.resolve(job["id"], "fail")
    assert excinfo.value.code == "confirmation_required"
    with pytest.raises(CreaaValidationError) as excinfo:
        await bridge.resolve(job["id"], "resume")
    assert excinfo.value.code == "provider_task_id_required"

    # Resume with a provider id: tracking continues on the same attempt via the online worker.
    ws = await register(bridge, "dev-1", "acct-a")
    resolved = await bridge.resolve(job["id"], "resume", provider_task_id="task-found-in-history", note="seen in history")
    assert resolved["state"] == m.SUBMITTED and resolved["provider_task_id"] == "task-found-in-history"
    assert resolved["resolution"]["action"] == "resume" and resolved["resolution"]["from_state"] == m.NEEDS_REVIEW
    resumes = ws.of_type("resume")
    assert len(resumes) == 1 and resumes[0]["job"]["attempt_id"] == job["attempt_id"]
    assert ws.of_type("execute") == []

    # Second job: needs_review -> fail with explicit confirmation frees the slot.
    second, _ = await bridge.submit("image", image_body(account_id="acct-a"))
    await send_event(bridge, ws, job["id"], job["attempt_id"], m.NEEDS_REVIEW, error={"code": "lost"})
    await bridge.dispatch_now()
    assert bridge.get_job(second["id"])["state"] == m.QUEUED
    failed = await bridge.resolve(job["id"], "fail", confirm_no_upstream_work=True)
    assert failed["state"] == m.FAILED and failed["error"]["code"] == "resolved_no_upstream_work"
    await bridge.dispatch_now()
    assert bridge.get_job(second["id"])["state"] == m.CLAIMED
    await bridge.close()


@pytest.mark.asyncio
async def test_needs_login_pauses_account_and_never_releases_uncertain_submission(tmp_path):
    bridge = await make_bridge(tmp_path)
    ws = await register(bridge, "dev-1", "acct-a")
    job = await submit_and_dispatch(bridge, ws)
    # Before submit intent: safe to requeue, but dispatch pauses until a logged-in register.
    ack = await send_event(bridge, ws, job["id"], job["attempt_id"], m.NEEDS_LOGIN)
    assert ack["accepted"] is True and ack["job_state"] == m.QUEUED
    await bridge.dispatch_now()
    assert bridge.get_job(job["id"])["state"] == m.QUEUED
    assert len(ws.of_type("execute")) == 1

    ws = await register(bridge, "dev-1", "acct-a", ws=FakeSocket())
    await bridge.dispatch_now()
    job = bridge.get_job(job["id"])
    assert job["state"] == m.CLAIMED
    await send_event(bridge, ws, job["id"], job["attempt_id"], m.SUBMITTING)
    ack = await send_event(bridge, ws, job["id"], job["attempt_id"], m.NEEDS_LOGIN)
    assert ack["accepted"] is True and ack["job_state"] == m.NEEDS_LOGIN
    # Slot is still held; explicit resolution required.
    other, _ = await bridge.submit("image", image_body(account_id="acct-a"))
    ws = await register(bridge, "dev-1", "acct-a", ws=FakeSocket())
    await bridge.dispatch_now()
    assert bridge.get_job(other["id"])["state"] == m.QUEUED
    assert ws.of_type("register_ack")[0]["pending_attempts"][0]["job_id"] == job["id"]
    await bridge.close()


@pytest.mark.asyncio
async def test_submitting_ack_delivery_failure_holds_job_for_review(tmp_path):
    bridge = await make_bridge(tmp_path)
    ws = await register(bridge, "dev-1", "acct-a")
    job = await submit_and_dispatch(bridge, ws)
    ws.fail_send = True  # send failure cannot prove whether the peer received bytes
    await bridge.handle_message(ws, json.dumps({"type": "job_event", "job_id": job["id"],
                                                "attempt_id": job["attempt_id"], "state": m.SUBMITTING}))
    after = bridge.get_job(job["id"])
    assert after["state"] == m.NEEDS_REVIEW and after["error"]["code"] == "submit_ack_delivery_uncertain"
    await bridge.close()


@pytest.mark.asyncio
async def test_account_selection_rules(tmp_path):
    bridge = await make_bridge(tmp_path)
    with pytest.raises(CreaaValidationError) as excinfo:
        await bridge.submit("image", image_body())
    assert excinfo.value.code == "no_worker_online" and excinfo.value.status == 503

    ws_a = await register(bridge, "dev-a", "acct-a")
    auto, _ = await bridge.submit("image", image_body())
    assert auto["account_id"] == "acct-a"  # exactly one eligible account -> chosen
    with pytest.raises(CreaaValidationError) as excinfo:
        await bridge.submit("video", {"model": "seedance-2.5", "prompt": "p"})
    assert excinfo.value.code == "model_not_available"

    await register(bridge, "dev-b", "acct-b", models=IMAGE_MODELS + VIDEO_MODELS)
    with pytest.raises(CreaaValidationError) as excinfo:
        await bridge.submit("image", image_body())
    assert excinfo.value.code == "account_required" and excinfo.value.status == 409
    assert excinfo.value.extra["eligible_accounts"] == ["acct-a", "acct-b"]
    video, _ = await bridge.submit("video", {"model": "creaa/seedance-2.5", "prompt": "p", "duration": 6})
    assert video["account_id"] == "acct-b"  # only acct-b advertises video
    with pytest.raises(CreaaValidationError) as excinfo:
        await bridge.submit("video", {"model": "seedance-2.5", "prompt": "p", "account_id": "acct-a"})
    assert excinfo.value.code == "model_not_available_for_account"
    with pytest.raises(CreaaValidationError) as excinfo:
        await bridge.submit("image", image_body(account_id="acct-zzz"))
    assert excinfo.value.code == "unknown_account"

    # Explicitly known but offline account may still queue; nothing is auto-routed elsewhere.
    await bridge.disconnect(ws_a)
    offline, _ = await bridge.submit("image", image_body(account_id="acct-a"))
    await bridge.dispatch_now()
    assert bridge.get_job(offline["id"])["state"] == m.QUEUED
    await bridge.close()


@pytest.mark.asyncio
async def test_queue_bound_per_account(tmp_path):
    bridge = await make_bridge(tmp_path, max_queued_per_account=2)
    ws = await register(bridge, "dev-1", "acct-a")
    await submit_and_dispatch(bridge, ws)  # becomes claimed, not queued
    await bridge.submit("image", image_body(account_id="acct-a"))
    await bridge.submit("image", image_body(account_id="acct-a"))
    with pytest.raises(CreaaValidationError) as excinfo:
        await bridge.submit("image", image_body(account_id="acct-a"))
    assert excinfo.value.code == "queue_full" and excinfo.value.status == 429
    await bridge.close()


@pytest.mark.asyncio
async def test_housekeeping_timeouts_never_reexecute(tmp_path):
    bridge = await make_bridge(tmp_path, claimed_timeout=10, submitting_timeout=20, running_timeout=30)
    ws = await register(bridge, "dev-1", "acct-a")
    a = await submit_and_dispatch(bridge, ws)
    assert (await bridge.run_housekeeping())["claimed_failed"] == 0
    counts = await bridge.run_housekeeping(now=time.time() + 11)
    assert counts["claimed_failed"] == 1
    assert bridge.get_job(a["id"])["state"] == m.FAILED

    b = await submit_and_dispatch(bridge, ws)
    await send_event(bridge, ws, b["id"], b["attempt_id"], m.SUBMITTING)
    counts = await bridge.run_housekeeping(now=time.time() + 21)
    assert counts["submitting_review"] == 1 and bridge.get_job(b["id"])["state"] == m.NEEDS_REVIEW
    assert len(ws.of_type("execute")) == 2  # nothing re-sent
    await bridge.close()


@pytest.mark.asyncio
async def test_duplicate_device_replaces_socket_and_register_requires_account(tmp_path):
    bridge = await make_bridge(tmp_path)
    old = await register(bridge, "dev-1", "acct-a")
    new = await register(bridge, "dev-1", "acct-a")
    assert old.closed == 1000 and new.closed is None
    assert [d["device_id"] for d in bridge.list_accounts()[0]["devices"]] == ["dev-1"]

    anonymous = FakeSocket()
    await bridge.connect(anonymous)
    await bridge.handle_message(anonymous, json.dumps({"type": "register", "device_id": "dev-x", "models": []}))
    assert anonymous.of_type("error")[0]["code"] == "account_id_required" and anonymous.closed == 1008
    assert "dev-x" not in {d["device_id"] for a in bridge.list_accounts() for d in a["devices"]}

    await bridge.handle_message(new, json.dumps({"type": "ping"}))
    assert new.of_type("pong")
    await bridge.handle_message(new, "not json")
    assert new.of_type("error")[-1]["code"] == "invalid_json"
    models = bridge.list_models()
    assert models[0]["id"] == "creaa/gpt-image-2" and models[0]["accounts"] == ["acct-a"]
    await bridge.close()


# --------------------------------------------------------------------------- REST + websocket smoke

@pytest.fixture
def api_key():
    original = config.api_key
    config.api_key = "creaa-test-key"
    yield "creaa-test-key"
    config.api_key = original
    creaa.set_service(None)


def make_app(bridge):
    @asynccontextmanager
    async def lifespan(app):
        await bridge.start()
        yield
        await bridge.close()

    app = FastAPI(lifespan=lifespan)
    creaa.set_service(bridge)
    app.include_router(creaa.router)
    return app


def test_rest_and_websocket_end_to_end(tmp_path, api_key):
    bridge = CreaaBridge(tmp_path / "creaa.db", housekeeping_interval=0)
    app = make_app(bridge)
    headers = {"Authorization": f"Bearer {api_key}"}
    with TestClient(app) as client:
        assert client.get("/v1/creaa/models").status_code == 401
        assert client.get("/v1/creaa/jobs", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/v1/creaa/models", headers=headers).json() == {"object": "list", "data": []}

        with pytest.raises(Exception):
            with client.websocket_connect("/creaa_ws?key=wrong") as ws:
                ws.receive_json()

        with client.websocket_connect(f"/creaa_ws?key={api_key}") as ws:
            ws.send_json({"type": "register", "device_id": "dev-1", "account_id": "acct-a",
                          "account_label": "Ankit", "models": IMAGE_MODELS, "capabilities": {"logged_in": True}})
            ack = ws.receive_json()
            assert ack["type"] == "register_ack" and ack["device_id"] == "dev-1"

            models = client.get("/v1/creaa/models", headers=headers).json()["data"]
            assert models[0]["id"] == "creaa/gpt-image-2" and models[0]["accounts"] == ["acct-a"]
            accounts = client.get("/v1/creaa/accounts", headers=headers).json()["data"]
            assert accounts[0]["online"] is True and accounts[0]["account_label"] == "Ankit"

            bad = client.post("/v1/creaa/images/generations", headers=headers, json={"model": "gpt-image-2"})
            assert bad.status_code == 400 and bad.json()["detail"]["code"] == "prompt_required"

            resp = client.post("/v1/creaa/images/generations", headers={**headers, "Idempotency-Key": "abc"},
                               json=image_body(aspect_ratio="16:9"))
            assert resp.status_code == 202
            job = resp.json()
            assert resp.headers["Location"] == f"/v1/creaa/jobs/{job['id']}"
            replay = client.post("/v1/creaa/images/generations", headers={**headers, "Idempotency-Key": "abc"},
                                 json=image_body(aspect_ratio="16:9"))
            assert replay.status_code == 202 and replay.json()["id"] == job["id"]
            assert replay.headers["Idempotent-Replayed"] == "true"
            conflict = client.post("/v1/creaa/images/generations", headers={**headers, "Idempotency-Key": "abc"},
                                   json=image_body(aspect_ratio="1:1"))
            assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "idempotency_payload_mismatch"

            execute = ws.receive_json()
            assert execute["type"] == "execute" and execute["job"]["id"] == job["id"]
            attempt = execute["job"]["attempt_id"]
            ws.send_json({"type": "job_event", "job_id": job["id"], "attempt_id": attempt, "state": "submitting"})
            assert ws.receive_json() == {"type": "event_ack", "job_id": job["id"], "attempt_id": attempt,
                                         "state": "submitting", "accepted": True, "job_state": "submitting"}
            cancel = client.post(f"/v1/creaa/jobs/{job['id']}/cancel", headers=headers)
            assert cancel.status_code == 409 and cancel.json()["detail"]["code"] == "cancel_unsupported_state"

            ws.send_json({"type": "job_event", "job_id": job["id"], "attempt_id": attempt, "state": "submitted",
                          "provider_task_id": "task-9"})
            assert ws.receive_json()["accepted"] is True
            ws.send_json({"type": "job_event", "job_id": job["id"], "attempt_id": attempt, "state": "succeeded",
                          "result": {"urls": ["https://cdn.example/out.png"], "media_type": "image"}})
            assert ws.receive_json()["accepted"] is True

            shown = client.get(f"/v1/creaa/jobs/{job['id']}", headers=headers).json()
            assert shown["state"] == "succeeded" and shown["provider_task_id"] == "task-9"
            assert shown["result"]["urls"] == ["https://cdn.example/out.png"]
            listed = client.get("/v1/creaa/jobs?limit=5&state=succeeded", headers=headers).json()["data"]
            assert [j["id"] for j in listed] == [job["id"]]

            queued = client.post("/v1/creaa/images/generations", headers=headers, json=image_body(account_id="acct-a"))
            assert queued.status_code == 202
            execute2 = ws.receive_json()
            assert execute2["job"]["id"] == queued.json()["id"]

            resolve = client.post(f"/v1/creaa/jobs/{job['id']}/resolve", headers=headers, json={"action": "fail"})
            assert resolve.status_code == 409 and resolve.json()["detail"]["code"] == "not_resolvable"
            schema = client.post(f"/v1/creaa/jobs/{job['id']}/resolve", headers=headers,
                                 json={"action": "explode", "extra": 1})
            assert schema.status_code == 422
            assert client.get("/v1/creaa/jobs/cj_missing", headers=headers).status_code == 404
